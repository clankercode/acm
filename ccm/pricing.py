"""Model rate table and cost arithmetic.

Costs are never stored. They are derived from token counts on every read, so an
edit to ``pricing.toml`` changes every number in the UI without a rescan.

Prompt tokens fall into four billing categories, and which ones a provider
actually charges for differs:

``fresh``       processed from scratch, at the base input rate.
``cached``      served from a warm cache, at a steep discount everywhere.
``cache write`` stored for later reuse. OpenAI and MiniMax do this for free and
                never report it; Anthropic bills 1.25x base input for a 5-minute
                entry and 2x for a 1-hour one.
``cache write 1h``  the long-TTL variant of the above, priced separately.

Modelling cache writes explicitly is what makes Anthropic clients costable at
all: on a coding agent they are a few percent of prompt tokens but, at 1.25-2x
base rate against a 0.1x read rate, a large share of the bill.

Two rate tiers exist. A request whose prompt exceeds the model's
``long_context_threshold`` is billed at long-context rates, which on the GPT-5.6
family means double the input rate and 1.5x the output rate. Because the tier is
a property of the individual request, it is part of the aggregation key -- see
:mod:`ccm.aggregate` -- which keeps bucket-level costing exactly equal to
summing per-request costs.
"""

from __future__ import annotations

import threading
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path

from .config import settings

DEFAULT_LONG_CONTEXT_THRESHOLD = 200_000

#: Provider prefixes that route to an underlying model rather than naming a
#: distinct one. ``pooler/gpt-5.6-sol`` is gpt-5.6-sol reached through a proxy,
#: and is billed at gpt-5.6-sol rates unless overridden. Documentation only: the
#: lookup falls back on any prefix, so an unlisted gateway still resolves.
KNOWN_PROVIDERS = ("pooler", "llmp", "xai", "openai", "anthropic", "google")


def split_model(model: str | None) -> tuple[str, str]:
    """Split ``pooler/gpt-5.6-sol`` into ``("pooler", "gpt-5.6-sol")``.

    Models without a prefix get the empty provider, which the UI labels
    "direct". Keeping the prefix as its own dimension is what makes the
    routed-vs-direct cache comparison possible.
    """
    if not model:
        return "", ""
    if "/" in model:
        provider, _, base = model.partition("/")
        return provider, base
    return "", model


@dataclass(frozen=True)
class Tier:
    """USD per million tokens for one context tier.

    The two cache-write rates default to zero, which is exactly right for every
    provider that does not charge for populating its cache -- OpenAI, xAI and
    MiniMax all fall into that group and never report the tokens either.
    """

    input: float
    cached_input: float
    output: float
    cache_write: float = 0.0
    cache_write_1h: float = 0.0


@dataclass(frozen=True)
class Rate:
    """A model's short and long tier rates plus the threshold between them."""

    short: Tier
    long: Tier
    threshold: int = DEFAULT_LONG_CONTEXT_THRESHOLD
    estimated: bool = False
    long_tier_unknown: bool = False
    source: str | None = None

    def tier(self, long_context: bool) -> Tier:
        return self.long if long_context else self.short

    def tier_for(self, input_tokens: int) -> Tier:
        return self.tier(input_tokens > self.threshold)


@dataclass(frozen=True)
class Cost:
    """A costed set of token counts.

    ``uncached`` is the counterfactual: what the same tokens would have cost at
    a zero cache hit rate. The gap between the two is the whole point of the
    tool.

    The four components sum to ``cost`` and exist because the split is not
    guessable from the outside: which of them dominates depends on the cache
    rate, the model mix and the context tier all at once, and the answer is
    routinely surprising -- at a 93% hit rate the discounted cache reads can
    still cost more than the tokens billed at full price.
    """

    cost: float
    uncached: float
    priced: bool
    fresh_cost: float = 0.0
    cached_cost: float = 0.0
    write_cost: float = 0.0
    output_cost: float = 0.0
    #: The same tokens at the model's standard-tier rates. Equal to ``cost``
    #: unless the request crossed the long-context threshold, so the difference
    #: is exactly what the crossing cost -- a lever the caller controls, and one
    #: nothing else on the invoice separates out.
    standard: float = 0.0

    @property
    def saved(self) -> float:
        return self.uncached - self.cost

    @property
    def surcharge(self) -> float:
        return self.cost - self.standard


ZERO = Cost(0.0, 0.0, priced=False)


def compute_tier(
    tier: Tier,
    input_tokens: int,
    cached_tokens: int,
    output_tokens: int,
    cache_write_tokens: int = 0,
    cache_write_1h_tokens: int = 0,
) -> Cost:
    """Cost tokens that are known to all sit in one tier.

    ``input_tokens`` is the whole prompt. Cached reads and cache writes are
    subsets of it charged at their own rates, so what remains after taking both
    out is the portion paying full freight.

    The counterfactual deliberately prices the entire prompt at the base input
    rate: with no cache there would be nothing to read back and nothing worth
    storing, so both discounted categories collapse into fresh input.
    """
    fresh = max(input_tokens - cached_tokens - cache_write_tokens - cache_write_1h_tokens, 0)
    fresh_cost = fresh * tier.input / 1e6
    cached_cost = cached_tokens * tier.cached_input / 1e6
    write_cost = (
        cache_write_tokens * tier.cache_write
        + cache_write_1h_tokens * tier.cache_write_1h
    ) / 1e6
    output_cost = output_tokens * tier.output / 1e6
    uncached = (input_tokens * tier.input + output_tokens * tier.output) / 1e6
    total = fresh_cost + cached_cost + write_cost + output_cost
    return Cost(
        total,
        uncached,
        priced=True,
        fresh_cost=fresh_cost,
        cached_cost=cached_cost,
        write_cost=write_cost,
        output_cost=output_cost,
        # One tier is all this sees, so there is no surcharge to report. A
        # caller holding the whole Rate overrides it.
        standard=total,
    )


def compute(
    rate: Rate | None,
    input_tokens: int,
    cached_tokens: int,
    output_tokens: int,
    long_context: bool | None = None,
    cache_write_tokens: int = 0,
    cache_write_1h_tokens: int = 0,
) -> Cost:
    """Cost a single request, or a bucket already partitioned by tier.

    Pass ``long_context`` explicitly when costing an aggregate whose tier is
    known from the grouping key; otherwise the tier is derived from
    ``input_tokens``, which is only correct for a single request.
    """
    if rate is None:
        return ZERO
    if long_context is None:
        long_context = input_tokens > rate.threshold
    args = (
        input_tokens,
        cached_tokens,
        output_tokens,
        cache_write_tokens,
        cache_write_1h_tokens,
    )
    cost = compute_tier(rate.tier(long_context), *args)
    if not long_context:
        return cost
    # Both tiers, so the surcharge for crossing the threshold is knowable here
    # and nowhere downstream -- the bucket keeps only the tier it landed in.
    return replace(cost, standard=compute_tier(rate.short, *args).cost)


def effective_rate(cost: float, input_tokens: int) -> float:
    """USD per million input tokens processed -- the headline efficiency metric.

    Lower is better. It folds cache hit rate, context tier and model mix into
    one number that is directly comparable over time.
    """
    if input_tokens <= 0:
        return 0.0
    return cost / (input_tokens / 1e6)


def _tier_from(spec: dict, fallback: Tier | None = None) -> Tier:
    base = fallback or Tier(0.0, 0.0, 0.0)
    return Tier(
        input=float(spec.get("input", base.input)),
        cached_input=float(spec.get("cached_input", base.cached_input)),
        output=float(spec.get("output", base.output)),
        cache_write=float(spec.get("cache_write", base.cache_write)),
        cache_write_1h=float(spec.get("cache_write_1h", base.cache_write_1h)),
    )


class PricingTable:
    """Rate lookup with alias resolution and mtime-based hot reload."""

    def __init__(self, path: Path | None = None):
        self._path = path or settings.pricing_path
        self._lock = threading.Lock()
        self._mtime: float | None = None
        self._rates: dict[str, Rate] = {}
        self._default_threshold = DEFAULT_LONG_CONTEXT_THRESHOLD
        self._unpriced: set[str] = set()
        self.reload()

    @property
    def path(self) -> Path:
        return self._path

    def reload(self) -> None:
        with self._lock:
            self._load_locked()

    def _load_locked(self) -> None:
        try:
            self._mtime = self._path.stat().st_mtime
            with self._path.open("rb") as fh:
                raw = tomllib.load(fh)
        except FileNotFoundError:
            self._mtime = None
            raw = {}

        defaults = raw.get("defaults", {}) or {}
        self._default_threshold = int(
            defaults.get("long_context_threshold", DEFAULT_LONG_CONTEXT_THRESHOLD)
        )

        models = raw.get("models", {}) or {}
        rates: dict[str, Rate] = {}

        def build(spec: dict) -> Rate:
            short = _tier_from(spec)
            # A model with no published long tier bills long prompts at its
            # short rates rather than silently inventing a premium.
            long = _tier_from(spec.get("long", {}) or {}, fallback=short)
            return Rate(
                short=short,
                long=long,
                threshold=int(
                    spec.get("long_context_threshold", self._default_threshold)
                ),
                estimated=bool(spec.get("estimated", False)),
                long_tier_unknown=bool(spec.get("long_tier_unknown", False)),
                source=spec.get("source"),
            )

        # Two passes so `inherit` can point at an entry declared later.
        for name, spec in models.items():
            if isinstance(spec, dict) and "inherit" not in spec:
                rates[name] = build(spec)
        for name, spec in models.items():
            if not isinstance(spec, dict):
                continue
            target = spec.get("inherit")
            if not target:
                continue
            base = rates.get(target)
            if base is None:
                continue
            merged = replace(base)
            if any(
                k in spec
                for k in (
                    "input",
                    "cached_input",
                    "output",
                    "cache_write",
                    "cache_write_1h",
                )
            ):
                merged = replace(merged, short=_tier_from(spec, fallback=base.short))
            if "long" in spec:
                merged = replace(
                    merged, long=_tier_from(spec["long"], fallback=merged.short)
                )
            if "long_context_threshold" in spec:
                merged = replace(merged, threshold=int(spec["long_context_threshold"]))
            for flag in ("estimated", "long_tier_unknown"):
                if flag in spec:
                    merged = replace(merged, **{flag: bool(spec[flag])})
            if "source" in spec:
                merged = replace(merged, source=spec["source"])
            rates[name] = merged

        self._rates = rates
        self._unpriced = set()

    def maybe_reload(self) -> bool:
        """Reload if the file changed on disk. Returns True if it did."""
        try:
            mtime = self._path.stat().st_mtime
        except FileNotFoundError:
            return False
        if mtime == self._mtime:
            return False
        self.reload()
        return True

    def get(self, model: str | None) -> Rate | None:
        """Resolve a rate, preferring an exact match over the bare base model.

        An explicit ``models."pooler/gpt-5.6-sol"`` entry wins; otherwise the
        prefix is stripped and the underlying model's rate applies.
        """
        if not model:
            return None
        with self._lock:
            rate = self._rates.get(model)
            if rate is not None:
                return rate
            _, base = split_model(model)
            rate = self._rates.get(base)
            if rate is None:
                self._unpriced.add(model)
            return rate

    def cost(
        self,
        model: str | None,
        input_tokens: int,
        cached_tokens: int,
        output_tokens: int,
        long_context: bool | None = None,
        cache_write_tokens: int = 0,
        cache_write_1h_tokens: int = 0,
    ) -> Cost:
        return compute(
            self.get(model),
            input_tokens,
            cached_tokens,
            output_tokens,
            long_context,
            cache_write_tokens,
            cache_write_1h_tokens,
        )

    def threshold_for(self, model: str | None) -> int:
        rate = self.get(model)
        return rate.threshold if rate else self._default_threshold

    def thresholds(self) -> dict[str, int]:
        """Per-model thresholds, for building the tier expression in SQL."""
        with self._lock:
            return {name: r.threshold for name, r in self._rates.items()}

    @property
    def default_threshold(self) -> int:
        return self._default_threshold

    def unpriced(self) -> list[str]:
        """Models seen at lookup time with no configured rate."""
        with self._lock:
            return sorted(self._unpriced)

    def as_dict(self) -> dict:
        with self._lock:
            return {
                "path": str(self._path),
                "default_threshold": self._default_threshold,
                "models": {
                    name: {
                        "input": r.short.input,
                        "cached_input": r.short.cached_input,
                        "output": r.short.output,
                        "cache_write": r.short.cache_write,
                        "cache_write_1h": r.short.cache_write_1h,
                        "long_input": r.long.input,
                        "long_cached_input": r.long.cached_input,
                        "long_output": r.long.output,
                        "threshold": r.threshold,
                        "charges_cache_writes": bool(
                            r.short.cache_write or r.short.cache_write_1h
                        ),
                        "has_long_tier": r.long != r.short,
                        "long_tier_unknown": r.long_tier_unknown,
                        "estimated": r.estimated,
                        "source": r.source,
                    }
                    for name, r in sorted(self._rates.items())
                },
                "unpriced": sorted(self._unpriced),
            }


pricing = PricingTable()

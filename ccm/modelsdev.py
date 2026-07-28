"""Reference prices from models.dev, cached on disk.

`pricing.toml` stays the source of truth -- it is what every figure is computed
from, and it is hand-checked against each vendor's own pricing page. This module
supplies a second opinion to hold it against, so a rate that goes stale shows up
as a flagged row rather than as a number nobody questions.

The feed is ~3 MB covering 170 providers, so it is fetched only when asked for,
cached to disk, and served from that cache thereafter. Being unable to reach it
is never an error: the tool works entirely offline, just without the comparison.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

API_URL = "https://models.dev/api.json"

#: Providers whose listing is the vendor's own, preferred when a model is
#: resold by many. Resellers occasionally quote a different (often marked-up or
#: differently-structured) price for the same model.
FIRST_PARTY = (
    "anthropic",
    "openai",
    "xai",
    "minimax",
    "zhipuai",
    "z-ai",
    "deepseek",
    "google",
    "opencode",
)

FETCH_TIMEOUT = 30.0


class ModelsDev:
    """Disk-cached view of the models.dev catalogue."""

    def __init__(self, cache_path: Path):
        self.cache_path = cache_path
        self._lock = threading.Lock()
        self._data: dict | None = None
        self._fetched_at: float | None = None
        self._error: str | None = None
        self._load()

    # -- cache -------------------------------------------------------------

    def _load(self) -> None:
        try:
            with self.cache_path.open("rb") as fh:
                payload = json.load(fh)
        except (OSError, ValueError):
            return
        if isinstance(payload, dict) and "providers" in payload:
            self._data = payload["providers"]
            self._fetched_at = payload.get("fetched_at")

    def _save(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_path.with_suffix(".tmp")
        with tmp.open("w") as fh:
            json.dump({"fetched_at": self._fetched_at, "providers": self._data}, fh)
        tmp.replace(self.cache_path)

    def refresh(self) -> dict:
        """Fetch the catalogue now. Never raises; reports failure in the result.

        On failure the previous cache is left intact, so a flaky network
        degrades the comparison to "stale" rather than losing it.
        """
        try:
            request = urllib.request.Request(
                API_URL, headers={"User-Agent": "agent-cache-monitor"}
            )
            with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
                payload = json.load(response)
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            with self._lock:
                self._error = str(exc)
            return self.status()
        if not isinstance(payload, dict):
            with self._lock:
                self._error = "unexpected payload shape"
            return self.status()
        with self._lock:
            self._data = payload
            self._fetched_at = time.time()
            self._error = None
            try:
                self._save()
            except OSError as exc:
                self._error = f"fetched but could not cache: {exc}"
        return self.status()

    def status(self) -> dict:
        with self._lock:
            return {
                "url": API_URL,
                "cache_path": str(self.cache_path),
                "fetched_at": self._fetched_at,
                "available": self._data is not None,
                "providers": len(self._data or {}),
                "error": self._error,
            }

    # -- lookup ------------------------------------------------------------

    def compare(self, pricing, volumes: dict[str, dict[str, int]]) -> list[dict]:
        """Hold every configured rate against the catalogue.

        ``volumes`` maps a model to the tokens actually observed in each billing
        category. It is what stops the table crying wolf: models.dev quotes a
        cache-write rate for the OpenAI models (1.25x input, its house
        convention) that OpenAI does not charge and Codex never reports. With
        zero tokens in that category the difference cannot move a single figure,
        so it is reported as inert rather than as a discrepancy.
        """
        table = pricing.as_dict()["models"]
        out: list[dict] = []
        for name, spec in sorted(table.items()):
            reference = self.rates_for(name)
            seen = volumes.get(name, {})
            row = {
                "model": name,
                "estimated": spec["estimated"],
                "observed_tokens": sum(seen.values()),
                "reference": None,
                "provider": None,
                "fields": [],
                "status": "unlisted",
            }
            if reference is None:
                out.append(row)
                continue
            best = reference["best"]
            row["reference"] = best
            row["provider"] = best["provider"]
            row["offers"] = len(reference["offers"])

            worst = "match"
            for field, volume_key in (
                ("input", "fresh"),
                ("cached_input", "cached"),
                ("cache_write", "written"),
                ("output", "output"),
            ):
                ours = spec[field]
                theirs = best.get(field)
                if theirs is None:
                    state = "unlisted"
                elif abs(ours - theirs) < 1e-9:
                    state = "match"
                elif seen.get(volume_key, 0) > 0:
                    state = "differs"
                    worst = "differs"
                else:
                    state = "inert"
                    if worst == "match":
                        worst = "inert"
                row["fields"].append(
                    {
                        "field": field,
                        "ours": ours,
                        "theirs": theirs,
                        "tokens": seen.get(volume_key, 0),
                        "state": state,
                    }
                )
            row["status"] = worst
            out.append(row)
        # Anything actually wrong first, then the merely inert, then matches.
        rank = {"differs": 0, "unlisted": 1, "inert": 2, "match": 3}
        out.sort(key=lambda r: (rank.get(r["status"], 4), -r["observed_tokens"]))
        return out

    def rates_for(self, model: str) -> dict | None:
        """Every provider's quoted rate for one model id, first-party first."""
        with self._lock:
            data = self._data
        if not data:
            return None
        offers = []
        for provider_id, provider in data.items():
            entry = (provider.get("models") or {}).get(model)
            if not isinstance(entry, dict):
                continue
            cost = entry.get("cost") or {}
            offers.append(
                {
                    "provider": provider_id,
                    "input": cost.get("input"),
                    "cached_input": cost.get("cache_read"),
                    "cache_write": cost.get("cache_write"),
                    "output": cost.get("output"),
                    "context": (entry.get("limit") or {}).get("context"),
                }
            )
        if not offers:
            return None
        offers.sort(key=lambda o: (o["provider"] not in FIRST_PARTY, o["provider"]))
        return {"best": offers[0], "offers": offers}

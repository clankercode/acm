# Long-context ("Long above") thresholds

**Date:** 2026-07-29

## What "Long above" means

**Long above** is a *billing* boundary, not a context-window size.

Many vendors publish two rate cards for the same model:

- **Short tier** — standard $/MTok rates for prompts at or under a length cutoff
- **Long tier** — higher rates for prompts that cross that cutoff

Once a request's prompt crosses the threshold, the vendor typically bills **the
entire request** at the long rates (not only the tokens above the line). That
cutoff is what acm stores as `long_context_threshold` in `pricing.toml`.

It is **not** the model's maximum context window (e.g. 1M tokens). A model can
have a 1M window and still charge one flat rate for the whole window, or it can
have a smaller window and still apply a long-context surcharge at 272K.

In the UI, "Long above" is the human label for this threshold. When short and
long rates are identical (or when the model has no long tier), the number may
still appear because of the table default, but it does not change cost.

## acm comparison convention

```text
long_context  ⇔  input_tokens > long_context_threshold
```

Implementation: `PricingRate.tier_for` and cost paths use strict greater-than
(`input_tokens > threshold`). Exactly-at-threshold requests stay on the short
tier.

Vendor wording varies:

| Vendor | Typical wording | acm threshold | Boundary note |
|--------|-----------------|---------------|---------------|
| OpenAI | `>272K` / short labeled `(<272K context length)` | `272_000` | Matches strict `>` |
| xAI | `(< 200k)` vs `(≥ 200k)` | `200_000` | Vendor is inclusive at 200k; acm bills short at exactly 200_000 (one-token edge) |
| MiniMax-M3 | `≤ 512k` vs `> 512k` | `512_000` | Matches strict `>` |

Default for any model that does not set its own threshold:

```toml
[defaults]
long_context_threshold = 200_000
```

Models with **no** `[models.X.long]` block still inherit this default. Their
resolved long rates equal short rates, so the default is **inert for cost**
(same $/token either side of the line). The UI may still show "Long above 200k"
for those models; treat that as display inheritance, not evidence of a surcharge.

## Decision summary (2026-07-29)

| Action | Models |
|--------|--------|
| Set threshold to **272_000** | `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`, `gpt-5.5`, `gpt-5.4` |
| Keep **200_000** (confirmed long tier) | `grok-4.5`, `grok-4.20-0309-non-reasoning`, `grok-4.5-build` (inherit), `grok-build` |
| Keep **512_000** | `MiniMax-M3` |
| No long tier — do **not** invent a threshold; default remains inert | All other shipped models (OpenAI mini/codex/5.2, Anthropic, MiniMax M2.7, GLM, Kimi, MiMo, free OpenCode aliases, composer) |

No redundant per-model `long_context_threshold` lines were present on no-long-tier
models; none were removed. Only the five OpenAI long-tier models changed.

## Full model table

Columns:

- **Current (pricing.toml)** — value after this change (or default inheritance)
- **Recommended** — research recommendation
- **Has long tier?** — whether `pricing.toml` / vendor publishes distinct long rates
- **Source** — primary docs (or models.dev when first-party is missing)
- **Confidence** — research confidence
- **Notes**

| Model | Current (pricing.toml) | Recommended | Has long tier? | Source | Confidence | Notes |
|-------|------------------------|-------------|----------------|--------|------------|-------|
| gpt-5.6-sol | 272_000 | 272_000 | yes | [OpenAI pricing](https://developers.openai.com/api/docs/pricing); models.dev tier size 272000 | high | Was 200_000; corrected to 272K |
| gpt-5.6-terra | 272_000 | 272_000 | yes | same | high | Was 200_000 |
| gpt-5.6-luna | 272_000 | 272_000 | yes | same | high | Was 200_000 |
| gpt-5.5 | 272_000 | 272_000 | yes | OpenAI pricing; models.dev 272000 | high | Was 200_000 |
| gpt-5.4 | 272_000 | 272_000 | yes | OpenAI pricing + model page | high | Was 200_000 |
| gpt-5.4-mini | 200_000 (default, inert) | none / inert | no | OpenAI pricing (long columns `-`) | high | No long rates; UI default only |
| gpt-5.2 | 200_000 (default, inert) | none / inert | no | OpenAI pricing | high | Flat rates only |
| gpt-5.2-codex | 200_000 (default via inherit gpt-5.2) | none / inert | no | OpenAI / models.dev | high | inherit gpt-5.2 |
| gpt-5.3-codex | 200_000 (default via inherit gpt-5.2) | none / inert | no | OpenAI / models.dev | high | inherit gpt-5.2 |
| gpt-5.1-codex-mini | 200_000 (default, inert) | none / inert | no | OpenAI pricing | high | Flat rates only |
| grok-4.5 | 200_000 | 200_000 | yes | [xAI pricing](https://docs.x.ai/developers/pricing); models.dev 200000 | high | Confirmed; keep |
| grok-4.20-0309-non-reasoning | 200_000 | 200_000 | yes | xAI pricing; models.dev 200000 | high | Confirmed; keep |
| grok-4.5-build | 200_000 (via inherit grok-4.5) | 200_000 | yes (inherited) | inherits grok-4.5; not own models.dev id | high | Alias of grok-4.5 billing |
| grok-build | 200_000 | 200_000 | yes | xAI (id grok-build-0.1); models.dev 200000 | high | Confirmed; keep |
| grok-composer-2.5-fast | 200_000 (default, inert) | unknown / none | no (long_tier_unknown) | Not on xAI dual-tier table | low | estimated rates; no documented long surcharge |
| claude-opus-5 | 200_000 (default, inert) | none / inert | no | [Claude pricing](https://platform.claude.com/docs/en/about-claude/pricing) | high | Full 1M at standard rates |
| claude-opus-4-8 | 200_000 (default via inherit) | none / inert | no | Anthropic | high | inherit claude-opus-5 |
| claude-fable-5 | 200_000 (default, inert) | none / inert | no | Anthropic | high | No long tier |
| claude-sonnet-4-6 | 200_000 (default, inert) | none / inert | no | Anthropic | high | No long tier |
| claude-sonnet-5 | 200_000 (default, inert) | none / inert | no | Anthropic | high | No long tier |
| claude-haiku-4-5 | 200_000 (default, inert) | none / inert | no | Anthropic | high | No long tier |
| claude-haiku-4-5-20251001 | 200_000 (default via inherit) | none / inert | no | Anthropic | high | inherit haiku-4-5 |
| claude-opus-4-6 | 200_000 (default via inherit) | none / inert | no | Anthropic | high | inherit claude-opus-5 |
| claude-sonnet-4-5 | 200_000 (default via inherit) | none / inert | no | Anthropic | high | inherit sonnet-4-6 |
| MiniMax-M3 | 512_000 | 512_000 | yes | [MiniMax paygo](https://platform.minimax.io/docs/guides/pricing-paygo); models.dev 512000 | high | Keep 512K |
| MiniMax-M2.7 | 200_000 (default, inert) | none / inert | no | MiniMax paygo (flat) | high | No ≤/> split |
| MiniMax-M2.7-highspeed | 200_000 (default, inert) | none / inert | no | MiniMax paygo (flat) | high | No ≤/> split |
| glm-5.2 | 200_000 (default, inert) | none / inert | no | [Z.ai pricing](https://docs.z.ai/guides/overview/pricing) | high | Single rates |
| glm-5.1 | 200_000 (default, inert) | none / inert | no | Z.ai | high | Single rates |
| glm-5 | 200_000 (default, inert) | none / inert | no | Z.ai | high | Single rates |
| glm-5-turbo | 200_000 (default, inert) | none / inert | no | Z.ai | high | Single rates |
| glm-4.7 | 200_000 (default, inert) | none / inert | no | Z.ai | high | Single rates |
| glm-4.5 | 200_000 (default, inert) | none / inert | no | Z.ai (estimated rates) | high | Single rates; rates estimated |
| k3 | 200_000 (default, inert) | none / inert | no | [Kimi k3](https://platform.kimi.ai/docs/pricing/chat-k3) | high | Full window, one price |
| k3-256k | 200_000 (default via inherit) | none / inert | no | Kimi | high | Context-capped lane of k3 |
| k2p7 | 200_000 (default, inert) | none / inert | no | [Kimi k2.7 code](https://platform.kimi.ai/docs/pricing/chat-k27-code) | high | Single rates |
| kimi-for-coding | 200_000 (default via inherit) | none / inert | no | models.dev / Kimi | high | inherit k2p7 |
| kimi-for-coding-highspeed | 200_000 (default, inert) | none / inert | no | Kimi | high | Premium lane, still flat |
| k2p5 | 200_000 (default, inert) | none / inert | no | Kimi k2.5 pricing | high | Single rates |
| kimi-k2.5-free | 200_000 (default, inert) | none / inert | no | models.dev / OpenCode free | medium | $0; no long tier |
| mimo-v2.5-pro | 200_000 (default, inert) | none / inert | no | [MiMo paygo](https://mimo.mi.com/docs/en-US/price/pay-as-you-go) | high | Pricing no longer length-tiered |
| mimo-v2.5-pro-ultraspeed | 200_000 (default, inert) | none / inert | no | MiMo paygo | high | Single rates |
| big-pickle | 200_000 (default, inert) | none / inert | no | OpenCode Zen free; models.dev | medium | $0 free alias |
| deepseek-v4-flash-free | 200_000 (default via inherit) | none / inert | no | OpenCode Zen free | medium | inherit big-pickle |

## Research log

Research assembled 2026-07-29 from models.dev (`https://models.dev/api.json`) and
first-party pricing pages.

### models.dev

API: `https://models.dev/api.json` (fetched successfully).

Tiered models (first-party `tiers` with `tier.type = "context"`):

| Model id (match) | Provider | tier.size |
|------------------|----------|-----------|
| gpt-5.6-sol | openai | 272000 |
| gpt-5.6-terra | openai | 272000 |
| gpt-5.6-luna | openai | 272000 |
| gpt-5.5 | openai | 272000 |
| gpt-5.4 | openai | 272000 |
| grok-4.5 | xai | 200000 |
| grok-4.20-0309-non-reasoning | xai | 200000 |
| grok-build → grok-build-0.1 | xai | 200000 |
| MiniMax-M3 | minimax | 512000 |

All other acm models either matched with **no** `tiers` array (no long-context
surcharge in models.dev) or were missing as first-party ids
(`grok-4.5-build`, `grok-composer-2.5-fast`).

Example models.dev shape for OpenAI long tier (gpt-5.4):

```text
tiers=[{"input":5,"output":22.5,"cache_read":0.5,"tier":{"type":"context","size":272000}}]
legacy_context_over_200k present
```

The legacy `context_over_200k` field is historical; current tier size is 272000.

### OpenAI (primary)

- **URL:** https://developers.openai.com/api/docs/pricing  
- **Secondary:** https://developers.openai.com/api/docs/models/gpt-5.4  
- **Evidence (long tier family):**  
  > Prompts with >272K input tokens are priced at 2x input and 1.5x output for the full request.  
  Pricing table short tier labeled **`(<272K context length)`**.
- **Models with long tier:** gpt-5.6-sol, gpt-5.6-terra, gpt-5.6-luna, gpt-5.5, gpt-5.4  
- **Models without:** gpt-5.4-mini, gpt-5.2, codex variants — long context columns shown as `-` / single flat rate.
- **Confidence:** high  
- **acm action:** set `long_context_threshold = 272_000` on the five long-tier models (was incorrectly 200_000).

### xAI (primary)

- **URL:** https://docs.x.ai/developers/pricing  
- **Secondary:** https://docs.x.ai/docs/models  
- **Evidence:** Rows labeled **`(< 200k prompt tokens)`** vs **`(≥ 200k prompt tokens)`**.  
  > requests whose prompt reaches the listed token threshold are billed at the higher rate for all tokens.
- **Models:** grok-4.5, grok-4.20-0309-non-reasoning, grok-build (and grok-4.5-build via inherit).
- **Confidence:** high for those four; **low** for grok-composer-2.5-fast (not listed with dual short/long tiers).
- **acm action:** keep `200_000`; leave composer without a long block (`long_tier_unknown = true`).

### Anthropic (primary)

- **URL:** https://platform.claude.com/docs/en/about-claude/pricing  
- **Evidence:**  
  > Long context pricing: Claude 4.6 and later models include the full 1M token context window at standard pricing. A 900k-token request is billed at the same per-token rate as a 9k-token request.
- **Confidence:** high  
- **acm action:** no `[long]` tables; default threshold remains inert.

### MiniMax (primary)

- **URL:** https://platform.minimax.io/docs/guides/pricing-paygo  
- **Secondary:** https://www.minimax.io/blog/minimax-m3  
- **Evidence (M3):** priced as **`≤ 512k`** vs **`> 512k`** input tokens (higher long rates).  
- **Evidence (M2.7 / highspeed):** single flat Input/Output/cache rates; no length split.
- **Confidence:** high  
- **acm action:** keep MiniMax-M3 at `512_000`; leave M2.7 family without long tier.

### Z.ai / Zhipu GLM (primary)

- **URL:** https://docs.z.ai/guides/overview/pricing  
- **Evidence:** Text Models table lists single Input/Cached Input/Output prices per model with no long-context tier.
- **Confidence:** high  
- **acm action:** none (inert default only).

### Moonshot / Kimi (primary)

- **URL:** https://platform.kimi.ai/docs/pricing/chat-k3  
- **Secondary:** https://platform.kimi.ai/docs/pricing/chat-k27-code  
- **Evidence:** One cache-hit / cache-miss / output price per model across the full context window (e.g. kimi-k3 1,048,576 tokens); no long-context surcharge tier.
- **Confidence:** high  
- **acm action:** none (inert default only).

### Xiaomi MiMo (primary)

- **URL:** https://mimo.mi.com/docs/en-US/price/pay-as-you-go  
- **Secondary:** https://mimo.mi.com/docs/en-US/news/latest/v2.5-price-update  
- **Evidence:** Pay-as-you-go lists single cache-hit/miss/output rates.  
  > new pricing no longer differentiates based on the input length.
- **Confidence:** high  
- **acm action:** none (inert default only).

### OpenCode free aliases

- **URL:** https://models.dev/ / OpenCode Zen docs  
- **Evidence:** free / no tiers for big-pickle and deepseek-v4-flash-free; no first-party long-context surcharge for free SKUs.
- **Confidence:** medium  
- **acm action:** none ($0 either side of any threshold).

## Implementation notes

1. Changing `long_context_threshold` triggers a **bucket rebuild** (see comment at
   top of `pricing.toml`), because hour buckets key on long vs short.
2. Do not invent thresholds for models that lack long rates. Prefer omitting the
   key and documenting inert default behavior over fake precision.
3. When vendor docs and models.dev disagree, prefer the vendor pricing page;
   models.dev is corroboration and a convenient machine-readable check.
4. xAI's inclusive `≥ 200k` vs acm's exclusive `>` is a documented one-token
   edge case; not adjusted unless product owners want a `>=` convention change.

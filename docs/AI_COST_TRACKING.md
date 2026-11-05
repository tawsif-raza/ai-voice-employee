# AI usage and cost tracking

Status: Phase 2 of the AI Engineering Toolkit (`tools/aitk/`). **Offline-verified only** — see
[What is and is not verified](#what-is-and-is-not-verified).

## Three different things — never confuse them

| | What it is | Where it comes from | Can it be wrong? |
|---|---|---|---|
| **Provider-reported usage** | Token counts a provider put in its own response | `usage` on a provider's final response dict (`src/inference/llm_usage.py`) | Only if the provider's metadata is wrong or this parser is |
| **Cost estimate** | Those counts × list prices **you configured** | `aitk.cost` (`tools/aitk/cost/`) | Yes — see below |
| **Actual billing** | What the provider invoices | The provider's invoice / billing dashboard | Authoritative; never computed here |

Every number this toolkit produces is an **estimate**. It is labelled `basis: "token_estimate"` and every
report `Result` carries the note *"Estimate: … Not an invoice"*. An estimate can differ from an invoice because it
**cannot see**:

- failed, aborted or retried attempts (a stream that dies mid-way may still be billed — only successful
  responses carry usage; `FallbackLLMProvider` reports only the provider that answered);
- free-tier allowances, committed-use discounts, credits, taxes, minimum fees, rounding;
- price tiers that depend on things not in the usage object (long-context tiers, batch/priority modes, cache
  time-to-live tiers) — if your plan has them, price them explicitly or treat the figure as indicative;
- requests made outside this process.

Reconcile against the real invoice periodically. If they disagree, trust the invoice and fix the price file.

## What gets reported

A successful remote-provider response gains an **optional** `"usage"` key. Nothing else about the final dict
changed (`text`, `latency_ms`, `provider`, `model`, `fallback_used`, … are as before), and no consumer reads it yet.

```python
{
    "usage_version": 1,
    "source": "provider_reported",
    "input_tokens": 100,               # prompt tokens NOT served from a cache
    "output_tokens": 50,               # billed output; includes reasoning/thinking tokens
    "cache_read_input_tokens": 20,     # only if the provider reported it
    "cache_creation_input_tokens": 5,  # only if the provider reported it (Anthropic)
    "reasoning_tokens": 12,            # only if reported; a SUBSET of output_tokens (informational)
    "response_model": "…",             # the model the provider says it ran, if it said
}
```

**Absent means unknown, never zero.** The key is omitted unless the provider reported both an input and an output
count and everything it reported is well-formed (non-negative integers; cached ≤ prompt). Malformed metadata
discards usage entirely; it never breaks the response.

### Provider coverage

| Provider | Where usage comes from | Cached tokens | Normalization | Status |
|---|---|---|---|---|
| `claude` | `message_start.message.usage` + cumulative `message_delta.usage`; model from `message_start.message.model` | `cache_read_input_tokens`, `cache_creation_input_tokens` (separate from `input_tokens`) | none — `input_tokens` already excludes cache. `message_start`'s `output_tokens` is a placeholder and is ignored; a stream without `message_delta` yields no usage | Parsed; synthetic-verified |
| `gemini` | last `usageMetadata` chunk; model from `modelVersion` | `cachedContentTokenCount` (**included** in `promptTokenCount`) | input = prompt − cached (+ tool-use prompt); output = candidates + thoughts (thinking is billed as output) | Parsed; synthetic-verified |
| `groq` | `usage` or `x_groq.usage` **if a streamed chunk carries it** | `prompt_tokens_details.cached_tokens` | input = prompt − cached | Parser exists, but **whether Groq sends usage on streamed chunks is unconfirmed**; the request was deliberately not changed (no `stream_options`). Expect "unknown usage" until verified live |
| `local` | none | — | — | No usage (no provider, no bill) |

Provider behaviour above is taken from the providers' public documentation as summarized by search results; it has
**not** been confirmed against live responses (see below).

## Cost statuses

`aitk.cost.tracker.estimate_cost` / `CostTracker.record_response` return a `CostEstimate` whose `status` is:

| Status | Meaning | `cost_usd` |
|---|---|---|
| `PRICED` | valid provider-reported usage **and** a configured price for every token category used | the estimate |
| `UNPRICED` | usage is valid but there is no price — unknown model, unknown identity, or tokens in a category (e.g. cache reads) that has no configured price | `None` |
| `UNKNOWN_USAGE` | usage missing, malformed, wrong version, or not `provider_reported` | `None` |

Rules that follow from "never fabricate":

- A missing price is **not** zero. A price of `0` exists only if someone wrote `0` in the file (e.g. a free tier).
- Cache tokens with no configured cache price make the call `UNPRICED`; they are never silently charged at the
  input rate.
- Zero tokens with a configured price is a real `$0` (`PRICED`), distinct from unknown.
- The model used for the price lookup is the one the provider says it **ran** (`response_model`) when reported,
  otherwise the configured one. A configured alias such as `…-latest` is never assumed to still mean the model you
  priced. Matching is by exact name or an explicit `aliases` entry — no prefixes, wildcards or case folding.
- Aggregates: `Totals.total_cost_usd` is `None` unless **every** recorded call was `PRICED`.
  `priced_cost_usd` is always available but is labelled as the cost of the priced calls only.

## Maintaining the pricing configuration

`tools/aitk/cost/pricing.json` is versioned (`pricing_version: 1`) and **ships empty** — the toolkit contains no
prices of its own, because a wrong built-in price is worse than an honest "unpriced". Copy
`tools/aitk/cost/pricing.example.json` (synthetic numbers) as a starting point.

Each entry needs `provider` (`claude` | `gemini` | `groq`), an exact `model`, `effective_from`, a `source`
(price-page URL or "invoice 2026-09" — where *you* got the number), and `input_per_million` /
`output_per_million` in USD. Optional: `aliases`, `effective_to`, `cache_read_input_per_million`,
`cache_creation_input_per_million`. Prices are stored as strings and computed with `Decimal`.

To change a price: **add** a new entry with the new `effective_from` and set `effective_to` on the old entry to the
day before. Never edit history in place — past usage must still price at the price that applied then. Overlapping
ranges for the same model are rejected at load time, so a lookup is never ambiguous. Validate with:

```bash
python - <<'EOF'
import sys; sys.path.insert(0, "tools")
from aitk.cost.pricing import PricingTable
t = PricingTable.load("path/to/pricing.json")   # raises PricingError with the reason if invalid
print(len(t), "prices OK")
EOF
```

Review checklist for a pricing change: the `source` is real and reachable; the model id is exactly what
`response_model` / the configured model shows in the logs; cache prices are present if you use caching; the old
entry was closed rather than overwritten; compare one real invoice line against `estimate_cost` output.

## Usage

```python
import sys
sys.path.insert(0, "tools")                       # the repo's flat-module convention; see tools/aitk/README.md
from datetime import date

from aitk.core import report
from aitk.core.schema import Evidence
from aitk.cost.pricing import PricingTable
from aitk.cost.tracker import CostTracker

tracker = CostTracker(PricingTable.load("path/to/my_pricing.json"))

# `final` is the last item a provider's generate_stream() yields (the dict).
estimate = tracker.record_response(final, as_of=date.today())
print(estimate.status, estimate.cost_usd, estimate.reason)

# Summaries drop straight into the Phase 1 results schema. The evidence label is REQUIRED:
# SIMULATED for synthetic data, LIVE / CANARY / PRODUCTION for real traffic.
report.write_report(tracker.build_report("my_run", Evidence.LIVE), "outputs/ai_cost.json")
```

`build_report` yields one `ai_cost:overall` result plus one per `(provider, model)`. Status: `PASS` = every call
priced; `PARTIAL` = some unpriced or unknown (and `total_cost_usd_estimate` is then **omitted**, only
`priced_cost_usd_estimate` is present); `NOT_RUN` = nothing recorded. Report numbers are floats converted from the
exact `Decimal` totals.

## Privacy

The tracker reads only `provider`, `model` and `usage` from a response. It never reads message text, keeps no
per-call record (memory is bounded by the number of distinct models), and has no logger. The parsers log at most a
provider name at DEBUG. Tests assert that prompt text, response text and key-like strings never reach an estimate,
summary or report.

## What is and is not verified

- **Verified (offline, `SIMULATED` evidence):** parsing, normalization, pricing, aggregation, the report schema and
  the end-to-end path from a faked provider stream to a priced estimate — `tests/test_llm_usage.py`,
  `tests/test_aitk_cost.py`. No test calls a provider; sockets are blocked in those tests.
- **Not verified:** that live Anthropic, Gemini or Groq responses contain exactly these fields. Direct
  documentation fetches were unavailable when this was written, so field shapes come from search summaries of the
  official docs. Confirm with the opt-in `scripts/live_provider_verification.py` (needs real keys; **not** part of
  the default suite and not run here) before relying on the numbers for budgeting. Known open questions: Groq
  streamed usage, and Gemini `thoughtsTokenCount` on streamed chunks.

## Not done in this phase

- `ConversationManager.handle_turn()` and the HTTP API do **not** surface `usage` (that would change the response
  contract). Wiring it into spans/metrics or a per-session ledger is a follow-up.
- No budget alerts, no invoice import/reconciliation, no estimate for providers that report no usage.
- `tools/` is dev tooling and is not copied into the production image; nothing under `src/` imports it.

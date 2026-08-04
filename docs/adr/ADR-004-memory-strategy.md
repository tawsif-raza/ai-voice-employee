# ADR-004: Stateless-by-Default Platform with a Storage-Only Memory Module

**Date:** 2026-08-04
**Architecture Version:** v1.0 (`architecture-v1.0`, commit `a7d567d`)
**Related Documents:**
- `ARCHITECTURE.md` §8
- `MODULES.md` §4 (Memory), Dependency Rule 3
- `SEQUENCE_DIAGRAMS.md` — Diagram 1, Diagram 6
- `DOMAIN_MODEL.md` — Group 1 (`ConversationSession`, `Turn`, `ContextWindow`)
- `ARCHITECTURE_REVIEW.md` finding 4.1

# Status

Accepted

# Context

`ARCHITECTURE.md` §8 chose a stateless platform — the caller supplies full conversation history on every request — for horizontal-scaling simplicity. `ARCHITECTURE_REVIEW.md` finding 4.1 (High severity) identified the unaddressed consequence: every turn re-sends and re-processes the entire history, so per-turn cost and latency grow unbounded with conversation length. A Memory module needed a design that closes that gap without contradicting the platform's existing dependency rules (specifically, without giving a leaf-service module a peer-to-peer dependency on another leaf-service module).

# Decision

The platform remains stateless-by-default at the transport level (`ARCHITECTURE.md` §8 is unchanged). `MODULES.md` §4 defines Memory's target design as a storage module with a sliding-window + running-summary strategy: the most recent `keep_last_n` turns are kept verbatim, older turns are collapsed into a running summary. Critically, Memory does not generate that summary itself — generating it is a text-generation call, and per Dependency Rule 3 only Conversation Manager may route a generation call through LLM Service. The flow is: Conversation Manager notices the trigger, assembles a summarization prompt, calls LLM Service, and hands the resulting text to `Memory.summarize()` to persist. Memory's role is strictly storage and composition.

# Alternatives Considered

- **Keep the platform stateless forever**, pushing truncation/summarization responsibility onto the Client. Not selected: the Client has no efficient way to summarize history without server-side LLM access anyway, so this doesn't actually solve the cost-scaling problem `ARCHITECTURE_REVIEW.md` 4.1 raised — it just relocates it to a place with fewer resources to solve it.
- **Memory calling LLM Service directly** to generate its own summaries. Not selected: this would violate Dependency Rule 3 (no leaf-service module calls another leaf-service module directly) and would give a storage-only module a text-generation dependency it doesn't otherwise need, undermining its "testable purely against its own storage contract" unit-test boundary (§4).
- **Truncation-only, no summarization** — always drop the oldest turns outright. Retained, but only as the fallback path when summarization is unavailable or fails, not as the primary strategy, since it's strictly lossier than summarizing.

# Consequences

**Positive**
- Bounds per-turn cost/latency growth regardless of true conversation length, directly closing `ARCHITECTURE_REVIEW.md` finding 4.1.
- Memory stays dependency-free (Dependency Rule 3 compliant) and independently unit-testable, since generation is never its own responsibility.

**Negative**
- Summarization adds an extra LLM Service call to the turn's critical path whenever the trigger fires, with no documented latency budget for how that call is scheduled relative to the user-facing response.

**Known Limitations**
- Memory itself is not yet implemented (§4 Status) — the platform is stateless today by the original `ARCHITECTURE.md` §8 design, and this entire strategy is target-state only.

# Trade-offs

Accepting an occasional extra generation call (for summarization) in exchange for bounded history size is a direct trade of per-turn latency/cost predictability against average-case efficiency — most turns pay nothing extra, but a turn that crosses the summarization threshold pays for two generation calls instead of one. The alternative (unbounded growth) was judged worse because it degrades every turn gradually rather than a few turns predictably.

# Risks

- No concrete default exists yet for the summarization trigger threshold (turn count or token budget) — `Config Service.get_memory_config()` is named but not populated with values.
- Memory's backing store and retention policy for conversation history — which may contain sensitive content given the platform's medicine domain — remain an open question (`MODULES.md` Open Questions; `docs/DATABASE.md`), intersecting with `ARCHITECTURE_REVIEW.md` finding 5.4 (conversation-content logging/PII exposure risk).
- A summarization failure falls back to truncation per §4 Error Handling, but the user-facing consequence of losing detail via truncation (vs. summarization) mid-conversation is not evaluated anywhere.

# Future Work

- Implement Memory and validate the summarization trigger/threshold against real conversation-length data.
- Decide the backing store and retention policy for stored history (`docs/DATABASE.md`).
- Evaluate PII/sensitive-content handling for stored and summarized conversation content specifically in the medicine domain.

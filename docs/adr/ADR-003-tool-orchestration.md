# ADR-003: Tool Orchestrator as the Sole Path to Business Systems

**Date:** 2026-08-04
**Architecture Version:** v1.0 (`architecture-v1.0`, commit `a7d567d`)
**Related Documents:**
- `ARCHITECTURE.md` — Core Principle 2, Communication Rule 7
- `MODULES.md` §8 (Tool Orchestrator), Ownership subsection
- `SEQUENCE_DIAGRAMS.md` — Diagram 3 (Tool Execution Flow)
- `DOMAIN_MODEL.md` — Group 4 (`ToolRequest`, `ToolResponse`, `ToolExecution`)
- `MODULES_REVIEW.md` finding 5.1

# Status

Accepted

# Context

Once a platform lets an LLM's output influence real-world business actions (placing orders, changing appointments, billing), something has to decide two separate questions: *whether/which* action a turn warrants, and *whether that specific action is safe and validated to execute*. Left unspecified, an LLM could be allowed to trigger actions directly from its generated text — a well-known class of risk for LLM-driven systems. `MODULES_REVIEW.md` finding 5.1 had flagged this translation layer as an undocumented gap in the original module review.

# Decision

Tool Orchestrator is the only module permitted to invoke external business systems (`ARCHITECTURE.md` Core Principle 2). Ownership is split explicitly: Conversation Manager decides *whether and which* action a turn warrants, using Intent Engine's classification and the LLM's response as signal — never as direct instruction; Tool Orchestrator owns validation, confirmation-gating, and execution against a fixed, versioned action catalog. Raw LLM output is never passed to `invoke()`. Actions that require confirmation are declared via `ActionSpec.requires_confirmation`, and Tool Orchestrator rejects an unconfirmed invocation with a `confirmation_required` status rather than executing it (`MODULES.md` §8).

# Alternatives Considered

- **LLM function-calling directly against business systems** (the LLM's output is itself the executable instruction). Not selected: this is precisely what `ARCHITECTURE.md` Core Principle 2 rules out — "the LLM never calls business APIs directly."
- **Tool Orchestrator self-deciding from parsed LLM output**, rather than Conversation Manager making the decision. Not selected: this would give Tool Orchestrator implicit trust in LLM text it has no way to validate against intent; the decision was resolved to Conversation Manager instead, which already holds both the classified intent and the full turn context.
- **No confirmation gating** — executing every validated action immediately. Not selected for irreversible actions (cancellations, billing changes); the `requires_confirmation` flag plus `confirmation_required` status was chosen so the same interface serves both reversible and irreversible actions without a second code path.

# Consequences

**Positive**
- A single, auditable boundary exists between free-form generation and real-world side effects — every business action traces back to one `ToolRequest`/`ToolResponse` pair Conversation Manager explicitly issued.
- Confirmation-gating is expressed in the interface contract itself (`ActionRequest.confirmed`, `ActionResult.status`), not left as an ad hoc convention each action has to reinvent.

**Negative**
- A confirmation-gated action costs an extra conversational turn — the user must be asked and must respond before the action executes.

**Known Limitations**
- No business actions exist yet; the entire flow (`MODULES.md` §8 Status) is target-state, exercised only in `SEQUENCE_DIAGRAMS.md` Diagram 3, not against a real integration.

# Trade-offs

Isolating business-API flakiness inside Tool Orchestrator's own retry/timeout policy (§8) is the one deliberate exception to the platform's otherwise fail-safe-over-retry posture — accepted because business actions are the one class of failure where "silently give up" and "keep trying briefly" have materially different real-world consequences (a failed retry vs. a failed appointment booking) that the rest of the platform's fallback model doesn't need to distinguish.

# Risks

- The retry count/backoff for Tool Orchestrator's self-owned policy has no specified bound (`SEQUENCE_DIAGRAMS.md` item M3) — a business API outage could retry indefinitely or insufficiently with no documented ceiling.
- The confirmation round-trip has no TTL — a stale `confirmed=true` request from an old turn could theoretically be replayed against a since-changed action if the calling code doesn't independently guard against it.
- Because no business action is implemented yet, the retry/confirmation contract is entirely unvalidated against real integration failure modes.

# Future Work

- Specify Tool Orchestrator's retry count/backoff policy once a real business integration exists.
- Consider a TTL or single-use constraint on `confirmation_required` responses.
- Implement and exercise the first real `ActionSpec` against this contract to validate it under real conditions.

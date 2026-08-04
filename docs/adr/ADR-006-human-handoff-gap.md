# ADR-006: Human Handoff Execution Ownership — Accepted Open Issue

**Date:** 2026-08-04
**Architecture Version:** v1.0 (`architecture-v1.0`, commit `a7d567d`)
**Related Documents:**
- `MODULES.md` §8 (Tool Orchestrator)
- `SEQUENCE_DIAGRAMS.md` — Diagram 5 (Human Handoff Flow), item H1
- `DOMAIN_MODEL.md` — Appendix B (Traceability Matrix)

# Status

**Accepted (Open Issue)** — this decision explicitly does not assign ownership. It records that the gap is known and intentionally unresolved in v1.0, and defers the ownership decision to Architecture v1.1.

# Context

When Safety Engine's clinical or handoff check triggers, Conversation Manager sets `is_handoff: true` (with confidence and evidence) in the turn's response. `SEQUENCE_DIAGRAMS.md` Diagram 5 was drawn to show the full handoff flow end-to-end, and in doing so surfaced a gap that had not been identified by either `ARCHITECTURE_REVIEW.md` or `MODULES_REVIEW.md`: no module in `MODULES.md` v1.0 is responsible for what happens *after* the flag is set. Tool Orchestrator is scoped to business actions (orders, appointments, billing) and does not list a handoff-transfer action. Conversation Manager's documented responsibility stops at producing the flag. The mechanism that actually connects a user to a Human Agent — telephony transfer, ticket creation, live-chat escalation, or something else entirely — has no owner anywhere in the frozen architecture.

# Decision

Architecture v1.0 makes a deliberate scoping decision, not a silent omission: Conversation Manager and Safety Engine are responsible only for *detecting and flagging* a handoff condition. Execution of the handoff is explicitly out of scope for v1.0 and is **not assigned to any existing module**. This ADR records the gap as an accepted, open architectural question and defers the ownership decision to Architecture v1.1. No module, existing or new, is designated here as the eventual owner, and no alternative below is recommended over another.

# Alternatives Considered

The following candidate approaches were identified while drawing `SEQUENCE_DIAGRAMS.md` Diagram 5. They are listed for Architecture v1.1 to evaluate — none is selected, preferred, or ruled out by this ADR:

- **Extend Tool Orchestrator's action catalog** with a handoff-transfer action once business-systems integration exists. This would reuse an already-validated request/response/confirmation contract (§8), but Tool Orchestrator's action catalog is currently scoped to business actions, and no business-systems integration exists yet to extend.
- **Introduce a new, dedicated module** for handoff execution (routing, telephony/ticketing integration, SLA tracking). This would give the concern a clean, single-purpose owner and its own interface, but adding a module is a larger structural change than this ADR is positioned to make — and is explicitly out of scope for the current session's constraints.
- **Treat handoff execution as an out-of-architecture, Client-side/operational concern.** This requires no architecture change and is closest to what happens by default today, but provides no consistency guarantee across deployments or environments, and leaves a capability that may be safety-critical (given the platform's clinical-question handling) outside the platform's documented boundary entirely.

# Consequences

**Positive**
- Architecture v1.0 could be frozen and shipped without blocking on a decision that is likely deployment-specific (telephony vs. ticketing vs. chat escalation varies by who operates the platform).
- The gap is documented and discoverable rather than silently absent — anyone building against v1.0 can see this ADR and `SEQUENCE_DIAGRAMS.md` H1 rather than discovering the omission by trial and error.

**Negative**
- No implementer today has architectural guidance for the actual transfer mechanism; any handoff-execution code written against v1.0 is not governed by any module contract in `MODULES.md`.

**Known Limitations**
- This is, in its entirety, a known limitation: handoff execution ownership is undecided. Nothing in v1.0 resolves it, and this ADR does not either.

# Trade-offs

Shipping the retrieval/safety/generation core now versus delaying the entire v1.0 freeze until handoff execution is fully designed: the decision favored shipping now. The `is_handoff` flag and its accompanying response text already give the user an in-band signal (e.g. "let me connect you with one now") even without a platform-level execution mechanism, which was judged sufficient to not block the freeze — at the cost of leaving the mechanism itself undefined.

# Risks

- Without an assigned owner, divergent and undocumented handoff-execution implementations could emerge across deployments before v1.1 formally decides ownership, with no architectural constraint keeping them consistent with each other.
- If handoff is safety-critical in a given deployment (e.g. a clinical escalation that needs to reach a pharmacist within a bounded time), the absence of any owning module means there is currently no architectural guarantee about execution reliability, latency, or auditability for that step.
- Because `check_clinical` and `check_handoff` share one matching engine (ADR-005), any handoff-execution design in v1.1 will need to account for both trigger paths converging on the same undefined execution step.

# Future Work

Architecture v1.1 must decide among the alternatives listed above — or introduce one not yet considered — and assign explicit ownership for handoff execution. This is the primary open item this ADR hands forward; it is not resolved here.

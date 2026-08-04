# ADR-001: Conversation Manager as the Sole Orchestration Hub

**Date:** 2026-08-04
**Architecture Version:** v1.0 (`architecture-v1.0`, commit `a7d567d`)
**Related Documents:**
- `ARCHITECTURE.md` — Core Principle 1
- `MODULES.md` §2 (Conversation Manager), Dependency Rules 1–3, "Known Implementation Debt"
- `SEQUENCE_DIAGRAMS.md` — Diagram 1 (Chat Request Flow), Diagram 6 (Error Recovery Flow)
- `ARCHITECTURE_REVIEW.md` finding 3.1
- `MODULES_REVIEW.md` findings 1.2, 2.1, 6.1

# Status

Accepted

# Context

A voice/chat platform assembling a response needs to invoke several independent capabilities per turn — intent classification, conversation state, safety checks, retrieval, generation, and (in future) business actions — in a specific order, with a single, consistent policy for what happens when any of them fails. Left undecided, that ordering and fallback logic could live in any of several places: distributed across the modules themselves, duplicated per client, or centralized in one place. `MODULES.md` needed one answer that every other module's interface and dependency direction could be designed against.

# Decision

Conversation Manager is the single orchestration hub for a turn. It is the only module in `MODULES.md` v1.0 permitted to depend on more than one leaf-service module (Dependency Rule 3) — Intent Engine, Memory, Safety Engine, RAG Engine, LLM Service, Tool Orchestrator, and Analytics are all invoked exclusively by Conversation Manager, never by each other or by API Gateway directly. Conversation Manager also owns all cross-module fallback/degradation policy in one place, rather than letting each downstream module decide its own user-facing failure behavior (`MODULES.md` §2).

# Alternatives Considered

- **Distributed choreography** — each module decides when to call the next (e.g. Safety Engine calls RAG Engine directly after clearing a message). Not selected: this would require duplicating fallback/degradation logic per module and would make it impossible to state a single "who calls whom" rule — exactly the ambiguity Dependency Rule 3 exists to prevent.
- **Per-flow-type orchestrators** — a separate orchestrator for chat turns vs. tool-use turns vs. voice turns. Not selected: fragments the single fallback policy `MODULES.md` §2 centralizes, and `SEQUENCE_DIAGRAMS.md` Diagram 2 shows voice is already just a client-side wrapper around the same chat flow, not a distinct orchestration path.
- **Fusing orchestration into the model-serving layer** — this is what today's implementation actually does (`VoiceAssistantInference` in `src/inference/predict.py`), not a rejected alternative but the accepted, documented gap between target and current state. `MODULES.md`'s "Known Implementation Debt" section and `ARCHITECTURE_REVIEW.md` finding 3.1 record this explicitly; it is not treated here as an intentional design choice.

# Consequences

**Positive**
- One place to reason about turn-level failure behavior — every fallback rule in `SEQUENCE_DIAGRAMS.md` Diagram 6 traces back to a single owning module.
- A clean single-hub dependency graph makes Dependency Rule 5 ("no cycles, by construction") provable rather than merely observed.

**Negative**
- Conversation Manager is, by construction, the highest-traffic and most-relied-upon module in the platform — any defect in its orchestration logic affects every turn, regardless of which downstream capability was actually involved.

**Known Limitations**
- As implemented today, Conversation Manager is not a standalone module — it is physically fused with LLM Service, so the orchestration sequence cannot be unit-tested independently of loading the model (`ARCHITECTURE_REVIEW.md` 3.1, High severity; `MODULES.md` "Known Implementation Debt").

# Trade-offs

Centralizing orchestration trades module-level autonomy for system-level predictability: no downstream module can independently decide to skip a step or change fallback behavior, which is the explicit point, but it also means Conversation Manager's own scope necessarily grows as new capabilities (e.g. Tool Orchestrator) come online. `MODULES_REVIEW.md` finding 1.2 examined this scale concern directly and concluded it's the correct SRP call for an orchestrator, not a violation — the trade-off is accepted, not incidental.

# Risks

- No structural enforcement exists for Dependency Rule 3 today — a future change could add a direct leaf-to-leaf call (e.g. Safety Engine calling RAG Engine) without any tooling catching it (`MODULES.md` Dependency Rule 6).
- Conversation Manager's Unit Test Boundary (`MODULES.md` §2 — "testable with all six downstream modules mocked") is currently unreachable in practice because of the implementation fusion noted above; a regression in fallback policy could go undetected until integration testing.
- No formal interface/contract abstraction is specified for what Conversation Manager depends on — `MODULES_REVIEW.md` finding 2.1 notes every dependency is named concretely, not against a stated Protocol/interface contract, which weakens the guarantee that mocking "the six dependencies" is reliable.

# Future Work

- Physically separate Conversation Manager from LLM Service so the orchestration sequence has its own test boundary, independent of model load time.
- Formalize the dependency-inversion contract `MODULES_REVIEW.md` 2.1 recommends (Conversation Manager depends on each module's Public Interfaces section as a stated contract).
- Add the import-linter/CI enforcement `MODULES.md` Dependency Rule 6 names as not-yet-built, so Dependency Rule 3 is structurally guaranteed rather than a code-review convention.

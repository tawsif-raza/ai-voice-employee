# Architecture Decision Records

This directory records **why** specific decisions embodied in the frozen Architecture v1.0 (`architecture-v1.0`, commit `a7d567d`) were made — not how they're implemented. Each ADR cross-references `ARCHITECTURE.md`, `MODULES.md`, `SEQUENCE_DIAGRAMS.md`, and `DOMAIN_MODEL.md` rather than restating them. Nothing in this directory changes architecture, module ownership, or code — ADRs are a historical/rationale record layered on top of the frozen documents.

## Index

| ADR | Title | Status | Decision |
|---|---|---|---|
| [001](ADR-001-conversation-manager.md) | Conversation Manager | Accepted | Conversation Manager is the sole orchestration hub — the only module permitted to depend on more than one leaf-service module |
| [002](ADR-002-rag-architecture.md) | RAG Architecture | Accepted | Retriever, Embedding Model, Vector Index, and Knowledge Base are consolidated into one RAG Engine module; retrieval failures degrade to an empty result, never an exception |
| [003](ADR-003-tool-orchestration.md) | Tool Orchestration | Accepted | Tool Orchestrator is the sole path to business systems; Conversation Manager owns the decision, Tool Orchestrator owns validation/execution/confirmation |
| [004](ADR-004-memory-strategy.md) | Memory Strategy | Accepted | Stateless-by-default platform; target Memory design is sliding-window + running-summary, storage-only — summary generation is routed through Conversation Manager, never called by Memory directly |
| [005](ADR-005-safety-pipeline.md) | Safety Pipeline | Accepted | One shared layered-matching engine, reused via two configurations (clinical, handoff); fails closed on internal error |
| [006](ADR-006-human-handoff-gap.md) | Human Handoff Gap | **Accepted (Open Issue)** | Handoff *detection* is owned by Conversation Manager/Safety Engine; handoff *execution* ownership is explicitly undecided and deferred to Architecture v1.1 |

## Template

Every ADR in this directory follows the same structure:

```
# Title
Date / Architecture Version / Related Documents
# Status
# Context
# Decision
# Alternatives Considered
# Consequences (Positive / Negative / Known Limitations)
# Trade-offs
# Risks
# Future Work
```

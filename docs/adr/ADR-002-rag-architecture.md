# ADR-002: RAG Engine as a Consolidated Retrieval Module

**Date:** 2026-08-04
**Architecture Version:** v1.0 (`architecture-v1.0`, commit `a7d567d`)
**Related Documents:**
- `MODULES.md` §6 (RAG Engine), naming cross-reference table
- `SEQUENCE_DIAGRAMS.md` — Diagram 4 (RAG Retrieval Flow)
- `DOMAIN_MODEL.md` — Group 3 (`RetrievalRequest`, `RetrievalResult`, `KnowledgeDocument`)
- `ARCHITECTURE_REVIEW.md` findings 3.4, 5.3

# Status

Accepted

# Context

`ARCHITECTURE.md` originally described grounding as several related but distinct concerns: a RAG Retriever, an Embedding Model, a Vector Index, and a Knowledge Base. `MODULES.md` needed to decide whether those stay independently addressable modules with their own interfaces and dependency edges, or are treated as one cohesive capability. The decision affects how many modules Conversation Manager has to know about, and how retrieval failures are represented to the rest of the platform.

# Decision

`MODULES.md` §6 consolidates the Retriever, Embedding Model, Vector Index, and Knowledge Base into a single RAG Engine module — the naming cross-reference table maps `ARCHITECTURE.md`'s "RAG Retriever + Knowledge Layer (Embedding Model, Vector Index, Knowledge Base)" onto one canonical name. RAG Engine exposes exactly two operations (`retrieve`, `rebuild_index`), depends only on Shared/Core and the Knowledge Base content it reads, and has no dependency on any other platform module. Retrieval failures are represented as an empty result set, never an exception — Conversation Manager treats "no grounding available" as a degraded-but-valid state.

# Alternatives Considered

- **Independent Embedding Model / Vector Index / Knowledge Base modules** — matching `ARCHITECTURE.md`'s more granular framing literally. Not selected: these three always change and fail together in practice (an index rebuild inherently touches all three), and splitting them would add dependency edges and interface surface without a corresponding independent-failure-mode benefit.
- **RAG Engine depending on Intent Engine or Safety Engine directly** (e.g. to skip retrieval for clinical questions itself). Not selected: `MODULES.md` §6 keeps RAG Engine dependency-free of every other leaf-service module — that decision belongs to Conversation Manager, which already knows the classified intent before deciding whether to invoke RAG Engine at all (§2 Responsibilities).
- **Raising an exception on retrieval failure** instead of returning an empty set. Not selected: this would force every caller to handle a RAG-specific error path; returning an empty result lets Conversation Manager's existing "proceed ungrounded, flag as degraded" fallback handle it uniformly with every other downstream failure (`SEQUENCE_DIAGRAMS.md` Diagram 6).

# Consequences

**Positive**
- Conversation Manager has one retrieval dependency to reason about, not three.
- Retrieval failure handling is uniform with the rest of the platform's fail-safe posture — no special-cased exception type.

**Negative**
- The consolidation means a partial failure inside RAG Engine (e.g. the embedding step succeeding but the index write failing) isn't independently observable from outside RAG Engine — the module's internal boundaries aren't visible at the platform level.

**Known Limitations**
- `IndexBuildReport` (the return type of `rebuild_index()`) has no field definition in any frozen document — `SEQUENCE_DIAGRAMS.md` item M4 and `DOMAIN_MODEL.md` Appendix B both record this as open.

# Trade-offs

Consolidating into one module trades fine-grained observability for a smaller, simpler dependency surface. `ARCHITECTURE_REVIEW.md` finding 3.4 flagged that Knowledge Base/Vector Index freshness coupling isn't separately risk-assessed — that finding is a direct consequence of this consolidation decision, accepted here rather than resolved by splitting the module back apart.

# Risks

- A stale index (per §6's self-heal-before-serving behavior) could still serve outdated facts during the window before staleness is detected and the rebuild completes — the exact size of that window isn't bounded anywhere in `MODULES.md`.
- Knowledge base content integrity and access control for the medicine domain specifically remain unaddressed (`ARCHITECTURE_REVIEW.md` 5.3) — a higher-stakes failure mode than a stale FAQ answer, given the platform's clinical-question handling.
- The operational trigger for a manual (non-automatic) `rebuild_index()` call is unspecified (`SEQUENCE_DIAGRAMS.md` item M4) — there is no documented process for who initiates a rebuild outside the automatic staleness check.

# Future Work

- Define `IndexBuildReport`'s field shape.
- Specify the operational/manual trigger path for `rebuild_index()`.
- Assess and document the Knowledge Base/Vector Index freshness coupling risk `ARCHITECTURE_REVIEW.md` 3.4 raised.
- Define an access-control/review process for medicine-domain knowledge content specifically (`ARCHITECTURE_REVIEW.md` 5.3).

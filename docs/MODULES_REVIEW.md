# Modules Review — docs/MODULES.md

**Reviewed:** `docs/MODULES.md` (all 12 module entries, both diagrams, Open Questions), cross-checked against the current codebase (`src/api/server.py`, `src/inference/predict.py`, `src/inference/handoff_detector.py`, `src/rag/retriever.py`, `src/rag/knowledge_base.py`, `src/voice/client_tts.py`, `src/eval/evaluate.py`, `src/eval/generate_benchmark.py`) and against `docs/ARCHITECTURE_REVIEW.md`.

**Status:** Review only. No changes have been made to `MODULES.md` or any other file.

**Scope requested:** Single Responsibility Principle, Dependency Inversion, Circular imports, Shared utilities, Missing interfaces, Overloaded modules.

---

## 1. Single Responsibility Principle

| # | Finding | Where | Severity |
|---|---|---|---|
| 1.1 | **Voice Pipeline bundles two independent concerns with different providers, different failure modes, and different implementation status.** Speech-to-text and text-to-speech are opposite-direction, independently-failing capabilities (§10 Error Handling even lists two unrelated failure modes: "transcription failure" vs. "synthesis failure"), yet they're one module with one status line. Confirmed against code: today only the TTS half exists (`src/voice/client_tts.py`), STT doesn't exist at all — so the module's own status ("partially implemented") is really "one of its two responsibilities is 0% done." Consider splitting into a Voice Input (STT) and Voice Output (TTS) module, or explicitly justify the merge (e.g. "both are thin adapters around a client-side audio loop, not worth separating") rather than leaving it implicit. | §10, lines 375–402 | Medium |
| 1.2 | **Conversation Manager's responsibility list is coherent as "orchestration," but that's a large single responsibility by construction** — decide call order for six other modules *and* own all cross-module fallback/degradation policy (line 160). This is explicitly intentional (the doc cites `ARCHITECTURE.md` Core Principle 1 as justification) and is the correct SRP call for an orchestrator — flagging only because it's the one module where "single responsibility" and "large scope" coexist, and the document should keep making that distinction explicit as the module grows. Not a defect. | §2, lines 147–177 | Low |
| 1.3 | **Configuration and Shared Utilities are correctly scoped** — both explicitly state "no business logic of its own" (§12, line 441) and describe per-utility/per-section responsibility rather than a grab-bag. This is the right SRP treatment for cross-cutting modules and is worth no action, noted here only as a positive check. | §11, §12 | — (no issue) |

---

## 2. Dependency Inversion

| # | Finding | Where | Severity |
|---|---|---|---|
| 2.1 | **No module's "Dependencies" line names an abstraction — every dependency is a concrete, specifically-named module.** Conversation Manager's Unit Test Boundary claims it's "testable with all six downstream modules mocked" (line 174), but nothing in the document formalizes *what* a mock must satisfy — the per-module "Public Interfaces" sections imply a contract, but it's never named as one (no stated Protocol/ABC/typed-interface convention). Without that, "mock the six dependencies" is only as reliable as each mock happening to match today's concrete method signatures. Recommend the document state explicitly that Conversation Manager depends on each module's *Public Interfaces* section as a formal contract, not on the concrete class. | §2, line 174; interface sections throughout | Medium |
| 2.2 | **LLM Service is the strongest DIP example in the document** — §7 explicitly forbids it from calling upward/sideways into RAG Engine, Safety Engine, or Tool Orchestrator (line 309), so the high-level orchestrator depends on LLM Service's narrow `generate()` interface, never the reverse. Worth using as the template when tightening 2.1 for the other modules. | §7, line 309 | — (no issue) |
| 2.3 | **RAG Engine and Safety Engine are both stated to depend only on Configuration (+ Shared Utilities for Safety Engine)** — genuinely leaf-ward, consistent with DIP (details depend on configuration/abstraction, not on the higher-level modules that call them). Confirmed against code: `Retriever` and `HandoffDetector` take constructor parameters and read no other module directly. Good. | §5 line 254, §6 line 281 | — (no issue) |

---

## 3. Circular imports

| # | Finding | Where | Severity |
|---|---|---|---|
| 3.1 | **The Master Dependency Diagram is acyclic and internally consistent with every module's prose "Dependencies" line** — cross-checked all 12 nodes' diagram edges (lines 46–75) against each module's own Dependencies paragraph; they match exactly, with no contradictions (unlike the naming drift `ARCHITECTURE_REVIEW.md` found elsewhere in this doc set, §6). The diagram also correctly avoids `ARCHITECTURE_REVIEW.md` finding 2.1's mistake (drawing a response/return edge as if it were a second dependency edge) by explicitly calling that out in its legend (line 78). This is a genuine improvement over `ARCHITECTURE.md`'s diagram. | Master diagram, lines 22–80 | — (no issue) |
| 3.2 | **The acyclic property is a documentation-level promise, not a structurally enforced one, and the document doesn't say so.** The actual codebase has no package boundaries (no `__init__.py` anywhere) — every existing cross-module import is done by manually inserting a sibling directory onto `sys.path` at runtime: `src/inference/predict.py` inserts `src/rag` to import `retriever` (predict.py:153–156); `src/api/server.py` inserts `src/inference` to import `predict` (server.py:24–25); `src/eval/evaluate.py` and `src/eval/generate_benchmark.py` each independently repeat the same pattern to reach `src/inference` and `src/rag`. Nothing in this mechanism prevents a future edit to, say, `src/rag/retriever.py` from importing back out of `src/inference/` and creating a real circular import — Python's import system enforces nothing here since there are no packages, only ad hoc `sys.path` mutation per file. Recommend a note (perhaps under Configuration or as a new cross-cutting note) that the dependency graph's acyclic guarantee currently depends entirely on convention/code review, with no linter or CI import-graph check backing it. | Master diagram vs. actual `sys.path` pattern in `predict.py`, `server.py`, `evaluate.py`, `generate_benchmark.py` | Medium |
| 3.3 | **The Request-Path vs. Off-Path diagram omits Voice Pipeline entirely.** Of the 12 modules the document defines, 11 appear in the second diagram's three subgraphs (RequestPath, OffPath, Leaf) — Voice Pipeline (§10) is not placed in any of them. Since this diagram's stated purpose is to show which modules a live turn blocks on vs. which are best-effort (line 114), and Voice Pipeline is neither "must complete before a response is returned" in the same sense as the internal modules (it's the external client making the request) nor "off the request path" like Analytics, its absence leaves genuinely ambiguous which failure-domain guarantee applies to it. Worth either adding a fourth category ("external client") or an explicit note on why it's excluded. | Second diagram, lines 84–112 (vs. §10 module list) | Low |

---

## 4. Shared utilities

| # | Finding | Where | Severity |
|---|---|---|---|
| 4.1 | **Configuration-loading duplication is understated.** §12 (line 453) says a config-loading pattern is "duplicated across more than one of today's modules" — that phrasing undersells it. Confirmed by grep: at least **five** independent config-loading implementations exist today with no shared loader — `src/eval/evaluate.py`, `src/inference/predict.py`, `src/inference/handoff_detector.py`, `src/export/merge_and_convert.py`, and `src/training/config.py`. Recommend quantifying this in the document; it strengthens the case for prioritizing Configuration's consolidation (§11, currently only "partially implemented as a pattern"). | §12, line 453 (undercount vs. actual code) | Medium |
| 4.2 | **Text normalization's forward dependency isn't cross-referenced.** §12 correctly identifies that normalization logic today lives privately inside Safety Engine's matching code (confirmed: `normalize()` in `src/inference/handoff_detector.py:48–53`), and that it's a Shared Utilities candidate. But §3 (Intent Engine, not yet built) already lists "Shared Utilities (text normalization)" as a hard dependency (line 196) — i.e., Intent Engine cannot be built without this extraction happening first, or it will duplicate the same logic a third time (Safety Engine already has it twice, once per clinical/handoff configuration per finding 3.2 in `ARCHITECTURE_REVIEW.md`). Worth an explicit forward-pointer between §3 and §12 stating this is a build-order dependency, not just a shared-code opportunity. | §3 line 196 vs. §12 line 453 | Low |
| 4.3 | **Small typed result records are duplicated at the serialization level, not just the field-shape level.** §12 names "common data types/contracts... (e.g. a consistent Turn/Chunk/ActionResult shape)" as in scope (line 439) — correct — but doesn't flag that three near-identical dataclasses already exist independently today, each hand-rolling its own `to_dict()`: `RetrievedChunk` (`src/rag/retriever.py`), `Chunk` (`src/rag/knowledge_base.py`), and `HandoffMatch` (`src/inference/handoff_detector.py`). Worth broadening the Shared Utilities scope statement to include a shared serialization convention (e.g. a common base dataclass or `to_dict` helper), not just shared field shapes. | §12, line 439 | Low |

---

## 5. Missing interfaces

| # | Finding | Where | Severity |
|---|---|---|---|
| 5.1 | **The Conversation Manager → Tool Orchestrator translation layer has no named owner.** §8 states Tool Orchestrator must "never accept raw LLM output as an executable instruction" (line 327), but no interface anywhere describes how a raw LLM response or an Intent Engine classification becomes a concrete `invoke(action, params, session_id)` call — that mapping logic isn't assigned to any module's responsibilities list. This is already flagged honestly as an Open Question (line 461), but it's also a genuine interface gap worth listing under Tool Orchestrator or Conversation Manager's own sections, not just the Open Questions list. | §8 line 327 vs. Open Questions line 461 | Medium |
| 5.2 | **Memory's Public Interfaces section doesn't expose the truncation/summarization responsibility the same section's Responsibilities list promises.** Line 214 names truncation/summarization as a future responsibility directly addressing `ARCHITECTURE_REVIEW.md` finding 4.1 (an explicit High-severity, unaddressed-cost-scaling finding) — but `get_history`/`append_turn` (lines 216–218) have no parameter or method for it (e.g. no max-turns/max-tokens argument, no `truncate_history` call). Since this module exists specifically to close a High-severity gap, its interface should show where that responsibility plugs in, even as an unimplemented stub signature. | §4, lines 214 vs. 216–218 | Medium |
| 5.3 | **Configuration's own interface is the least specified relative to how many modules depend on it.** Every other module's Public Interfaces section gives full method signatures; Configuration gives one generic `get(section) -> dict` plus "typed accessors per module (e.g. `get_safety_config()`, `get_rag_config()`)" (line 417) — only 2 of the 7 modules that list Configuration as a dependency get a named accessor. Intent Engine, Memory, LLM Service, Tool Orchestrator, and Analytics's config accessors are left to "e.g." Given Configuration is the single most-depended-on module in the graph (7 of 11 other modules), its interface deserves the same rigor as, say, Safety Engine's two fully-typed methods. | §11, line 417 | Low |
| 5.4 | **`TurnEvent` and `BenchmarkCase`, Analytics's inputs, are never given a field shape anywhere in the document**, unlike every other cross-module payload (`SafetyMatch`, `IntentResult`, `TurnResult`, `RetrievedChunk`, `ActionResult` are all inlined with fields in their owning module's Outputs section). Since Analytics is described as consuming events from up to six upstream modules (line 365), `TurnEvent`'s shape is the de facto contract all of them must agree on — it should get the same field-level treatment `TurnResult` gets in §2. | §9, lines 358–359 | Low |

---

## 6. Overloaded modules

| # | Finding | Where | Severity |
|---|---|---|---|
| 6.1 | **Confirmed against code: `VoiceAssistantInference` (`src/inference/predict.py`, 525 lines) is simultaneously today's LLM Service, Conversation Manager (turn orchestration + prompt assembly), Safety Engine caller (pre-generation clinical guard *and* post-generation handoff check), and RAG Engine caller (retrieval + context injection) — one class, one file.** This is the single most load-bearing honesty claim in the document, and it checks out exactly: `__init__` loads the model (LLM Service), instantiates `HandoffDetector` twice for two different configs (§5's shared-engine coupling, confirmed), and instantiates `Retriever` (§6); `generate_response_stream` runs the clinical pre-check, then retrieval, then prompt assembly, then generation, then the post-check handoff check — i.e., the entire Conversation Manager turn sequence described in §2's Responsibilities list (lines 152–158), inline. The document already names this plainly at §2 line 174 and §7 line 313 rather than hiding it — no correction needed, this is called out here only to confirm it's accurate, not aspirational. | §2 line 174, §7 line 313; confirmed in `src/inference/predict.py` | — (confirmed accurate, no action needed) |
| 6.2 | **The "Unit Test Boundary" sections for Safety Engine and RAG Engine read as if the whole module is already cleanly separable, when only their leaf logic is.** Confirmed: `HandoffDetector` (`src/inference/handoff_detector.py`) and `Retriever` (`src/rag/retriever.py`) are genuinely standalone, independently unit-testable classes with no back-reference to `VoiceAssistantInference` — that part of §5/§6's claims is accurate today. But the *decision of when to call them and what to do with the result* — the part that is actually Conversation-Manager-shaped orchestration logic — lives inside the same class as model loading (per 6.1), so that slice specifically cannot be tested without a loaded model, contrary to what a literal reading of §5/§6's "Unit Test Boundary: Fully offline... every rule, pattern, and threshold is testable" (line 258) and "Testable against a small fixture knowledge base... independent of the LLM Service or Conversation Manager" (line 285) would suggest to a reader who hasn't also read §2's caveat. Recommend §5 and §6 each add a one-line pointer to §2's finding-3.1 caveat, the way §2 and §7 already do for each other. | §5 line 258, §6 line 285 vs. §2 line 174 | Low |

---

## Summary

| Category | High | Medium | Low | Total |
|---|---|---|---|---|
| Single Responsibility Principle | 0 | 1 | 1 | 2 |
| Dependency Inversion | 0 | 1 | 0 | 1 |
| Circular imports | 0 | 1 | 1 | 2 |
| Shared utilities | 0 | 1 | 2 | 3 |
| Missing interfaces | 0 | 2 | 2 | 4 |
| Overloaded modules | 0 | 0 | 1 | 1 |

**No High-severity findings** — this reflects that `MODULES.md` is already unusually disciplined about stating current-vs-target status honestly (the "Status" line on every module) and cross-referencing its own known gaps via `ARCHITECTURE_REVIEW.md`. The findings above are refinements to an already-solid document, not corrections of misrepresented state.

**Highest-priority items** (recommend addressing first, pending your approval):
- **3.2** — the acyclic dependency graph is a convention, not an enforced constraint; today's `sys.path`-hack import pattern (repeated independently in 4 files) has no structural guard against a future circular import.
- **4.1** — five independent config-loading implementations exist today, not "more than one" — worth quantifying to prioritize Configuration's consolidation.
- **5.1 / 5.2** — the Tool Orchestrator dispatch contract and Memory's truncation interface are both gaps between "we've identified this problem" (Open Questions, `ARCHITECTURE_REVIEW.md` 4.1) and "here's the interface it plugs into."
- **2.1** — formalize "Conversation Manager depends on each module's Public Interfaces section" as the stated contract, so the "mock the six dependencies" test boundary claim is enforceable rather than aspirational.

No changes have been made. Let me know which of the above you'd like applied, and I'll update `docs/MODULES.md` accordingly.

# Repository Structure

**Date:** 2026-08-04
**Architecture Version:** v1.0 (`architecture-v1.0`, commit `a7d567d`)

This document maps the repository's actual directory layout to `MODULES.md` v1.0's 12 modules. It does not restate `ARCHITECTURE.md`, `MODULES.md`, `SEQUENCE_DIAGRAMS.md`, `DOMAIN_MODEL.md`, `API_SPEC.md`, or `docs/adr/ADR-001` through `ADR-006` — every claim below is a pointer into one of those frozen documents. No directory is created, renamed, or moved by this document; no module is added, renamed, or re-owned.

## 1. Introduction

This document exists to answer one question precisely: for each of `MODULES.md`'s 12 modules, where does its code actually live today, and where would it live if fully built out? Two layouts appear side by side throughout:

- **Target layout** — what the module boundaries in `MODULES.md` imply, if every module became its own directory.
- **Current layout** — what `src/` actually contains, verified directly against the filesystem, not inferred.

This mirrors how `MODULES.md` itself distinguishes target module boundaries from the "Known Implementation Debt" of today's fused `VoiceAssistantInference`. Nothing here invents new target structure beyond what `MODULES.md` already authorizes — a "target directory" is just "where this already-defined module's code would live," never a proposal for a new module.

## 2. Repository Design Principles

- **One directory per implemented module**, where a module exists as its own code. Where it doesn't yet exist as its own module (fused, or not yet built), this document says so rather than implying a directory that isn't there.
- **A module's directory never imports from a module above it in the layering** — the repository-level expression of Dependency Rule 2.
- **Shared/Core code is importable by everything and imports nothing back** — the repository-level expression of Dependency Rules 1 and 4.
- **This document is descriptive of current layout, and only prescriptive within already-authorized target boundaries.** It never proposes a directory for something `MODULES.md` doesn't already define as a module.

## 3. Top-Level Directory Layout

| Directory | Purpose | Scope |
|---|---|---|
| `src/` | All module implementation code | Cross-cutting — contains multiple modules; see Section 4 |
| `docs/` | Architecture, design, and decision records | Cross-cutting; see Section 11 |
| `configs/` | Structured configuration files | Maps to Shared/Core → Config Service target (§12a); see Section 9 |
| `data/` | Training data and knowledge base content | Mixed — `data/knowledge` belongs to RAG Engine (§6); `data/raw`, `data/processed`, `data/cleaned`, `data/custom` belong to the offline training pipeline, which is not a `MODULES.md` module (see note below) |
| `models/` | Trained model artifacts (base + LoRA checkpoints) | LLM Service (§7) — "Load the base model and fine-tuning adapter" |
| `notebooks/` | Exploratory data-analysis scripts | Pre-architecture tooling, not module-mapped |
| `outputs/` | Generated artifacts: eval reports, RAG index, training logs | Mixed — `outputs/evaluation` belongs to Analytics (§9); `outputs/rag_index` belongs to RAG Engine (§6); `outputs/training.log`, `outputs/dry-run` belong to the training pipeline |
| `scripts/` | Operational entry-point shell scripts | Cross-cutting — each script invokes one or more modules (`run_eval.sh` → Analytics, `build_rag_index.sh` → RAG Engine, `chat.sh`/`run_tts_client.sh` → API Gateway/Voice TTS clients) |
| `tests/` | Unit tests | Mapped per module Unit Test Boundary; see Section 10 |
| `docker/` | Deployment packaging (`Dockerfile`, `docker-compose.yml`) | Cross-cutting; see Section 12 |

**Note on the training pipeline:** `src/training/`, `src/data/`, `notebooks/`, and most of `data/`/`outputs/` implement the offline process that *produces* the checkpoint LLM Service loads — they are not themselves one of `MODULES.md`'s 12 runtime modules. `ARCHITECTURE_REVIEW.md` finding 1.5 already notes the Training Pipeline is documented in `ARCHITECTURE.md` §5 but absent from its §4 logical diagram, for the same reason: it's a build-time input to LLM Service, not a request-path module.

## 4. Source Tree Layout

**Update (post-M2):** the extraction milestone `docs/IMPLEMENTATION_ROADMAP.md` M2 describes (Conversation Manager / LLM Service decoupling) has been implemented. The table below reflects that; it is the one row-set in this document expected to change as milestones land, per Section 1's stated purpose.

| Module (`MODULES.md` §) | Target directory | Current directory | Status |
|---|---|---|---|
| API Gateway (§1) | `src/api_gateway/` | `src/api/` (`server.py`) | Matches — implemented, now calls Conversation Manager directly (see §5 below) |
| Conversation Manager (§2) | `src/conversation_manager/` | `src/agent/conversation_manager.py` | Extracted — standalone module, no longer fused with LLM Service (M2 complete) |
| Intent Engine (§3) | `src/intent/` | *(none)* | Not yet implemented |
| Memory (§4) | `src/memory/` | `src/memory/` — **exists, empty** | Scaffolded, not yet implemented |
| Safety Engine (§5) | `src/safety/` | `src/inference/handoff_detector.py` | Implemented, housed under `inference/` rather than a dedicated directory — unchanged by M2 |
| RAG Engine (§6) | `src/rag/` | `src/rag/` | Matches — implemented, unchanged by M2 |
| LLM Service (§7) | `src/llm_service/` | `src/inference/llm_service.py` | Extracted — standalone module, no orchestration logic remains (M2 complete) |
| Tool Orchestrator (§8) | `src/tool_orchestrator/` | *(none)* | Not yet implemented |
| Analytics (§9) | `src/analytics/` | `src/eval/` | Implemented (offline benchmark path only, per §9 Status), housed under `eval/` |
| Voice STT (§10) | `src/voice_stt/` | *(none)* | Not yet implemented |
| Voice TTS (§11) | `src/voice_tts/` | `src/voice/` (`client_tts.py`) | Implemented, housed under `voice/` |
| Shared/Core (§12) | `src/shared/` | *(none — duplicated across 5 files instead)* | Not yet implemented; see Section 8. M2 did not reduce this — `conversation_manager.py` and `llm_service.py` each still load `configs/config.yaml` independently, matching the existing pattern rather than consolidating it (out of scope for M2; scoped to M1) |
| *(backward-compat facade)* | — | `src/inference/predict.py` — `VoiceAssistantInference` | No longer the orchestration boundary; now a thin wrapper delegating to `build_conversation_manager()` for callers that predate the extraction (`src/eval/evaluate.py`, the CLI) |

## 5. Module Ownership

Which code symbol currently implements each module's `MODULES.md`-documented Public Interface:

| Module | Public Interface (`MODULES.md`) | Implementing Symbol | File |
|---|---|---|---|
| API Gateway | `GET /health`, `POST /generate` | `health()`, `generate()` — calls `ConversationManager.handle_turn()` directly | `src/api/server.py` |
| Conversation Manager | `turn(...)` | `ConversationManager.handle_turn()` (streaming; a blocking `turn()`-style wrapper is provided by the `VoiceAssistantInference` facade's `generate_response()` for pre-M2 callers) | `src/agent/conversation_manager.py` |
| Intent Engine | `classify(...)` | *(not implemented)* | — |
| Memory | `append`/`retrieve`/`summarize`/`truncate`/`clear` | *(not implemented)* | — |
| Safety Engine | `check_clinical(...)`, `check_handoff(...)` | `HandoffDetector.score()` — two separately-configured instances, one per check (§5 internal note); instances are now constructed once by `conversation_manager.build_conversation_manager()` and injected into `ConversationManager`, not constructed inline in the inference class | `src/inference/handoff_detector.py` |
| RAG Engine | `retrieve(...)`, `rebuild_index()` | `Retriever.retrieve()`, `Retriever.build()` — now injected into `ConversationManager` rather than constructed inline | `src/rag/retriever.py` |
| LLM Service | `generate(...)`, `generate_stream(...)` | `LLMService.generate_stream()` — standalone, no orchestration logic (extracted from `VoiceAssistantInference` by M2) | `src/inference/llm_service.py` |
| Tool Orchestrator | `list_actions()`, `invoke(...)` | *(not implemented)* | — |
| Analytics | `record_turn(...)`, `run_benchmark(...)` | Offline harness only — no live `record_turn()` | `src/eval/evaluate.py`, `src/eval/generate_benchmark.py` |
| Voice STT | `transcribe(...)` | *(not implemented)* | — |
| Voice TTS | `synthesize(...)`, `synthesize_stream(...)` | Inline in the client's streaming loop, not extracted as a standalone function | `src/voice/client_tts.py` |
| Shared/Core | `get(...)`, typed accessors | *(not implemented — 5 separate ad hoc loaders instead)* | `src/eval/evaluate.py`, `src/inference/predict.py`, `src/inference/handoff_detector.py`, `src/export/merge_and_convert.py`, `src/training/config.py` |

## 6. Dependency Rules

How `MODULES.md`'s Dependency Rules 1–6 map onto the actual `src/` import structure today:

- **Rule 1 (Layering)** — not expressed structurally; there are no packages (`__init__.py`) to encode layers. The layering exists only as documentation.
- **Rule 2 (Allowed directions)** — honored in practice, verified by inspection: `src/inference/predict.py` imports from `src/rag/` (downward), `src/api/server.py` imports from `src/inference/` (downward), `src/eval/` imports from both (downward). No upward or sideways import exists today.
- **Rule 3 (Single hub)** — now enforceable in practice: `ConversationManager` is a standalone module (`src/agent/conversation_manager.py`) constructed with `LLMService`, `Retriever`, and two `HandoffDetector` instances injected, and it is the only caller of any of them for a given turn. `src/api/server.py` and `src/inference/predict.py` both call `ConversationManager`/`build_conversation_manager()`, never the leaf modules directly.
- **Rule 4 (Shared/Core is the only common dependency)** — violated in spirit: there is no Shared/Core location, so the five duplicated config-loading implementations (Section 8) are the direct counter-example this rule was written to prevent.
- **Rule 5 (No cycles)** — holds today, verified by inspection (no back-edges found among `src/api`, `src/inference`, `src/rag`, `src/eval`).
- **Rule 6 (Enforcement gap)** — already self-documented in `MODULES.md`: cross-module imports use runtime `sys.path.insert(...)` per file, not a package structure, so nothing prevents a future edit from violating Rules 1–5 except code review.

### 6.1 Dependency Enforcement

This subsection documents *intended* enforcement mechanisms for Dependency Rule 6's gap. **These are documentation only** — no tool is installed, no CI file is added, and no test is written by this document. They are deferred to the implementation phase, listed here so that phase has a concrete starting point rather than an open-ended problem.

| Mechanism | What it would do | Status |
|---|---|---|
| **Architecture tests** | A test suite (e.g. under `tests/`) asserting the import graph's shape directly — "module X's source contains no `import` of module Y" — codifying Dependency Rules 1–5 as executable assertions | Not implemented; documented here as a future test category |
| **Static import analysis** | A tool (e.g. `import-linter`, `pydeps`, or a custom AST-based checker) that parses `src/` and fails on a forbidden import edge, run locally or in CI | Not implemented; requires package structure (`__init__.py`) to be fully effective, which doesn't exist today |
| **CI validation** | A pipeline step running the above two on every change | Not implemented — no CI configuration exists in this repository today (no `.github/workflows` or equivalent found) |
| **Code review** | Human reviewers checking new imports against Dependency Rules 1–5 | **The only mechanism in effect today** — matches `MODULES.md` Dependency Rule 6's own statement |

Architecture v1.1 is the natural place to decide which of these to build, and in what order — this document takes no position on that beyond listing the options, consistent with not resolving accepted open items.

## 7. Public vs Internal Interfaces

A sample of each implemented module's public surface vs. its internal helpers — illustrative, not exhaustive:

| Module | Public (per `MODULES.md`) | Internal helpers (examples) |
|---|---|---|
| Safety Engine | `HandoffDetector.score()`, `.detect()` | `normalize()`, `_match_exact()`, `_match_regex()`, `_match_synonyms()`, `_match_semantic()` |
| RAG Engine | `Retriever.retrieve()`, `.build()` | `_load_or_build()`, `_load()`, `_index_paths()` |
| API Gateway | `health()`, `generate()` (FastAPI route handlers) | `lifespan()` (startup/shutdown), `ndjson()` (streaming generator) |
| Conversation Manager | `ConversationManager.handle_turn()`, `build_conversation_manager()` | `_normalize_history()`, `_final()`, `_load_rag_config()` |
| LLM Service | `LLMService.generate_stream()` | `_resolve_adapter_path()`, `_latest_numbered_checkpoint()` |
| *(backward-compat facade)* | `VoiceAssistantInference.generate_response()`, `.generate_response_stream()`, `.detect_handoff()` | Delegates to the above two; no orchestration logic of its own (see §4's note on `src/inference/predict.py`) |

Modules with no implementation (Intent Engine, Memory, Tool Orchestrator, Voice STT, Shared/Core) have no public/internal distinction to document yet.

## 8. Shared Libraries

Maps to Shared/Core (§12). **Current reality:** no shared library location exists. The three concerns Shared/Core is meant to consolidate are each duplicated instead:

- **Config loading** (§12a) — five independent implementations (`MODULES_REVIEW.md` finding 4.1): `src/eval/evaluate.py`, `src/inference/predict.py`, `src/inference/handoff_detector.py`, `src/export/merge_and_convert.py`, `src/training/config.py`.
- **Text normalization** (§12b) — lives privately inside `src/inference/handoff_detector.py`'s `normalize()`, not shared (`MODULES_REVIEW.md` finding 4.2).
- **Serialization convention** (§12b) — `RetrievedChunk`, `Chunk`, and `HandoffMatch` each hand-roll their own `to_dict()` (`MODULES_REVIEW.md` finding 4.3).

**Target:** a single shared location (`src/shared/`, per Section 4) hosting Config Service and Shared Utilities once extracted. This document does not create that directory — it documents where the extraction target would go, consistent with `MODULES.md` §12's own "not yet implemented" status for both sub-components.

## 9. Configuration Structure

| File | Read by (today) | Target owner |
|---|---|---|
| `configs/config.yaml` (`rag:` section) | `src/inference/predict.py` | Config Service `get_rag_config()` (§12a) |
| `configs/clinical_triggers.yaml` | `src/inference/predict.py`, via a separately-configured `HandoffDetector` instance | Config Service `get_safety_config()` (§12a) |
| `configs/handoff_phrases.yaml` | `src/inference/handoff_detector.py` (default config path) | Config Service `get_safety_config()` (§12a) |

All three are read directly by the module that needs them today, rather than through a shared loader — the file-level instance of the Section 8 gap.

## 10. Test Structure

| Test file | Exercises | `MODULES.md` Unit Test Boundary | Match? |
|---|---|---|---|
| `tests/test_handoff_detector.py` | Safety Engine's handoff check | §5: "Fully offline... every rule, pattern, and threshold is testable against fixed input strings" | Matches |
| `tests/test_clinical_guard.py` | Safety Engine's clinical check | §5, same boundary (shared engine, §5 internal note) | Matches |
| `tests/test_retriever.py` | RAG Engine | §6: "Testable against a small fixture knowledge base" | Matches |
| `tests/test_conversation_manager.py` | Conversation Manager's orchestration sequence, with `LLMService`/`Retriever` faked and the real `HandoffDetector` | §2: "testable with all downstream dependencies mocked... without loading a real model" | Matches — this boundary did not exist before M2 (`ARCHITECTURE_REVIEW.md` finding 3.1) |
| `tests/test_predict_facade.py` | `VoiceAssistantInference`'s backward-compatible public interface (signatures, class attributes, delegation to `ConversationManager`), without instantiating it | Not a `MODULES.md` module boundary — a compatibility contract for the pre-M2 facade | New |
| `tests/test_server_api.py` | API Gateway's `/health` and `/generate` routes end-to-end, with a fake `LLMService` injected via a real `ConversationManager` | §1: "Testable with the Conversation Manager stubbed" | Matches — this boundary also did not exist before M2 |

**Gaps, noted honestly rather than silently:**
- No tests exist for Memory, Intent Engine, or Tool Orchestrator — consistent with their "not yet implemented" status.
- Analytics' offline-benchmark boundary (§9: "testable end-to-end against fixed benchmark cases") is exercised by running `src/eval/evaluate.py`/`generate_benchmark.py` directly, not via a `tests/` file — a different testing tier (evaluation harness) than unit tests, not a gap in the same sense as the others above.
- `LLMService` itself (model loading, real generation) has no test file — it requires torch/transformers and a real or downloaded checkpoint, unavailable in this environment; only its orchestration-facing contract is exercised, via fakes, in `test_conversation_manager.py` and `test_server_api.py`.

## 11. Documentation Structure

| Path | Contents | Category |
|---|---|---|
| `docs/ARCHITECTURE.md` | System-level architecture | Frozen source of truth |
| `docs/MODULES.md` | Module-level design (12 modules, Dependency Rules) | Frozen source of truth |
| `docs/SEQUENCE_DIAGRAMS.md` | Turn-level Mermaid sequence diagrams | Frozen source of truth |
| `docs/DOMAIN_MODEL.md` | Shared domain entities and contracts | Frozen source of truth |
| `docs/API_SPEC.md` | HTTP-level API contract | Frozen source of truth |
| `docs/adr/` (`README.md` + `ADR-001`–`ADR-006`) | Decision rationale for the above | Frozen source of truth |
| `docs/ARCHITECTURE_REVIEW.md` | Point-in-time review of `ARCHITECTURE.md` | Review artifact, not itself a source of truth |
| `docs/MODULES_REVIEW.md` | Point-in-time review of `MODULES.md` | Review artifact, not itself a source of truth |
| `docs/DATA_FLOW.md`, `docs/DATABASE.md`, `docs/DECISIONS.md`, `docs/ROADMAP.md` | Pre-existing reference documents | Not modified by this session's architecture work |
| `docs/REPOSITORY_STRUCTURE.md` | This document | Repository-to-module mapping |

## 12. Build & Deployment Files

| File | Purpose |
|---|---|
| `docker/Dockerfile` | Container image build |
| `docker/docker-compose.yml` | Local multi-service orchestration |
| `requirements.txt` (repo root) | Python dependencies — plain pip, no `pyproject.toml`/Poetry packaging |

**Gap, noted honestly:** no CI configuration exists in this repository (no `.github/workflows` or equivalent found). This is the same gap Section 6.1's "CI validation" row describes — there is currently nowhere for that mechanism to run even if written.

## 13. Naming Conventions

- **Files and directories:** `snake_case` throughout.
- **No `__init__.py` anywhere** — there is no formal Python package structure; cross-module imports use runtime `sys.path.insert(...)` (Dependency Rule 6).
- **Directory names loosely match module concepts but aren't a strict slug of `MODULES.md`'s canonical names** — e.g. `inference/` (not `conversation_manager/`/`llm_service/`), `eval/` (not `analytics/`), `voice/` (not `voice_tts/`). Documented as the current pattern; Section 4's "Target directory" column shows what a canonical-name alignment would look like, without performing any rename here.
- **Config files:** `<topic>.yaml` under `configs/`.
- **Test files:** `test_<subject>.py` under `tests/`, named after what's tested, not a strict per-module mapping.

## 14. Future Repository Extensions

Where directories for not-yet-implemented modules would go once built — a placement note, not a creation. No directory listed here is created by this document:

- `src/intent/` — Intent Engine (§3)
- `src/memory/` — Memory (§4); **already scaffolded and empty**, needs content only
- `src/tool_orchestrator/` — Tool Orchestrator (§8)
- `src/voice_stt/` — Voice STT (§10)
- `src/shared/` — Shared/Core (§12), per Section 8

**`src/agent/`** — no longer empty: now hosts `conversation_manager.py` (Conversation Manager, §2). This resolves the "maps to no `MODULES.md` module" note this section previously carried; `ADR-001`'s Future Work item ("physically separate Conversation Manager from LLM Service") is complete. LLM Service (§7) now lives at `src/inference/llm_service.py`, alongside (not fused with) the pre-existing `src/inference/handoff_detector.py` and the backward-compatible `src/inference/predict.py` facade — `src/inference/` was not split into a separate directory for LLM Service, since `MODULES.md` Section 4's target directory for it (`src/llm_service/`) is aspirational, not required, and keeping it in `inference/` alongside its own tests/callers was lower-risk for this extraction.

## 15. Appendix – Repository Ownership Matrix

| Module (§) | Directory/File(s) | Status | Related ADR |
|---|---|---|---|
| API Gateway (§1) | `src/api/server.py` | Implemented — calls Conversation Manager directly | — |
| Conversation Manager (§2) | `src/agent/conversation_manager.py` | Extracted — standalone (M2 complete) | ADR-001 |
| Intent Engine (§3) | *(none)* | Not yet implemented | — |
| Memory (§4) | `src/memory/` (empty) | Scaffolded, not yet implemented | ADR-004 |
| Safety Engine (§5) | `src/inference/handoff_detector.py` | Implemented | ADR-005 |
| RAG Engine (§6) | `src/rag/` | Implemented | ADR-002 |
| LLM Service (§7) | `src/inference/llm_service.py` | Extracted — standalone (M2 complete) | ADR-001 |
| Tool Orchestrator (§8) | *(none)* | Not yet implemented | ADR-003 |
| Analytics (§9) | `src/eval/` | Implemented (offline only) | — |
| Voice STT (§10) | *(none)* | Not yet implemented | — |
| Voice TTS (§11) | `src/voice/client_tts.py` | Implemented | — |
| Shared/Core (§12) | *(none — 5 duplicated loaders)* | Not yet implemented | — |
| *(backward-compat facade)* | `src/inference/predict.py` — `VoiceAssistantInference` | Thin wrapper over Conversation Manager, for pre-M2 callers | ADR-001 |

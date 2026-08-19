# Implementation Roadmap

**Status:** Milestone list frozen (Section 4: M1–M9)
**Baseline:** Architecture v1.0 (`architecture-v1.0`, commit `a7d567d`) — `ARCHITECTURE.md`, `MODULES.md`, `DOMAIN_MODEL.md`, `SEQUENCE_DIAGRAMS.md`, `docs/adr/`
**Date:** 2026-08-05

This document is a documentation-only implementation plan. It does not change, refactor, or implement any code, and it does not modify the frozen Architecture v1.0. Every milestone below describes work that fits inside the module boundaries, interfaces, and dependency rules already frozen in `MODULES.md` — no milestone adds, renames, or removes a module. Any issue identified while drafting this roadmap that would require an architecture change is documented in Section 8 (Deferred Work – Architecture v1.1), not resolved here.

## 1. Purpose

To sequence the work needed to close the gaps between today's implementation and the target module boundaries already defined in `MODULES.md` v1.0 — including modules not yet built (Intent Engine, Memory, Tool Orchestrator, STT Service, Shared/Core), modules that exist but not at their target boundary (`VoiceAssistantInference`'s fused Conversation Manager/LLM Service logic), and specific findings raised in `ARCHITECTURE_REVIEW.md` and `MODULES_REVIEW.md` that are addressable without an architecture change.

## 2. Relationship to Frozen Architecture v1.0

Every milestone in Section 4 is scoped to be implementable entirely within the module definitions, public interfaces, dependency rules, and communication rules already frozen in `ARCHITECTURE.md` and `MODULES.md`. Where a milestone's `Out of Scope` excludes something, that exclusion is either (a) genuinely separate follow-up work within the same frozen boundaries, or (b) an item requiring an architecture change, in which case it is cross-referenced to Section 8 rather than silently dropped. This roadmap does not reopen, reinterpret, or extend any ADR in `docs/adr/` — it only sequences work the frozen decisions already describe as target state.

## 3. Sequencing / Dependency Overview

Milestone order follows the dependency chain each milestone's own `Dependencies` field declares, consistent with `MODULES.md`'s layering (entry → orchestrated → leaf-service → foundation) and Dependency Rule 3 (single hub).

```
M1 (Shared Utilities Extraction)
 ├─→ M4 (STT Integration)
 ├─→ M5 (TTS Hardening)
 ├─→ M6 (Intent Engine) ─────────┐
 └─→ M9 (Prompt-Injection)       │
                                 │
M2 (CM / Model Layer Decoupling) │
 ├─→ M3 (Memory) ────────┐       │
 ├─→ M7 (Tool Orchestrator) ←────┘
 └─→ M9 (Prompt-Injection, call-site move)
                          │
M3 + M1 ─────────────────┴─→ M8 (Analytics)
```

M1 and M2 have no dependencies on each other and may proceed in parallel; both are prerequisites for the majority of downstream milestones, so scheduling them first minimizes rework.

## 4. Milestones

### M1 — Shared Utilities Extraction

**Milestone Name:** Shared Utilities Extraction (Config Service + Shared Utilities)

**Objective:** Extract the configuration-loading and text-normalization/serialization logic currently duplicated across five modules into the frozen Shared/Core module (`MODULES.md` §12), giving every other milestone a single foundation dependency per Dependency Rule 4.

**Scope:**
- Implement Config Service (§12a): single loader, startup validation, safe-default fallback, typed per-module accessors (`get_safety_config()`, `get_rag_config()`, `get_intent_config()`, `get_memory_config()`, `get_llm_config()`, `get_tool_orchestrator_config()`, `get_analytics_config()`, `get_stt_config()`, `get_tts_config()`)
- Implement Shared Utilities (§12b): text-normalization helper (extracted from Safety Engine's private `normalize()`), a shared serialization convention for `RetrievedChunk`, `Chunk`, and `HandoffMatch`-style records
- Migrate the five existing independent config-loading implementations (`src/eval/evaluate.py`, `src/inference/predict.py`, `src/inference/handoff_detector.py`, `src/export/merge_and_convert.py`, `src/training/config.py`) to call Config Service

**Out of Scope:**
- A remote/external config-service backend (§12a names this as future work, not this milestone)
- Runtime hot-reload policy for safety-critical config sections — an implementation decision resolvable within Config Service's existing interface, not an architecture change (MODULES.md Open Questions)
- Any new module or change to which modules may depend on Shared/Core (already fixed by Dependency Rules 1–5)

**Architecture References:** `MODULES.md` §12, §12a, §12b, Dependency Rule 4; `MODULES_REVIEW.md` findings 4.1, 4.2, 4.3, 5.3.

**Dependencies:** None.

**Deliverables:**
- Config Service module implementing `get(section)` plus the nine typed accessors per §12a
- Shared Utilities module implementing text normalization and the shared serialization convention per §12b
- Five existing modules migrated to call Config Service instead of loading their own config
- Safety Engine's existing `normalize()` logic relocated to Shared Utilities with no behavior change

**Acceptance Criteria:**
- All nine typed accessors listed in §12a exist and return validated, typed objects
- No module outside Shared/Core reads its own YAML/JSON config file directly (§12a)
- Safety Engine's fail-startup-not-silent exception for its own rule sets is preserved exactly (§12a Error Handling)
- Text-normalization output is identical to today's Safety Engine `normalize()` output on existing test fixtures

**Required Tests:**
- Unit tests for Config Service against fixture configuration files/dicts, including missing/malformed-config fallback behavior (§12a Unit Test Boundary)
- Unit tests for each Shared Utilities helper in isolation (§12b Unit Test Boundary)
- Regression test proving Safety Engine's existing clinical/handoff detection test suite passes unchanged after normalization is relocated
- Regression test proving the five migrated modules produce identical config values pre/post migration

**Exit Criteria:**
- Zero remaining independent config-loading implementations outside Shared/Core
- Safety Engine's existing test suite passes unmodified
- Milestones M4, M5, M6, and M9 are unblocked

**Risks:**
- Relocating `normalize()` risks a subtle behavior change given its shared-fate coupling with the handoff detector (`ARCHITECTURE_REVIEW.md` finding 3.2) — mitigated by the regression test above, not by redesigning the function
- Migrating five modules at once increases the chance of a config-key mismatch going unnoticed until runtime

**Estimated Complexity:** Medium

---

### M2 — Conversation Manager / Model Layer Decoupling

**Milestone Name:** Conversation Manager / Model Layer Decoupling

**Objective:** Extract the turn-orchestration sequence currently fused inside `VoiceAssistantInference` into a standalone Conversation Manager, so it matches its already-frozen module boundary (`MODULES.md` §2) and becomes testable without a loaded model.

**Scope:**
- Separate the four responsibilities `VoiceAssistantInference` currently performs (per `MODULES.md`'s "Known Implementation Debt" table): call-order decision logic (§2 Conversation Manager) from model loading/generation (§7 LLM Service), from the pre/post-generation safety checks (§5 Safety Engine, already standalone), and from retrieval (§6 RAG Engine, already standalone)
- Implement Conversation Manager's `turn()` interface and streaming variant per §2 Public Interfaces
- Preserve existing fallback/degradation behavior exactly: clinical-guard short-circuit, RAG-unavailable degraded-grounding, LLM-failure fixed apology-and-handoff (§2 Error Handling)

**Out of Scope:**
- Wiring Intent Engine, Memory, Tool Orchestrator, or Analytics into the call sequence — Conversation Manager's interface may reserve call sites for them, but implementing those calls is scoped to M3, M6, M7, M8
- Changing the order safety/retrieval/generation run in — already fixed by `ARCHITECTURE.md` Communication Rules 3–4

**Architecture References:** `ARCHITECTURE.md` Core Principle 1, Core Principle 7, §10 ("Extract the Conversation Manager into a standalone module"); `MODULES.md` §2, "Known Implementation Debt" section; `ARCHITECTURE_REVIEW.md` finding 3.1 (High).

**Dependencies:** None (M1 is not a hard prerequisite, but completing M1 first reduces rework since Conversation Manager will call Config Service).

**Deliverables:**
- Standalone Conversation Manager module implementing `turn(message, history, session_id) -> TurnResult` and its streaming variant (§2 Public Interfaces)
- LLM Service reduced to exactly the responsibilities in §7 (load model/adapter, generate, generate_stream, report metadata), with no orchestration logic remaining
- `VoiceAssistantInference`'s current call sequence reproduced exactly inside the new Conversation Manager — relocation, not a behavior change

**Acceptance Criteria:**
- Conversation Manager can be exercised with LLM Service, Safety Engine, and RAG Engine all mocked, per §2 Unit Test Boundary
- LLM Service contains no clinical-check, handoff-check, or retrieval logic
- Existing end-to-end behavior (per the Evaluation Harness benchmark) is unchanged before and after the refactor

**Required Tests:**
- New unit test suite for Conversation Manager with all downstream dependencies mocked, verifying orchestration order (e.g. "if Safety Engine flags a clinical trigger, LLM Service.generate is never called," per §2 Unit Test Boundary)
- Full Evaluation Harness benchmark run pre- and post-refactor, diffed for behavioral parity
- Existing Safety Engine and RAG Engine test suites re-run unmodified to confirm no regression from the extraction

**Exit Criteria:**
- `ARCHITECTURE_REVIEW.md` finding 3.1 is closed: Core Principle 7 ("every component is independently testable... without requiring a full model load") holds in practice for Conversation Manager
- Milestones M3 and M7 are unblocked

**Risks:**
- Highest-risk milestone in this roadmap: refactoring the largest existing class risks a behavior regression despite the parity tests
- The two `HandoffDetector` instances (clinical, handoff) must be re-wired to the new Conversation Manager without altering which configuration each uses

**Estimated Complexity:** High

---

### M3 — Memory Module Implementation

**Milestone Name:** Memory Module Implementation

**Objective:** Implement the Memory module's sliding-window + running-summary strategy defined in `MODULES.md` §4 and ADR-004, closing the unbounded conversation-history growth gap.

**Scope:**
- Implement all five Memory public interfaces: `append`, `retrieve`, `summarize`, `truncate`, `clear` (§4)
- Implement storage/composition logic only — the summarization *generation* call is issued by Conversation Manager through LLM Service, never by Memory itself (ADR-004; Dependency Rule 3)
- Wire Conversation Manager to call `summarize()` when the configured trigger (turn count/token budget, via `get_memory_config()`) is reached, falling back to `truncate()` on LLM Service failure

**Out of Scope:**
- Choosing Memory's backing store or retention policy — open question in `MODULES.md` Open Questions and `docs/DATABASE.md`; not required to implement the storage-only interface contract itself
- PII/PHI handling policy for stored conversation content in the medicine domain (`ARCHITECTURE_REVIEW.md` finding 5.4) — a policy decision, not implementation scope
- Any change to `ARCHITECTURE.md` §8's stateless-by-default transport design (unchanged by ADR-004)

**Architecture References:** `MODULES.md` §4; ADR-004; `ARCHITECTURE_REVIEW.md` finding 4.1 (High); Dependency Rule 3.

**Dependencies:** M2 (summarization generation calls are routed through Conversation Manager per ADR-004 — requires Conversation Manager's orchestration boundary to exist as a standalone, callable module first).

**Deliverables:**
- Memory module implementing the five public interfaces per §4
- Conversation Manager wiring: trigger detection → summarization-prompt assembly → `LLM Service.generate()` call → `Memory.summarize()` persistence
- Truncation fallback path wired for LLM Service failure during summarization

**Acceptance Criteria:**
- `retrieve()` returns the running summary (if present) as a synthetic leading turn followed by the verbatim `keep_last_n` window, exactly as specified in §4 Composition
- Memory has zero dependencies on Conversation Manager, LLM Service, or any other module (§4 Dependencies; Dependency Rule 3), verified by a test suite requiring no orchestration-module mocks
- A read/write failure degrades to an empty-history state rather than failing the turn (§4 Error Handling)
- A summarization failure at the Conversation Manager/LLM Service level falls back to `truncate()` rather than failing the turn

**Required Tests:**
- Unit tests for Memory against its own storage contract (in-memory fake or isolated real backing store), per §4 Unit Test Boundary
- Unit tests for `summarize()`'s storage/composition logic with the text-generation call mocked
- Integration test verifying the summarization trigger fires at the configured threshold and produces a bounded-size `retrieve()` result
- Integration test verifying the truncate-fallback path when LLM Service generation fails during summarization

**Exit Criteria:**
- `ARCHITECTURE_REVIEW.md` finding 4.1 (High) is closed: per-turn cost/latency no longer grows unbounded with conversation length
- Milestone M8 is unblocked

**Risks:**
- No concrete default exists yet for the summarization trigger threshold (ADR-004 Risks) — must be chosen with no prior production data
- Summarization adds an extra LLM Service call to the turn's critical path with no documented latency budget (ADR-004 Consequences)
- Backing store/retention policy remains undecided (see Out of Scope); this milestone must use a placeholder storage backend that doesn't presuppose the eventual answer

**Estimated Complexity:** Medium

---

### M4 — Speech-to-Text (STT) Integration

**Milestone Name:** Speech-to-Text (STT) Integration

**Objective:** Implement the STT Service module defined in `MODULES.md` §10, closing `ARCHITECTURE_REVIEW.md` finding 1.1 (no STT component exists today).

**Scope:**
- Implement `transcribe(audio: AudioStream) -> TranscriptionResult { text, confidence }` per §10 Public Interfaces
- Integrate a configured STT provider as a swappable external dependency, selected via `get_stt_config()`
- Client-side sequencing (capture → transcribe → send text to API Gateway) remains the Client/Voice App's responsibility, unchanged by this milestone (§10 intro note)

**Out of Scope:**
- Any change to API Gateway or Conversation Manager to accommodate audio input — STT Service does not call API Gateway itself (§10 Responsibilities), so no other module's interface changes
- Multi-language support beyond what the chosen provider supports natively

**Architecture References:** `MODULES.md` §10; `ARCHITECTURE_REVIEW.md` finding 1.1 (High).

**Dependencies:** M1 (requires the `get_stt_config()` typed accessor from Config Service).

**Deliverables:**
- STT Service module implementing `transcribe()` per §10
- Provider integration with credentials referenced (not hardcoded) via Config Service
- Client/Voice App updated to call `transcribe()` before sending text to API Gateway — client-side sequencing only, no server-side module changes

**Acceptance Criteria:**
- `transcribe()` returns a typed low-confidence result rather than raising on unintelligible audio or provider error (§10 Error Handling)
- STT Service has no dependency on API Gateway, Conversation Manager, or any other module besides Shared/Core and the external provider (§10 Dependencies)
- Confidence/quality metadata is reported when the provider supports it (§10 Responsibilities)

**Required Tests:**
- Unit tests against recorded audio fixtures with the provider mocked, per §10 Unit Test Boundary
- Failure-path tests: unintelligible audio, provider timeout/error, confirming typed low-confidence/error results rather than unhandled exceptions

**Exit Criteria:**
- `ARCHITECTURE_REVIEW.md` finding 1.1 is closed for the input half: spoken input can become text without a manual transcription step
- STT Service's `MODULES.md` §10 Status line is eligible for a future documentation update (not a deliverable of this milestone)

**Risks:**
- Provider selection introduces an external dependency with latency/availability characteristics not yet measured against the platform's CPU-bound latency budget (`ARCHITECTURE.md` §8)
- Transcription-confidence thresholds for prompting the user to repeat are not defined anywhere in the frozen documents and must be chosen during this milestone

**Estimated Complexity:** Medium

---

### M5 — Text-to-Speech Hardening / Voice Output Completion

**Milestone Name:** Text-to-Speech Hardening / Voice Output Completion

**Objective:** Extract a standalone `synthesize()`/`synthesize_stream()` boundary from today's combined `client_tts.py` script, formalizing the TTS Service module per `MODULES.md` §11 and closing the remaining gap between today's code and its target boundary.

**Scope:**
- Extract `synthesize(text) -> AudioStream` and `synthesize_stream(text_chunks) -> Iterator[AudioChunk]` from `src/voice/client_tts.py`'s combined API-calling/sequencing/synthesis logic, per §11 Public Interfaces
- Preserve today's ElevenLabs-backed, sentence-chunked streaming synthesis behavior exactly — an extraction, not a provider or behavior change
- Leave API-calling/sequencing logic in the Client, per §11 Purpose

**Out of Scope:**
- Changing the TTS provider or voice configuration
- Any change to sentence-chunking logic itself (`SentenceChunker`), only its packaging boundary

**Architecture References:** `MODULES.md` §11; `ARCHITECTURE_REVIEW.md` finding 1.2 (High, closed on the output side by this milestone).

**Dependencies:** M1 (requires the `get_tts_config()` typed accessor from Config Service).

**Deliverables:**
- Standalone TTS Service module implementing `synthesize()` and `synthesize_stream()` per §11
- `src/voice/client_tts.py` reduced to client-side sequencing only, calling the new TTS Service for synthesis

**Acceptance Criteria:**
- TTS Service has no dependency on API Gateway, Conversation Manager, or any other module besides Shared/Core and the external provider (§11 Dependencies)
- A synthesis failure falls back to text-only display rather than failing the interaction (§11 Error Handling)
- Existing sentence-chunked streaming playback behavior is unchanged, verified by comparison pre/post extraction

**Required Tests:**
- Unit tests against fixed text inputs with the provider mocked, per §11 Unit Test Boundary
- Regression test comparing provider call sequence/audio output before and after extraction

**Exit Criteria:**
- `ARCHITECTURE_REVIEW.md` finding 1.2 is closed: TTS Service exists as a named, bounded component rather than folded into a generic client script
- TTS Service's `MODULES.md` §11 Status line is eligible for a future documentation update reflecting the completed extraction (not a deliverable of this milestone)

**Risks:**
- Lowest-risk milestone in this roadmap — TTS already works today; the risk is in preserving exact behavior during extraction, not in building new capability

**Estimated Complexity:** Low

---

### M6 — Intent Engine Implementation

**Milestone Name:** Intent Engine Implementation

**Objective:** Build the Intent Engine module defined in `MODULES.md` §3, giving Conversation Manager an explicit classification step instead of intent-shaped decisions being made implicitly by the Safety Engine's clinical check and the LLM's own in-band handoff behavior.

**Scope:**
- Implement `classify(message, history) -> IntentResult { intent, confidence }` per §3 Public Interfaces
- Define the fixed, versioned intent taxonomy (e.g. `knowledge_lookup`, `appointment_action`, `escalation_request`, `clinical_question`, `out_of_domain`, `chit_chat` — §3 Responsibilities)
- Wire Conversation Manager to call Intent Engine and use its output to decide which downstream modules a turn invokes

**Out of Scope:**
- Deciding whether classification is rule-based or model-based, and if model-based, whether it shares LLM Service or uses a separate classifier — an open implementation decision (`MODULES.md` Open Questions) resolvable within §3's unchanged interface contract, not an architecture change
- Any retrieval or generation dependency — Intent Engine must classify before RAG Engine or LLM Service have run (§3 Dependencies)

**Architecture References:** `MODULES.md` §3; `MODULES.md` Open Questions (classification approach).

**Dependencies:** M1 (hard dependency on Shared Utilities' text normalization, per `MODULES.md` line 196).

**Deliverables:**
- Intent Engine module implementing `classify()` per §3
- Fixed, versioned intent taxonomy definition, loaded via Config Service (`get_intent_config()`)
- Conversation Manager updated to call Intent Engine as the first step of a turn and route downstream calls based on its output

**Acceptance Criteria:**
- Unclassifiable input defaults to the broadest, safest fallback intent rather than raising (§3 Error Handling)
- Intent Engine has no dependency on RAG Engine, LLM Service, or Safety Engine (§3 Dependencies)
- Classification runs against a fixed labeled example set with a documented accuracy baseline

**Required Tests:**
- Fully offline unit tests against a fixed table of (utterance → expected intent) pairs, per §3 Unit Test Boundary
- Conversation Manager integration test verifying downstream module selection changes correctly based on classified intent, with Intent Engine's output mocked at known values

**Exit Criteria:**
- Intent Engine's `MODULES.md` §3 Status line is eligible for a future documentation update from "not yet implemented" (not a deliverable of this milestone)
- Milestone M7 is unblocked

**Risks:**
- The rule-based-vs-model-based open question has no prior decision to build on; choosing wrong increases rework risk for M7, which also consumes Intent Engine's output
- No labeled evaluation data set for the taxonomy currently exists in the frozen documents; one must be created as part of this milestone

**Estimated Complexity:** Medium

---

### M7 — Tool Orchestrator Dispatch Contract

**Milestone Name:** Tool Orchestrator Dispatch Contract

**Objective:** Implement the Tool Orchestrator module and the Conversation-Manager-owned LLM-output-to-action translation layer defined in `MODULES.md` §8 and ADR-003, closing the previously undocumented dispatch gap (`MODULES_REVIEW.md` finding 5.1).

**Scope:**
- Implement `list_actions() -> list[ActionSpec]` and `invoke(request: ActionRequest) -> ActionResult` per §8 Public Interfaces
- Implement the `ActionRequest`/`ActionResult` contracts exactly as specified in §8, including `requires_confirmation` gating and the `confirmation_required` status
- Wire Conversation Manager to map Intent Engine's classification plus the LLM's generated response (as signal, never as direct instruction) to a specific `ActionSpec` and typed `params` — never passing raw LLM output to `invoke()` (§8 Ownership; ADR-003 Decision)

**Out of Scope:**
- Any real business-API integration (orders, appointments, billing) — `MODULES.md` §8 Status confirms no business actions exist yet; this milestone builds and validates the contract, not a live integration
- Specifying Tool Orchestrator's retry count/backoff policy bound, and any TTL/single-use constraint on `confirmation_required` responses — both are ADR-003 Future Work items requiring a real integration to validate against

**Architecture References:** `ARCHITECTURE.md` Core Principle 2, Communication Rule 7; `MODULES.md` §8; ADR-003; `MODULES_REVIEW.md` finding 5.1.

**Dependencies:** M2 (Conversation Manager must exist as a standalone module before it can own the decision layer), M6 (dispatch decision uses Intent Engine's classification as signal).

**Deliverables:**
- Tool Orchestrator module implementing `list_actions()` and `invoke()` per §8
- At least one mock `ActionSpec` with `requires_confirmation: false` and one with `requires_confirmation: true`, sufficient to exercise both code paths
- Conversation Manager's decision layer implemented: intent + LLM response → `ActionRequest`, never raw text → `invoke()`

**Acceptance Criteria:**
- `invoke()` rejects a `requires_confirmation` action invoked with `confirmed=False`, returning `confirmation_required` rather than executing (§8 Response contract)
- Tool Orchestrator has no dependency on LLM Service, RAG Engine, Safety Engine, or Intent Engine (§8 Dependencies)
- Business-API failures return a structured `ActionResult{status: "failure", error: ...}` rather than raising uncaught (§8 Error Handling)
- No code path passes raw LLM-generated text directly into `invoke()`'s `action` or `params` fields

**Required Tests:**
- Unit tests against a mocked business-API layer, verifying action validation, confirmation gating, and result normalization, per §8 Unit Test Boundary
- Conversation Manager integration test verifying the decision layer correctly maps a given (intent, LLM response) pair to the expected `ActionRequest`, using the mock `ActionSpec`s
- Negative test confirming an unconfirmed `requires_confirmation` action never reaches the mocked business API

**Exit Criteria:**
- `MODULES_REVIEW.md` finding 5.1 is closed: the Conversation-Manager-to-Tool-Orchestrator translation layer has a named owner and a tested contract
- Tool Orchestrator's `MODULES.md` §8 Status line is eligible for a future documentation update reflecting the contract's implementation (real business actions remain future work)

**Risks:**
- Because no real business integration exists yet, the retry/confirmation contract is validated only against mocks — ADR-003 already flags this as unvalidated against real failure modes, and this milestone does not resolve that
- The intent-plus-LLM-response-to-action mapping logic has no prior art beyond the interface contract; the specific mapping rules must be designed during this milestone

**Estimated Complexity:** High

---

### M8 — Analytics Module + TurnEvent Contract

**Milestone Name:** Analytics Module + TurnEvent Contract

**Objective:** Implement the Analytics module's live event-recording path and define the `TurnEvent`/`BenchmarkCase` field shapes, per `MODULES.md` §9, closing `MODULES_REVIEW.md` finding 5.4.

**Scope:**
- Implement `record_turn(event: TurnEvent) -> None` per §9 Public Interfaces
- Define the `TurnEvent` field shape with the same rigor `TurnResult` gets in §2 (per `MODULES_REVIEW.md` finding 5.4): intent, retrieval hits, safety decisions, handoff decisions, latency
- Wire Conversation Manager to emit a `TurnEvent` at the end of each turn, off the request path (non-blocking)
- Preserve the existing offline `run_benchmark(cases: list[BenchmarkCase]) -> BenchmarkReport` path unchanged

**Out of Scope:**
- Live dashboards/metrics export (§9 Responsibilities lists this as future work) — this milestone builds the event-recording path only
- Formally defining `BenchmarkCase`'s field shape — the existing offline evaluation harness already has a working shape in use; formalizing its documentation is separate from this milestone's implementation scope

**Architecture References:** `MODULES.md` §9; `MODULES_REVIEW.md` finding 5.4; Request-Path vs. Off-Path Diagram (`MODULES.md`).

**Dependencies:** M1 (shared serialization convention from Shared Utilities), M3 (Memory is one of Analytics's upstream event sources).

**Deliverables:**
- Analytics module implementing `record_turn()` per §9, using the `TurnEvent` contract
- `TurnEvent` field-shape specification, documented with the same field-level detail as `TurnResult` (§2)
- Conversation Manager wired to emit a `TurnEvent` at the end of every turn, confirmed non-blocking

**Acceptance Criteria:**
- A `record_turn()` failure is logged and dropped, never propagated back to affect the user-facing turn (§9 Error Handling)
- Analytics is never on Conversation Manager's blocking critical path, verified by a fault-injection test
- The existing offline benchmark path (`run_benchmark`) continues to pass unchanged

**Required Tests:**
- Unit tests asserting on emitted `TurnEvent`s with the rest of the pipeline mocked, per §9 Unit Test Boundary
- Fault-injection test: Analytics recording fails/throws, turn still completes successfully and returns the expected response to the client
- Full existing offline benchmark suite re-run to confirm no regression

**Exit Criteria:**
- `MODULES_REVIEW.md` finding 5.4 is closed for `TurnEvent`
- Analytics's `MODULES.md` §9 Status line is eligible for a future documentation update from "partially implemented" to reflect the completed live-recording path

**Risks:**
- Because Analytics must never block or fail a turn (§9 Purpose), the non-blocking wiring must be implemented carefully — a naive synchronous call risks silently violating this constraint under load
- `TurnEvent`'s shape is a de facto contract for up to six upstream modules; defining it before M6/M7 are complete risks a later shape revision if a downstream milestone surfaces an unanticipated field

**Estimated Complexity:** Medium

---

### M9 — Prompt-Injection Hardening (Safety Engine)

**Milestone Name:** Prompt-Injection Hardening (Safety Engine)

**Objective:** Add adversarial user-input detection to the existing Safety Engine, closing `ARCHITECTURE_REVIEW.md` finding 5.1 (High) — prompt-injection risk from user input designed to override system instructions or talk the model out of a required escalation.

**Scope:**
- Extend Safety Engine's existing layered-matching engine (normalize → exact phrase → regex → synonym → semantic-similarity, per ADR-005) with a new configuration targeting known prompt-injection patterns in user input, reusing the same shared engine rather than building a separate detector, consistent with ADR-005's Decision
- Apply the new check pre-generation, alongside (not replacing) the existing clinical trigger check

**Out of Scope:**
- Any change to the shared matching engine's fundamental architecture (normalize → exact → regex → synonym → semantic layers) — this milestone adds a third configuration of the existing engine per ADR-005, not a redesign
- Resolving whether fail-closed error handling should distinguish "rule matched" from "engine error" in its evidence field (ADR-005 Future Work) — a separate, smaller follow-up not required to close finding 5.1

**Architecture References:** `MODULES.md` §5; ADR-005; `ARCHITECTURE_REVIEW.md` finding 5.1 (High).

**Dependencies:** M1 (extends the same normalization logic being relocated to Shared Utilities; sequencing after M1 avoids modifying the same code twice), M2 (the pre-generation check's call site moves into the new standalone Conversation Manager).

**Deliverables:**
- New prompt-injection detection configuration for Safety Engine's shared matching engine, reusing the `check_clinical`-style invocation (§5 Public Interfaces) as a third configuration
- Updated Conversation Manager pre-generation sequencing to include the new check, with the same short-circuit behavior as the existing clinical check
- A documented, versioned pattern/rule set for known prompt-injection techniques, loaded via Config Service

**Acceptance Criteria:**
- The new check fails closed on internal error (`triggered: true`), consistent with Safety Engine's existing fail-closed policy (§5 Error Handling; ADR-005 Decision)
- Existing clinical and handoff check behavior is unchanged, verified by re-running both existing test suites unmodified
- A documented, regression-tested set of prompt-injection example inputs is caught by the new check, per `ARCHITECTURE_REVIEW.md` finding 5.1's threat description

**Required Tests:**
- Fully offline unit tests for the new configuration against fixed input strings, per §5 Unit Test Boundary
- Full regression run of the existing clinical and handoff test suites, confirming the shared-engine change does not alter either configuration's behavior (per ADR-005's documented shared-fate risk)
- Fail-closed test: malformed prompt-injection configuration results in `triggered: true`, not silent pass-through

**Exit Criteria:**
- `ARCHITECTURE_REVIEW.md` finding 5.1 (High) is closed
- ADR-005's documented shared-fate risk is explicitly re-verified against this change, not assumed safe

**Risks:**
- ADR-005 already documents that a change intended to fix or extend one check "can silently change the other check's behavior" — this is the largest risk of this milestone, mitigated only by the mandatory full regression run, not eliminated by design
- Prompt-injection pattern sets are inherently incomplete against novel adversarial phrasing; this milestone reduces but does not eliminate the risk category

**Estimated Complexity:** Medium

---

## 5. Cross-Milestone Traceability

| Milestone | Closes | Source |
|---|---|---|
| M1 | Config-loading duplication (5 implementations); text-normalization/serialization duplication | MODULES_REVIEW 4.1, 4.2, 4.3, 5.3 |
| M2 | "Independently testable" principle not true in practice | ARCHITECTURE_REVIEW 3.1 (High) |
| M3 | Unbounded per-turn history growth | ARCHITECTURE_REVIEW 4.1 (High); ADR-004 |
| M4 | No STT component | ARCHITECTURE_REVIEW 1.1 (High) |
| M5 | No standalone TTS component boundary | ARCHITECTURE_REVIEW 1.2 (High) |
| M6 | Intent Engine not yet implemented | MODULES.md §3 Status |
| M7 | CM→Tool Orchestrator translation layer had no named owner | MODULES_REVIEW 5.1; ADR-003 |
| M8 | `TurnEvent` had no defined field shape | MODULES_REVIEW 5.4 |
| M9 | Prompt-injection risk from user input unaddressed | ARCHITECTURE_REVIEW 5.1 (High) |
| — (deferred, Section 8) | Handoff execution ownership undecided | ADR-006 |

## 6. Testing and Evaluation Strategy

Each milestone's Required Tests are scoped to its own module boundary, consistent with the Unit Test Boundary each module already defines in `MODULES.md`. In addition to per-milestone tests:

- The existing offline Evaluation Harness benchmark (`src/eval/evaluate.py`) is re-run after M2, M3, and M9 specifically, since these three milestones touch shared or safety-relevant logic paths where a silent regression is most consequential.
- No milestone introduces a new evaluation mechanism; M8 extends Analytics's existing offline benchmark path rather than replacing it.

## 7. Risk Summary

| Severity driver | Milestones affected |
|---|---|
| Refactor of largest existing code path (regression risk) | M2 |
| Shared-engine, shared-fate coupling (a fix to one check can change another) | M9 (also inherited risk from ADR-005, relevant to any future Safety Engine change) |
| Undecided implementation approach with no prior art | M6 (classification approach), M7 (LLM-output-to-action mapping rules) |
| Contract validated only against mocks, no real integration | M7 |
| Undecided operational policy (storage backend, retention) | M3 |
| External provider dependency (latency/availability not yet measured) | M4 |

No milestone in Section 4 carries a risk that requires resolving an architecture change to mitigate — where a risk touches an open architectural question, it is cross-referenced to Section 8.

## 8. Deferred Work (Architecture v1.1)

### Human Handoff Execution — Deferred

**Status:** Accepted Open Issue (ADR-006). Not assigned a milestone number. Not scheduled under Architecture v1.0.

**Gap:** When Safety Engine's clinical or handoff check triggers, Conversation Manager sets `is_handoff: true` in the turn's response. No module in `MODULES.md` v1.0 owns what happens after that flag is set — Tool Orchestrator's action catalog is scoped to business actions and does not include a handoff-transfer action; Conversation Manager's documented responsibility stops at producing the flag.

**Why this is deferred, not resolved here:** ADR-006 explicitly declines to assign ownership, and lists three candidate approaches (extending Tool Orchestrator's action catalog, introducing a new dedicated module, or treating it as an out-of-architecture client/operational concern) without selecting one. Two of the three candidates require an architecture change (a new module, or extending a module's scope beyond what `MODULES.md` §8 currently defines) — resolving this within the current roadmap would mean silently changing the frozen architecture, which this document is explicitly prohibited from doing.

**Carried forward to Architecture v1.1:** ADR-006's Future Work item stands unchanged — "Architecture v1.1 must decide among the alternatives listed [in ADR-006] — or introduce one not yet considered — and assign explicit ownership for handoff execution." This roadmap does not add to, narrow, or resolve that decision.

**Note:** If any milestone in Section 4 surfaces a further issue that would require an architecture change during implementation, it must be added to this section rather than resolved ad hoc, consistent with the constraint this roadmap operates under.

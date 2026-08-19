# Phase 3 — Policy Engine Report

## 1. Executive Summary

Phase 3 adds a deterministic **Policy Engine** (`src/agent/policy_engine.py`) as a config-driven enforcement boundary sitting between the existing safety/routing components (Clinical Safety Guard, Intent Engine, Handoff Detector) and the rest of `ConversationManager`'s orchestration. It does not reimplement any existing detection logic — it aggregates already-computed results (a `HandoffMatch` from the clinical guard, a `RoutingDecision` from the Intent Engine, a `HandoffMatch` from the post-generation handoff detector) into one uniform, typed `PolicyDecision`, and adds three new policy categories that didn't exist before: **tool permission** (lookup only — no Tool Orchestrator exists yet), **confirmation requirements**, and a **privacy field boundary**. `ConversationManager`'s two existing control-flow decisions (block on clinical trigger; short-circuit on `CLARIFICATION`) now route through `PolicyEngine.evaluate_clinical()`/`evaluate_generation()` instead of checking the underlying result objects directly — behavior is unchanged (proven by the full existing test suite passing unmodified), but there is now one auditable place these decisions pass through.

## 2. Policy Architecture

```
ClinicalSafetyGuard.score()  →  HandoffMatch
                                     │
IntentEngine.classify()      →  RoutingDecision  ──┐
                                     │              │
HandoffDetector.score()      →  HandoffMatch  ──┐  │
 (post-generation)                              │  │
                                                 ▼  ▼
                                         PolicyEngine
                                    ┌────────────┼────────────┬─────────────┬───────────┐
                                    ▼            ▼            ▼             ▼           ▼
                              evaluate_    evaluate_    evaluate_     evaluate_    evaluate_
                              clinical()   generation() handoff()     tool_        confirmation()
                                                                      action()
                                    │            │            │             │           │
                                    └────────────┴─────┬──────┴─────────────┴───────────┘
                                                        ▼
                                                  resolve(decisions)
                                                        │
                                                        ▼
                                                 PolicyDecision
                                          {allowed, policy, rule, action, reason}
```

`evaluate_privacy()` is a fifth, independent category (not part of the per-turn resolve chain above) — a standalone boundary a future logging/persistence component can call.

## 3. Policy Types

| Category | Method | Inputs | Never re-implements |
|---|---|---|---|
| **Clinical** | `evaluate_clinical(clinical_result)` | The clinical guard's `HandoffMatch` | Phrase/regex/synonym matching (stays in `HandoffDetector`) |
| **Generation** | `evaluate_generation(intent_routing)` | The Intent Engine's `RoutingDecision` | Intent classification (stays in `IntentEngine`) |
| **Tool** | `evaluate_tool_action(action_name, params)` | An action name string | Tool execution — no Tool Orchestrator exists; this only answers "would it be permitted," and is explicitly forbidden from executing anything per Phase 3's constraints |
| **Handoff** | `evaluate_handoff(clinical_triggered, intent_routing, post_generation_handoff)` | Booleans/results from the other components | Phrase matching — pure signal aggregation over `configs/policies/handoff.yaml`'s ordered rules |
| **Privacy** | `evaluate_privacy(field_name, operation)` | A field name + operation (`log`/`persist`/`expose_downstream`/`include_in_metadata`/`store_in_session`) | Free-text PII detection — explicitly out of scope; this is a named-field boundary only |
| **Confirmation** | `evaluate_confirmation(action_name, confirmed)` | An action name + a caller-supplied trusted boolean | Nothing — `confirmed` must be sourced from trusted application state by the caller; see Security Boundary below |

**Clinical integration:** `evaluate_clinical()` takes the existing `ClinicalSafetyGuard`'s (`HandoffDetector` configured from `configs/clinical_triggers.yaml`) result as its only input. No clinical rule content was copied into `PolicyEngine` or a second YAML file — `configs/clinical_triggers.yaml` remains the single source of truth for what counts as a clinical trigger.

## 4. Configuration Changes

New directory `configs/policies/`:

| File | Purpose |
|---|---|
| `generation.yaml` | Ordered rules mapping an Intent Engine route/intent to a generation policy outcome (`SAFE_FAQ`, `UNKNOWN_REQUEST` → `CLARIFY`, etc.) |
| `tools.yaml` | Registered-action allowlist (`BOOK_APPOINTMENT`, `CANCEL_APPOINTMENT`, `RESCHEDULE_APPOINTMENT`, `ORDER_LOOKUP`); default `BLOCK` for anything unregistered |
| `confirmation.yaml` | Per-action `requires_confirmation` flags; default `true` (fail-closed) for unconfigured actions |
| `handoff.yaml` | Ordered signal-aggregation rules (clinical listed first — highest precedence) |
| `privacy.yaml` | Restricted-field lists per operation |

**Source of truth for clinical policy:** unchanged — `configs/clinical_triggers.yaml`, read exclusively by `HandoffDetector` (the existing `ClinicalSafetyGuard`). Nothing under `configs/policies/` duplicates or shadows it; `PolicyEngine` only ever receives the guard's already-computed `HandoffMatch`.

## 5. Policy Decision Model

```python
@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    policy: str      # "clinical" | "generation" | "tool" | "handoff" | "privacy" | "confirmation"
    rule: str
    action: str       # ALLOW | BLOCK | HANDOFF | CLARIFY | REQUEST_CONFIRMATION
    reason: str
```

A frozen dataclass, matching the repository's existing convention for domain result types (`HandoffMatch`, `RetrievedChunk`, `IntentResult`, `RoutingDecision` are all frozen dataclasses; Pydantic is used only at the FastAPI request/response boundary in `src/api/server.py`, never for internal domain objects). `to_dict()` produces exactly the shape requested in `plan.md`.

## 6. Policy Precedence

Explicit, documented, code-level ordering — never accidental dict/file order:

```python
PRECEDENCE = ("clinical", "handoff", "confirmation", "tool", "privacy", "generation")
```

`PolicyEngine.resolve(decisions: list[PolicyDecision]) -> PolicyDecision`: among all supplied decisions with `allowed=False`, the one whose `policy` is highest in `PRECEDENCE` wins. If every decision allows, the highest-precedence *allowed* decision is returned. This is unit-tested directly (`TestPolicyPrecedence`): a tool request + a required confirmation + a clinical risk, evaluated together and passed to `resolve()` in a deliberately shuffled order, always returns the clinical decision.

**Conflicting rules within one config file** (e.g. two rules both matching `FAQ`, one `ALLOW` and one `DENY`) resolve via **first-match-wins**, rules evaluated top-to-bottom in the order they appear in the YAML list. This ordering is the explicitly documented model (stated in each config file's header comment and in `policy_engine.py`'s module docstring), not an incidental consequence of dict iteration — `TestConflictingRules.test_reordered_rules_change_which_wins_deterministically` proves the same two contradictory rules produce the opposite winner when their list order is reversed, demonstrating the resolution is genuinely order-driven rather than some other hidden tiebreak.

## 7. ConversationManager Integration

`PolicyEngine` is injected into `ConversationManager` (constructor parameter `policy_engine`, defaulting to a real `PolicyEngine()` — same pattern as `handoff_detector`/`intent_engine`). It is called at two points inside `handle_turn()`:

1. **After the clinical guard scores the message, before deciding whether to short-circuit** — `clinical_policy = self.policy_engine.evaluate_clinical(clinical_match)`; the short-circuit condition changed from `if clinical_match.is_handoff` to `if not clinical_policy.allowed`. Since `evaluate_clinical` is a pure wrapper (`allowed=False` iff `clinical_match.is_handoff`), this is behavior-preserving by construction.
2. **After Intent Engine classifies the turn, before deciding whether to short-circuit to clarification** — `generation_policy = self.policy_engine.evaluate_generation(routing)`; the short-circuit condition changed from `if routing.route == Route.CLARIFICATION` to `if generation_policy.action == Action.CLARIFY`, which `generation.yaml`'s only matching rule makes equivalent.
3. **After the post-generation handoff check**, `evaluate_handoff()` is additionally called and its decision attached to the final result's `policy` field for auditability — this call is observability-only; `is_handoff` in the response still comes directly from `handoff_match`, exactly as before Phase 3, so it cannot change or weaken existing handoff behavior.

This matches `plan.md`'s intended flow (Clinical Safety/Policy → Intent/Routing → RAG → LLM → Handoff/Response Policy → Response) without bypassing `ConversationManager` — `PolicyEngine` has no orchestration authority of its own; it is called by `ConversationManager`, exactly like every other collaborator (`Retriever`, `LLMService`, `HandoffDetector`).

## 8. Security Boundary

> **LLM output is untrusted and cannot authorize or override policy decisions.**

Concretely enforced by the following properties, all covered by `TestLLMCannotOverridePolicy`:

- No `PolicyEngine` method has a parameter that accepts free-text model output. `evaluate_clinical`/`evaluate_handoff` only accept already-typed `HandoffMatch`/`RoutingDecision` objects produced by deterministic code; `evaluate_tool_action`/`evaluate_confirmation`/`evaluate_privacy` only accept plain strings/booleans the caller controls directly. A structural test (`test_evaluate_methods_have_no_parameter_for_raw_model_text`) asserts none of the six public methods' signatures contain a parameter named anything resembling `llm_output`/`model_text`/etc.
- `evaluate_confirmation(action_name, confirmed)`'s `confirmed` boolean is never parsed out of text anywhere in this module — three explicit scenario tests (fake `approved=true`, fake `confirmation_received=true` text, fake `safe=true`) each show the policy decision is unaffected by the presence of model-shaped claims, because those claims are never passed to any evaluate method as anything but inert test data.
- `PolicyDecision` is an immutable frozen dataclass — nothing downstream, including a careless attempt to patch a decision based on model output, can mutate an already-produced decision.

**Important scope note, stated plainly:** this module's guarantee is that *it* never trusts model text. A future caller could still misuse the API by, say, parsing a model response into a Python bool and passing that as `confirmed=` — that misuse is a caller-side bug PolicyEngine cannot prevent from inside itself. Phase 4 (Tool Orchestrator) is where the actual trusted-vs-untrusted plumbing for confirmation/authentication state must be built; Phase 3 establishes the boundary's shape and proves the engine itself never crosses it.

## 9. Tests

- **Tests added:** 45 (`tests/test_policy_engine.py`), plus 2 assertion updates in `tests/test_conversation_manager.py` to account for the new `policy` metadata key.
- **Tests executed:** full repository suite, 174 tests.
- **Passing:** 173.
- **Failing:** 0 caused by Phase 3. 1 pre-existing environment error (`test_retriever.TestRetriever.setUpClass` — `ModuleNotFoundError: No module named 'faiss'`; `faiss-cpu` is not installed in this sandbox, unrelated to any code in this repository and unrelated to Phase 3 — `src/rag/` was not touched).
- **Security tests:** `TestLLMCannotOverridePolicy` (5 tests) — model-claimed approval/confirmation/safety all proven inert; structural signature check.
- **Policy conflict tests:** `TestConflictingRules` (3 tests) — first-match-wins proven order-driven, not accidental.
- **Malformed configuration tests:** `TestMalformedConfiguration` (8 tests) — missing files, rules missing fields, empty/non-string action and field names — all degrade to safe, deterministic defaults, none crash, none silently permit.

## 10. Compatibility

- **Existing behavior preserved:** the full pre-Phase-3 test suite (`test_handoff_detector.py`, `test_clinical_guard.py`, `test_conversation_manager.py`, `test_intent_engine.py`, `test_predict_facade.py`, `test_server_api.py`) passes unmodified (aside from the two key-set assertions updated to include the new `policy` field, which is additive metadata, not a behavior change).
- **API compatibility:** `src/api/server.py`'s `/generate` and `/health` contracts are unchanged — `ChatResponse` is still constructed from named fields, so the new `policy`/`intent` metadata never reaches the HTTP response body.
- **Clinical Safety Guard compatibility:** untouched. `configs/clinical_triggers.yaml` and `HandoffDetector` are unmodified; `PolicyEngine` only wraps their output.
- **RAG compatibility:** untouched — `src/rag/` was not modified in this phase.
- **Handoff Detector compatibility:** untouched — `configs/handoff_phrases.yaml` and the post-generation `handoff_detector.score()` call are unmodified; `PolicyEngine.evaluate_handoff()` only aggregates its result for auditability metadata, never gates `is_handoff`.

## 11. Remaining Technical Debt

- `evaluate_tool_action`'s `params` argument is accepted but unused (no per-parameter rules yet) — reasonable for Phase 3 since no Tool Orchestrator or real action schema exists to validate against.
- `PolicyEngine` constructs its own YAML-loading logic (a sixth independent config loader, alongside the five already documented in `docs/MODULES_REVIEW.md` finding 4.1) — consistent with the existing repository pattern, but does not reduce that pre-existing, already-documented debt.
- `evaluate_privacy()`'s restricted-field lists are hand-curated and static; there is no free-text PII scanning, by design (explicitly out of scope per `plan.md`'s Phase 3 instructions — this is the boundary Phase 6 builds on top of).
- The `resolve()` precedence model only orders *between* policy categories; it does not yet compose multiple *simultaneous* denials from the same category into one merged explanation (not required by any Phase 3 test scenario, but worth noting for a future multi-rule-match use case).

## 12. Recommended Next Phase

Proceed to Phase 4 (Tool Orchestrator) as `plan.md` already specifies next — it is the natural consumer of `evaluate_tool_action()` and `evaluate_confirmation()`, and is where the trusted-authentication/trusted-confirmation plumbing this report's Security Boundary section flagged as a caller responsibility must actually be built.

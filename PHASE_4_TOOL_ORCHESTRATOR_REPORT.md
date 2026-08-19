# Phase 4 — Tool Orchestrator Report

## 1. Executive Summary

Phase 4 adds a controlled **Tool Orchestrator** (`src/agent/tool_orchestrator.py`) — the only application layer permitted to execute a registered business tool — plus a **Tool Registry** (`src/agent/tool_registry.py`), typed **action models** (`src/agent/action_models.py`), and four **safe mock tools** (`src/agent/mock_tools.py`: `BOOK_APPOINTMENT`, `CANCEL_APPOINTMENT`, `RESCHEDULE_APPOINTMENT`, `ORDER_LOOKUP`). It is wired into `ConversationManager`'s existing `TOOL_ORCHESTRATOR` route (previously inert metadata-only, since Phase 2) so appointment/order intents can now actually execute — but only when the LLM has produced no part of the decision to do so: parameters are extracted deterministically from the raw message (an existing record's ID only — never a guessed date, doctor, or time), and every execution passes through Phase 3's `PolicyEngine`, a trusted `AuthContext`, and a trusted `confirmed` flag before anything runs. The LLM is not in this execution loop at all — it is never called for a clean tool-backed turn.

## 2. Architecture

```
LLM / any proposal source
      │  UNTRUSTED ActionProposal  {action, parameters, request_id, session_id, metadata}
      ▼
ToolOrchestrator.validate_proposal()
      │  structural validation only — resolves against ToolRegistry,
      │  checks required/unexpected params and basic types
      ▼
ToolRequest   {action, params, session_id, confirmed=False, request_id}
      │  — the ONLY shape invoke() accepts
      ▼
ToolOrchestrator.invoke(tool_request, auth)
      ├── PolicyEngine.evaluate_tool_action()     — trusted, deterministic (Phase 3)
      ├── AuthContext check                       — trusted, caller-supplied
      ├── PolicyEngine.evaluate_confirmation()    — trusted, deterministic (Phase 3)
      ├── idempotency / duplicate-request check
      ├── timeout-bounded execution of the REGISTERED callable only
      └── ToolExecutionResult  {success, tool, status, result, error, request_id, metadata}
```

`ConversationManager` integration (`src/agent/conversation_manager.py`, step "2.6"): when the Intent Engine's route is `TOOL_ORCHESTRATOR` and a `ToolOrchestrator` is configured, `_handle_tool_action()` maps the classified intent to a registered action name, extracts parameters conservatively, and delegates entirely to the sequence above — never guessing missing information, never invoking the LLM.

## 3. Tool Registry

Four mock tools registered by `mock_tools.build_default_tool_registry()`, action names matching `configs/policies/tools.yaml`/`confirmation.yaml` exactly:

| Action | `requires_confirmation` | `destructive` | Backing store |
|---|---|---|---|
| `BOOK_APPOINTMENT` | No | No | `MockAppointmentStore` (in-memory, predictable `appt_<n>` IDs) |
| `CANCEL_APPOINTMENT` | Yes | Yes | same |
| `RESCHEDULE_APPOINTMENT` | Yes | Yes | same |
| `ORDER_LOOKUP` | No | No | `MockOrderStore` (in-memory, pre-seeded) |

No production system is contacted. Mock tools support two underscore-prefixed test-only simulation hooks (`_simulate_failure`, `_simulate_delay_seconds`) read from `params`, deliberately named to be unmistakable as scaffolding.

## 4. Action Model

`ActionProposal` (new — `docs/DOMAIN_MODEL.md` has no equivalent since Tool Orchestrator didn't exist when it was written): `{action, parameters, request_id, session_id, metadata}` — untrusted by construction; carries no `approved`/`execute` field of any kind. `ToolRequest` reuses `docs/DOMAIN_MODEL.md` Group 4's frozen shape exactly (`action, params, session_id, confirmed, request_id`) — the only structure `invoke()` accepts, produced exclusively by `validate_proposal()`.

## 5. Execution Result

`ToolExecutionResult` extends `docs/DOMAIN_MODEL.md`'s `ToolResponse` additively (permitted by that document's own Global Versioning Convention: "new optional fields may be added without a version bump") with `tool`, `request_id`, `metadata`, and a convenience `success` bool alongside `status` (`success` | `failure` | `confirmation_required` | `policy_denied` | `duplicate` | `timeout`).

## 6. Security Boundaries

- **LLM is untrusted.** `ActionProposal` has no field that could mean "approved." No `PolicyEngine`/`ToolOrchestrator` method accepts free-text model output — confirmed structurally in Phase 3's `test_evaluate_methods_have_no_parameter_for_raw_model_text` and behaviorally here in `TestLLMTrustBoundary`.
- **LLM cannot authorize actions.** `invoke()`'s `auth: AuthContext` parameter defaults to `ANONYMOUS_CONTEXT` (`authenticated=False`) — omitting authentication fails **closed**, not open. A forged `{"user_id": "admin", "role": "administrator"}`-shaped dict is never accepted as `auth`; only a real `AuthContext` instance is (Attack 2).
- **LLM cannot directly execute tools.** `ToolRegistry` is the only path to a callable tool implementation; nothing registers a tool dynamically from model output, and arbitrary names (`execute_python`, `os.system`, ...) are structurally unresolvable (Attack 4).
- **PolicyEngine is authoritative.** Every `invoke()` call evaluates `evaluate_tool_action()` and `evaluate_confirmation()` before execution; a policy-denied action never reaches the tool callable regardless of what any proposal claims (Attacks 1, 1b, 6).
- **Authentication/confirmation come from trusted application state.** `tool_request.confirmed` is set exactly once, by `validate_proposal()`, always to `False`; only a caller re-issuing a `ToolRequest` with an explicit `confirmed=True` (from its own trusted state — session confirmation is Phase 5's job) can clear that gate (Attack 3). `ConversationManager.handle_turn()`'s new `auth`/`confirmed` parameters are documented as caller-trusted-only and are never derived from `user_input`.

## 7. Mock Tools

See Section 3. `MockAppointmentStore`/`MockOrderStore` hold state per-instance (never module-level globals), so tests never leak state across each other.

## 8. Failure Handling

| Condition | `ToolExecutionResult.status` | Notes |
|---|---|---|
| Unknown tool | `failure` (`UNKNOWN_TOOL`) | Also caught earlier by `validate_proposal()` raising `ToolValidationError` |
| Malformed/missing parameters | — | `validate_proposal()` raises `ToolValidationError`, never reaches `invoke()` |
| Policy denial | `policy_denied` | |
| No/failed authentication | `failure` (`AUTHENTICATION_REQUIRED`) | |
| Insufficient role | `failure` (`INSUFFICIENT_PERMISSIONS`) | |
| Confirmation missing | `confirmation_required` | |
| Timeout | `timeout` | Enforced via `concurrent.futures.ThreadPoolExecutor.result(timeout=...)`, per-`ActionSpec.timeout_seconds` |
| Tool exception (domain error) | `failure` (verbatim `ValueError`/`KeyError` message — safe, authored by this repo) | |
| Tool exception (other) | `failure` (`TOOL_EXECUTION_FAILED`, generalized) | Never leaks internal exception text, matching `ConversationManager`'s existing discipline |
| Malformed tool response (non-dict) | `failure` (`MALFORMED_TOOL_RESULT`) | |
| Duplicate `request_id` | `duplicate` | In-memory idempotency tracking |
| Destructive operation failure | — | **Never automatically retried**, proven by `test_destructive_operation_is_never_automatically_retried` (call count stays at 1) |

## 9. Tests

- **Tests added:** 42 (`tests/test_tool_orchestrator.py`) + 8 new `ConversationManager` integration tests (`TestToolOrchestratorIntegration` in `tests/test_conversation_manager.py`) = 50.
- **Tests executed:** full repository suite, 223 tests.
- **Passing:** 222.
- **Failing:** 0 caused by Phase 4. 1 pre-existing environment error (`test_retriever.py` — `faiss` not installed in this sandbox, unrelated to `src/agent/`).
- **Security regression results (Step 4.11, all 6 mandatory attack scenarios):** all pass — fake approval, fake authentication, fake confirmation, arbitrary tool name, arbitrary/unexpected parameter (URL-shaped), and a "policy override" instruction string are each proven to have zero effect on the outcome.
- **Integration test results:** existing FAQ→RAG→LLM and clinical→handoff flows independently confirmed unaffected by a `ToolOrchestrator` being configured; new tool-backed flows (order lookup succeeds without an LLM call, missing-info booking asks for specifics without guessing, unconfirmed cancellation blocks, confirmed cancellation succeeds) all pass.

## 10. Compatibility

- **ConversationManager:** `tool_orchestrator` is an additive, optional constructor parameter defaulting to `None` — any `ConversationManager` constructed without it (all of Phase 1–3's existing tests) behaves exactly as before: `TOOL_ORCHESTRATOR`-routed turns fall through to normal generation, metadata only, nothing executes.
- **PolicyEngine:** unmodified — `evaluate_tool_action()`/`evaluate_confirmation()` (Phase 3) are consumed as-is, no new policy categories or config schema changes were needed.
- **Clinical Safety Guard:** untouched; `ToolOrchestrator.invoke()` deliberately does not call `evaluate_clinical()` at all (verified structurally by `test_clinical_policy_overrides_tool_request`) — clinical safety is `ConversationManager`'s job, upstream of any tool proposal ever forming.
- **Intent Engine:** unmodified — a new `_INTENT_TO_TOOL_ACTION` mapping lives entirely in `ConversationManager`, not in `IntentEngine` itself.
- **RAG:** untouched.
- **Existing API (`src/api/server.py`):** unchanged this phase — `handle_turn()`'s new `auth`/`confirmed` parameters default to safe values (`None`→`ANONYMOUS_CONTEXT`, `False`), so the HTTP contract is unaffected; wiring real trusted auth through the API is Phase 7's job.
- **Existing tests:** all pass unmodified except two `test_conversation_manager.py` key-set assertions updated to include the new additive `tool` metadata field (same pattern as Phase 3's `policy` field).

## 11. Remaining Technical Debt

- **No real NLU/slot-filling exists.** `BOOK_APPOINTMENT` almost always requires clarification in practice, since doctor/date/time are never inferred from free text — an honest capability gap, not a bug, but worth flagging for whoever picks up multi-turn slot collection.
- **No real authentication system exists yet** (`docs/ARCHITECTURE.md` §9's long-documented gap) — `ConversationManager.handle_turn()`'s `auth` parameter is plumbing with no real identity provider behind it until Phase 7. Every tool action is therefore unauthenticated-by-default (fails closed) until a caller explicitly supplies a trusted `AuthContext`.
- **No real session-backed confirmation exists yet** (Phase 5) — `confirmed` is a bare boolean parameter today; a real deployment needs `WAITING_FOR_CONFIRMATION` session state so a later turn's "yes" can be tied back to the specific pending action it confirms, not just any pending action.
- **Idempotency tracking is in-memory and per-`ToolOrchestrator`-instance** — resets on process restart; a durable store is Phase 5's job.
- **Retry policy is "never retry" for everything**, not just destructive actions — simpler and safer than a partial retry policy, but a future phase may want bounded retries for read-only actions like `ORDER_LOOKUP` on transient failures.
- **This phase did not touch `docs/REPOSITORY_STRUCTURE.md` or any frozen architecture document** — `docs/DOMAIN_MODEL.md`/`docs/MODULES.md`/`docs/adr/ADR-003` remain the reference for `ToolRequest`/`ActionSpec`, and this report + the module docstrings in `action_models.py`/`tool_orchestrator.py` are where the Phase-4-specific additions (`ActionProposal`, `ToolExecutionResult`'s extra fields) are documented instead, consistent with the same reconciliation pattern used in Phases 2 and 3.

## 12. Next Recommended Phase

Proceed to Phase 5 (Session and Memory) as `plan.md` already specifies — it is the natural next step for exactly the two gaps flagged above (durable, session-backed confirmation state and idempotency tracking), and is a prerequisite for Phase 7's real authentication to have anywhere meaningful to attach identity.

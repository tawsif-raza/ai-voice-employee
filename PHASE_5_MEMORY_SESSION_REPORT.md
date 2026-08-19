# Phase 5 — Session and Memory Architecture Report

## 1. Executive Summary

Phase 5 adds a controlled Session and Memory architecture: `SessionManager`/`SessionRepository` (`src/agent/session_manager.py`, `session_models.py`) for short-lived, expiring workflow state, and `MemoryManager`/`MemoryRepository` (`src/agent/memory_manager.py`, `memory_models.py`) for durable, policy-gated cross-session preferences. Both are wired into `ConversationManager` as optional collaborators. The headline capability this unlocks: a Tool Orchestrator confirmation ("cancel my appointment" → "are you sure?") can now be resolved across two separate turns — the second turn's plain "yes" is recognized by this application's own deterministic reply classifier and checked against **trusted, server-side session state**, never against anything the LLM said or interpreted. A session's pending action becomes permanently unexecutable the instant its TTL lapses, even if a "yes" arrives afterward — proven directly by test.

## 2. Architecture

```
ConversationManager
       │
       ├── SessionManager ──────► SessionRepository (in-memory)
       │        │
       │        └── SessionState {status, workflow_state, pending_action,
       │                           pending_parameters, expires_at, ...}
       │
       └── MemoryManager ───────► PolicyEngine.evaluate_privacy()
                │                        │
                │                        ▼
                └──────────────► MemoryRepository (in-memory)
                                       │
                                       └── MemoryRecord {user_id, category, key, value, ...}
```

`ConversationManager.handle_turn()`'s new step "2.4" (between the clinical guard and intent classification): looks up/creates a session for the given `session_id`, and — if that session has a pending confirmation — decides whether the current message resolves it, using `_AFFIRMATIVE_PATTERN`/`_NEGATIVE_PATTERN` (this application's own regex classifiers, not the LLM). `_handle_tool_action()` (Phase 4) now additionally persists/clears pending state on the session when a `confirmation_required` result comes back. Memory context is injected at prompt assembly as its own, separate system message — never merged with RAG's knowledge-base context.

## 3. Session Model

**States** (`SessionStatus`): `ACTIVE`, `WAITING_FOR_INPUT`, `WAITING_FOR_CONFIRMATION`, `COMPLETED`, `EXPIRED`, `FAILED` — a closed enum; `transition_state()` rejects a plain string and enforces `ALLOWED_TRANSITIONS`, an explicit table (not derived from anything implicit). `COMPLETED`/`EXPIRED`/`FAILED` are terminal — no outgoing transitions.

**Reconciliation with the frozen `docs/DOMAIN_MODEL.md`:** `ConversationSession` (Group 1) already exists there, frozen with exactly `{session_id, status(active|closed), created_at}` required and `{user_id, last_activity_at, retention_expires_at}` optional — it predates Tool Orchestrator/confirmation workflows. `SessionState`'s core identity fields match that contract exactly; every workflow field (`workflow_state`, `pending_action`, `pending_parameters`, `confirmation_state`) is an **additive** extension, explicitly permitted by that document's own Global Versioning Convention. Not silently changed — documented here and in `session_models.py`'s module docstring, same pattern as Phases 2–4.

**Expiration:** every session carries `created_at`/`updated_at`/`expires_at` (default TTL 30 minutes). `get_session()` lazily transitions a past-due session to `EXPIRED` — clearing `pending_action`/`pending_parameters`/`workflow_state` in the same step — and returns `None`. A caller therefore can never observe or act on a stale pending action through the normal read path.

**Authorization:** `get_session(session_id, user_id=...)` returns `None` (never raises, never leaks existence) when the session belongs to a different `user_id`.

## 4. Memory Model

**Categories** (`MemoryCategory`): `PREFERENCE`, `WORKFLOW_CONTEXT`, `COMMUNICATION_PREFERENCE` — deliberately narrow, per the explicit instruction not to create broad medical categories. `MemoryRecord` is frozen, immutable, `{id, user_id, category, key, value, source, created_at, updated_at, expires_at, metadata}`.

**Persistence path:** `propose_memory()` (builds an untrusted candidate, never persists) → `validate_memory()`/`persist_memory()` (the *only* path to storage, gated by `PolicyEngine.evaluate_privacy(key, operation="persist")` — reusing Phase 3's existing method and existing `configs/policies/privacy.yaml` restricted-field list, no new policy category added).

**Retrieval path:** `get_allowed_context(user_id, category=None)` — scoped by `user_id`, optionally by category, and independently re-filtered through `evaluate_privacy(key, operation="expose_downstream")` even for records that somehow made it into storage (defense in depth, tested directly).

## 5. Security Model

- **LLM cannot access storage directly.** No method on `MemoryManager`/`SessionManager` resembles `database.query(...)`/`get_all()` — confirmed by a structural test (`test_no_raw_database_access_method_exists`).
- **LLM cannot modify session state.** Every session mutation goes through `SessionManager.update_session()`/`transition_state()`, called only by `ConversationManager`'s own code with parameters it derived deterministically (extracted IDs, tool results) — never from parsing a model response.
- **LLM cannot authorize actions.** A pending confirmation is resolved by `_AFFIRMATIVE_PATTERN` matching the *user's* raw message, checked against the *session's* trusted `pending_action` — the model is never consulted, and re-invoking the pending action still passes through `ToolOrchestrator.invoke()`'s full policy/auth gate sequence unchanged (Phase 4, untouched).
- **Memory writes are policy-controlled.** `persist_memory()` always calls `evaluate_privacy()`; a model-shaped candidate (`{"remember": true, "memory": {"key": "medical_condition", ...}}`) is proven inert — `MemoryManager` never even accepts such a dict as a parameter, only an already-typed `MemoryRecord`, and `source="llm_output_..."`-labeled records are denied identically to any other (`test_model_claimed_remember_flag_does_not_bypass_policy`).
- **Cross-user access is blocked.** Both `SessionManager.get_session()` and every `MemoryManager` read/delete method are scoped by `user_id`; `TestCrossUserIsolation` proves User B can neither read nor delete User A's memory.
- **Expired workflow state cannot execute actions.** `test_expired_session_does_not_execute_stale_pending_action` proves this at the full `ConversationManager` level: a "yes" arriving after expiry leaves the underlying appointment untouched.

## 6. Tool Integration

`ToolOrchestrator` itself (Phase 4) is **unmodified**. `ConversationManager._handle_tool_action()` now takes an optional `session` argument: on a `confirmation_required` result it calls `SessionManager.update_session(..., workflow_state="AWAITING_CONFIRMATION", pending_action=..., pending_parameters=...)`; on any other outcome it clears that state. A new `_execute_pending_action()` re-issues the pending action as a fresh `ToolRequest(confirmed=True)` and still runs it through `ToolOrchestrator.invoke()`'s complete gate sequence (policy, auth, confirmation-policy, idempotency, timeout) — session-derived confirmation does not skip any of Phase 4's checks, it only supplies the trusted `confirmed=True` that `invoke()` already required.

## 7. Privacy

This phase deliberately does **not** implement PII detection/redaction — `MemoryManager`'s only privacy control is the named-field boundary already built in Phase 3 (`configs/policies/privacy.yaml`'s `restricted_fields`), applied to memory `key`s. It does not scan free-text `value`s for embedded PII, does not redact logs, and does not implement consent tracking. This is the explicit scope boundary Phase 6 (Privacy and PII Protection Layer) is meant to build on top of — not claimed as complete here.

## 8. Failure Handling

| Condition | Behavior |
|---|---|
| Session storage/lookup fails (missing/expired/unauthorized) | `get_session()` returns `None`; `ConversationManager` treats this as "no usable session" and proceeds with ordinary (non-session) turn handling — **optional context fails open** |
| A pending tool action's session is unavailable when a "yes" arrives | The `2.4` interception step is simply skipped (no session found) — the tool action is **not** re-executed; the message is handled as an ordinary new turn instead — **authorization/workflow state fails closed** |
| `update_session()`/`transition_state()` on a missing/expired/unauthorized session | Raises `SessionNotFoundError` — callers within this codebase only reach these from already-`get_session()`-validated state, so this path is a defensive invariant, not a normal-flow error |
| Invalid state transition | Raises `InvalidTransitionError`, distinct from `SessionNotFoundError` even for a session that exists but is terminal |
| Memory policy denial | Raises `MemoryPolicyDeniedError` — `persist_memory()` never silently drops or silently succeeds |

## 9. Tests

- **Tests added:** 21 (`tests/test_session_manager.py`) + 19 (`tests/test_memory_manager.py`) + 5 (`TestSessionAndMemoryIntegration` in `tests/test_conversation_manager.py`) = 45.
- **Tests executed:** full repository suite, 268 tests.
- **Passing:** 267.
- **Failing:** 0 caused by Phase 5. 1 pre-existing environment error (`test_retriever.py` — `faiss` not installed, unrelated).
- **Security tests:** cross-user session access denial, cross-user memory read/delete denial, expired-session-cannot-execute (both at the `SessionManager` unit level and the full `ConversationManager` integration level), model-claimed-remember-flag inertness.
- **Expiration tests:** session-within-TTL usable, session-past-TTL returns `None` and clears pending state, explicit `expire_session()`, memory-record expiry excluded from listings.

## 10. Compatibility

- **ConversationManager:** `session_manager`/`memory_manager` are additive, optional constructor parameters (default `None` in the raw class; `build_conversation_manager()` enables both by default since they no-op safely without a `session_id`). Every existing test that doesn't pass `session_id=` to `handle_turn()` is completely unaffected — confirmed by the full pre-Phase-5 suite passing unmodified.
- **PolicyEngine:** unmodified — `evaluate_privacy()` is reused exactly as Phase 3 built it.
- **ToolOrchestrator:** unmodified.
- **Clinical Safety Guard:** unmodified; the session-lookup/pending-confirmation step is placed strictly after the clinical guard in `handle_turn()`, so a "yes" reply is still subject to clinical safety like any other message.
- **Intent Engine / RAG:** unmodified.
- **Existing API (`src/api/server.py`):** unchanged this phase — it doesn't pass `session_id`/`auth` yet, so Phase 5 has zero effect on the live HTTP path today; wiring a real session identifier through the API is future work (see below).

## 11. Remaining Technical Debt

- **In-memory storage only** — `SessionRepository`/`MemoryRepository` hold everything in a process-local dict; nothing survives a restart, and nothing is shared across replicas. A durable backend (matching `docs/DATABASE.md`'s still-open question) is unimplemented by design, per plan.md's explicit "if no persistence implementation exists, introduce the simplest appropriate abstraction."
- **`src/api/server.py` does not yet pass `session_id`/`auth` to `handle_turn()`** — Phase 5's capability exists but isn't reachable over HTTP yet; that wiring (a session header/cookie, real authentication) is Phase 7's job.
- **The affirmative/negative reply classifier is intentionally simple** (a short regex list) — an ambiguous reply while a confirmation is pending currently falls through to ordinary turn handling rather than re-prompting explicitly; a future refinement could have `ConversationManager` explicitly re-ask "Sorry, was that a yes or a no?" instead.
- **No concurrent-update protection** — two simultaneous turns for the same `session_id` could race on `SessionRepository`'s plain dict; acceptable for this phase's single-process, mostly-single-turn-per-session scope, but worth flagging before any concurrent deployment.
- **Memory has no explicit `list_allowed_memory`-vs-`get_allowed_context` documentation surfaced to callers beyond docstrings** — both exist and are tested, but a future phase should decide whether a management/admin surface (not just LLM-context injection) is actually needed.

## 12. Recommended Next Phase

Proceed to Phase 6 (Privacy and PII Protection Layer) as `plan.md` specifies — it is the natural next step for the boundary Section 7 above explicitly left open (free-text PII detection/redaction across logs, session state, memory values, and tool parameters), and should be sequenced before Phase 7's real authentication starts attaching genuine user identity to the session/memory infrastructure this phase built.

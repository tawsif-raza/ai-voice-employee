# Phase 7 — Authentication, Authorization and Identity Boundary Report

## 1. Executive Summary

Phase 7 establishes a trusted identity boundary: a deterministic `AuthenticationProvider` interface with a clearly-isolated, TEST-ONLY `DevelopmentAuthenticationProvider` (`src/agent/identity.py`), a typed `Role`/`Permission` taxonomy, and a new `PolicyEngine.evaluate_authorization()` method that governs both permission-checking and resource-ownership enforcement. Rather than introduce a second, competing identity type, this phase extends `AuthContext` — already used pervasively since Phase 4 — with the fields plan.md's "IdentityContext" asked for (`permissions`, `authentication_method`, `metadata`), reusing rather than duplicating. `src/api/server.py` now resolves identity from an `Authorization: Bearer <token>` header, backward-compatible with every existing caller (no header → anonymous, exactly today's pre-Phase-7 behavior) while failing closed on an invalid token (401). A genuine, tested resource-ownership check ("USER + CANCEL_APPOINTMENT + own appointment → ALLOW; + another user's appointment → DENY") now runs through the real `ToolOrchestrator`/`PolicyEngine` stack.

## 2. Identity Architecture

```
Client
  │  Authorization: Bearer <token>  (optional)
  ▼
FastAPI (src/api/server.py) — resolve_identity() dependency
  │
  ▼
DevelopmentAuthenticationProvider.authenticate()   — TEST-ONLY, isolated behind AuthenticationProvider
  │
  ▼
AuthContext  (this codebase's IdentityContext — see Section 11's reconciliation note)
  {user_id, authenticated, roles, permissions, authentication_method, metadata}
  │
  ▼
ConversationManager.handle_turn(auth=...)   — consumes identity, never authenticates, never assigns roles
```

No `Authorization` header → `ANONYMOUS_CONTEXT` (unauthenticated, no roles/permissions) — the same value every pre-Phase-7 caller already implicitly used. A header with an invalid/unrecognized token → `401 Unauthorized` before `ConversationManager` is ever reached (`self.recorder.calls == []` in `test_invalid_bearer_token_returns_401`).

## 3. Authorization Architecture

```
AuthContext (identity)
      +
Permission (required action)
      +
resource_owner_user_id (optional — the resource being acted on)
      ↓
PolicyEngine.evaluate_authorization()
      ↓
ALLOW / DENY
```

`evaluate_authorization()` is the single authoritative decision point — added to Phase 3's existing `PolicyEngine`, not a second authorization engine. `PRECEDENCE` extended to `("clinical", "authorization", "handoff", "confirmation", "tool", "privacy", "generation")` — authorization outranks tool/confirmation/privacy decisions, second only to clinical safety. Wired into `ToolOrchestrator.invoke()` as a new gate (step "2.1", between the existing role check and the confirmation check) that only activates when an `ActionSpec.required_permission` is set.

## 4. Roles and Permissions

| Role | Permissions |
|---|---|
| `USER` | `READ_OWN_SESSION`, `READ_OWN_MEMORY`, `WRITE_OWN_MEMORY`, `BOOK_APPOINTMENT`, `CANCEL_APPOINTMENT`, `READ_ORDER` |
| `STAFF` | same as `USER` today — defined per plan.md's requested taxonomy, but no staff-only capability exists yet in this system, so no additional permission was invented for it (least privilege: don't create what isn't required) |
| `ADMIN` | every permission, including `ADMIN_OPERATIONS` |

The four default mock tools now declare `required_permission`: `BOOK_APPOINTMENT`→`BOOK_APPOINTMENT`, `CANCEL_APPOINTMENT`/`RESCHEDULE_APPOINTMENT`→`CANCEL_APPOINTMENT` (reused — no separate reschedule permission was needed), `ORDER_LOOKUP`→`READ_ORDER`. No `DO_EVERYTHING` wildcard exists (`test_no_do_everything_wildcard_permission`).

## 5. Session Security

**Unchanged from Phase 5, confirmed still correct:** `SessionManager.get_session(session_id, user_id=...)` already denies cross-user access (`None` for a session belonging to a different `user_id`). Phase 7's contribution is guaranteeing the `user_id` passed into that check comes from a *trusted* `AuthContext` (resolved by `resolve_identity()`), not from anything a client could set directly — `src/api/server.py`'s new `session_id` field on `ChatRequest` is explicitly documented as "not an identity claim," and `test_client_supplied_identity_in_body_is_ignored` proves a client-injected `user_id`/`role`/`authenticated` field in the JSON body has zero effect.

## 6. Memory Security

**Unchanged from Phase 5, confirmed still correct:** `MemoryManager`'s existing per-`user_id` scoping (`list_allowed_memory`, `get_allowed_context`, `remove_memory`) is exercised again here as Step 7.15 Attack 5, unmodified, still denying cross-user reads/deletes.

## 7. Tool Security

`ToolOrchestrator.invoke()`'s gate sequence is now: tool policy → authentication → role (existing) → **permission + resource ownership (new)** → confirmation → idempotency → execution. `ToolRequest.resource_owner_user_id` (new, additive field) lets a caller supply a trusted ownership fact for `evaluate_authorization()` to check. `ConversationManager._handle_tool_action()` populates this automatically for `BOOK_APPOINTMENT` (the booker trivially owns what they create, via a new `owner_user_id` injected into `params` *after* proposal validation, from trusted `auth` — never from the untrusted `ActionProposal`). See Section 12 for what is *not* yet automatically wired.

## 8. Privacy

Audited (grep across `src/agent/`, `src/api/`) for `Authorization`/`Bearer`/`token`/`password`/`secret`/`credential`: no logging or print statement anywhere touches credential material — every exception path (`AuthenticationError`, the 401 `HTTPException`) uses a fixed generic message, never the submitted token. Confirmed behaviorally, not just by inspection: `test_error_message_never_echoes_submitted_token` and `test_401_response_does_not_echo_submitted_token` both submit a secret-looking token and assert it never appears in the resulting error text. Phase 6's `PrivacyService`/logging boundary was not modified this phase — no new logging call was added that touches identity data.

## 9. LLM Trust Boundary

> **The LLM cannot establish identity, authenticate the user, assign roles, grant permissions, or access another user's resources.**

All 6 mandatory Step 7.15 attacks pass (`tests/test_authorization.py::TestLLMTrustBoundaryIdentitySpoofing`): fake identity in a proposal (rejected as an unexpected parameter, and irrelevant to authorization regardless — only the `auth` argument to `invoke()` is ever consulted), fake `authenticated=true` claim, fake `role=ADMIN` claim, fake `permissions=[...]` claim, cross-user memory access, cross-user session access. `ActionProposal` has no `user_id`/`role`/`permissions` field at all — there is no code path anywhere in `identity.py`, `policy_engine.py`, or `tool_orchestrator.py` that could read one even if a caller tried to smuggle it in.

## 10. Tests

- **Tests added:** 20 (`tests/test_identity.py`) + 21 (`tests/test_authorization.py`, including all 6 Step 7.15 attacks) + 8 (`tests/test_server_api.py`'s new `TestAuthenticationBoundary`) = 49.
- **Tests executed:** full repository suite, 363 tests.
- **Passing:** 362.
- **Failing:** 0 caused by Phase 7. 1 pre-existing environment error (`test_retriever.py` — `faiss` not installed, unrelated).
- **Spoofing tests:** 6/6 Step 7.15 attacks pass, plus Step 7.11's client-body-injection test at the API layer.
- **Cross-user tests:** session and memory isolation re-verified (Phase 5 mechanisms, unmodified) plus a new resource-ownership test for tool actions specifically.
- **Authorization tests:** valid/missing permission, valid/insufficient role (existing), own/another's resource, admin override, least privilege, precedence ordering.

## 11. Compatibility

- **PolicyEngine:** extended (`evaluate_authorization()`, `"authorization"` added to `PRECEDENCE`), not replaced. All Phase 3–6 `PolicyEngine` tests pass unmodified except one updated to reflect the new `PRECEDENCE` tuple (an intentional addition, not a behavior regression).
- **AuthContext (this codebase's IdentityContext):** extended additively (`permissions`, `authentication_method`, `metadata`, `has_permission()`). Every pre-Phase-7 call site that constructed an `AuthContext` without these fields still compiles and runs — the new fields default to empty/`"none"`. **However**, every existing test that authenticated a user for a *permission-gated* tool action (the four default mock tools, which now declare `required_permission`) needed its fixture `AuthContext` updated to include a real permission set — 16 test call sites across `tests/test_tool_orchestrator.py` and `tests/test_conversation_manager.py` were updated (via a new shared `_authenticated_user()`/`permissions_for_roles()` helper) to construct realistic, fully-permissioned identities instead of an empty-permissions placeholder. This is a **test-fixture update, not a production behavior change** — no non-test code depended on the old, permission-less construction.
- **ToolOrchestrator:** `resource_owner_user_id` is an additive, optional `ToolRequest` field; `required_permission` is an additive, optional `ActionSpec` field (`None` skips the new check entirely, preserving exact Phase 4–6 behavior for any custom-registered tool that doesn't set it).
- **ConversationManager:** unchanged constructor surface; `_handle_tool_action()`'s internal `owner_user_id` injection only applies to `BOOK_APPOINTMENT` and only when `auth` is supplied.
- **API:** `/health` remains fully public (unchanged). `/generate` remains reachable with **no** `Authorization` header — deliberately, per plan.md's own caution ("do not blindly protect health checks/existing infrastructure") and because `src/voice/client_tts.py` and the entire pre-Phase-7 test suite depend on unauthenticated access continuing to work. A new optional `session_id` field was added to `ChatRequest` (Phase 5's capability, previously unreachable over HTTP — flagged as technical debt in the Phase 5 report, closed here).

## 12. Limitations

- **`DevelopmentAuthenticationProvider` is explicitly not production-grade.** Fixed, non-secret bearer tokens (`test-user-token`, `test-admin-token`), no password hashing, no token expiry, no external identity provider, no cryptographic verification. It exists solely to let the rest of this system's authorization architecture be built and tested against a *real* `AuthenticationProvider` implementation before a production identity provider is integrated — which this phase does not attempt (no OAuth/OIDC provider is configured in this repository, and plan.md explicitly instructs not to build one speculatively).
- **`/generate` does not currently enforce authentication.** It *accepts* it when offered, and fails closed on a bad token, but a request with no `Authorization` header at all still succeeds anonymously. This is a deliberate compatibility choice (see Section 11), not an oversight — a production deployment wanting mandatory authentication would need to change `resolve_identity()`'s "no header → anonymous" branch to instead reject the request, and that policy decision belongs to a deployment configuration this phase does not make on its own.
- **Token lifecycle is nonexistent.** No expiry, no refresh, no revocation — `DevelopmentAuthenticationProvider`'s tokens are permanently valid for the lifetime of the process. Not a concern for a test-only provider, but a hard requirement for any real provider that replaces it.
- **No unsupported production-authentication claim is made anywhere** in this code or report — every docstring referencing `DevelopmentAuthenticationProvider` states plainly that it is test-only.

## 13. Remaining Technical Debt

- **Resource-ownership lookup is not automatically wired for `CANCEL_APPOINTMENT`/`RESCHEDULE_APPOINTMENT`.** The mechanism (`ToolRequest.resource_owner_user_id` + `MockAppointmentStore.get_owner()`) is built and fully tested (`tests/test_authorization.py::TestToolOrchestratorResourceOwnership`), but `ConversationManager._handle_tool_action()` does not yet call `get_owner()` to populate it automatically when a user says "cancel appt_1005" — doing so would require either a real datastore or extending the mock store's query surface reachable from `ConversationManager` without violating the `ToolOrchestrator` abstraction boundary Phase 4 deliberately built (`ConversationManager` should not reach into `MockAppointmentStore` directly). This is flagged honestly rather than silently left as an apparent gap: the *authorization mechanism itself* is complete and tested; the *automatic plumbing* for two of the four default tools is not.
- **`STAFF` role has no distinct capability yet** — defined per plan.md's requested taxonomy, currently identical to `USER`. Add real staff-only permissions/tools when a real staff-facing capability exists, rather than inventing one speculatively now.
- **No token revocation, expiry, or refresh mechanism** — acceptable for a test-only provider, a hard requirement before any real provider replaces it.
- **`/generate`'s optional-authentication design** (Section 11/12) means a deployment that wants mandatory authentication must make that policy change itself; this phase does not provide a configuration flag for it (only `DEV_AUTH_ENABLED` for disabling the *test provider* itself, which is a different concern).

## 14. Recommended Next Phase

Proceed to Phase 8 (Observability, Auditability and Security Event Monitoring) as `plan.md` specifies next. It is the natural continuation of this phase's identity work — audit logging of authentication/authorization decisions (who was denied what, and why) is exactly the kind of security event Phase 8's monitoring layer should capture, and `PolicyEngine`'s already-typed `PolicyDecision` objects (including the new `evaluate_authorization()` decisions) are ready-made structured events for it to consume.

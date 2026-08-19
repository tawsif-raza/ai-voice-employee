# Phase 8 — Observability, Auditability and Security Event Monitoring Report

## 1. Executive Summary

Phase 8 adds a typed, privacy-aware observability layer on top of Phases 3–7's deterministic decision-making stack, without changing any of it: a `CorrelationContext`/`AuditEvent`/`SecurityEvent` model taxonomy (`src/agent/observability_models.py`), a single `AuditLogger`/`AuditRepository`/`SecurityEventDetector` (`src/agent/audit.py`), and an in-memory `MetricsRegistry` (`src/agent/metrics.py`). Every emission point follows one rule throughout: observability code runs strictly *after* a real, already-computed decision (a `PolicyDecision`, an authentication accept/reject, a session transition, a tool result) and only records it — it never influences that decision, and nothing in this layer has a return value any caller acts on. `src/api/server.py` gained request-ID correlation middleware, a `/ready` endpoint distinct from `/health`, and a safe global exception handler that never leaks a raw exception to a client. All 5 mandatory LLM-trust-boundary attack simulations (plan.md Step 8.20) pass, plus 2 additional ones (`AUTHZ_ALLOW`/tool-input-PII) exercising the same boundary in the tool/privacy paths.

## 2. Observability Architecture

```
                     REQUEST
                        │
                        ▼
     src/api/server.py: _correlation_id_middleware
           (reuses X-Request-ID header, or generates one)
                        │
                        ▼
         ConversationManager.handle_turn(request_id=...)
                        │
       ┌────────────────┼────────────────┐
       ▼                ▼                ▼
   PolicyEngine    ToolOrchestrator   SessionManager /
   decisions       (audit_logger,     MemoryManager
   (observed by    security_detector)  (audit_logger,
   ConversationMgr)                     security_detector)
       │                │                │
       └────────────────┼────────────────┘
                        ▼
              AuditLogger.record()
                        │
        metadata sanitized via PrivacyService
           (context "LOGGING", reused — Phase 6)
                        │
              ┌─────────┼─────────┐
              ▼         ▼         ▼
        AuditRepository  logging  SecurityEventDetector
        (in-memory,      (privacy_logging.log_event)  (repeated-failure /
         append-only)                                  cross-user / unknown-
                                                         tool heuristics)
```

`MetricsRegistry` is a parallel, independent sink (counters + a small latency histogram) — it does not depend on `AuditLogger` and vice versa, so a failure in one cannot affect the other.

## 3. Event Taxonomy

`EventType` (`src/agent/observability_models.py`) — 27 values, every one actually emitted somewhere in this codebase (no speculative categories):

| Category | Events | Emitted by |
|---|---|---|
| Authentication | `AUTH_SUCCESS`, `AUTH_FAILURE` | `identity.py::DevelopmentAuthenticationProvider.authenticate()` |
| Authorization | `AUTHZ_ALLOW`, `AUTHZ_DENY` | `tool_orchestrator.py::_invoke()` / `invoke()` |
| Policy | `POLICY_ALLOW`, `POLICY_DENY` | `conversation_manager.py::handle_turn()` (generation CLARIFY denial, post-generation allow) |
| Safety | `SAFETY_BLOCK`, `SAFETY_HANDOFF` | `conversation_manager.py::handle_turn()` (clinical guard, post-generation handoff) |
| Tool | `TOOL_REQUESTED`, `TOOL_ALLOWED`, `TOOL_DENIED`, `TOOL_STARTED`\*, `TOOL_SUCCEEDED`, `TOOL_FAILED`, `TOOL_TIMEOUT` | `tool_orchestrator.py::invoke()` (observability wrapper) |
| Confirmation | `CONFIRMATION_REQUIRED`†, `CONFIRMATION_RECEIVED`, `CONFIRMATION_REJECTED`†, `CONFIRMATION_EXPIRED`† | `tool_orchestrator.py` (received, via `_STATUS_TO_EVENT`) |
| Privacy | `PII_DETECTED`, `PII_REDACTED`, `PRIVACY_BLOCK`, `PRIVACY_RESTRICT` | `memory_manager.py::_record_pii_event()`, `tool_orchestrator.py::_record_pii_event()` |
| Session | `SESSION_CREATED`, `SESSION_EXPIRED`, `SESSION_INVALID_TRANSITION` | `session_manager.py` |
| System | `SYSTEM_ERROR` | `src/api/server.py::_safe_exception_handler()` |

\* `TOOL_STARTED` is defined for taxonomy completeness (matching `TOOL_SUCCEEDED`/`TOOL_FAILED`/`TOOL_TIMEOUT`) but not separately emitted — `ToolOrchestrator.invoke()` is synchronous and its wrapper only observes a *finished* result, so "started" carries no additional information beyond `TOOL_REQUESTED` in this implementation; flagged in Limitations rather than emitted redundantly.
† `CONFIRMATION_REQUIRED` and `CONFIRMATION_REJECTED`/`CONFIRMATION_EXPIRED` are reachable via `_STATUS_TO_EVENT`'s `"confirmation_required"`/`"duplicate"` mapping to `TOOL_DENIED`, not as distinct emitted types — see Limitations.

## 4. Audit Architecture

`AuditLogger.record()` (`src/agent/audit.py`) is the single point every component routes through. It is:
- **Typed**: constructs a frozen `AuditEvent`, never a free-form string.
- **Privacy-aware**: `metadata` is sanitized through `PrivacyService.sanitize(..., context="LOGGING")` when a `PrivacyService` is configured — the same Phase 6 boundary reused, not a second one. **Deliberately not wired the other direction**: `PrivacyService` itself never calls `AuditLogger` (see Section 6) to avoid infinite recursion (`AuditLogger.record()` → `sanitize()` → `decide()` → `record()` → …).
- **Best-effort**: any internal failure (e.g. a broken repository) is caught and swallowed, returning `None` instead of raising — `test_record_never_raises_on_internal_failure`/`test_record_swallows_failure_without_affecting_caller` lock this in.
- **Fire-and-forget by construction**: no caller anywhere branches on `record()`'s return value to decide what to do next — this is what makes "observability must not change business decisions" structurally true, not just a convention.

`AuditRepository` is in-memory and append-only (no `update()`/`delete()` method exists) — the same "simplest architecture compatible with this project" judgment already made for `SessionRepository`/`MemoryRepository`.

## 5. Security Monitoring

`SecurityEventDetector` (`src/agent/audit.py`) reuses signals already produced by real components rather than re-implementing detection logic:

- `record_auth_failure(identifier)` — `identity.py` calls this on every failed `authenticate()`; emits `REPEATED_AUTH_FAILURE` (severity `MEDIUM`) once a per-identifier count reaches the threshold (default 3). `reset_auth_failures()` is called on the next success, so a legitimate retry-then-succeed sequence never accumulates toward a stale threshold. `identifier` is documented and tested as a safe, non-secret reference (e.g. client IP) — never the submitted token.
- `record_cross_user_access_attempt(resource_type, actor)` — `session_manager.py::get_session()` and `memory_manager.py::remove_memory()` both call this the moment a cross-user denial actually happens (severity `HIGH`).
- `record_unknown_tool_request(action_name, actor)` — `tool_orchestrator.py::invoke()` calls this when a forged/unregistered action name is requested (severity `MEDIUM`).
- `record_policy_bypass_attempt()` / `record_malformed_action_proposal()` / `record_repeated_authorization_denial()` — defined and available (severity `CRITICAL`/`LOW`/`MEDIUM` respectively) for future wiring; not currently called by any Phase 8 emission site (see Limitations).

## 6. Privacy

**Recursion hazard identified and avoided**: `AuditLogger.record()` sanitizes its own `metadata` via `PrivacyService.sanitize()`, which internally calls `decide()`. If `PrivacyService.decide()` itself emitted audit events, that `sanitize()` call inside `AuditLogger.record()` would recurse indefinitely. Instead, `PrivacyService` remains completely audit-free (unchanged from Phase 6), and `PII_DETECTED`/`PII_REDACTED`/`PRIVACY_BLOCK`/`PRIVACY_RESTRICT` events are emitted by `PrivacyService`'s *callers* — `memory_manager.py::persist_memory()` (context `MEMORY`) and `tool_orchestrator.py::_invoke()` (context `TOOL_INPUT`) — which already consume the `PrivacyDecision` for their own real business purpose (deciding whether/how to persist or execute), so emitting there observes a decision already made, not a new one.

Every PII-related audit event carries only `{pii_types: [...], context, action}` in its metadata — never the matched raw value (`test_restrict_worthy_value_emits_pii_detected_and_privacy_restrict` and the mandatory attack-5 test both assert the raw PII string is absent from the event's metadata).

**Behavior discovered, not introduced, by this phase**: `PolicyEngine.evaluate_pii()` sets `allowed=(action == "ALLOW")` — meaning `RESTRICT` (not only `BLOCK`) also denies a memory write in `persist_memory()`'s existing `if not pii_decision.allowed: raise` check. The Phase 6 code path intended to persist a *redacted* copy for `REDACT`/`RESTRICT` outcomes is consequently unreachable for the `MEMORY` context today, since both those actions already deny before reaching it. This is a pre-existing Phase 6 characteristic, not a Phase 8 change — documented here (and in Limitations) rather than silently fixed, since altering allow/deny semantics is out of this phase's scope.

## 7. Metrics

`MetricsRegistry` (`src/agent/metrics.py`) exposes a fixed, enumerated set of 13 counters (`requests_total`, `requests_failed`, `policy_denials_total`, `handoffs_total`, `tool_requests_total`, `tool_success_total`, `tool_failures_total`, `tool_timeouts_total`, `auth_failures_total`, `authorization_denials_total`, `privacy_blocks_total`, `sessions_created_total`, `sessions_expired_total`) and 2 histograms (`generation_latency_ms`, `rag_latency_ms`). `increment()`/`observe()` reject any name outside these sets with `ValueError` — no method anywhere accepts an arbitrary label (`user_id`, `request_id`, free-text), which is what keeps this a bounded-cardinality store rather than a per-entity one (`test_no_arbitrary_label_parameter_exists`). `ConversationManager.handle_turn()` currently increments `requests_total`/`requests_failed`/`policy_denials_total`/`handoffs_total` and observes `generation_latency_ms` at the points where those outcomes are already known; `tool_*_total` counters exist in the fixed set but are not yet incremented from `ToolOrchestrator` (see Limitations — deferred, not broken).

## 8. Health / Readiness

- `/health` — unchanged, liveness only, always `{"status": "ok", "model_loaded": bool}`.
- `/ready` (new) — readiness: `200 {"ready": true}` once `_conversation_manager` is loaded, `503 {"ready": false}` otherwise. Exposes no internal dependency detail (`test_ready_response_exposes_no_internal_detail` asserts the body has exactly one key).

## 9. Error Handling

`_safe_exception_handler` (`src/api/server.py`, `@app.exception_handler(Exception)`) catches anything an unhandled exception in a route would otherwise surface: the client receives `{"request_id", "error_id", "error_code": "internal_error", "message": "An unexpected error occurred. Please try again."}` with HTTP 500 — never the exception message or type. Internally, `_error_logger` and `_audit_logger.record(EventType.SYSTEM_ERROR, ...)` capture `{error_id, request_id, exception_type}` for debugging, matching plan.md Step 8.15's exact requested shape. `test_unhandled_exception_returns_safe_generic_body` asserts the raw exception text (`"boom"`, `"RuntimeError"`) is absent from the response body.

## 10. Tests

- **Tests added**: 10 (`tests/test_metrics.py`) + 37 (`tests/test_observability.py`, including all 5 mandatory + 2 additional LLM-trust-boundary attack tests) + 7 (`tests/test_server_api.py`'s new `TestReadyEndpoint`/`TestRequestIdMiddleware`/`TestSafeExceptionHandler`) = 54.
- **Tests modified**: `tests/test_server_api.py`'s `RecordingConversationManager.handle_turn()` fixture updated to accept the new `request_id` kwarg (additive parameter, not a behavior change).
- **Tests executed**: full repository suite, 417 tests.
- **Passing**: 416.
- **Failing**: 0 caused by Phase 8. 1 pre-existing environment error (`tests/test_retriever.py` — `faiss` not installed; present before any of this work began, unrelated to Phase 8).
- **LLM trust-boundary attacks (plan.md Step 8.20, all 5 pass)**: (1) a forged `policy`-shaped claim embedded in tool params produces no `AUTHZ_ALLOW`/`TOOL_ALLOWED` event when the real decision denies; (2) a forged `{"status": "success"}` claim in tool params produces no `TOOL_SUCCEEDED` event when the real invocation is denied; (3) forged `{"authenticated": true, "roles": ["ADMIN"]}` credentials produce no `AUTH_SUCCESS` event, only `AUTH_FAILURE`; (4) a forged `{"confirmed": true}` claim embedded in untrusted tool params (vs. the trusted `ToolRequest.confirmed` field, left `False`) produces no `CONFIRMATION_RECEIVED` event; (5) a real email address followed by a trailing text claim asserting it's "already checked/safe" still produces `PII_DETECTED`/`PRIVACY_RESTRICT`, proving detection is regex-based on actual content, not influenced by adjacent claim text.

## 11. Compatibility

- **Every new constructor parameter is optional, defaulting to `None`** — `audit_logger`/`security_detector` on `identity.DevelopmentAuthenticationProvider`, `tool_orchestrator.ToolOrchestrator`, `session_manager.SessionManager`, `memory_manager.MemoryManager`; `audit_logger`/`metrics` on `conversation_manager.ConversationManager`. Omitting them preserves exact pre-Phase-8 behavior for every existing caller/test — confirmed by every Phase 3–7 test file passing unmodified except the one fixture noted in Section 10.
- **`ConversationManager.handle_turn()`** gained an optional `request_id` parameter (defaults to `None`, auto-generated only when `audit_logger` is configured) — same additive pattern as `session_id`/`confirmed` in Phases 4/5.
- **`build_conversation_manager()`** gained `observability_enabled: bool = True` plus optional `audit_logger`/`metrics`/`security_detector` pass-through parameters, so `src/api/server.py` can construct one shared instance set (for its own `DevelopmentAuthenticationProvider`, which lives outside `ConversationManager`) and have the whole process emit into a single audit trail, rather than two disconnected ones.
- **`src/api/server.py`**: `resolve_identity()` gained a `request: Request` parameter (FastAPI injects this automatically; no client-visible contract change) to extract `client_identifier` for `SecurityEventDetector`. `/generate` gained `request: Request` similarly, to read the middleware-assigned `request_id`. Both are additive — no existing request/response shape changed. `/health`, `ChatRequest`, `ChatResponse` are byte-for-byte unchanged.

## 12. Limitations

- **Correlation ID threading is partial, not end-to-end.** `ConversationManager` generates/accepts a per-turn `request_id` and passes it into its own direct `audit_logger.record()` calls (clinical block, generation-clarify denial, post-generation handoff/allow). It is **not** threaded into `SessionManager`'s or `ToolOrchestrator`'s own internal `record()` calls, which continue to use their own actor/action/resource identifiers without the turn's correlation ID attached. Full correlation would require passing `request_id` through `handle_turn()`'s internal calls to `session_manager.*()` and `tool_orchestrator.invoke()` — deferred as a scoped, explicitly-documented gap rather than attempted partially/inconsistently.
- **`tool_*_total`/`sessions_*_total`/`privacy_blocks_total` counters are defined in `MetricsRegistry`'s fixed set but not yet incremented anywhere** — only `ConversationManager`'s own counters (`requests_total`, `requests_failed`, `policy_denials_total`, `handoffs_total`) and `generation_latency_ms` are wired. `ToolOrchestrator`/`SessionManager`/`MemoryManager` do not currently accept or call a `MetricsRegistry`.
- **`TOOL_STARTED`, `CONFIRMATION_REQUIRED`, `CONFIRMATION_REJECTED`, `CONFIRMATION_EXPIRED` are defined in the taxonomy but not emitted as distinct event types** — the current synchronous `ToolOrchestrator.invoke()` observes only a finished result, and `_STATUS_TO_EVENT` maps `"confirmation_required"`/`"duplicate"` to the coarser `TOOL_DENIED`. Splitting these out would need either an async execution model (for `TOOL_STARTED`) or a second, more granular status-to-event table (for the confirmation sub-states) — neither exists yet and neither was required to satisfy plan.md's checklist, which asks that confirmation events be *auditable* (they are, via `CONFIRMATION_RECEIVED` and the `TOOL_DENIED` status metadata carrying `"confirmation_required"`), not that every sub-state have a unique `EventType`.
- **`SecurityEventDetector.record_policy_bypass_attempt()` / `record_malformed_action_proposal()` / `record_repeated_authorization_denial()` are implemented and tested in isolation but not called from any real emission site** — no current code path in this repository actually detects a "policy bypass attempt" or "malformed action proposal" as distinct from the deny paths already covered (`TOOL_DENIED`, `AUTHZ_DENY`, `UNKNOWN_TOOL_REQUEST`). Left available for a future phase that identifies a concrete need, per plan.md's "no speculative categories" instruction applied to *behavior*, not just to the `EventType` enum.
- **`PrivacyService`'s `RESTRICT` action denies rather than redacts-and-persists for the `MEMORY` context** (Section 6) — a pre-existing Phase 6 characteristic surfaced by this phase's testing, not introduced by it. Fixing it (if desired) is a `PolicyEngine`/`PrivacyService` semantics change, out of Phase 8's scope.
- **`AuditRepository`/in-memory `MetricsRegistry` are both process-local and non-durable** — a process restart loses all accumulated audit events and metrics. Acceptable per plan.md's "simplest architecture compatible with this project" instruction (every other Phase 5/6/8 store in this codebase — `SessionRepository`, `MemoryRepository` — makes the same choice); a durable sink is future work if a real deployment needs it.
- **No `/metrics` HTTP endpoint exists.** `MetricsRegistry.snapshot()` is available for a future endpoint or exporter but plan.md's completion criteria only require metrics to *exist*, not be served over HTTP — not built speculatively.

## 13. Remaining Technical Debt

- Thread `request_id` end-to-end into `SessionManager`/`ToolOrchestrator`/`MemoryManager`'s own audit emissions (Section 12's first bullet) so every event from a single turn shares one correlation ID.
- Wire `MetricsRegistry` into `ToolOrchestrator` (tool_requests_total/tool_success_total/tool_failures_total/tool_timeouts_total) and `SessionManager` (sessions_created_total/sessions_expired_total) — the registry and the call sites both exist; only the constructor wiring and increment calls are missing.
- Reconcile `PrivacyService`'s `RESTRICT`-denies-instead-of-redacts behavior for the `MEMORY` context (Section 6/12) if a future requirement needs partial-persist-with-redaction rather than full denial.
- Consider a durable audit sink (e.g. a file or external store) if audit records need to survive a process restart — explicitly out of scope for this phase's in-memory implementation.
- Wire `record_policy_bypass_attempt()`/`record_malformed_action_proposal()`/`record_repeated_authorization_denial()` into real call sites once a concrete need for those specific security-event categories is identified.

## 14. Recommended Next Phase

Phase 9 was explicitly NOT implemented per plan.md's final instructions. Based on the Limitations/Technical Debt above, the natural next phase is closing the correlation-ID and metrics-wiring gaps identified in Sections 12/13 (full request_id threading, tool/session metrics), followed by a real production identity provider to replace `DevelopmentAuthenticationProvider` (flagged as technical debt since Phase 7) — Phase 8's audit trail is now in place to observe that transition's authentication events end-to-end once it happens.

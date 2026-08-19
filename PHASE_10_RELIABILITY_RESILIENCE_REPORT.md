# Phase 10 — Reliability, Resilience and Failure-Safety Engineering Report

## 1. Executive Summary

Phase 10 hardens the system's runtime behavior under dependency failure without touching any of the deterministic security/control architecture Phases 3–9 built. A new `src/agent/reliability.py` toolkit (`RetryPolicy`, `IdempotencyClass`, thread-safe `CircuitBreaker`) is applied narrowly to three dependency categories — LLM generation, RAG retrieval, and tool "Business API" execution — each with bounded, idempotency-aware retry and circuit breaking. `PolicyEngine`/`ClinicalSafetyGuard`/`AuthenticationProvider`/`PrivacyService` are deliberately never retried or circuit-broken; instead, every previously-unguarded call to them was wrapped in `try`/`except` that fails closed (deny/block/clarify) on an internal error, closing a real gap where a `PolicyEngine` exception would have propagated uncaught. A genuine, independently-verified bug was found and fixed along the way: `ToolOrchestrator`'s timeout enforcement used `with ThreadPoolExecutor(...)`, whose `__exit__` blocks on an internal `atexit`-registered thread join regardless of the declared timeout — a "5-second timeout" against a permanently-hung tool call actually hung the process at exit. It was replaced with a daemon-thread-based execution model that genuinely returns within the configured timeout. Thread-safety locks were added to every in-memory repository (`SessionRepository`, `MemoryRepository`, `AuditRepository`, `MetricsRegistry`, `ToolOrchestrator`'s idempotency set) plus a coarse `SessionManager`-level lock covering its read-modify-write sequences, closing real race windows FastAPI's threadpool-based sync routes can hit under concurrent requests.

## 2. Reliability Architecture

```
Request
  │
  ▼
Authentication (Phase 9) — fails closed on JWKS/key-resolution failure, never retried
  │
  ▼
ConversationManager.handle_turn()
  │
  ├── Clinical/Generation/Handoff PolicyEngine calls
  │     — try/except → deny/block/clarify on internal failure (Phase 10 fix)
  │     — NEVER retried, NEVER circuit-broken (ADR-008)
  │
  ├── RAG retrieval — _retrieve_with_reliability()
  │     circuit breaker check → bounded retry (always safe, read-only) → same unchanged degraded fallback
  │
  ├── LLM generation — generation semaphore + circuit breaker + bounded retry
  │     retry ONLY before any chunk of the current attempt has been yielded;
  │     once streaming has begun, always falls through to the unchanged
  │     LLM_FAILURE_RESPONSE fallback (can't un-send partial output)
  │
  └── ToolOrchestrator.invoke() (Phase 4-9 gates unchanged)
        │
        ├── tool/confirmation/authorization PolicyEngine calls
        │     — try/except → policy_denied/confirmation_required/failure (Phase 10 fix)
        │
        └── _execute_with_reliability() — circuit breaker check →
              _execute_once() (daemon-thread timeout, Phase 10 fix) →
              bounded retry ONLY for ActionSpec.idempotency != NON_IDEMPOTENT_WRITE
              AND ONLY on a "timeout" outcome (never a validation error)
  │
  ▼
Audit + Metrics (Phase 8, extended with Phase 10 event/counter names)
```

## 3. Timeout Policy

| Dependency | Timeout | Source |
|---|---|---|
| Tool execution | Per-`ActionSpec.timeout_seconds` (unchanged, Phase 4) — enforced via a daemon-thread + `Queue.get(timeout=...)` (Phase 10 fix, see Section 8) | `action_models.py` |
| LLM generation stall | 300s (unchanged, `LLMService.GENERATION_TIMEOUT_S`, Phase 1-era) | `llm_service.py` |
| JWKS fetch | 10s (`jwks_timeout_seconds`, Phase 9) | `configs/auth.yaml` |
| Configured "reliability" defaults | `llm: 60s`, `rag: 5s` (advisory — not independently enforced beyond the above; see Limitations) | `configs/reliability.yaml` |

## 4. Retry Policy

- **Retryable**: LLM transient failure (before any chunk streamed), RAG retrieval exception (always — read-only), tool execution `"timeout"` status for `IDEMPOTENT_WRITE`/`READ_ONLY` actions.
- **Never retried**: clinical/generation/handoff/tool/confirmation/authorization `PolicyEngine` evaluation failures (deny instead); tool validation errors (`ValueError`/`KeyError`, status `"failure"`); any action classified `NON_IDEMPOTENT_WRITE`; a partially-streamed LLM response.
- **Counts**: configurable per dependency (`configs/reliability.yaml`: `llm.max_retries=1`, `rag.max_retries=2`, `tools.max_retries=1`) via `RetryPolicy(max_attempts=max_retries+1, ...)`.
- **Backoff**: exponential, capped (`RetryPolicy.compute_delay()`), optional jitter — pure/computable, so tests assert on delay values without sleeping (`tests/test_reliability.py::TestRetryPolicyBackoff`, 0.002s to run 19 tests).
- **No infinite loop**: bounded by `max_attempts` in every call site; `tests/test_tool_reliability.py::test_retry_is_bounded_no_infinite_loop` and `tests/test_conversation_reliability.py::test_repeated_failure_bounded_no_infinite_loop` assert the exact call count, not just "eventually stops."

## 5. Idempotency

`ActionSpec.idempotency` (new field, `action_models.py`, default `"NON_IDEMPOTENT_WRITE"` — fail closed) classifies each of the four default tools:

| Action | Classification | Reasoning |
|---|---|---|
| `ORDER_LOOKUP` | `READ_ONLY` | No state change. |
| `CANCEL_APPOINTMENT` | `IDEMPOTENT_WRITE` | End state converges regardless of retry count; the mock raises (not silently double-cancels) on a genuine repeat, so a retry after the first call already landed produces a harmless failure, never an unsafe second effect. |
| `RESCHEDULE_APPOINTMENT` | `IDEMPOTENT_WRITE` | Same (`appointment_id`, `date`, `time`) converges to the same end state. |
| `BOOK_APPOINTMENT` | `NON_IDEMPOTENT_WRITE` | Creates a new resource each call — a retried lost response would create a duplicate appointment. |

## 6. Tool Reliability

Duplicate business-action protection: Phase 4's existing `request_id`-keyed dedup set is now lock-protected (`tests/test_tool_reliability.py::test_concurrent_same_request_id_executes_exactly_once` — 10 concurrent threads submitting the identical `request_id`, exactly 1 success). Retry is gated by `idempotency`, never by `destructive` (a destructive action can still be safely idempotent, as `CANCEL_APPOINTMENT` demonstrates) and only for a `"timeout"` outcome specifically — a caller validation error is never retried regardless of classification.

## 7. Circuit Breakers

`CircuitBreaker` (CLOSED/OPEN/HALF_OPEN, thread-safe) applied only to: LLM generation, RAG retrieval, tool "Business API" execution. **Never** applied to `PolicyEngine`, `ClinicalSafetyGuard`, `AuthenticationProvider`, `PrivacyService` (ADR-008) — `tests/test_tool_reliability.py::test_circuit_breaker_never_applied_to_policy_gates` proves a tool's circuit being OPEN has zero effect on its confirmation-policy gate, which runs earlier and is unrelated. An OPEN circuit short-circuits to the same controlled failure/fallback each dependency already had (`DEPENDENCY_UNAVAILABLE` for tools, the unchanged degraded-RAG path, the unchanged `LLM_FAILURE_RESPONSE`) — never a new, different behavior.

## 8. Failure Modes

- **LLM**: transient (pre-stream) → bounded retry → success or safe fallback; mid-stream failure → never retried, safe fallback; permanent → bounded attempts then safe fallback (`tests/test_conversation_reliability.py::TestLLMRetry`, 6 tests).
- **RAG**: transient → bounded retry → success; permanent → degrades to the existing, unchanged "no grounding, continue ungrounded" path — **never** "ask the LLM to invent an answer" (`test_permanent_retrieval_failure_degrades_without_hallucination` asserts `retrieved_chunks == []` and the LLM is still called with no fabricated context).
- **Policy** (clinical/generation/handoff/tool/confirmation/authorization): internal failure → deny/block/clarify, never uncaught, never default-allow (`tests/test_conversation_reliability.py::TestPolicyEngineFailClosed`, `tests/test_concurrency.py::TestToolOrchestratorPolicyFailClosed` — 6 tests total).
- **Safety** (`ClinicalSafetyGuard.score()`): unchanged from Phase 1/3 — an internal exception is already treated as `is_handoff=True` (fail closed); Phase 10 additionally guarantees the *policy interpretation* of that result can't itself fail open.
- **Authentication**: unchanged from Phase 9 — JWKS/key-resolution failure denies (`tests/test_oidc_provider.py::test_key_resolution_exception_denied`).
- **Tools**: see Sections 5–7.
- **Memory / Sessions**: no realistic "storage unavailable" injection exists for the current in-memory repositories (see Limitations); concurrent-access safety is covered instead (Section 9).
- **TTS** (`src/voice/client_tts.py`): out of scope for this phase's automated tests (not part of the FastAPI request path) — existing 120s flat `requests` timeout noted, no retry/circuit-breaker added; flagged as technical debt.

## 9. Concurrency

Locks added (all in `src/agent/`): `SessionRepository` (per-call), `SessionManager` (coarse `RLock` covering each public method's full read-modify-write sequence — necessary because the race lives *above* the repository, in "get a mutable `SessionState`, mutate in place, save back"), `MemoryRepository` (per-call — `MemoryRecord` is frozen, so only dict-iteration safety is needed), `AuditRepository` (per-call), `MetricsRegistry` (per-call), `ToolOrchestrator._executed_request_ids` (dedicated lock around the check-then-add sequence). All verified under real concurrent threads, not just inspection: 12 tests in `tests/test_concurrency.py` (30 concurrent session creates all land uniquely, 20 concurrent updates never lose the final write, 30 concurrent cross-user reads never leak, 10 concurrent same-`request_id` tool calls execute exactly once, 5000/2000-increment metrics races lose zero updates, 2000-event audit-append race loses zero events).

## 10. Resource Limits

`ChatRequest` (`src/api/server.py`), sourced from `configs/reliability.yaml`'s `request_limits`: `max_message_length` (4000), `max_history_turns` (50), `max_history_turn_length` (4000) — enforced via Pydantic `Field(max_length=...)` and a `field_validator`, rejected with a normal 422 (no internal detail leaked) before reaching `ConversationManager`. A rejection increments `request_rejections_total`. `max_concurrent_generations` (default 1) bounds concurrent `generate_stream()` calls via a `threading.Semaphore` in `ConversationManager` — verified under real concurrent threads never to overlap (`tests/test_conversation_reliability.py::test_concurrent_generations_are_serialized_when_limit_is_one`). Generated-token count (`max_new_tokens`) and retrieved-chunk count (`rag_top_k`) were already bounded (Phase 1/Phase 2, unchanged).

## 11. Graceful Shutdown

`src/api/server.py`'s `lifespan()` now emits `EventType.GRACEFUL_SHUTDOWN` after `yield` (verified via source inspection, since the startup half requires a real model to exercise end-to-end — `tests/test_server_api.py::TestGracefulShutdown`). `uvicorn.run()` is configured with `timeout_graceful_shutdown` (bounded, from `configs/reliability.yaml`, default 30s) — never an indefinite wait. No external connection pool/persistent client requires an explicit close in this process (no database, no long-lived session kept open across requests).

## 12. Health/Readiness

Unchanged from Phase 8: `/health` (liveness) and `/ready` (readiness — `_conversation_manager is not None`) were reviewed against plan.md Step 10.19's guidance that optional dependencies (RAG) should not block readiness — RAG's existing degrade-and-continue behavior means an unavailable retriever never prevents `/ready` from reporting ready, which is the correct, already-existing behavior; no code change was needed here.

## 13. Observability

New `EventType` members: `DEPENDENCY_TIMEOUT`, `DEPENDENCY_FAILURE`, `RETRY_ATTEMPT`, `CIRCUIT_OPEN`, `CIRCUIT_HALF_OPEN` (defined, not currently emitted — no code path transitions into `HALF_OPEN` and separately audits it, since `allow_request()` transitioning the state is an implementation detail observed only via the subsequent success/failure outcome), `CIRCUIT_CLOSED` (defined, not separately emitted — `record_success()`'s CLOSED transition is implicit in the absence of a `CIRCUIT_OPEN` event), `IDEMPOTENCY_DUPLICATE`, `GRACEFUL_SHUTDOWN`. New `MetricsRegistry` counters: `timeouts_total`, `retries_total`, `dependency_failures_total`, `circuit_breaker_open_total`, `idempotency_duplicates_total`, `request_rejections_total` — all wired into real emission sites (`ToolOrchestrator`, `ConversationManager`, `src/api/server.py`), not merely defined. Correlation IDs (`request_id`) are threaded into every new event, reusing Phase 8's existing mechanism.

## 14. Failure Injection Tests

- `tests/test_tool_reliability.py` (9): timeout retry (idempotent succeeds after retry, non-idempotent never retries, bounded/no-infinite-loop), validation errors never retried, circuit breaker opens on repeated timeouts and short-circuits further calls without touching policy gates, concurrent duplicate requests, metrics integration.
- `tests/test_conversation_reliability.py` (16): LLM transient/permanent/mid-stream/repeated failure, LLM circuit breaker, generation concurrency semaphore, RAG transient/permanent failure (no hallucination), RAG circuit breaker, PolicyEngine fail-closed for clinical/generation/handoff evaluation, audit-repository-failure independence (Step 10.23).
- `tests/test_concurrency.py` (12): session/memory/metrics/audit races, ToolOrchestrator PolicyEngine-failure-denies for all three of its policy call sites.
- `tests/test_reliability.py` (19): `RetryPolicy`/`IdempotencyClass`/`CircuitBreaker` unit-level correctness, including a 5-thread/1000-failure `CircuitBreaker` thread-safety race.
- `tests/test_server_api.py` (+8 new): resource-limit rejection (message/history length, at-limit acceptance, rejection metric, no internal detail leaked), graceful-shutdown source verification.

No chaos-style *combinatorial* test matrix (e.g. "LLM timeout + audit failure") was built as a separate artifact — per plan.md Step 10.22's own instruction not to build "a full chaos-engineering platform," the individual failure/observability paths are exercised directly (Section 13's audit/metrics assertions run *alongside* the same failure-injection tests above, e.g. `test_retry_emits_audit_events_and_metrics` combines an LLM failure injection with audit/metrics verification in one test), which is the lightweight, deterministic approach the plan asks for rather than a combinatorial matrix.

## 15. Regression Tests

- **Tests added**: 19 + 9 + 16 + 12 + 8 = 64.
- **Tests executed**: full repository suite, 521 tests.
- **Passing**: 520.
- **Failing**: 0 caused by Phase 10. 1 pre-existing environment error (`tests/test_retriever.py` — `faiss` not installed; present before any of this work began).
- Every Phase 3–9 test file passes unmodified.
- **Regression classification discipline**: the only genuine regression risk encountered during this phase was self-inflicted and caught immediately — the `ThreadPoolExecutor` timeout bug (Section 1) was discovered because a *new* Phase 10 test hung, not because an existing test failed; it was root-caused, fixed, and reverified (existing `test_tool_orchestrator.py` timeout tests now also run measurably faster, with no behavior change to their assertions) before continuing.

## 16. Compatibility

- **Phase 3 PolicyEngine**: unmodified logic; only new `try`/`except` wrapping at call sites in `ConversationManager`/`ToolOrchestrator`, never inside `PolicyEngine` itself.
- **Phase 4 ToolOrchestrator**: `_execute_once()`'s external contract (`ToolExecutionResult`, `status` values) is unchanged; only its internal execution mechanism was fixed. `ActionSpec.idempotency` is additive with a safe default.
- **Phase 5 Session/Memory**: `SessionManager`/`MemoryManager`'s public method signatures are unchanged; only internal locking was added.
- **Phase 6 Privacy**: untouched.
- **Phase 7 Authentication (dev)**: untouched.
- **Phase 8 Observability**: extended (new `EventType`/counter names), never replaced.
- **Phase 9 Production Identity**: untouched; `OIDCAuthenticationProvider`'s existing fail-closed JWKS/key-resolution behavior already satisfies Phase 10's authentication-failure-mode requirement without modification.
- Every new `ConversationManager`/`ToolOrchestrator`/`build_conversation_manager()` parameter is optional and defaults to preserving exact pre-Phase-10 behavior (no retries, no circuit breaking) — the live system gets real reliability behavior only via `build_conversation_manager()`'s `reliability_enabled=True` default, matching every prior phase's "additive, optional, enabled-by-default-in-the-factory-only" convention.

## 17. Limitations

- **No durable-storage failure mode exists to inject.** `SessionRepository`/`MemoryRepository`/`AuditRepository` are in-memory dicts/lists — there is no realistic "storage unavailable" scenario to simulate until a real durable store replaces them (inherited scope boundary from Phase 5/8).
- **Circuit-breaker/retry state is per-process, in-memory.** A multi-instance deployment has independent circuit state per instance, not a shared view of dependency health.
- **`configs/reliability.yaml`'s advisory `timeout_seconds` for LLM/RAG are not independently enforced as new wall-clock timeouts** — LLM generation continues to rely on `LLMService`'s existing 300s stall-detection (Section 3); RAG retrieval has no dedicated timeout wrapper (a hung `retriever.retrieve()` call is not separately bounded, only retried after it eventually raises or returns). Adding a true wall-clock wrapper around these would require either `asyncio` or the same daemon-thread technique now used for tool execution — noted as follow-up work, not implemented speculatively this phase.
- **TTS (`src/voice/client_tts.py`) reliability was not hardened** — it sits outside the FastAPI request path this phase focused on; its existing flat 120s timeout with no retry/circuit-breaker is unchanged.
- **No distributed/multi-instance reliability coordination** (shared circuit state, distributed rate limiting) — explicitly out of scope, consistent with plan.md's "do not introduce distributed infrastructure unless the repository requires it."
- **A permanently stuck tool callable's worker thread is still leaked** for the life of the process — Python cannot forcibly terminate a thread; this is an inherent limitation of the daemon-thread fix (Section 1/8), not a complete solution to "true cancellation."

## 18. Remaining Technical Debt

- Add a genuine wall-clock timeout wrapper for RAG retrieval (currently timeout-shaped failures are indistinguishable from other exceptions — both are retried identically, but neither is bounded independently of the retriever's own behavior).
- Harden `src/voice/client_tts.py` with the same retry/circuit-breaker pattern if TTS reliability becomes an operational concern.
- Consider a durable audit/session/memory store with a real failure mode, once one exists, so "storage unavailable" failure injection becomes meaningful rather than artificial.
- `CIRCUIT_HALF_OPEN`/`CIRCUIT_CLOSED` events are defined but not separately emitted (only `CIRCUIT_OPEN` is) — low priority, since the same information is derivable from the surrounding `DEPENDENCY_FAILURE`/success events, but could be added for a more complete circuit-breaker observability picture.

## 19. Recommended Next Phase

Proceed to Phase 11 (Security Red Team, Adversarial Evaluation and Trust-Boundary Hardening) as `plan.md` specifies next. Phase 10's new dependency-failure surface (retries, circuit breakers, timeouts) is itself a natural target for adversarial testing — e.g., verifying an attacker cannot force a circuit open to trigger a specific fallback, or exploit retry timing to duplicate a business action — which Phase 11's methodology (Steps 11.20–11.22, resource exhaustion / retry abuse / concurrency-race attacks) is designed to probe.

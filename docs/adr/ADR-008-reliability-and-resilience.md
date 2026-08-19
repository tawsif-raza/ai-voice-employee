# ADR-008: Bounded, Idempotency-Aware Reliability Primitives — Never Applied to Security/Control Components

**Date:** 2026-08-18
**Related Documents:**
- `reliability.py` module docstring (Phase 10)
- `PHASE_10_RELIABILITY_RESILIENCE_REPORT.md`
- ADR-005 (fail-closed precedent for the Safety Pipeline)

# Status

Accepted

# Context

Phases 1–9 built a deterministic control plane (`ClinicalSafetyGuard`, `PolicyEngine`, `AuthenticationProvider`, `PrivacyService`, `SessionManager`, `MemoryManager`, `ToolOrchestrator`) but had no systematic answer for what happens when an *external* dependency (the LLM, RAG/FAISS, a tool's business API) is slow, flaky, or down, nor for what happens when a *local* control component itself raises internally rather than returning a decision. Two concrete gaps existed: (1) `ToolOrchestrator._execute_once()`'s `with ThreadPoolExecutor(...)` context manager blocked the calling thread for the full duration of a stuck call at `__exit__`, defeating its own declared timeout; (2) several `PolicyEngine.evaluate_*()` call sites in `ConversationManager`/`ToolOrchestrator` were not wrapped in `try`/`except`, so an internal `PolicyEngine` failure would propagate as an uncaught exception rather than a deny decision.

# Decision

Add a small, dependency-free reliability toolkit (`src/agent/reliability.py`): `RetryPolicy` (bounded, exponential-backoff retry decisions), `IdempotencyClass` (`READ_ONLY`/`IDEMPOTENT_WRITE`/`NON_IDEMPOTENT_WRITE`, defaulting fail-closed to non-idempotent), and `CircuitBreaker` (CLOSED/OPEN/HALF_OPEN, thread-safe). These are applied only around calls to the LLM, RAG retriever, and a tool's registered callable — never around `PolicyEngine`, `ClinicalSafetyGuard`, `AuthenticationProvider`, or `PrivacyService`, which instead get a narrower fix: every previously-unwrapped call site is wrapped in `try`/`except` that returns the *deny* decision appropriate to that call (block/handoff for clinical, clarify for generation, policy-denied/confirmation-required for tool gates) rather than raising. This preserves the existing architectural rule (ADR-005) that a local control component fails closed on its own, rather than being retried or circuit-broken as if it were an unreliable external service — retrying a `PolicyEngine` call that is denying for a *good* reason (or circuit-breaking it into "always allow" once it's failed enough times) would be a security regression disguised as a reliability improvement.

Separately, `ToolOrchestrator._execute_once()` was rewritten to use a raw `threading.Thread(daemon=True)` with a result `Queue` instead of `ThreadPoolExecutor`, because `ThreadPoolExecutor` registers its workers with an `atexit` hook that joins them at interpreter shutdown regardless of `shutdown(wait=False)` — verified experimentally: a `future.result(timeout=0.1)` call against a permanently-hung submission still blocked *process exit* for the full duration of that hang. A daemon thread has no such registry and does not block process exit, at the honestly-documented cost that Python cannot forcibly kill a thread, so a genuinely stuck call's worker thread is still leaked in memory until it finishes naturally.

# Alternatives Considered

- **A single generic `@retry` decorator applied broadly.** Not selected: plan.md explicitly warns against "add[ing] retries everywhere," and a blanket decorator would have no way to distinguish a transient LLM timeout from a `PolicyEngine` internal bug — the latter must never be retried, only denied.
- **Circuit-breaking `PolicyEngine`/`AuthenticationProvider` alongside the external dependencies**, on the theory that "any component can fail." Not selected: these are in-process, no-I/O components; their failures are bugs, not transient network conditions, and a circuit breaker's "OPEN → treat every call as failed" behavior for an authorization check would silently become "deny everything" (acceptable) only by accident, not by design — an explicit `except: deny` is clearer and cannot be reconfigured into "allow everything" by a misconfigured recovery timeout the way a circuit breaker's HALF_OPEN retry could.
- **Switching `ToolOrchestrator`'s execution model to `asyncio` with real task cancellation** instead of a thread-based approach. Not selected: `ConversationManager`/`ToolOrchestrator`'s entire call chain is synchronous (FastAPI's sync routes already run them in a threadpool), and converting to `asyncio` end-to-end is a much larger architectural change than this phase's "smallest compatible change" mandate justifies; the daemon-thread fix solves the concrete bug (process-exit hang) without that rewrite.
- **A process-based executor** (`ProcessPoolExecutor`) for true forced cancellation. Not selected: it would require every mock/real tool callable to be picklable and would introduce IPC overhead disproportionate to this application's actual tool latencies (sub-second mock calls); documented as a possible future option if a real tool integration ever needs true forced cancellation.

# Consequences

**Positive**
- A transient LLM/RAG/tool failure can now recover automatically within a bounded number of attempts, without ever retrying a non-idempotent business action or a security/control decision.
- `ToolOrchestrator`'s timeout enforcement is now genuinely bounded at the call site instead of silently blocking on process exit — a real bug fixed, not merely a new feature added.
- The retry/circuit-breaker boundary is structurally narrow (only three call categories: LLM, RAG, tool execution), so there is no ambiguity about what Phase 10 does and does not apply to.

**Negative**
- Two different failure-handling idioms now coexist in the same files: `try/except → deny` for `PolicyEngine` calls, and `RetryPolicy`/`CircuitBreaker` for dependency calls — a future contributor must understand which applies where (documented in both `reliability.py`'s module docstring and this ADR).
- A permanently stuck tool callable's worker thread is still leaked for the life of the process (inherent Python threading limitation, not fully solved).

**Known Limitations**
- `SessionRepository`/`MemoryRepository`/`AuditRepository` are in-memory dicts/lists with no realistic "storage unavailable" failure mode to inject — Phase 10's failure-injection tests exercise real concurrency races on these (locking, no lost updates) but not a durable-storage-outage scenario, since no durable storage exists yet (inherited from Phase 5/8's own documented scope).
- Retry/circuit-breaker state (`CircuitBreaker` instances) is per-process and in-memory — a multi-instance deployment would have independent circuit state per instance, not a shared one.

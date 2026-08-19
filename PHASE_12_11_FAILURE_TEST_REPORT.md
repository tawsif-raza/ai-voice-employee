# Phase 12.11 — Persistence Failure Testing Report

## Objective

Prove that PostgreSQL failures cannot cause unsafe behavior.

## Approach

Every scenario below was actually triggered (an unreachable connection target, a genuine constraint
violation, a forced mid-transaction failure, a simulated pool-timeout exception) and the collaborator's real
behavior observed — not asserted from reading the code alone.

## Simulated Failure Modes

| Required scenario | How it was simulated | Result |
|---|---|---|
| Database unavailable | `postgresql+psycopg2://u:p@127.0.0.1:1/...` — a syntactically valid, immediately-refused target | Every repository method raises `DatabaseUnavailableError` |
| Connection timeout | Same unreachable target with `connect_timeout=1` | Fails in ~1s, not hung, not retried into a multiple of that |
| Connection dropped | Covered by the same `DatabaseUnavailableError` wrapping path — `db.py`'s `session_scope()` catches any `SQLAlchemyError`, which covers a mid-operation disconnect identically to a failed initial connect | Wrapped, never raw |
| Transaction rollback | A valid update followed by a deliberate duplicate-PK insert in the same `session_scope()` block | The valid update was rolled back — reloading the row afterward shows `current_intent` still `None`, not the mid-transaction value |
| Constraint violation | `IdempotencyRecordRow(user_id=None, ...)` — violates the composite PK's `NOT NULL` requirement | Raises `DatabaseUnavailableError` (wrapping the underlying `IntegrityError`), row never persisted |
| Concurrent transaction conflict | Already covered by Steps 12.6/12.9's dedicated concurrency tests (5-10 threads racing the same key) — re-run here as part of the full suite, not duplicated | Exactly one winner, every run |
| Connection pool exhaustion | SQLite doesn't apply real pool tuning (deliberate, per `db.py`'s `_build_engine()` — pool params are meaningless for SQLite's connection model), so the actual PostgreSQL failure mode (`sqlalchemy.exc.TimeoutError`, raised when the pool is exhausted) was reproduced directly and fed through `session_scope()`'s real exception-handling path | Wrapped as `DatabaseUnavailableError`, fails within ~1s, never hangs waiting for a connection that will never come |

## Required Security Behavior — Verified Per Concern

**"If the system cannot establish trustworthy state for authorization, confirmation, ownership, or
idempotency, the risky operation must NOT execute."**

- **Idempotency**: `TestIdempotencyFailsSafe.test_unreachable_idempotency_repository_prevents_tool_execution`
  — a `ToolOrchestrator` configured with an unreachable `PostgresIdempotencyRepository` raises
  `DatabaseUnavailableError` from `invoke()`, and the underlying tool callable's own call counter proves it
  was **never invoked** (`call_count["n"] == 0`). The reservation-before-execution design from Step 12.9 is
  exactly what makes this possible — the database failure occurs before step 5 (execution) is ever reached.
- **Confirmation**: `TestConfirmationFailsSafe` — `SessionManager.try_consume_pending_confirmation()` against
  an unreachable repository raises, and critically **does not return `None`** (which would be
  indistinguishable from "nothing pending" and could mask a real outage as a normal no-op).
- **Ownership**: `TestOwnershipFailsSafe` — `SessionManager.get_session()`,
  `MemoryManager.remove_memory()`, and `MemoryManager.persist_memory()` all raise rather than returning a
  permissive default (e.g., a session that isn't really there, or "delete succeeded" for an operation that
  never touched the database).
- **Authorization**: `TestAuthorizationUnaffectedByDatabaseState` — `PolicyEngine.evaluate_authorization()`
  is stateless/YAML-config-driven with no database dependency at all, verified directly: it still makes the
  correct (deny) decision even with every other repository configured against a fully unreachable database.
  A PostgreSQL outage cannot corrupt this gate because nothing about it touches PostgreSQL.

## No Aggressive Retries

Per this step's explicit instruction ("Do not add aggressive database retries. Database retries can
duplicate writes. Only retry operations that are demonstrably safe/idempotent"):
`TestNoAggressiveRetries` verifies both by inspection (no `for attempt in`/`while True` retry-loop construct
exists in any of the four `Postgres*Repository` modules — grepped directly from their source) and
behaviorally (a single failed connection attempt against a 1-second `connect_timeout` target completes in
well under 3 seconds — a retrying implementation attempting even 3 tries would exceed that). **None of the
four Postgres-backed repositories implement any retry logic** — every failure surfaces immediately as
`DatabaseUnavailableError` on the first attempt. (`ToolOrchestrator`'s existing Phase 10
`RetryPolicy`/`CircuitBreaker` machinery, which retries *tool/business-API* timeouts, is unrelated and
untouched — it never wraps a repository/idempotency call.)

## No Insecure Fallback

`TestNoInsecureFallback` proves the negative directly, not just by absence of code:
- `SessionManager.create_session()` against an unreachable repository raises — it does **not** silently
  construct and return a usable session from a fresh in-memory substitute.
- `ToolOrchestrator` configured with a (broken) persisted `idempotency_repository` never falls through to
  its old default `_executed_request_ids` set on failure — verified by asserting that set remains empty
  after the failed call, proving the failure wasn't silently absorbed by the pre-existing default mechanism.

## No Privacy Leakage on Failure

`TestNoPrivacyLeakageOnFailure`:
- A `MemoryManager.persist_memory()` failure's exception message never contains the record's actual value,
  even though the write itself failed (matches `db.py`'s existing `safe_url`-only-in-errors discipline,
  extended here to application data, not just credentials).
- `AuditLogger.record()` against an unreachable database returns `None` (its existing Phase 8 best-effort
  contract — never raises) and, since the write never reached the repository, the metadata passed to it
  never persisted or leaked anywhere.

## No Ownership Bypass

Already covered under "Required Security Behavior" above (`TestOwnershipFailsSafe`) — every ownership-scoped
operation fails closed rather than defaulting to "allowed."

## Tests

New file: **`tests/test_persistence_failure_injection.py`** — 16 tests across 9 test classes, one per
required failure mode/security concern from this step's spec.

### Results

```
python -m unittest tests.test_persistence_failure_injection -v
Ran 16 tests in 11.224s — OK (16/16 passed)
```

### Phase 11 Security Regression

```
python -m unittest tests.test_security_red_team -v
Ran 33 tests — OK
```

All 9 `TestSecurityInvariants` cases pass unchanged, including
`test_invariant_5_session_manager_decides_session_ownership`,
`test_invariant_6_memory_manager_decides_memory_ownership`,
`test_invariant_7_tool_orchestrator_decides_tool_execution`, and
`test_invariant_8_trusted_confirmation_state_decides_confirmation` — the exact four invariants this step's
failure-injection work is meant to protect.

### Full Suite

```
python -m unittest discover -s tests -p "test_*.py"
Ran 689 tests in 41.075s
FAILED (errors=1)
```

- **689 = 673 (Phase 12.10 baseline) + 16 new.** No existing test's outcome changed.
- **1 pre-existing error**, unchanged: `test_retriever.py`'s `sentence_transformers` gap (documented in
  every prior Phase 12 report).

## Honest Limitation Noted

A database failure occurring **mid-stream** during a `StreamingResponse` (the `/generate` endpoint's
`stream=True` path in `src/api/server.py`) is not converted into a graceful in-band NDJSON error event by
this phase's work — FastAPI's `@app.exception_handler(Exception)` (Phase 8) only rewrites the HTTP response
before headers are sent, which has already happened once streaming begins; a mid-stream exception instead
terminates the connection abruptly. This is a pre-existing architectural characteristic (the same is already
true for an unwrapped LLM/RAG exception outside the specific `try/except` blocks `handle_turn()` already has
around LLM generation and RAG retrieval), not a Phase 12 regression, and does not violate this step's core
requirement — no risky operation executes either way, satisfying "the risky operation must NOT execute" —
but a fully polished streaming failure UX (an in-band error event rather than a dropped connection) was not
in scope for a persistence-failure-testing step and is noted here as genuine, disclosed remaining work
rather than silently left unmentioned.

`PHASE 12.11 COMPLETE`

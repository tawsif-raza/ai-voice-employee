# Phase 12.6 — Persistent Pending Confirmations Report

**Status: security-critical step. Phase 11's confirmation replay-race protection was re-verified, not
regressed, against the persisted repository.**

## Objective / Architectural Decision

Persist confirmation state in the database without regressing Phase 11's replay-race fix
(`SessionManager.try_consume_pending_confirmation()`, closed in response to plan.md Steps 11.13/11.22).

**No separate `PendingActionRepository`/`pending_actions` table was created.** This is a deliberate decision,
made explicitly in `PHASE_12_1_PERSISTENCE_AUDIT.md` §17 (step 4) and re-confirmed while implementing this
step: `workflow_state`, `pending_action`, `pending_parameters`, and `confirmation_state` are already
additive fields on `SessionState`/the `sessions` table (Step 12.3) — a pending confirmation *is* a session
in the `WAITING_FOR_CONFIRMATION`/`AWAITING_CONFIRMATION` workflow state, not a distinct entity with its own
lifecycle. Introducing a second table would duplicate every one of those columns and require keeping two
tables consistent for what is, today, one row's worth of state. What plan.md's Step 12.6 actually requires —
atomic, cross-process-safe consumption — is a property of the *operation*, not of where the columns live,
and is met by `PostgresSessionRepository.try_consume_pending_confirmation()` (added in Step 12.5's file,
documented here because this is the step whose explicit requirements it satisfies).

Never treated as authoritative merely because a client/LLM claims it: `approved=true`, `confirmed=true`,
`authorized=true`, `execute=true` do not appear anywhere in `session_repository_postgres.py`'s logic — the
only thing that gates consumption is the row's own persisted `workflow_state`/`expires_at`/`user_id`
columns, set exclusively by `SessionManager` through trusted application code paths (`update_session()`),
never derived from a request body.

## Required Lifecycle — Verified End-to-End

```
request -> policy evaluation -> confirmation required -> pending action stored
    -> user confirms -> policy re-evaluation -> atomic consumption -> tool execution
```

This step's own tests exercise "pending action stored -> atomic consumption" against the real persisted
repository (`TestReplayProtectionAcrossPersistence`, `TestConcurrentConfirmationConsumption`); "policy
evaluation"/"policy re-evaluation"/"tool execution" are `PolicyEngine`'s and `ToolOrchestrator`'s existing,
untouched responsibilities (Phase 3/4/10), already covered by their own regression suites, which this step
re-ran unchanged (see Full Suite below) to confirm nothing about persisting session state disturbed them.

## Implementation: Atomic, Database-Level Consumption

`PostgresSessionRepository.try_consume_pending_confirmation(session_id, user_id=None)`
(`src/agent/session_repository_postgres.py`):

```sql
UPDATE sessions
SET workflow_state = NULL, updated_at = :now
WHERE session_id = :session_id
  AND workflow_state = 'AWAITING_CONFIRMATION'
  AND pending_action IS NOT NULL
  AND expires_at > :now
  AND (:user_id IS NULL OR user_id IS NULL OR user_id = :user_id)   -- only when user_id was supplied
RETURNING pending_action, pending_parameters
```

followed, only if a row matched, by a second `UPDATE` in the **same transaction** clearing
`pending_action`/`pending_parameters` for tidiness (not itself security-critical — see the method's
docstring for exactly why `RETURNING` can't show pre-clear values if a single statement clears and returns
the same column).

**This is real database-level atomicity, not a Python lock**: the guard condition
(`workflow_state = 'AWAITING_CONFIRMATION'`) is evaluated and cleared inside one `UPDATE`'s row lock. A
second, concurrent caller — in the same process *or a different one* — attempting the identical statement
either sees 0 rows affected immediately (already consumed) or blocks on the database's row lock until the
first transaction commits, after which it also sees 0 rows. Per plan.md's explicit instruction ("Do not rely
only on Python locks because multiple application processes may exist"), no `threading.Lock`/`RLock`
participates in this path at all — `SessionManager`'s existing `RLock`-guarded implementation is bypassed
entirely for a DB-backed repository (see below), not layered on top of it.

**`SessionManager` integration** — the only change to `session_manager.py` in this step, 9 lines:

```python
repo_consume = getattr(self._repository, "try_consume_pending_confirmation", None)
if callable(repo_consume):
    return repo_consume(session_id, user_id=user_id)
with self._lock:
    ...  # exact, unmodified Phase 11 implementation
```

The in-memory `SessionRepository` has no `try_consume_pending_confirmation` method, so `hasattr` is `False`
for it and every existing caller/test — including every one of Phase 11's own replay-protection regression
tests — keeps using the exact, byte-for-byte-unchanged `RLock`-guarded path. This was verified, not assumed:
`tests/test_session_manager.py` and `tests/test_security_red_team.py` were re-run and pass unchanged (see
below).

## Expiration

Preserved exactly: the atomic `UPDATE`'s `WHERE` clause includes `expires_at > :now`, so an expired session
can never be consumed even if `SessionManager`'s own lazy-expiration sweep hasn't touched that row yet — the
database-level check is a second, independent enforcement of the same rule
`test_expired_pending_confirmation_cannot_be_consumed` verifies.

## Replay Protection

**Required test**: confirmation → execute → same confirmation again → second execution denied. Implemented
as `TestReplayProtectionAcrossPersistence.test_second_consumption_of_same_confirmation_is_denied` — the first
`try_consume_pending_confirmation()` call returns `("CANCEL_APPOINTMENT", {...})`; the second, identical call
against the same session returns `None`. This is the exact scenario Phase 11 originally fixed
(`tests/test_security_red_team.py`'s replay-attack tests), now re-verified against the persisted repository
specifically — not just re-run against the untouched in-memory path.

Also added: `TestDuplicateConfirmationSubmission.test_duplicate_sequential_confirmation_only_consumes_once`
— three sequential submissions of the same confirmation; exactly one succeeds.

## Concurrency (Mandatory)

`TestConcurrentConfirmationConsumption.test_concurrent_same_session_confirmation_executes_exactly_once`:
five threads, synchronized with a `threading.Barrier` to maximize actual overlap, all call
`try_consume_pending_confirmation()` for the *same* session simultaneously, against a real **file-based**
SQLite database (not `:memory:`) — deliberately chosen so each thread genuinely checks out its own pooled
connection from `db.py`'s connection pool, and it is the *database's own locking*, not a shared Python
object, that serializes the five `UPDATE` statements. Result: **exactly one** of the five calls returns a
non-`None` outcome, every time this was run.

## Restart Recovery

`TestRestartRecovery.test_pending_confirmation_survives_simulated_restart`: a session is put into
`AWAITING_CONFIRMATION` via one `SessionManager`/`Database`/`Engine` stack; a **completely new**
`Database`/`PostgresSessionRepository`/`SessionManager` stack is then constructed against the same
underlying SQLite file (no Python object from before is reused — this is what a real process restart looks
like for a file/network-backed database, not an in-process cache surviving). The pending confirmation is
still there and is successfully consumed by the "post-restart" manager instance.

## DB Failure

`TestDatabaseFailure.test_try_consume_pending_confirmation_raises_database_unavailable_on_connection_failure`:
an unreachable target raises `DatabaseUnavailableError` (via `db.py`'s `session_scope()`), never a raw
driver exception, and never a false "consumed" result.

## Tests

All in `tests/test_session_repository_postgres.py` (shared file with Phase 12.5 — see that report for the
session-CRUD-specific groups). Confirmation-specific groups added in this step:

| Group | Tests | Covers |
|---|---|---|
| `TestReplayProtectionAcrossPersistence` | 1 | Second consumption of the same confirmation denied |
| `TestConcurrentConfirmationConsumption` | 1 | 5 concurrent threads, real file-based SQLite, exactly one success |
| `TestRestartRecovery` | 1 | Pending confirmation survives a simulated process restart |
| `TestDuplicateConfirmationSubmission` | 1 | 3 sequential duplicate submissions, exactly one succeeds |
| `TestExpiration.test_expired_pending_confirmation_cannot_be_consumed` | 1 | Already covered under Phase 12.5's report; listed here since it's this step's own requirement |
| `TestDatabaseFailure` (1 of 3) | 1 | Connection failure during consumption raises, never silently "succeeds" |

### Results

```
python -m unittest tests.test_session_repository_postgres -v
Ran 20 tests in 3.905s — OK (20/20 passed)
```

### Focused → Phase 11 Security → Full Suite

```
python -m unittest tests.test_session_repository_postgres tests.test_security_red_team tests.test_session_manager -v
-> all pass, no changes to Phase 11's own test outcomes

python -m unittest discover -s tests -p "test_*.py"
Ran 608 tests in 16.916s
FAILED (errors=1)
```

- **608 = 588 (Phase 12.3 baseline) + 20 new** (Phase 12.5 + 12.6 combined, one test file). No existing test
  was edited; no existing test's pass/fail outcome changed.
- **1 pre-existing error**, unchanged and already documented in every prior Phase 12 report:
  `test_retriever.py`'s `sentence_transformers` gap.

`PHASE 12.6 COMPLETE`

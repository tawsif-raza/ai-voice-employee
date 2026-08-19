# Phase 12.9 — Persistent Idempotency Report

## Objective

Make business-action idempotency durable across application restarts and multiple application workers.

## First — Inspection

Inspected `ToolOrchestrator._invoke()` (steps 4 and 5, `tool_orchestrator.py`) and the Phase 10/11
idempotency implementation before changing anything: `_executed_request_ids: set[str]`, a bare in-process
set keyed by `request_id` alone, with the add happening only `if result.success` — recorded lock-protected
via `_executed_request_ids_lock` (Phase 10, plan.md Step 10.15). **This default behavior was not replaced.**
It remains ToolOrchestrator's exact, byte-for-byte-unchanged path whenever no `idempotency_repository` is
configured — verified by `test_default_orchestrator_is_unaffected_when_no_repository_given` and by every
one of Phase 4/10/11's own pre-existing tests passing unchanged (see Full Regression below).

## Schema Change Required — and Why

The Step 12.3 schema gave `idempotency_records` a single-column primary key (`request_id`). Step 12.9's own
required test — **"same key + different user → isolated"** — is structurally impossible against that
schema: a second user's `INSERT` for the same `request_id` string would violate the first user's uniqueness
constraint before any user-scoping logic could even run. This is a genuine, necessary schema revision, not
scope creep: I re-scoped the primary key to the composite **`(user_id, request_id)`**, and added an
`expires_at` column for the also-required "expired key → correct expiration behavior" test (Step 12.3 had
no expiration concept at all for this table).

New migration: **`alembic/versions/e6799137d151_..._composite_primary_key.py`** — a drop-and-recreate of
`idempotency_records` (not an in-place `ALTER TABLE ... DROP/ADD CONSTRAINT`), justified because this table
has no production data anywhere yet (Phase 12 hasn't shipped) — per
`PHASE_12_1_PERSISTENCE_AUDIT.md` §12's own "no live data migration is required" finding. Verified
upgrade → downgrade → upgrade-again against a clean SQLite database (same discipline as Step 12.3's
migration). `tests/test_db_migrations.py` was updated (not weakened) for the new composite key: its
duplicate-rejection test now inserts a `user_id`, and a new
`test_same_request_id_different_user_is_not_a_duplicate` test proves the isolation property at the raw SQL
constraint layer, independent of any Python repository code.

## Implementation

New files:
- **`src/agent/idempotency_repository.py`** — `InMemoryIdempotencyRepository`, an explicit, opt-in
  alternative implementation (NOT ToolOrchestrator's default — see above) with the richer
  `(user_id, request_id, action, ttl)`-scoped interface this step requires.
- **`src/agent/idempotency_repository_postgres.py`** — `PostgresIdempotencyRepository`, the persisted,
  cross-process-safe implementation of the same interface.

### Design decision: reserve-before-execute, not record-after-success

The original raw-set mechanism checks membership *before* execution but only records success *after*
execution (line ~458 of the pre-Phase-12.9 `tool_orchestrator.py`). Tracing this carefully: for a
single-process, lock-protected set this is safe in practice (the existing Phase 10/11 concurrency tests
confirm it), but it is **not** safe for state shared across multiple processes, because the "check" and the
"record" are two separate operations with no atomicity between them across process boundaries.

For the new `idempotency_repository`-configured path, I therefore changed the sequencing: `try_reserve()`
is called **before** step 5 (execution) — only the caller that wins the atomic reservation may proceed to
actually invoke the tool. This is what makes "exactly one execution" a genuine cross-process guarantee
rather than a single-process convenience. This only applies to the new, opt-in path; the default path's
sequencing is completely untouched.

A consequence I had to handle explicitly: reserving before execution means a **failed** attempt would
otherwise permanently consume the key (blocking a legitimate later retry). Fixed by adding `release()`
(deletes the reservation) and calling it whenever `result.success` is `False` for the persisted path —
restoring the same "only success is locked in" semantics the original `if result.success:` guard already
had for the default path. Verified by
`TestToolOrchestratorIntegration.test_failed_execution_releases_the_key_for_legitimate_retry`.

### Concurrency mechanism

`PostgresIdempotencyRepository.try_reserve()`:
```sql
INSERT INTO idempotency_records (user_id, request_id, action, result_status, executed_at, expires_at)
VALUES (:user_id, :request_id, :action, 'in_progress', :now, :expires_at)
ON CONFLICT (user_id, request_id) DO UPDATE
  SET action = :action, result_status = 'in_progress', executed_at = :now, expires_at = :expires_at
  WHERE idempotency_records.expires_at < :now
RETURNING user_id
```
One statement. A row lock taken by the first transaction's insert/update branch blocks a concurrent second
transaction's identical statement until the first commits; the second then re-evaluates the `WHERE`
condition against the now-committed row and — whether the conflict was a fresh unexpired reservation (`DO
UPDATE` doesn't fire, `RETURNING` empty) or the row was already reclaimed by someone else in the meantime
(same result) — correctly sees it lost the race. The **same statement** atomically reclaims a genuinely
expired row, satisfying the expiration requirement with the identical mechanism, not a separate code path.

### Scoping semantics (Step 12.9's exact three required scenarios)

| Scenario | Behavior | Rationale |
|---|---|---|
| Same user + same key + same operation | Second call denied — executes once | Standard idempotency |
| Same key, different user | **Isolated** — each user gets an independent reservation | Two different users' key strings are not the same identity namespace; matches real-world idempotency-key conventions (e.g. Stripe scopes keys per API key) |
| Same key, different operation (same user) | **Rejected** — the key is already claimed, regardless of which action it names | An idempotency key must never be silently repurposed for a different operation once claimed |

### Expiration

Default TTL: **24 hours** (`idempotency_repository.DEFAULT_TTL`) — long enough for a legitimate client retry
after a network blip or redeploy to still hit the same key, short enough that the table doesn't grow
unbounded; matches the same convention real idempotency-key APIs (e.g. Stripe) use. Lazy expiration, not a
background sweep — the same pattern `SessionManager` already uses (expire-on-read/write, no scheduled job)
— an expired row is reclaimed the moment any caller's `try_reserve()` touches it.

## Security

- **LLM cannot create or control trusted idempotency authorization**: no repository method accepts or
  interprets any client/model-supplied "user_id" claim — `ToolOrchestrator` is the only caller, and it
  passes `auth.user_id` exclusively, which by this point in `_invoke()` (step 4, after step 2's
  authentication gate) is guaranteed to be a real, `AuthenticationProvider`-resolved identity, never text
  parsed from a request body or model output. `test_reservation_requires_a_real_authenticated_user_id_not_client_supplied_text`
  documents this structurally (the parameter is a required keyword, not an optional/inferred value).
- **Client cannot reuse another user's idempotency state**: proven directly —
  `test_client_cannot_reuse_another_users_idempotency_state_via_repository` has "user-b" reuse "user-a"'s
  exact key string and confirms user-a's own record (action, status) is completely untouched; user-b merely
  gets their own independent slot (the "isolated" behavior above), never visibility into or control over
  user-a's.

## Restart Recovery

`test_restart_recovery_duplicate_still_blocked`: an operation executes successfully against one
`Database`/`ToolOrchestrator` stack; a completely new stack (fresh `Database`/`Engine`/repository/
orchestrator, no Python object reused) is constructed against the same underlying SQLite file, and a repeat
of the identical request is still correctly denied as a duplicate.

## Concurrency

`test_postgres_backed_concurrent_reservations_real_file_db`: five threads, synchronized with a
`threading.Barrier`, all call `try_reserve()` for the identical `(user_id, request_id)` simultaneously
against a real **file-based** SQLite database (not `:memory:`, so each thread genuinely checks out its own
pooled connection and it's the database's own row locking — not a shared Python object — doing the
serializing). Result: exactly one of the five wins, every run.

## Tests

New file: **`tests/test_idempotency_repository_postgres.py`** — 15 tests, most run against *both*
implementations via a shared `_repos()` helper (proving the contract, not just one backend).

| Group | Tests | Covers |
|---|---|---|
| `TestRequiredScopingScenarios` | 5 | The exact three required scenarios (same-user/same-key/same-op; cross-user isolation; cross-operation rejection) + expired-key reclaim + unexpired-key-not-reclaimed |
| `TestReleaseOnFailure` | 1 | A released key allows a legitimate retry |
| `TestConcurrency` | 2 | In-memory (10 threads) and Postgres-backed-via-real-file-SQLite (5 threads, barrier-synchronized) — exactly one winner each |
| `TestSecurity` | 2 | Structural user-scoping requirement; cross-user reuse attempt is isolated, never grants access to the victim's record |
| `TestToolOrchestratorIntegration` | 4 | Default (unconfigured) orchestrator is completely unaffected; configured repository blocks a same-user duplicate; a failed execution releases the key; restart recovery still blocks a duplicate |
| `TestDatabaseFailure` | 1 | `try_reserve()` raises `DatabaseUnavailableError`, never a raw driver exception |

Plus updates to the Step 12.3 migration test file (`tests/test_db_migrations.py`): the duplicate-idempotency
test was adjusted for the new composite key (not weakened — it still asserts rejection, now correctly
scoped), and a new test proves cross-user isolation holds at the raw SQL/constraint level independent of any
Python repository code.

### Results

```
python -m unittest tests.test_idempotency_repository_postgres -v
Ran 15 tests in 1.516s — OK (15/15 passed)

python -m unittest tests.test_tool_orchestrator tests.test_tool_reliability tests.test_security_red_team tests.test_concurrency tests.test_db_migrations -v
Ran 108 tests — OK
```

Every one of Phase 4/10/11's own pre-existing idempotency-adjacent tests
(`test_idempotency_conflict_on_duplicate_request_id`,
`test_concurrent_same_request_id_executes_exactly_once`,
`test_idempotency_duplicate_increments_counter`) passes unchanged, confirming the default path is truly
untouched.

### Full Regression

```
python -m unittest discover -s tests -p "test_*.py"
Ran 662 tests in 30.133s
FAILED (errors=1)
```

- **662 = 646 (Phase 12.8 baseline) + 15 new + 1 new migration test.** No existing test's outcome changed.
- **1 pre-existing error**, unchanged: `test_retriever.py`'s `sentence_transformers` gap (documented in
  every prior Phase 12 report).

**This completes persistence for all four Step-12.1-identified data types** (sessions, memory, audit,
idempotency). Step 12.10 (Integrate Persistent Repositories into `build_conversation_manager()`'s
`DATABASE_URL`-gated wiring) has not been done yet — every repository built in Steps 12.5-12.9 is
independently tested but not yet wired into the live application factory.

`PHASE 12.9 COMPLETE`

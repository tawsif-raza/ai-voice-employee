# Phase 12.7 — Persistent Memory Report

## Objective

Move durable memory from process-local storage to PostgreSQL, preserving `MemoryManager`'s existing
ownership/authorization/privacy/retention behavior exactly.

## Preserve

`MemoryManager` remains responsible for ownership, authorization, privacy, retention semantics, and memory
behavior — verified, not assumed: `memory_manager.py`'s public method signatures are unchanged, and every
existing test in `tests/test_memory_manager.py` (19 tests) passes unchanged against the new repository
(re-run below). The one change to `memory_manager.py` (see "Security gap found and fixed" below) adds a
check inside an existing method; it does not move any decision into the repository.

## Implementation

New file: **`src/agent/memory_repository_postgres.py`** — `PostgresMemoryRepository`, implementing exactly
`MemoryManager`'s required interface: `get`, `save`, `delete`, `list_for_user`. No new capability was
invented — no `list_all()`, no `query()`, nothing beyond what `MemoryManager` already calls today
(`test_no_unscoped_list_all_method_exists` asserts this directly).

Same patterns as Phase 12.5's session repository, applied consistently:
- `save()` uses `db.py`'s shared `upsert_row()` — one atomic `INSERT ... ON CONFLICT (id) DO UPDATE`.
- `_row_to_record()` reuses `session_repository_postgres._aware()` to fix the same SQLite-naive-datetime
  gap Phase 12.5 discovered (`created_at`/`updated_at`/`expires_at` re-attached to UTC when read back from
  SQLite) — one shared fix, not two divergent ones.
- `list_for_user()` is a single indexed (`ix_memory_records_user_id`, Step 12.3) `SELECT ... WHERE user_id
  = :user_id` — the only list query this repository needs, matching the audit's finding that this is the
  only list access pattern `MemoryManager` has ever required.

## Security Gap Found and Fixed (backend-independent)

While tracing plan.md's required "User B attempts to modify it → DENY" test through the actual code, I found
`MemoryManager.persist_memory()` had **no ownership check on write** — it always upserted by `record.id`
with no verification that the caller's `record.user_id` matched whoever already owned that id. This is a
pre-existing gap in `memory_manager.py` itself, present since Phase 5, **not something the Postgres backend
introduced** (the in-memory `MemoryRepository.save()` has the exact same unconditional upsert-by-id
behavior) — but it directly blocks the security test this step explicitly requires.

Fix: `persist_memory()` now checks whether `record.id` already exists and, if so, whether the existing
owner matches `record.user_id`; a mismatch raises the new `MemoryOwnershipError` and records a
`CROSS_USER_ACCESS_ATTEMPT` security event (mirroring `remove_memory()`'s existing ownership-check pattern
exactly — same discipline, not a new one). This is a **generic fix in `MemoryManager`**, so it protects
both the in-memory and Postgres-backed repository equally, and required no repository-specific code.

Verified not to regress the one existing test that intentionally exercises same-id reuse
(`test_duplicate_memory_ids_do_not_raise`, always same user) — still passes unchanged, both in
`tests/test_memory_manager.py` (in-memory) and as
`TestCrossUserSecurity.test_same_user_can_still_update_own_memory` (persisted) in the new test file.

## Security Tests (plan.md Step 12.7's exact three)

| Required test | Implemented as | Result |
|---|---|---|
| User A creates memory; User B attempts to **read** it → DENY | `test_user_b_cannot_read_user_a_memory` | User B's `list_allowed_memory()` returns `[]` |
| User B attempts to **modify** it → DENY | `test_user_b_cannot_modify_user_a_memory` | `MemoryOwnershipError` raised; User A's original value unchanged |
| User B attempts to **delete** it → DENY | `test_user_b_cannot_delete_user_a_memory` | `remove_memory()` returns `False`; record still present |
| Error behavior doesn't reveal existence | `test_error_does_not_reveal_existence_via_return_type` | `remove_memory()` returns the identical `False` for "doesn't exist" and "belongs to someone else" — no distinguishing signal |

## Privacy

**Required**: persistence must not bypass `PrivacyService`; PostgreSQL being "private infrastructure" is not
a reason to store raw sensitive data.

- `test_restricted_field_denied_persistence_still_enforced` — a `medical_condition`-keyed record is denied
  by `PolicyEngine.evaluate_privacy()` exactly as before; never reaches the repository.
- `test_pii_in_value_never_reaches_the_database` — a value containing a phone number is denied by
  `PolicyEngine.evaluate_pii()` (context `MEMORY`, `PHONE → RESTRICT` per `configs/policies/privacy.yaml`)
  and confirmed absent from the repository entirely by reading straight from
  `PostgresMemoryRepository.get()`, bypassing `MemoryManager`.
  **Note on what this test found**: I initially expected a REDACT-worthy finding to be stored as a redacted
  copy (per `persist_memory()`'s own docstring/code branch for `action in ("REDACT", "RESTRICT")`). Tracing
  `PolicyEngine.evaluate_pii()` showed `allowed = (worst_action == "ALLOW")` — every non-`ALLOW` action
  (`REDACT`, `RESTRICT`, `BLOCK` alike) already sets `allowed=False`, so `persist_memory()`'s redact branch
  is currently unreachable for any PII finding: a PII-flagged value is denied outright, never redacted-and-
  stored. This is **pre-existing `PolicyEngine`/Phase 6 behavior** (identical to `ToolOrchestrator`'s own
  PII gate at `tool_orchestrator.py` step 2.5, which treats `evaluate_pii()`'s `allowed` the same way) —
  not something this step changed, and arguably a *stronger* privacy guarantee than redaction would be
  (outright denial vs. a redacted copy still being stored). Flagged here as a genuine, pre-existing
  inconsistency between `persist_memory()`'s dead redact-branch and its actual reachable behavior — not
  fixed in this step, since altering `PolicyEngine.evaluate_pii()`'s `allowed` semantics would ripple into
  `ToolOrchestrator` and is out of this step's scope ("Do not invent new memory capabilities" /
  persistence-only step).

## Restart Test

**Required**: create memory → restart application → retrieve memory → survives.
`TestRestartRecovery.test_memory_survives_simulated_restart`: memory is persisted via one
`Database`/`MemoryManager` stack; a completely new stack (fresh `Database`/`Engine`/repository/manager, no
Python object reused) is constructed against the same underlying SQLite file and successfully retrieves the
same record — same "genuine restart, not an in-process cache" simulation Phase 12.6 already established.

## Tests

New file: **`tests/test_memory_repository_postgres.py`** — 18 tests.

| Group | Tests | Covers |
|---|---|---|
| `TestRepositoryRoundTrip` | 7 | get/save/delete/list_for_user round-trip; upsert semantics; no unscoped list method |
| `TestCrossUserSecurity` | 5 | The three mandatory DENY tests + existence-non-revealing + same-user update still works |
| `TestPrivacyNotBypassed` | 2 | Restricted field still denied; PII-laden value never reaches the database |
| `TestRestartRecovery` | 1 | Memory survives a simulated process restart |
| `TestDatabaseFailure` | 3 | `get`/`save`/`list_for_user` all raise `DatabaseUnavailableError` on connection failure, never a raw driver exception |

### Results

```
python -m unittest tests.test_memory_repository_postgres -v
Ran 18 tests in 3.368s — OK (18/18 passed)

python -m unittest tests.test_memory_manager -v
Ran 19 tests in 0.128s — OK (unchanged — confirms the ownership-guard fix doesn't regress existing behavior)
```

### Full Suite

```
python -m unittest discover -s tests -p "test_*.py"
Ran 626 tests in 20.875s
FAILED (errors=1)
```

- **626 = 608 (Phase 12.6 baseline) + 18 new.** No existing test was edited; no existing test's outcome
  changed.
- **1 pre-existing error**, unchanged: `test_retriever.py`'s `sentence_transformers` gap (documented in
  every prior Phase 12 report).

**Audit persistence was not implemented in this step** — `AuditLogger` continues to use its existing
in-memory `AuditRepository` exclusively (Step 12.8).

`PHASE 12.7 COMPLETE`

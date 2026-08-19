# Phase 12.3 — PostgreSQL Schema & Migrations Report

**Status: schema-only. No repository was implemented. No integration into SessionManager, MemoryManager,
AuditLogger, or ToolOrchestrator. Not committed.**

## First

Read `PHASE_12_1_PERSISTENCE_AUDIT.md` and `PHASE_12_2_DATABASE_FOUNDATION_REPORT.md` before starting. Every
table below traces directly to a real structure those documents already audited — no field was invented
without tracing it to `session_models.SessionState`, `memory_models.MemoryRecord`,
`observability_models.AuditEvent`/`SecurityEvent`, or (new in this phase) `ToolOrchestrator`'s previously
untyped idempotency `set()`.

No `users` table was added — this system's identity is already externally owned
(`identity.py`/`oidc_provider.py`'s OIDC-based `AuthContext`, per Phase 7/9); duplicating that as local user
storage would contradict this step's own instruction ("Do not duplicate the external OIDC identity system
unnecessarily") and nothing in the audited architecture needs it.

## Tables

### `sessions` (mirrors `session_models.SessionState`)

| Column | Type | Constraints |
|---|---|---|
| `session_id` | `String(64)` | **PK** |
| `user_id` | `String(128)` | nullable, indexed |
| `status` | `String(32)` | NOT NULL, `CHECK` in `('ACTIVE','WAITING_FOR_INPUT','WAITING_FOR_CONFIRMATION','COMPLETED','EXPIRED','FAILED')` |
| `created_at` / `updated_at` / `expires_at` | `DateTime(timezone=True)` | NOT NULL; `expires_at` indexed |
| `current_intent` | `String(128)` | nullable |
| `workflow_state` | `String(64)` | nullable |
| `pending_action` | `String(64)` | nullable |
| `pending_parameters` | `JSON` | NOT NULL, default `{}` |
| `confirmation_state` | `JSON` | NOT NULL, default `{}` |
| `metadata` | `JSON` | NOT NULL, default `{}` |

### `memory_records` (mirrors `memory_models.MemoryRecord`)

| Column | Type | Constraints |
|---|---|---|
| `id` | `String(64)` | **PK** |
| `user_id` | `String(128)` | NOT NULL, indexed |
| `category` | `String(32)` | NOT NULL, `CHECK` in `('PREFERENCE','WORKFLOW_CONTEXT','COMMUNICATION_PREFERENCE')` |
| `key` | `String(128)` | NOT NULL |
| `value` | `Text` | NOT NULL |
| `source` | `String(64)` | NOT NULL |
| `created_at` / `updated_at` | `DateTime(timezone=True)` | NOT NULL |
| `expires_at` | `DateTime(timezone=True)` | nullable |
| `metadata` | `JSON` | NOT NULL, default `{}` |

### `audit_events` (mirrors `observability_models.AuditEvent`; append-only)

| Column | Type | Constraints |
|---|---|---|
| `event_id` | `String(64)` | **PK** |
| `timestamp` | `DateTime(timezone=True)` | NOT NULL |
| `event_type` | `String(64)` | NOT NULL, indexed |
| `request_id` | `String(64)` | nullable, indexed |
| `conversation_id` / `session_id` | `String(64)` | nullable |
| `actor` | `String(128)` | nullable |
| `action` | `String(64)` | nullable |
| `resource` | `String(128)` | nullable |
| `outcome` | `String(32)` | NOT NULL |
| `policy` | `String(64)` | nullable |
| `reason` | `Text` | nullable |
| `metadata` | `JSON` | NOT NULL, default `{}` |

Indexes on `event_type` and `request_id` deliberately match `AuditRepository.list_events()`'s exact two
filter parameters — no index was added speculatively.

### `security_events` (mirrors `observability_models.SecurityEvent`)

| Column | Type | Constraints |
|---|---|---|
| `event_id` | `String(64)` | **PK** |
| `timestamp` | `DateTime(timezone=True)` | NOT NULL |
| `type` | `String(64)` | NOT NULL, indexed |
| `severity` | `String(16)` | NOT NULL, `CHECK` in `('INFO','LOW','MEDIUM','HIGH','CRITICAL')` |
| `request_id` | `String(64)` | nullable |
| `actor` | `String(128)` | nullable |
| `resource` | `String(128)` | nullable |
| `outcome` | `String(32)` | NOT NULL |
| `reason` | `Text` | NOT NULL |

### `idempotency_records` (new in Phase 12 — see audit §7)

| Column | Type | Constraints |
|---|---|---|
| `request_id` | `String(64)` | **PK** |
| `action` | `String(64)` | NOT NULL |
| `result_status` | `String(32)` | NOT NULL |
| `executed_at` | `DateTime(timezone=True)` | NOT NULL |

No predecessor class existed for this table — `ToolOrchestrator` previously tracked only a bare `set()` of
executed `request_id`s with no attached action/outcome/timestamp (audit §3/§7). `request_id` as the primary
key gives Step 12.9's planned `INSERT ... ON CONFLICT (request_id) DO NOTHING RETURNING request_id` pattern
(audit §10) a real unique constraint to conflict against — this is the schema decision that makes that
concurrency-safe pattern possible later, not something this step implements yet (still schema-only).

## Relationships

No foreign keys were added between tables. Per the audit (§9/§11): "Security-sensitive relationships should
have database constraints where practical, but authorization remains application-level" — and, concretely,
none of the existing manager classes ever join across these tables (each repository's methods are scoped to
exactly one table). A `session_id` value appearing in `audit_events` is not FK-constrained to `sessions`
because audit events must always be recordable even for a session that has since expired/been deleted
(`audit.py`'s own "best-effort, never blocks the underlying action" posture) — an FK would make audit writes
fail exactly when they're most needed (after a session is gone).

## Indexes

Every index traces to an actual existing query pattern, not speculation:

- `ix_sessions_user_id`, `ix_sessions_expires_at` — session lookup by user; expiry sweep (a future scheduled
  job scanning for `expires_at < now()`, not yet implemented, but the column this step's audit already
  flagged as needing an index for that purpose).
- `ix_memory_records_user_id` — the *only* list query `MemoryRepository` has (`list_for_user()`).
- `ix_audit_events_event_type`, `ix_audit_events_request_id` — `AuditRepository.list_events()`'s exact two
  filter parameters.
- `ix_security_events_type` — mirrors `event_type`'s indexing rationale for the analogous field.

## Constraints

- `CHECK` constraints enforce every enum-shaped column (`sessions.status`, `memory_records.category`,
  `security_events.severity`) at the database layer — a defense-in-depth backstop, not a replacement for the
  Python-level enum validation `SessionState`/`MemoryRecord`/`SecurityEvent` already perform. Verified by
  `tests/test_db_migrations.py::TestConstraintsRejectInvalidData` (see Tests below).
- Primary-key uniqueness is the only uniqueness rule needed anywhere — no table has a secondary unique
  constraint, matching the audit's finding that no repository method requires one.

## Migrations

**Tool**: Alembic (installed in Step 12.2), initialized at the repo root (`alembic.ini` + `alembic/`).

`alembic/env.py` was customized (not left at its generated default) to:
1. Import `src/agent/db_models.py`'s `Base.metadata` as `target_metadata`, enabling `--autogenerate`.
2. Resolve the connection URL through `src/agent/db.py`'s `load_database_config()` — the exact same
   `DATABASE_URL`/`PERSISTENCE_MODE` resolution the running application uses — rather than a second,
   independent URL source living only in `alembic.ini`. `alembic.ini`'s own `sqlalchemy.url` line is left at
   its generated placeholder (`driver://user:pass@localhost/dbname` — never a real credential) and is only
   reached if this override path itself fails, in which case the failure is re-raised rather than silently
   falling back to that placeholder and migrating the wrong database.

**Initial migration**: `alembic/versions/0c8ab0c30f96_initial_schema_....py` — one `op.create_table()` per
table above plus their indexes, generated via `alembic revision --autogenerate` against the models in
`src/agent/db_models.py`, then reviewed by hand (not blindly accepted) to confirm every `CheckConstraint`
rendered as valid, dialect-portable SQL.

### Verified: clean database → migration → schema → connection

```
DATABASE_URL="sqlite:///./alembic/_verify.db" python -m alembic upgrade head
  -> Running upgrade  -> 0c8ab0c30f96, initial schema: ...
```
Confirmed via direct `sqlite_master` inspection: all 5 tables and all 6 non-PK indexes present.
Confirmed CHECK constraints reject an invalid `status`/`category`/`severity` value and that duplicate
primary keys are rejected, while well-formed rows are accepted (manual verification, then codified as
`tests/test_db_migrations.py`, run automatically — not just eyeballed once).

### Verified: migration → rollback → clean state

```
DATABASE_URL="sqlite:///./alembic/_verify.db" python -m alembic downgrade base
  -> Running downgrade 0c8ab0c30f96 -> , initial schema: ...
```
Confirmed all 5 application tables are gone after downgrade (only Alembic's own `alembic_version`
bookkeeping table remains), and that upgrading again from that clean state succeeds without error —
downgrade doesn't leave the migration graph in a state a subsequent upgrade can't recover from.

**Tables were never created manually outside the migration system** — every table in every test and every
manual verification pass in this step came from `alembic upgrade head` against `db_models.py`'s metadata,
never a hand-written `CREATE TABLE`.

## Tests

New file: **`tests/test_db_migrations.py`** — 11 tests. Each test gets its own throwaway SQLite file
(`tempfile.mkstemp`, always removed in `tearDown`, whether the test passes or fails) and a correctly-scoped
`DATABASE_URL` set for the duration of that test only (restored afterward) — no cross-test state leakage.

| Group | Covers |
|---|---|
| `TestCleanDatabaseMigration` (3) | Migration succeeds against a genuinely nonexistent file; all 5 expected tables exist after; all 6 expected indexes exist after |
| `TestConstraintsRejectInvalidData` (6) | Invalid `status`/`category`/`severity` each rejected; duplicate `sessions.session_id` rejected; duplicate `idempotency_records.request_id` rejected; a well-formed row is accepted for every constrained table (proves the constraints aren't over-broad) |
| `TestRollback` (2) | `downgrade base` removes every application table; downgrade-then-upgrade-again produces the exact same clean schema |

### Results

```
python -m unittest tests.test_db_migrations -v
Ran 11 tests in 1.974s — OK (11/11 passed)
```

### Full Suite

```
python -m unittest discover -s tests -p "test_*.py"
Ran 588 tests in 16.436s
FAILED (errors=1)
```

- **588 = 577 (Phase 12.2 baseline) + 11 new.** No existing test was modified; no existing test's outcome
  changed.
- **1 pre-existing error**, unchanged and already documented in Phases 12.1/12.2:
  `test_retriever.TestRetriever.setUpClass` — `ModuleNotFoundError: No module named 'sentence_transformers'`
  (unrelated ML dependency gap, not a Phase 12 regression).

**No repository was integrated.** `db_models.py` is imported only by `alembic/env.py` and
`tests/test_db_migrations.py`'s SQL-level verification — `SessionManager`, `MemoryManager`, `AuditLogger`,
and `ToolOrchestrator` are entirely unaware this schema exists, exactly as this step requires.

## Files Added

- `src/agent/db_models.py` — SQLAlchemy ORM table definitions (schema only, no repository logic).
- `alembic.ini`, `alembic/env.py` (customized), `alembic/script.py.mako`, `alembic/README` — migration
  tooling scaffolding.
- `alembic/versions/0c8ab0c30f96_initial_schema_....py` — the initial migration.
- `tests/test_db_migrations.py` — 11 new tests.

`PHASE 12.3 COMPLETE`

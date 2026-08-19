# Phase 12.2 — Database Foundation & Configuration Report

**Status: foundation only. No repositories, no schema/migrations, no integration into SessionManager/
MemoryManager/AuditLogger/ToolOrchestrator. Not committed.**

## Selected Database Stack

Per `PHASE_12_1_PERSISTENCE_AUDIT.md` §12/§13 and this step's own instruction ("If no database stack exists,
use the smallest production-appropriate PostgreSQL stack"):

- **SQLAlchemy 2.0.52** — engine/session/connection-pool layer. Chosen because it works identically (same
  API surface) against both PostgreSQL (production) and SQLite (offline tests), which is what makes this
  repo's "fully offline, stdlib-first" test convention achievable without a second, parallel test-only data
  layer.
- **psycopg2-binary 2.9.12** — the PostgreSQL driver. `-binary` specifically because it ships a prebuilt
  wheel (no C compiler required in this or any deployment environment), matching this repo's existing
  "prebuilt wheel over local compilation" bias implicit in every other pinned dependency.
- **Alembic 1.19.1** — installed now since it's tightly coupled to the SQLAlchemy engine this step defines,
  but **not used yet**: no migration scaffolding, no `alembic.ini`, no migration files exist after this step.
  That is Step 12.3's scope exactly, per plan.md's own step boundary ("Do NOT implement session persistence...
  yet" / Step 12.3's "This step is schema-only").

No second ORM, no second database abstraction was introduced — this is the only database stack in the
repository.

## Dependencies Added

`requirements.txt`:
```
SQLAlchemy==2.0.52
psycopg2-binary==2.9.12
alembic==1.19.1
```
Pinned to the exact versions installed and tested, matching every other dependency in this file's exact-pin
convention.

## Configuration

New module: **`src/agent/db.py`**.

Environment variables (all read via `os.environ` by default, injectable via an explicit `env` dict for
deterministic tests — same pattern `reliability_config.py`'s `config_path` parameter already uses for the
same reason):

| Variable | Default | Meaning |
|---|---|---|
| `PERSISTENCE_MODE` | `dev` | `dev` → in-memory repositories stay the default everywhere (this module isn't even consulted by existing callers). `production`/`postgres`/`postgresql` → requires `DATABASE_URL`. |
| `DATABASE_URL` | *(dev: `sqlite:///:memory:`)* | SQLAlchemy connection URL. **Required** (fails closed) when `PERSISTENCE_MODE` is a production alias. |
| `DB_POOL_SIZE` | `5` | SQLAlchemy pool size (ignored for SQLite — see below). |
| `DB_MAX_OVERFLOW` | `10` | SQLAlchemy max overflow connections. |
| `DB_POOL_TIMEOUT_SECONDS` | `30` | Seconds to wait for a pooled connection before erroring. |
| `DB_POOL_RECYCLE_SECONDS` | `1800` | Recycle a pooled connection after this many seconds (avoids stale-connection errors from a server-side idle timeout). |
| `DB_ECHO` | `false` | SQLAlchemy SQL statement logging — `true` for local debugging only. |

This mirrors `src/api/server.py`'s existing `AUTH_MODE` dev/production split exactly (same two-mode shape,
same "production requires explicit configuration or refuses to start" posture as `AUTH_MODE`'s own
"required production configuration is missing" fail-closed branch).

## Connection Architecture

```
load_database_config(env) -> DatabaseConfig (frozen dataclass; url + safe_url + pool tuning + mode)
        │
        ▼
Database(config)
        │
        ├── .engine            — the SQLAlchemy Engine (pooled for Postgres; unpooled for SQLite)
        ├── .health_check()    — SELECT 1 round-trip; raises DatabaseUnavailableError, never a raw driver exception
        ├── .session_scope()   — @contextmanager; one transaction per `with` block, commit-or-rollback, always closes
        └── .dispose()         — closes all pooled connections (test isolation / graceful shutdown)
```

- **One `Engine` per `Database` instance**, never a module-level global — matches this repo's existing
  "no hidden global state" convention (`PolicyEngine`, `AuditLogger`, etc. are all explicitly constructed
  and threaded through call sites, never singletons).
- **SQLite gets no pool-size/overflow tuning** (`_build_engine()`): those parameters are meaningless for
  SQLite's connection model and SQLAlchemy's SQLite dialect rejects them outright. An in-memory SQLite
  engine is created with `check_same_thread=False` so it can be shared safely within one test process (the
  same discipline this repo's own in-memory repositories already apply via explicit `threading.Lock`s).
- **PostgreSQL gets the full configured pool** plus `pool_pre_ping=True` (detects a dropped/stale connection
  before handing it to a caller, converting a would-be mid-transaction failure into a clean reconnect).
- `session_scope()` implements the exact transaction-boundary decision from
  `PHASE_12_1_PERSISTENCE_AUDIT.md` §9: one commit-or-rollback unit of work per call, always closing the
  session in a `finally`.

## Security Considerations (plan.md Step 12.2's explicit requirements)

1. **Never log database passwords / full connection strings / credentials / tokens.**
   `DatabaseConfig.safe_url` masks the password (`urlsplit`/`urlunsplit`, replacing the password component
   with `***`) and is the *only* URL form used in `health_check()`'s and `session_scope()`'s exception
   messages — the raw `url` field is never interpolated into any string this module produces. A malformed
   URL that can't even be parsed falls back to a fixed placeholder string
   (`"***MALFORMED_DATABASE_URL***"`) rather than risking echoing raw credential-shaped text.
   Verified by `TestCredentialSafety` and `TestConnectionFailure.test_connection_failure_message_never_contains_password`
   in `tests/test_db.py` — both assert the literal password value is absent from every observable string.
2. **Production must not silently fall back to in-memory persistence if PostgreSQL is unavailable.**
   `load_database_config()` raises `DatabaseConfigurationError` immediately when `PERSISTENCE_MODE` is a
   production alias and `DATABASE_URL` is missing/blank — it never substitutes the SQLite default in that
   branch. Note the scope of what this step can guarantee: `load_database_config()` is *configuration*
   validation (is a URL present at all); *reachability* validation is `Database.health_check()`, a separate,
   explicit call a production startup path must make and act on (Step 12.10's integration work) — this
   module provides both primitives but does not itself wire "call health_check() at app startup and refuse
   to serve if it fails," since no such startup path exists to wire into yet at this schema-free, repository-free
   step. This is flagged, not silently deferred, as remaining work below.
3. **Never hard-code credentials.** No credential value appears anywhere in `db.py`, `requirements.txt`, or
   `tests/test_db.py` — every test URL is either password-free (SQLite) or uses an obviously fake
   placeholder (`u:supersecret@127.0.0.1:1`, an address that is deliberately unroutable/refused, never a
   real reachable target).

## Tests

New file: **`tests/test_db.py`** — 23 tests, fully offline (SQLite only; no real network connection is ever
made — `TestConnectionFailure`'s "unreachable host" cases target `127.0.0.1:1`, refused immediately by the
local TCP stack, not a real remote server).

| Group | Covers |
|---|---|
| `TestConfigLoadingDefaults` (5) | No env → dev/in-memory SQLite; explicit dev `DATABASE_URL` honored; default pool values; `is_production()` |
| `TestProductionModeFailsClosed` (4) | Production mode without/with blank `DATABASE_URL` raises; `postgres` alias also fails closed; production mode with a URL succeeds |
| `TestInvalidConfiguration` (4) | Non-numeric / zero / negative pool settings all raise `DatabaseConfigurationError` |
| `TestCredentialSafety` (3) | Password masked in `safe_url`; password never appears in a configuration-error message; URL without a password passes through unchanged |
| `TestDatabaseConnection` (3) | `health_check()` succeeds against SQLite; `session_scope()` yields a working session and commits cleanly |
| `TestConnectionFailure` (2) | Unreachable target raises `DatabaseUnavailableError`, not a raw driver exception; password never leaks into that error either |
| `TestDatabaseIsolation` (2) | Two `Database` instances never share an engine/state; `DatabaseConfig` is genuinely immutable (frozen dataclass) |

### Results

```
python -m unittest tests.test_db -v
Ran 23 tests in 2.099s — OK (23/23 passed)
```

### Full Suite

```
python -m unittest discover -s tests -p "test_*.py"
Ran 577 tests in 12.850s
FAILED (errors=1)
```

- **577 = 554 (Phase 12.1 baseline) + 23 new** — every pre-existing test still passes unchanged; nothing in
  `db.py` was imported by, or altered the behavior of, any existing manager/component.
- **1 pre-existing error**, unchanged from the Phase 12.1 baseline:
  `test_retriever.TestRetriever.setUpClass` — `ModuleNotFoundError: No module named 'sentence_transformers'`
  (an ML dependency not installed in this dev environment; unrelated to persistence, unrelated to any change
  in this step). Documented, not hidden, per this project's standing discipline.

**No existing behavior regressed.** No existing file was modified except `requirements.txt` (additive: three
new dependency lines).

## Remaining Work (explicitly deferred, not forgotten)

- No table/ORM model exists yet (Step 12.3).
- No repository implementations exist yet (Steps 12.5-12.9) — `db.py` is infrastructure only, not yet reachable
  from `SessionManager`/`MemoryManager`/`AuditLogger`/`ToolOrchestrator`.
- No startup-time `health_check()` call is wired into `src/api/server.py` yet — per Security Considerations
  item 2 above, that wiring belongs to Step 12.10 (Integrate Persistent Repositories), once there is an
  actual production repository path for a failed health check to gate.
- This step's tests exercise SQLite exclusively; genuine PostgreSQL connectivity (a real server, not just a
  refused-connection failure path) remains unverified in this environment per
  `PHASE_12_1_PERSISTENCE_AUDIT.md`'s documented constraint (no reachable PostgreSQL, Docker engine not
  running) — carried forward as an open item for every subsequent Phase 12 step.

`PHASE 12.2 COMPLETE`

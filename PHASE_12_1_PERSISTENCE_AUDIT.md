# Phase 12.1 — Persistence Architecture Audit

**Status: AUDIT ONLY. No source code was modified. No dependencies were installed. No database tables were created.**

## Baseline

- `python -m unittest discover -s tests -p "test_*.py"`: **554 tests found, 553 passing, 1 pre-existing error**
  (`test_retriever.TestRetriever.setUpClass` — `ModuleNotFoundError: No module named 'sentence_transformers'`,
  an ML dependency not installed in this dev environment; unrelated to any Phase 3–11 source change, and
  unrelated to persistence work. `faiss-cpu` was confirmed importable after a `pip install` performed purely
  to verify the environment — no source or config file was touched to do this.)
- The plan's stated baseline ("563 tests, 563 passed") does not match what this checkout actually produces;
  554 is the real, currently-verified number and is the baseline this audit and all of Phase 12 will be
  measured against.
- No PostgreSQL server is reachable from this environment (`psql`/`pg_ctl` not on PATH). Docker Desktop is
  installed but its engine is not running and could not be started headlessly from this session. This has no
  bearing on 12.1 (no infra decisions are executed yet) but is recorded here because it directly shapes the
  Testing Strategy (§13) and Migration Strategy (§12) proposed below.

---

## 1. Current Architecture

```
FastAPI (src/api/server.py)
        │
        ▼
build_conversation_manager()  ── single wiring point (src/agent/conversation_manager.py:1113)
        │
        ▼
ConversationManager
   ├── PolicyEngine            (authorization/policy — YAML-config-driven, stateless)
   ├── ClinicalSafetyGuard/HandoffDetector (stateless, config-driven)
   ├── PrivacyService          (stateless — delegates to PolicyEngine.evaluate_pii())
   ├── SessionManager ────────► SessionRepository (in-memory dict)
   ├── MemoryManager  ────────► MemoryRepository  (in-memory dict)
   ├── ToolOrchestrator
   │      ├── ToolRegistry     (in-memory dict, built once at construction)
   │      ├── _executed_request_ids (in-memory set — idempotency)
   │      └── CircuitBreaker/RetryPolicy (in-memory counters, per-instance)
   ├── AuditLogger ───────────► AuditRepository (in-memory list, append-only)
   └── SecurityEventDetector ─► in-memory dict (auth-failure counters)
```

Every stateful component already sits behind a narrow repository-shaped interface
(`get`/`save`/`delete`, sometimes `list_for_user`/`list_events`) owned by a manager/service class that
holds the actual business rules. **No component currently persists anything across a process restart.**
All state listed above is lost on interpreter exit — including active sessions, pending confirmations,
durable memory, the audit trail, and idempotency records.

## 2. Current State Ownership

| State | Owning class | Storage today | Business rules live in |
|---|---|---|---|
| Sessions / workflow state / pending actions | `SessionManager` (`session_manager.py`) | `SessionRepository` (dict) | `SessionManager` |
| Durable user memory | `MemoryManager` (`memory_manager.py`) | `MemoryRepository` (dict) | `MemoryManager` + `PolicyEngine.evaluate_privacy()` |
| Audit events / security events | `AuditLogger` / `SecurityEventDetector` (`audit.py`) | `AuditRepository` (two lists) | `AuditLogger` (structure only — never a decision) |
| Idempotency (executed `request_id`s) | `ToolOrchestrator` (`tool_orchestrator.py:127`) | a bare `set()` on the instance | `ToolOrchestrator._invoke()` step 4 |
| Tool registry (which tools exist) | `ToolRegistry` (`tool_registry.py`) | dict, populated once at process start from `mock_tools.py` | N/A — intentionally static/code-defined, not user data |
| Auth failure counters | `SecurityEventDetector` | dict | `SecurityEventDetector` |
| Circuit breaker state | `CircuitBreaker` (`reliability.py`) | instance attributes | `CircuitBreaker` |

Every manager class already refuses to let its repository make a business decision — repositories are
pure storage today, in-memory or not. This is the exact discipline Phase 12 needs to preserve.

## 3. Current In-Memory State

Concretely, everything in the table above is a plain Python object (`dict`, `list`, `set`) guarded by a
`threading.Lock`/`RLock` for concurrent-access safety (see `session_manager.py`'s `SessionRepository`/
`SessionManager` docstrings, `memory_manager.py`'s `MemoryRepository`, `audit.py`'s `AuditRepository`,
`tool_orchestrator.py`'s `_executed_request_ids_lock`). None of it survives a restart; none of it is shared
across multiple process instances (relevant for any future horizontal scaling — today correctness relies on
there being exactly one `ConversationManager`/`ToolOrchestrator` instance per running process).

## 4. Existing Abstractions

The repository already follows the "manager owns rules, repository owns storage" split Phase 12 is asked to
extend to PostgreSQL, consistently across every Phase 5/8 component:

- `SessionRepository` — `get(session_id)`, `save(session)`, `delete(session_id)`.
- `MemoryRepository` — `get(memory_id)`, `save(record)`, `delete(memory_id)`, `list_for_user(user_id)`.
- `AuditRepository` — `append(event)`, `append_security_event(event)`, `list_events(...)`, `list_security_events()`.

All three are already constructor-injectable (`SessionManager(repository=...)`,
`MemoryManager(repository=...)`, `AuditLogger(repository=...)`) — every one of the 554 existing tests that
exercises these managers already passes a fresh repository instance or lets the default in-memory one be
constructed. **This means a PostgreSQL-backed repository implementing the same three methods per class is a
drop-in replacement with no change to the manager classes themselves.**

There is **no** existing database abstraction (no SQLAlchemy, no Alembic, no `psycopg2`/`asyncpg`, no
connection pool, no `Base`/`Session` pattern) anywhere in `src/`. `docs/DATABASE.md` is an explicit,
unfilled placeholder (`_Status: placeholder — architecture design phase not yet started._`). This
architecture audit is that design phase.

`ToolOrchestrator._executed_request_ids` is **not** currently behind a repository interface — it's a bare
set on the orchestrator instance. This is a gap Phase 12 must close (a proper `IdempotencyRepository`) since
idempotency state is exactly as durability-sensitive as session/memory state (a process restart today would
let a previously-executed, non-idempotent `request_id` be replayed and re-executed).

## 5. Required Persistent Data

Per plan.md's own Level 2/Level 3 split (already documented in `plan.md` Phase 5's Memory Model) plus the
Phase 8 audit trail and Phase 10 idempotency guarantee, the following **must** survive a process restart to
make the existing security guarantees meaningful in production:

1. **Sessions** (`SessionState`) — including `workflow_state`, `pending_action`, `pending_parameters`,
   `confirmation_state`. Losing this on restart currently means every in-flight
   `WAITING_FOR_CONFIRMATION` workflow silently vanishes rather than failing safe/expiring — acceptable for
   a single dev process, not acceptable once the service can restart/redeploy/scale independently of a
   user's conversation.
2. **Durable memory** (`MemoryRecord`) — the whole point of "durable" per plan.md Phase 5 §"Level 3".
3. **Audit events / security events** (`AuditEvent`, `SecurityEvent`) — an audit trail that doesn't survive
   a restart is not an audit trail; this is also plan.md Phase 8's own implicit expectation even though
   Phase 8 shipped in-memory only (documented as a known Phase 8 limitation in
   `PHASE_8_OBSERVABILITY_AUDIT_REPORT.md`).
4. **Idempotency records** (executed `request_id` → outcome) — required for Phase 10's "duplicate/replayed
   request never re-executes a destructive action" guarantee to hold across a restart, not just within one
   process's uptime.

## 6. Data That Should Remain Ephemeral

- **Circuit breaker state** (`reliability.py`'s `CircuitBreaker`) — deliberately process-local; persisting it
  would make circuit state stale/wrong the moment a new process starts with a clean dependency, and plan.md
  never asks for it. Restarting with a closed circuit is the *correct* fail-safe behavior.
- **Auth-failure counters** (`SecurityEventDetector._auth_failure_counts`) — a lightweight, best-effort
  signal (plan.md Step 8.17 explicitly: "not a SIEM"). Persisting this crosses into building a real
  brute-force-protection subsystem, which is out of scope for Phase 12 (a genuine future phase, not this
  one).
- **Turn context** (plan.md Phase 5's "Level 1") — conversation-turn-local inference the LLM needs for the
  *current* request only; there is no dedicated type for this in the codebase today (it's assembled ad hoc
  inside `ConversationManager.handle_turn()`), and plan.md is explicit that Level 1 "does not automatically
  become persistent memory."
- **`ToolRegistry` contents** — intentionally static and code-defined at process start
  (`mock_tools.build_default_tool_registry()`); persisting "which tools exist" would contradict Phase 4's
  core invariant that the model/config can never cause a new tool name to become resolvable.

## 7. Proposed Repository Interfaces

Each new repository keeps the exact method shape its in-memory predecessor already has, so manager classes
require **zero interface changes** — only their constructor's `repository=` argument changes at the
`build_conversation_manager()` wiring point.

```text
SessionRepository (existing ABC-shaped class, in-memory today)
    get(session_id) -> Optional[SessionState]
    save(session: SessionState) -> None
    delete(session_id) -> None
  + PostgresSessionRepository(SessionRepository)   # new, same 3 methods, SQLAlchemy-backed

MemoryRepository
    get(memory_id) -> Optional[MemoryRecord]
    save(record: MemoryRecord) -> None
    delete(memory_id) -> None
    list_for_user(user_id) -> list[MemoryRecord]
  + PostgresMemoryRepository(MemoryRepository)      # new

AuditRepository
    append(event: AuditEvent) -> None
    append_security_event(event: SecurityEvent) -> None
    list_events(event_type=None, request_id=None) -> list[AuditEvent]
    list_security_events() -> list[SecurityEvent]
  + PostgresAuditRepository(AuditRepository)         # new

IdempotencyRepository                                 # new interface — no in-memory predecessor exists
    has_executed(request_id: str) -> bool
    mark_executed(request_id: str, action: str, result_status: str) -> None
  + InMemoryIdempotencyRepository (extracted from ToolOrchestrator._executed_request_ids, default)
  + PostgresIdempotencyRepository                      # new
```

`ToolOrchestrator` gains an optional `idempotency_repository=` constructor argument (default: a new
`InMemoryIdempotencyRepository`, preserving exact current behavior byte-for-byte); its existing
`_executed_request_ids`/`_executed_request_ids_lock` become that default implementation's internals, not a
behavior change.

None of these interfaces expose a `query()`/`get_all()`/raw-SQL method — matching plan.md's explicit "no
`database.query(...)`-shaped method" instruction for MemoryManager, generalized to every repository.

## 8. Proposed PostgreSQL Schema

```sql
sessions (
    session_id            TEXT PRIMARY KEY,
    user_id               TEXT NULL,
    status                TEXT NOT NULL,
    created_at             TIMESTAMPTZ NOT NULL,
    updated_at             TIMESTAMPTZ NOT NULL,
    expires_at             TIMESTAMPTZ NOT NULL,
    current_intent         TEXT NULL,
    workflow_state         TEXT NULL,
    pending_action         TEXT NULL,
    pending_parameters     JSONB NOT NULL DEFAULT '{}',
    confirmation_state     JSONB NOT NULL DEFAULT '{}',
    metadata               JSONB NOT NULL DEFAULT '{}'
)
  INDEX (user_id), INDEX (expires_at)   -- expiry sweep, user lookup

memory_records (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL,
    category      TEXT NOT NULL,
    key           TEXT NOT NULL,
    value         TEXT NOT NULL,
    source        TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL,
    expires_at    TIMESTAMPTZ NULL,
    metadata      JSONB NOT NULL DEFAULT '{}'
)
  INDEX (user_id)   -- MemoryRepository.list_for_user() is the only list query

audit_events (
    event_id         TEXT PRIMARY KEY,
    timestamp         TIMESTAMPTZ NOT NULL,
    event_type        TEXT NOT NULL,
    request_id        TEXT NULL,
    conversation_id   TEXT NULL,
    session_id        TEXT NULL,
    actor             TEXT NULL,
    action            TEXT NULL,
    resource          TEXT NULL,
    outcome           TEXT NOT NULL,
    policy            TEXT NULL,
    reason            TEXT NULL,
    metadata          JSONB NOT NULL DEFAULT '{}'
)
  INDEX (event_type), INDEX (request_id)   -- matches list_events()'s two filters exactly

security_events (
    event_id     TEXT PRIMARY KEY,
    timestamp     TIMESTAMPTZ NOT NULL,
    type          TEXT NOT NULL,
    severity      TEXT NOT NULL,
    request_id    TEXT NULL,
    actor         TEXT NULL,
    resource      TEXT NULL,
    outcome       TEXT NOT NULL,
    reason        TEXT NOT NULL
)

idempotency_records (
    request_id     TEXT PRIMARY KEY,
    action         TEXT NOT NULL,
    result_status  TEXT NOT NULL,
    executed_at    TIMESTAMPTZ NOT NULL
)
```

No table stores raw credential material, and no table is designed to hold unrestricted clinical content —
`memory_records.value` is a free-text column but is only ever reached *after* `MemoryManager.persist_memory()`
has already run it through `PolicyEngine.evaluate_privacy()` and (if configured) `PrivacyService`/PII
redaction, exactly as today. The schema doesn't add a new place PII could leak that doesn't already exist in
the in-memory dict.

## 9. Transaction Boundaries

Every current manager method is a single logical unit of work already serialized by the manager's own lock
(`SessionManager._lock` is an `RLock` covering full read-modify-write sequences —
see `session_manager.py`'s own docstring explaining exactly why the repository's per-call lock alone isn't
enough). Each repository method below therefore maps to **one SQL transaction, committed or rolled back
within that single call**:

- `SessionRepository.save()` → `INSERT ... ON CONFLICT (session_id) DO UPDATE` in one transaction.
- `MemoryRepository.save()` → same upsert pattern.
- `AuditRepository.append()` → single-row `INSERT`, one transaction (already append-only, no update path).
- `IdempotencyRepository.mark_executed()` → single-row `INSERT ... ON CONFLICT DO NOTHING`, and
  `has_executed()`+`mark_executed()` together must be **one transaction** (see §10) to preserve
  `ToolOrchestrator`'s existing "concurrent same-`request_id` executes exactly once" guarantee
  (`tests/test_tool_reliability.py::TestConcurrentDuplicateRequests`).

No multi-row, multi-table transaction is required anywhere — this mirrors the existing single-repository,
single-lock-scope design; Phase 12 does not need to introduce cross-repository transactions (e.g. "save
session and append audit event atomically") because the codebase has never required that atomicity — audit
emission is explicitly best-effort and decoupled from the state change it describes (`audit.py`'s module
docstring: "an internal AuditLogger failure... is caught and swallowed, never propagated into the caller's
control flow").

## 10. Concurrency Risks

- **Idempotency check-then-insert race** (the one place true DB-level atomicity is load-bearing): today
  `ToolOrchestrator` closes this with a Python lock around the `set()` check-and-add
  (`_executed_request_ids_lock`, see `tool_orchestrator.py:415-431`). A PostgreSQL-backed
  `IdempotencyRepository` must replace that in-process lock with a database-level equivalent — either a
  single `INSERT ... ON CONFLICT (request_id) DO NOTHING RETURNING request_id` (atomic, and the `RETURNING`
  row tells the caller whether *this* call actually inserted first) or an explicit
  `SELECT ... FOR UPDATE` inside a transaction. The `INSERT ... ON CONFLICT ... RETURNING` form is strongly
  preferred: it needs no explicit row lock, works correctly under Postgres's default `READ COMMITTED`
  isolation, and is a single round trip. This is the one place where reusing this repository across
  multiple *processes* (not just multiple threads in one process, which is all today's lock protects
  against) actually matters — a future horizontally-scaled deployment is exactly the scenario the current
  in-process lock cannot cover.
- **Session read-modify-write races**: `SessionManager`'s own `RLock` already serializes all session
  mutation *within one process*. Once sessions persist to a shared database reachable by multiple processes,
  that guarantee weakens to "serialized per-process" unless the repository's `save()` uses an
  optimistic-concurrency check (e.g. `UPDATE ... WHERE session_id = ? AND updated_at = ?`) or the manager
  moves to `SELECT ... FOR UPDATE`. This is a genuine architectural gap Phase 12 must decide on explicitly
  (see §16 Risks) — for a single-process deployment (this repo's current actual deployment shape per
  `docker-compose.yml`, which runs exactly one `api` service) it is not yet a live bug, but the schema and
  repository should not foreclose the correct fix later.
- **Connection pool exhaustion under the existing thread-based tool timeout mechanism**:
  `ToolOrchestrator._execute_once()` runs each tool call on a raw `threading.Thread` (not a pool — see its
  own docstring on why `ThreadPoolExecutor` was rejected). A slow/blocked DB connection held by a repository
  call inside a tool implementation could tie up a pooled connection until FastAPI's own threadpool is
  saturated. Mitigated entirely by keeping repository calls fast and giving the SQLAlchemy pool a bounded
  `pool_size`/`max_overflow`/`pool_timeout` (Step 12.2) — not a new problem Phase 12 introduces, just one it
  must not make worse.

## 11. Security Risks

- **`DATABASE_URL` must never be logged.** Every existing DB-adjacent config in this repo (OIDC issuer/JWKS
  URLs) is read via `os.environ.get(...)` and never echoed back in an error message
  (`oidc_provider.py`'s own discipline: "never raised with credential material in its message" — same
  standard `identity.py`'s `AuthenticationError` already documents). The connection string contains a
  password; any exception path that might include it (e.g. a raw `SQLAlchemyError` string) must be
  sanitized before it reaches `AuditLogger`/logs, exactly like every other secret in this codebase already
  is.
- **Cross-user isolation must be re-verified at the repository layer, not just the manager layer.**
  Today `SessionManager.get_session()` and `MemoryManager.remove_memory()` enforce
  `user_id` ownership entirely in Python, against an in-memory dict that has no independent access control
  of its own. A PostgreSQL-backed repository does not need row-level security to preserve this (the manager
  layer's ownership check remains authoritative and sufficient, exactly as today), but the audit explicitly
  flags this: **no repository method should be added that allows fetching by anything other than the exact
  same keys the in-memory version supports** (`get(id)`, `list_for_user(user_id)`) — no unscoped
  `list_all()`/`query()` method, or the manager's ownership check becomes bypassable by a future caller that
  reaches for the "obviously more convenient" unscoped method.
- **SQL injection**: eliminated by construction by using SQLAlchemy Core/ORM with bound parameters
  throughout (no repository method should ever build a query by string interpolation) — the same "never
  hand-roll a security-relevant primitive" discipline `requirements.txt` already documents for JWT/crypto
  (`PyJWT`/`cryptography`, "never hand-rolled crypto/JWT parsing").
- **Migration files must never embed real credentials or seed real PII** — schema-only, matching this
  repo's existing config-file discipline (no secrets committed anywhere in `configs/`).

## 12. Migration Strategy

- Use **Alembic** (the de facto standard for SQLAlchemy, and the only realistic choice that keeps this to
  "the smallest production-appropriate PostgreSQL stack," per Step 12.2's own instruction) for versioned,
  reviewable schema migrations — one migration per table introduced in §8, run via
  `alembic upgrade head` in deployment tooling (`scripts/`, `docker/Dockerfile`/`docker-compose.yml`), never
  applied by application code at import time.
- **Rollout is additive, not a cutover**: every manager (`SessionManager`, `MemoryManager`, `AuditLogger`,
  `ToolOrchestrator`) keeps defaulting to its existing in-memory repository when no `DATABASE_URL` is
  configured — mirroring `AUTH_MODE`'s exact existing pattern in `src/api/server.py` (unset/dev → today's
  behavior unchanged; a new `PERSISTENCE_MODE`/`DATABASE_URL`-gated production mode → Postgres-backed). This
  is what makes "existing behavior preserved" (a Phase 12 completion criterion) achievable without a
  flag-day migration, and is why 554 existing tests, which all construct managers directly with no
  `DATABASE_URL` in their environment, need not change at all.
- No live data migration is required — there is no existing persisted data anywhere in this system to
  migrate (everything today is in-memory and lost on restart already).

## 13. Testing Strategy

**Constraint discovered during this audit, stated plainly**: this development environment has no reachable
PostgreSQL server, and Docker Desktop's engine is installed but not running and could not be started from
this non-interactive session. This does not block 12.1 (no code was written), but it directly shapes every
later step:

- The existing test suite is explicitly "fully offline, stdlib only" by this repo's own stated convention
  (see e.g. `test_session_manager.py`'s module docstring). Phase 12's new repository unit tests will follow
  the same rule by running against **SQLite** (`sqlite:///:memory:` via SQLAlchemy) as the lightweight,
  dependency-light test double for the exact same repository code path used against PostgreSQL in
  production — the repository classes will be written using only SQLAlchemy Core/ORM constructs that behave
  identically on both engines (no PostgreSQL-only SQL, `JSONB` handled via SQLAlchemy's engine-agnostic
  `JSON` type). This is the same "simplest thing that actually exercises the real code" philosophy this
  repo already applies everywhere else (in-memory repositories as the "simplest appropriate abstraction
  when nothing exists to build on," per `session_manager.py`'s own docstring) — SQLite is that same choice
  at the test layer, not a shortcut around it.
- Tests that require genuinely PostgreSQL-specific behavior (e.g. a real `INSERT ... ON CONFLICT ...
  RETURNING` race test, per §10) will be written to run against a real PostgreSQL instance and will be
  **skipped with a clear reason** (`unittest.skip`) when `DATABASE_URL` is not set in the test environment —
  never silently reported as passing, and this gap will be called out explicitly, not hidden, in every later
  phase's report until a CI/dev environment with real PostgreSQL is available to actually exercise them.
- Every existing manager-level test (`test_session_manager.py`, `test_memory_manager.py`,
  `test_tool_orchestrator.py`, `test_tool_reliability.py`, etc.) continues to run exactly as today, since
  they construct managers with the default in-memory repository — these tests are the regression net proving
  Phase 12 never changes *behavior*, only *durability*.
- New repository-level tests (one file per repository, e.g. `tests/test_session_repository_postgres.py`)
  will assert the SQLite-backed implementation satisfies the exact same contract the in-memory
  `SessionRepository` already satisfies (round-trip `save`/`get`, `delete`, `list_for_user` scoping) plus the
  idempotency atomicity property from §10.

## 14. Files Expected to Change

- `requirements.txt` — add SQLAlchemy, Alembic, and a PostgreSQL driver (psycopg2-binary or a modern
  equivalent), pinned per this repo's existing exact-pin convention.
- New: `src/agent/db.py` or `src/persistence/` (naming TBD at Step 12.2 — see §17) — engine/session
  factory, `DATABASE_URL` config loading, connection pooling.
- New: `src/persistence/models.py` (or similar) — SQLAlchemy table definitions matching §8.
- New: `alembic/` (or `migrations/`) directory + `alembic.ini` — migration scaffolding and one migration
  per table.
- New: `src/agent/session_repository_postgres.py`, `memory_repository_postgres.py`,
  `audit_repository_postgres.py`, `idempotency_repository.py` (interface + in-memory default, extracted
  from `tool_orchestrator.py`) — or co-located in existing files, decided at Step 12.2 based on which reads
  cleaner against this repo's existing "one manager + its repository per file" convention.
- `src/agent/tool_orchestrator.py` — add optional `idempotency_repository=` constructor parameter; internal
  behavior of `_invoke()` step 4 becomes a call into that repository instead of the raw set.
- `src/agent/conversation_manager.py`'s `build_conversation_manager()` — the single wiring point gains
  conditional construction of Postgres-backed repositories when `DATABASE_URL` is configured, passed into
  the existing `SessionManager(repository=...)`, `MemoryManager(repository=...)`, `AuditLogger(repository=...)`
  constructor arguments that already exist today.
- `docker/docker-compose.yml` — add a `postgres` service (and a `DATABASE_URL` environment entry for the
  `api` service), matching this file's existing profile-based structure.
- `docs/DATABASE.md` — filled in (currently an explicit placeholder) to document the schema, retention, and
  backup/recovery posture once implemented.
- New test files per §13.

## 15. Files That Should NOT Change

- `session_models.py`, `memory_models.py`, `observability_models.py`, `action_models.py` — the typed domain
  models themselves. Persistence is a storage concern; these dataclasses' shape is already the serialization
  contract (`to_dict()` methods already exist on every one of them) and needs no change.
- `policy_engine.py`, `privacy_service.py`, `identity.py`, `intent_engine.py` — stateless/config-driven,
  entirely out of scope for a persistence phase (plan.md's own instruction: "Repositories must only provide
  persistence... do NOT move business decisions into repositories").
- `SessionManager`, `MemoryManager`, `AuditLogger`'s **public method signatures** — every business rule
  (expiration semantics, cross-user denial, transition validation, PolicyEngine-gated writes) stays exactly
  where it is today; only the object each manager's `repository=`/`_repository` attribute points at changes.
- `tool_registry.py`, `mock_tools.py` — per §6, the tool registry stays code-defined/static, not persisted.
- Any of the 554 existing test files — Phase 12 must not need to edit a single existing test to keep passing
  (new tests are added; old ones are untouched), which is itself the proof that backward compatibility held.

## 16. Risks

1. **Multi-process session/memory races (§10)** are a real architectural question this audit surfaces but
   does not resolve: today's single-process deployment doesn't need optimistic concurrency control, but
   nothing about "PostgreSQL persistence" alone fixes a race that could appear the moment the `api` service
   is scaled to more than one replica. Recommendation: document this explicitly as deferred (not silently
   ignored) rather than over-engineer optimistic locking for a deployment shape that doesn't exist yet in
   this repo's actual `docker-compose.yml`.
2. **No live PostgreSQL in this environment** means later steps' "run the tests" instruction can only be
   fully honored against SQLite, with PostgreSQL-specific tests explicitly skipped and disclosed (§13) rather
   than claimed as passing. This will be repeated verbatim in every later Phase 12 report until it's no
   longer true, per this project's own "never claim tests passed unless they were actually executed"
   discipline.
3. **`test_retriever.py`'s pre-existing `sentence_transformers` gap** is unrelated to persistence but will
   continue to show up in every full-suite run through Phase 12; each later report will continue to document
   it as pre-existing/environment-only rather than re-litigating it as a Phase 12 regression.
4. **Idempotency repository is new, not a straight lift** — unlike Session/Memory/Audit, there is no
   existing `IdempotencyRepository`-shaped class to mirror; §7/§10's design is this audit's own proposal and
   should get the most scrutiny/tests of the four in Step 12.9, since it's the one place true DB-level
   atomicity is load-bearing for an existing security guarantee (Phase 10/11's replay-attack protection).
5. **JSONB portability**: SQLAlchemy's generic `JSON` type works on both SQLite and PostgreSQL but SQLite
   stores it as text with looser querying support — acceptable since no repository method here ever queries
   *inside* a JSON column (only whole-row reads/writes), but worth stating explicitly so a future author
   doesn't add a `WHERE metadata->>'x' = ...`-shaped query that would silently behave differently across
   engines.

## 17. Step-by-Step Phase 12 Plan

1. **12.2 — Database Foundation & Configuration**: `DATABASE_URL`-driven SQLAlchemy engine/session factory
   (pooled), test configuration (SQLite), no repository/table code yet.
2. **12.3 — PostgreSQL Schema & Migrations**: Alembic scaffolding + one migration per table from §8, applied
   against SQLite in tests, documented as intended-for-Postgres.
3. **12.5 — Persistent Session Repository**: `PostgresSessionRepository` implementing exactly
   `SessionRepository`'s 3 methods; `SessionManager` unchanged; new tests mirror
   `test_session_manager.py`'s existing contract tests against the new repository.
4. **12.6 — Persistent Pending Confirmations**: covered by 12.5 (confirmation state lives on `SessionState`
   already, not a separate table) — this step verifies the `WAITING_FOR_CONFIRMATION`/expiry interaction
   (plan.md Step 5.5's "expired pending action must not execute") holds against the persisted repository too.
5. **12.7 — Persistent Memory**: `PostgresMemoryRepository`, same pattern as 12.5.
6. **12.8 — Persistent Audit Events**: `PostgresAuditRepository`, same pattern.
7. **12.9 — Persistent Idempotency**: new `IdempotencyRepository` interface + in-memory default (extracted
   from `ToolOrchestrator`, zero behavior change) + Postgres implementation using
   `INSERT ... ON CONFLICT ... RETURNING` per §10; the concurrency test this needs is the most important new
   test in all of Phase 12.
8. **12.10 — Integrate Persistent Repositories**: wire all four into `build_conversation_manager()` behind
   `DATABASE_URL`, preserving the in-memory default exactly.
9. **12.11 — Persistence Failure Testing**: simulated repository failures (connection error, malformed
   row) — confirm §"required vs optional" failure posture from plan.md Step 5.12 (session/idempotency
   failures fail closed; audit failures stay best-effort, unchanged from today).
10. **12.12 — Persistence Recovery Verification**: process-restart simulation (new manager/repository
    instances against the same SQLite file) proving state survives.
11. **12.13 — Persistent Security Regression**: re-run the existing cross-user isolation and LLM
    trust-boundary regression suites against the persisted repositories, unchanged expected outcomes.
12. **12.14 — Persistence Performance Baseline**: basic timing of repository operations against SQLite (and
    against real PostgreSQL if/when reachable) — documented as a baseline, not a guarantee, given §"No live
    PostgreSQL" above.
13. **12.15 — Final Verification & Baseline Update**: full suite run, updated test-count baseline, final
    Phase 12 report.

---

## Summary

- **Files inspected**: `session_manager.py`, `session_models.py`, `memory_manager.py`, `memory_models.py`,
  `audit.py`, `observability_models.py`, `conversation_manager.py` (full + `build_conversation_manager()`),
  `tool_orchestrator.py`, `tool_registry.py`, `action_models.py`, `identity.py`, `privacy_service.py`,
  `policy_engine.py` (signatures), `reliability_config.py`, `requirements.txt`, `docs/DATABASE.md`,
  `docker/docker-compose.yml`, `docs/adr/` listing, `configs/` listing, `src/api/server.py` (env-var
  conventions), plus a full test-suite run (554 tests).
- **Architecture finding**: the codebase already follows a clean manager/repository split for every stateful
  component except idempotency (currently a bare set inside `ToolOrchestrator`); this makes Phase 12
  additive rather than a rewrite — new repository implementations, no manager-class interface changes.
- **Persistence candidates**: sessions, durable memory, audit/security events, idempotency records.
  Explicitly NOT persisted: circuit breaker state, auth-failure counters, turn context, tool registry.
- **Risks**: no live PostgreSQL in this environment (testing strategy adapts via SQLite + explicit skips);
  multi-process session/memory races are a known, explicitly-deferred gap; the idempotency repository is new
  design, not a lift-and-shift, and needs the most test scrutiny.
- **Proposed next step**: proceed to Phase 12.2 (Database Foundation & Configuration) — engine/config only,
  no repository or schema code yet, per that step's own scope boundary.

`PHASE 12.1 COMPLETE — AUDIT ONLY`

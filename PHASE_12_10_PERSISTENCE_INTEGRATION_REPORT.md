# Phase 12.10 — Integrate Persistent Repositories Report

## Objective

Wire the production application services to PostgreSQL repositories while preserving existing service
interfaces and security boundaries.

## Services Integrated

`SessionManager`, `MemoryManager`, `AuditLogger`, the confirmation workflow (via `SessionManager`'s existing
`try_consume_pending_confirmation()`, unchanged since Step 12.6), and `ToolOrchestrator`/idempotency — all
five wired at `build_conversation_manager()`, the repository's single application-assembly point
(`conversation_manager.py:1113`). **No business logic was rewritten** — every manager's method signatures
and internal decision logic are byte-for-byte unchanged from Steps 12.5-12.9; this step only changes what
object each manager's `repository=`/`idempotency_repository=` constructor argument receives.

## Target Architecture — Achieved

```
ConversationManager
       ↓
SessionManager / MemoryManager / AuditLogger / ToolOrchestrator   (Service)
       ↓
SessionRepository / MemoryRepository / AuditRepository / IdempotencyRepository   (Repository Interface)
       ↓
PostgresSessionRepository / PostgresMemoryRepository / PostgresAuditRepository / PostgresIdempotencyRepository
       ↓
PostgreSQL
```

## Implementation

New module-level function in `conversation_manager.py`: **`resolve_persistence_repositories(persistence_enabled=True)`**,
returning a `PersistenceRepositories` value object (`.database`, `.session`, `.memory`, `.audit`,
`.idempotency`). Extracted as its own function — not inlined into `build_conversation_manager()` — specifically
so it can be unit-tested directly: `build_conversation_manager()` as a whole requires a real LLM/torch and
cannot be invoked in this environment (confirmed by `test_server_api.py`'s own
`TestGracefulShutdown` docstring), but the persistence-wiring *decision* has nothing to do with the LLM.

`build_conversation_manager()` itself now calls this function once, near the top (before `AuditLogger` is
constructed, since `AuditLogger` needs `persistence.audit` at construction time), and threads the four
repository attributes into the existing `SessionManager(repository=...)`, `MemoryManager(repository=...)`,
`AuditLogger(repository=...)`, and `ToolOrchestrator(idempotency_repository=...)` constructor arguments —
every one of which already existed from Steps 12.5-12.9; none of those signatures changed in this step.

## Production Mode

Mirrors `src/api/server.py`'s existing `AUTH_MODE` pattern exactly (same dev/production split, same
fail-closed posture):

- **`PERSISTENCE_MODE` unset/`"dev"`** (default) → `db.load_database_config().is_production()` is `False`,
  `resolve_persistence_repositories()` returns an all-`None` `PersistenceRepositories`, and every manager
  falls through to its existing in-memory repository default — **zero behavior change** for every caller
  that doesn't set `PERSISTENCE_MODE`, which is every one of this repository's 673 tests (none of which call
  `build_conversation_manager()` at all, but every *other* construction path — direct
  `ConversationManager(...)`, `SessionManager()`, etc. — is untouched either way).
- **`PERSISTENCE_MODE=production`** (or `"postgres"`/`"postgresql"`) → `Database.health_check()` is called
  synchronously, before `resolve_persistence_repositories()` returns. **If unreachable, it raises
  `DatabaseUnavailableError` and the exception propagates out of `build_conversation_manager()` — it is NOT
  caught anywhere in this path and there is no fallback to in-memory repositories.** Verified directly by
  `test_unreachable_database_raises_not_silently_falls_back` (an unreachable target raises) and
  `test_unreachable_database_error_never_contains_password` (the raised message carries no credential
  material, matching `db.py`'s existing `safe_url` discipline).

## Test Mode

`persistence_enabled=False` (mirrors every other `_enabled` flag `build_conversation_manager()` already has)
skips persistence resolution entirely regardless of `PERSISTENCE_MODE`, verified by
`test_persistence_enabled_false_ignores_production_mode` — confirming this escape hatch works and that
"tests may continue using in-memory fakes where appropriate" holds: nothing in this step forces any existing
or future unit test to require PostgreSQL. Every one of the 673 tests in this suite constructs its
collaborators directly (`SessionManager()`, `ConversationManager(...)`, etc.), never through
`build_conversation_manager()`, so this flag is a defensive convenience rather than something exercised by
the existing suite — documented honestly, not overstated.

## Verification — Required End-to-End Flows

New file: **`tests/test_persistence_integration.py`** — 11 tests, all run against **`PERSISTENCE_MODE=production`
with a SQLite `DATABASE_URL`** (this project's consistent, entirely-offline PostgreSQL stand-in throughout
Phase 12 — `db.load_database_config()`'s production/dev branching depends only on `PERSISTENCE_MODE`, never
on which URL scheme backs it).

| Required flow | Test | Result |
|---|---|---|
| **FAQ**: request → safety → policy → RAG → LLM → response → audit | `test_faq_flow_end_to_end_with_persisted_audit` | Real `ConversationManager` (fake LLM/retriever, real `HandoffDetector`/`PolicyEngine`) wired to `AuditLogger(repository=persistence.audit)`; LLM consulted exactly once, response not a handoff |
| **Clinical**: risky request → `ClinicalSafetyGuard` → LLM NOT called → handoff | `test_clinical_flow_end_to_end_llm_never_called` | Same wiring; `llm.calls == []`, `final["is_handoff"] is True` |
| **Appointment**: request → policy → confirmation → PostgreSQL → restart → confirmation → tool → exactly once | `test_appointment_workflow_confirmation_survives_restart_and_executes_exactly_once` | Full workflow against `SessionManager(repository=persistence.session)` + `ToolOrchestrator(idempotency_repository=persistence.idempotency)`: confirmation stored, consumed atomically, tool executes once, a second identical `request_id` is denied as `duplicate`, and a second confirmation-consumption attempt on the same session is denied |
| **Unauthorized**: User A → User B resource → DENY | `test_cross_user_session_access_denied_against_persisted_repository` | `SessionManager.get_session()` against the persisted repository returns `None` for a cross-user access attempt |

Additional tests: dev-mode defaults, the `persistence_enabled=False` escape hatch, production-mode wiring
returns real `Postgres*Repository` instances (type-checked, not just non-`None`), and the two fail-safe
database-failure cases already described above.

### Results

```
python -m unittest tests.test_persistence_integration -v
Ran 11 tests in 2.833s — OK (11/11 passed)
```

### Full Suite

```
python -m unittest discover -s tests -p "test_*.py"
Ran 673 tests in 32.334s
FAILED (errors=1)
```

- **673 = 662 (Phase 12.9 baseline) + 11 new.** No existing test was edited; no existing test's outcome
  changed. `tests.test_conversation_manager`, `tests.test_conversation_reliability`, and
  `tests.test_server_api` (83 tests) were specifically re-run and pass unchanged, confirming the
  `build_conversation_manager()` refactor introduced no regression in any code path other than the new
  persistence-resolution block itself.
- **1 pre-existing error**, unchanged: `test_retriever.py`'s `sentence_transformers` gap (documented in
  every prior Phase 12 report).

Per this step's own instruction — "Do not proceed to the final Phase 12 verification until all relevant
tests pass" — all relevant tests pass; proceeding to Step 12.11.

`PHASE 12.10 COMPLETE`

# Phase 12.8 — Persistent Audit Events Report

## Objective

Persist the existing audit system in PostgreSQL without changing its security/privacy architecture.

## Critical Constraint — Verified, Not Just Asserted

Required ordering preserved exactly:

```
Business Decision -> AuditLogger -> Privacy Sanitization -> AuditRepository -> PostgreSQL
```

**`AuditLogger` was not modified at all in this step.** `audit.py`'s `AuditLogger.record()` still sanitizes
`metadata` via `self._privacy_service.sanitize(safe_metadata, context="LOGGING")` *before* constructing the
`AuditEvent` and calling `self._repository.append(event)` — swapping in `PostgresAuditRepository` changes
only what `append()` does with an already-sanitized event, never when sanitization happens.

**The forbidden path was not introduced**: `PrivacyService -> AuditLogger -> PrivacyService` (the Phase 8
recursion hazard `AuditLogger.record()`'s own docstring documents — `PrivacyService.sanitize()` calls
`decide()` internally, and if that path itself emitted an audit event, it would recurse forever).
`audit_repository_postgres.py` **imports nothing from `privacy_service.py`, `privacy_models.py`, or
`pii_detector.py`** — grep-verifiable, not just asserted — and has no code path back into `PrivacyService`
at all. `TestPrivacySanitizationPreserved` in the new test file exercises the real, unmodified `AuditLogger`
+ real `PrivacyService` + the new Postgres repository together and confirms the *persisted row* (read back
independently, not the in-memory `AuditEvent` object) contains no raw PII.

## Implementation

New file: **`src/agent/audit_repository_postgres.py`** — `PostgresAuditRepository`, implementing exactly
`AuditLogger`'s required interface: `append`, `append_security_event`, `list_events`, `list_security_events`.
Append-only, matching the in-memory `AuditRepository` exactly — no `update()`/`delete()` method exists
(`test_append_is_append_only_no_update_method` asserts this directly). `EventType` was not redesigned; no
new event semantics were invented — the persisted schema (Step 12.3's `audit_events`/`security_events`
tables) already matched `AuditEvent`/`SecurityEvent` field-for-field, so this step required zero schema
changes.

## Security

Audit events represent actual application decisions, never client-supplied claims — unchanged, since this
step touches only how an already-constructed `AuditEvent` is stored, never how/when one is constructed.
`TestAuditCannotAlterBusinessDecisions` makes this concrete: even when the database is completely
unreachable, `AuditLogger.record()` and `SecurityEventDetector.record_*()` still never raise (the existing
Phase 8 best-effort `try/except` swallow, now proven against a real connection failure rather than just an
in-memory stub) — an audit-layer failure can never block, alter, or fail the business decision it's
recording after the fact.

## Querying

**Required filter set** (plan.md's exact wording: "correlation ID, user ID, session ID, event type, time
range"): the pre-existing in-memory `AuditRepository.list_events()` supported only `event_type` and
`request_id` (verified by grepping every call site in the repository — no existing caller needed more).
Added `actor` (user ID), `session_id`, `start_time`, `end_time` as additive, optional parameters to **both**
`AuditRepository.list_events()` (in-memory, `audit.py`) and `PostgresAuditRepository.list_events()` — kept
consistent across both, not just the new one. Every pre-Phase-12.8 call (`list_events(event_type=...)`,
`list_events(request_id=...)`, `list_events()`) is unaffected (`test_pre_phase_12_8_call_shape_still_works`).

**No unrestricted public audit endpoint was created** — nothing in `src/api/server.py` was touched; these
filters exist only at the repository/Python level, exactly matching the audit's explicit security
requirement (§11 of `PHASE_12_1_PERSISTENCE_AUDIT.md`: no method beyond the exact access patterns already
required).

## Tests

New file: **`tests/test_audit_repository_postgres.py`** — 20 tests.

| Group | Tests | Covers |
|---|---|---|
| `TestAuditPersistence` | 3 | append/list round-trip; append-only (no update/delete); security-event persistence |
| `TestQueryFiltering` | 7 | Each of the 5 required filters individually, combined, and a no-match case |
| `TestInMemoryRepositoryFiltersUnchangedBehavior` | 2 | Same new filters work identically on the in-memory repository; pre-existing call shapes unaffected |
| `TestPrivacySanitizationPreserved` | 3 | PII in metadata sanitized before the database ever sees it; no raw token/credential material persisted; `AuditEvent`'s schema structurally cannot carry prompt/completion/reasoning text |
| `TestAuditCannotAlterBusinessDecisions` | 2 | `AuditLogger`/`SecurityEventDetector` never raise even against a fully unreachable database |
| `TestRestartRecovery` | 1 | Audit trail survives a simulated process restart |
| `TestDatabaseFailure` | 2 | `append`/`list_events` raise `DatabaseUnavailableError`, never a raw driver exception |

### Results

```
python -m unittest tests.test_audit_repository_postgres -v
Ran 20 tests in 4.288s — OK (20/20 passed)
```

### Full Security Regression

```
python -m unittest tests.test_observability tests.test_security_red_team -v
Ran 70 tests — OK
```

All 9 of `test_security_red_team.py`'s `TestSecurityInvariants` cases pass unchanged, including
`test_invariant_9_audit_logger_records_only_real_decisions` — the exact invariant this step's Critical
Constraint depends on.

### Full Suite

```
python -m unittest discover -s tests -p "test_*.py"
Ran 646 tests in 25.172s
FAILED (errors=1)
```

- **646 = 626 (Phase 12.7 baseline) + 20 new.** No existing test was edited; no existing test's outcome
  changed.
- **1 pre-existing error**, unchanged: `test_retriever.py`'s `sentence_transformers` gap (documented in
  every prior Phase 12 report).

**Idempotency persistence was not implemented in this step** — `ToolOrchestrator` continues to track
executed `request_id`s in its in-process `set()` exclusively (Step 12.9).

`PHASE 12.8 COMPLETE`

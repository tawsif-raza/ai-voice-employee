# Phase 24 — Data and Privacy Hardening Report

## 1. Objective

Per plan.md's Phase 24: review the complete lifecycle of user data (transcripts, audio, conversations, audit events, logs, database, caches, temporary files, traces, provider requests/responses) — what is stored, why, for how long, and who can access it — and implement retention/deletion/redaction/safe-logging/data-minimization where justified, never merely because data is technically available.

## 2. Data Lifecycle Audit

| Data category | Stored where | Contains PII? | Retention today | Access control | Finding |
|---|---|---|---|---|---|
| Raw audio (call frames) | Nowhere — streamed in-process between Twilio WS ↔ STT ↔ TTS, never written to disk (verified: no `tempfile`/`.wav`/binary-write call anywhere in `src/voice/`) | N/A (never persisted) | None needed | N/A | None — already minimal by construction. |
| Transcripts / conversation text (per-turn `user_input`, LLM response) | Transiently in-process only (`CallSession.conversation_history`, an in-memory list on the telephony handler object) — never written to `SessionManager`'s persisted `Session`, `MemoryManager`, or `AuditLogger` (verified by reading `SessionState`'s fields — Section 2 of `PHASE_24` review — and `AuditEvent`'s fields, neither has a raw-text field) | Yes, while it exists | Lives only as long as the call/process; gone on call end or process restart | In-process only | None — already minimal; raw transcript text is never durably stored anywhere in this codebase today. |
| Session state (`SessionState`: `pending_parameters`, `metadata`, workflow state) | `SessionRepository` — in-memory (dev) or PostgreSQL `sessions` table (`PERSISTENCE_MODE=production`) | Limited — business identifiers (`appointment_id`, `order_id`), Twilio start-frame `custom_parameters`, `authenticated_caller` flag; no raw transcript | **Indefinite** in Postgres mode — `SessionManager._expire()` only flips `status=EXPIRED`, never deletes the row (already disclosed in `docs/DATABASE.md` §"Storage Hygiene": "no scheduled background cleanup daemon is mandatory... though periodic archiving may be configured") | `SessionManager.get_session()` enforces per-user ownership (Phase 7/11) | **Real gap, now partially fixed** — see Section 3. |
| Durable memory (`MemoryRecord`: user-approved facts) | `MemoryRepository` — in-memory (dev) or PostgreSQL `memory_records` table | Yes, by design (this is literally "remembered facts about the user," gated through `PolicyEngine`/`PrivacyService` before it can be proposed at all — Phase 5/6) | `expires_at` defaults to `None` ("keep indefinitely") unless explicitly set per-record; even when set, expiry is enforced only at read time, never auto-deleted | `MemoryManager` enforces per-user ownership; `MemoryRepositoryPostgres` deliberately exposes no unscoped/cross-user query method (documented design decision, Phase 12) | **Real gap, found and explicitly NOT auto-fixed** — see Section 4 (requires a decision this phase does not make unilaterally). |
| Audit events (`AuditEvent`/`SecurityEvent`) | `AuditRepository` — in-memory (dev) or PostgreSQL (`audit_events`/`security_events` tables) | PII-filtered through `PrivacyService` before storage (Phase 6/11) — actor/session/request IDs and structured `reason` text, never raw transcript | **Indefinite, append-only by design** — no `delete()`/`update()` method exists anywhere in the audit stack (verified by reading both `AuditRepository` implementations) | `list_events()` is a filtered query, never exposed via any HTTP route (Phase 12.8) | **Not fixed — deliberately.** See Section 5: retention here is a compliance/legal decision, not an engineering one. |
| Structured logs | stdout / whatever log aggregator the deployer configures — not this repository's concern | PII-redacted, log-injection-hardened (Phase 11 F-01) before emission | Deployer-controlled (outside this codebase) | Deployer-controlled | None new — already correctly minimized at the point this codebase controls (what gets logged), retention is infrastructure-layer and correctly out of scope here. |
| Caches | None found — no cache layer exists in this codebase (RAG's FAISS index is a build artifact, not a per-user cache; no Redis/memcached dependency exists) | N/A | N/A | N/A | None — nothing to audit; confirmed absent rather than assumed. |
| Temporary files | None found (Section above — audio; also no other `tempfile` usage in `src/` outside test fixtures) | N/A | N/A | N/A | None. |
| Distributed traces (OpenTelemetry) | Your configured OTLP exporter (Jaeger/etc.) when `TRACING_ENABLED=true`; off by default | No — every span attribute is metadata (IDs, names, booleans, latencies), verified PII/secret-free in Phase 18 §6 | Exporter/backend-controlled, outside this codebase | Exporter/backend-controlled | None new — already audited in Phase 18. |
| Provider requests/responses (Claude/Gemini/Groq API calls) | Nowhere durable — sent over HTTPS to the provider, response consumed and converted to the turn's reply text in-process, never separately logged or stored | Yes, transiently, in flight (the request necessarily contains conversational context) | None (not persisted by this codebase) | N/A | None new — already covered by each provider's own data-handling terms, outside this codebase's control; this codebase does not add its own additional persistence of provider payloads. |
| Idempotency records | `IdempotencyRepository` — in-memory or PostgreSQL | Request fingerprint/result, not raw text | Has its own existing TTL handling (`grep` found `ttl`/retention logic already in `idempotency_repository*.py`, pre-existing, not touched this phase) | Internal only | None — already has lifecycle handling from an earlier phase. |

## 3. Fix: Session Data Retention

**Finding:** Confirmed via code reading (not assumption) that PostgreSQL-backed sessions accumulate indefinitely — `SessionManager._expire()` calls `self._repository.save(session)` (a status-field update), never `delete()`. `docs/DATABASE.md` already disclosed this as an architectural characteristic ("no scheduled background cleanup daemon is mandatory... though periodic archiving may be configured") but no actual deletion capability existed anywhere — not even a manual, operator-invoked one.

**Fix (safe, additive, does not decide a new retention period — only completes what "expired" already means):**
- `SessionRepository.delete_expired_before(cutoff)` (in-memory, `session_manager.py`) and `PostgresSessionRepository.delete_expired_before(cutoff)` (`session_repository_postgres.py`, a single bulk `DELETE ... WHERE expires_at < cutoff`) — both delete only rows the application's own existing `expires_at`/`is_expired()` logic already considers expired.
- `SessionManager.purge_expired_sessions(before=None)` — the operator-facing wrapper; emits one new `DATA_PURGED` audit event (`observability_models.EventType`, new) summarizing the count, so a purge is itself part of the audit trail like every other real decision this class makes. Never called automatically anywhere (no scheduler exists in this codebase, and none is added here).
- `scripts/purge_expired_sessions.py` — operator-invoked CLI, **dry-run by default**, `--confirm` required to actually delete, `--older-than-days` for a more conservative cutoff than "expired at all."

7 new tests across `tests/test_session_manager.py` (4: deletion scope, non-deletion of active sessions, audit event content, no-op-means-no-audit-event) and `tests/test_session_repository_postgres.py` (2: bulk-delete scope, nothing-expired case), plus the in-memory `MemoryRepository` test described in Section 4.

## 4. Finding, Deliberately Not Auto-Fixed: Memory Record Retention

**Finding:** Memory records (`MemoryRecord.expires_at`) have the identical "expiry is a read-time filter, not a deletion" characteristic as sessions. Unlike sessions, this phase does **not** add a Postgres-level bulk-delete method for it.

**Why not:** `memory_repository_postgres.py`'s own module docstring states a deliberate, pre-existing security decision: *"No raw/unscoped query method exists here... never a `list_all()`/`query()`-shaped method"* — specifically so no caller can act on memory data across users without going through `MemoryManager`'s per-user ownership/authorization path. A bulk `DELETE WHERE expires_at < cutoff` is exactly the shape that decision excludes (it is, definitionally, an unscoped, cross-user operation). Overriding that constraint unilaterally to add convenience would weaken a boundary this codebase chose deliberately, for a stated reason — the same category of mistake plan.md Rule 8 (preserve existing architecture) and Rule 5 (do not invent security/business decisions) both warn against.

**What this phase did instead:**
- Added `MemoryRepository.delete_expired_before(cutoff)` to the **in-memory** repository only (used in dev/test `PERSISTENCE_MODE`, where no cross-user-query boundary exists to cross — it's a private, in-process dict, never exposed as a query API), with a test, so the capability exists and is exercised for whichever persistence backend can safely support it today.
- Documented the gap plainly here, with two real options for whoever makes this call: (a) accept that memory records never expire automatically in production and rely on `MemoryManager`'s existing per-user `delete_memory()`-shaped operations for individual cleanup only, or (b) deliberately add a narrowly-scoped, non-application-facing "maintenance" data-access path (e.g., a separate admin-only repository interface, explicitly gated, never reachable from ordinary request handling) — a genuine architecture decision, not an engineering default.

## 5. Finding, Deliberately Not Fixed: Audit Event Retention

**Finding:** Audit events (`audit_events`/`security_events` tables) are append-only with zero deletion/retention mechanism — confirmed by reading both `AuditRepository` implementations, neither exposes a `delete()`/`update()` method at all (not even per-row), consistent with the append-only design already stated in both files' docstrings.

**Why not fixed:** Retention period for an audit/compliance trail is a legal/business decision, not an engineering one (plan.md Rule 5's explicit "legal/compliance direction" category) — and, given this system's clinical/healthcare-adjacent domain, getting this wrong in either direction is genuinely consequential: deleting audit records too early could violate a real regulatory retention requirement (e.g., HIPAA's typical multi-year minimum) that this codebase has no visibility into, while this phase inventing an "always keep forever" policy would be an unstated assumption presented as a decision. Stated plainly rather than silently worked around: **this requires the deployer's own compliance/legal input** before any deletion capability is added here. No code change was made for this category.

## 6. Provider Data Minimization

Reviewed: no request/response payload sent to or received from Claude/Gemini/Groq is stored by this codebase beyond the single in-flight turn that needs it — confirmed by reading `ConversationManager`'s LLM-call path end to end. Nothing to minimize further; already minimal.

## 7. Files Changed

- `src/agent/session_manager.py` — `SessionRepository.delete_expired_before()`, `SessionManager.purge_expired_sessions()`.
- `src/agent/session_repository_postgres.py` — `PostgresSessionRepository.delete_expired_before()`.
- `src/agent/memory_manager.py` — `MemoryRepository.delete_expired_before()` (in-memory only, documented as deliberately not mirrored to Postgres).
- `src/agent/observability_models.py` — new `EventType.DATA_PURGED`.
- `scripts/purge_expired_sessions.py` — new, dry-run-by-default operator CLI.
- `tests/test_session_manager.py`, `tests/test_session_repository_postgres.py`, `tests/test_memory_manager.py` — 7 new tests total.

## 8. Tests

- **New tests this phase:** 7.
- **Full project suite (`pytest tests/ -q`):** **924 passed, 0 failed**, 52 subtests passed, same 2 pre-existing unrelated SQLAlchemy warnings as prior phases.

## 9. Security Impact

Net-positive, no regression: closes a genuine indefinite-data-growth gap for session data with a fail-safe (dry-run-by-default, never-automatic) mechanism; explicitly declines to weaken an existing, deliberate cross-user-query security boundary for memory data; explicitly declines to make a compliance decision on audit-data retention that isn't this phase's to make.

## 10. Known Limitations

- Memory-record retention remains unresolved pending an architecture decision (Section 4).
- Audit-event retention remains unresolved pending a compliance/legal decision (Section 5) — genuinely out of scope for engineering judgment alone.
- `scripts/purge_expired_sessions.py` is not wired into any scheduler — an operator must run it manually (or build their own cron/Task Scheduler entry around it) until a deployment decision is made about automation, which this phase does not make unprompted (no scheduler infrastructure exists anywhere in this repository today).
- This audit covers what the *application code* does with data; it does not cover infrastructure-layer retention (log aggregator retention, DB backup retention, host-level disk snapshots), which are deployer/infrastructure decisions outside this codebase's scope.

## 11. Acceptance Criteria

Per plan.md Phase 24: implement retention/deletion/redaction/safe-logging/data-minimization "where justified... never store data merely because it is technically available." **Met** — implemented where an engineering judgment call was safe and justified (session data), explicitly declined and documented where the decision belongs to someone else (memory-record cross-user access boundary, audit-event compliance retention) rather than silently fabricating a policy either way.

## 12. Final Status

`PHASE 24 COMPLETE`

## 13. Next Phase

Phase 23 (Cost and Provider Optimization) still requires real production usage data this system does not have. Phase 25 (Disaster Recovery) involves genuinely destructive testing scenarios (database outage, container restart, provider outage) — Phase 16 already covered a database-outage scenario safely (a disposable local container, never touching data), but a full Phase 25 pass touches more infrastructure surface and warrants explicit confirmation before proceeding autonomously, per this controller's own stop conditions ("a destructive/irreversible action requires approval"). Recommending a check-in with the user before Phase 25 rather than continuing automatically.

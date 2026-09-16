# Phase 25 — Disaster Recovery Report

## 1. Objective

Per plan.md's Phase 25 and this session's explicit user-approved scope: verify the system can recover from infrastructure failure — database outage, application/container restart, provider outage — with real, measured evidence (never inferred from the absence of an error), against a confirmed-disposable local environment only.

## 2. Environment Verification (steps 1–3, before any destructive action)

| Check | Result |
|---|---|
| Container exists | `docker-postgres-1` (confirmed via `docker ps -a`) |
| Image | `postgres:16-alpine` |
| Matches a known compose service | Yes — `docker/docker-compose.yml`'s `postgres` service, `profiles: ["test", "dev"]` (verified by grep against the actual file, not assumed) |
| Credentials | `POSTGRES_USER=voice_app` / `POSTGRES_PASSWORD=voice_secret` / `POSTGRES_DB=ai_voice_agent` — trivial, hardcoded dev-only values, identical to every prior phase's (16, 21) documented local setup |
| Volume | Named Docker volume `docker_postgres_data` (`local` driver) — not a bind-mount to any real host data directory |
| Referenced anywhere as production/staging | No — grepped the entire repository; no `docker-compose.prod.yml` or equivalent exists, and no other document names this container as anything other than local/test/dev |
| Pre-existing data | Checked before this phase touched anything: 1 stray `sessions` row (leftover from Phase 21's own DB-pressure test), 0 rows in every other table — no meaningful or valuable data |

**Conclusion: confirmed strictly disposable.** This is the exact same container Phase 16 and Phase 21 already used safely (start/stop only, volume never removed). This conclusion, and the container identity/credentials/profile evidence above, is re-verified programmatically by `scripts/disaster_recovery_test.py`'s own `preflight_verify_environment()` function at the start of every run (result `25.0` below) — not just asserted once manually.

**No production, staging, or shared database was touched at any point in this phase.**

## 3. Seed Data / Pre-Failure Baseline (steps 4–5)

Reset to a clean, known state via `TRUNCATE` (confirmed-disposable data only) before the run. Baseline sessions/tool records were then created by the test script itself as each scenario needed them (e.g. `dr-seed-1`, `dr-restart-1`, `dr-idempotency-shared-1`) — each one's pre-failure state is recorded and re-verified after recovery in the scenario results below, not assumed.

## 4. Recovery Artifacts (step 6)

No separate backup/restore mechanism was required or used: every scenario is a `docker stop` / `docker start` cycle against the named volume, which persists across a container stop by design — there is no data-loss path to recover *from* in this test shape (the volume is never removed). This was confirmed directly: Section 5's data-consistency checks (25.1.f, 25.2.d/e) prove data written before an outage is still present, byte-identical, after the container restarts.

## 5. Scenarios Executed

Script: `scripts/disaster_recovery_test.py` (rerunnable). All results below are the actual output of its third clean run (its first two runs hit test-harness bugs, described in Section 7 — never an application defect masquerading as one). Run against the real FastAPI application (real live uvicorn server, same harness convention as `scripts/stability_verification.py`/`performance_load_test.py`) and the real `docker-postgres-1` container.

### 5.1 Database Outage

| # | Check | Result | Evidence |
|---|---|---|---|
| 25.1.pre | Pre-failure baseline established | **PASS** | `/ready`=200, seed `/generate`=200, DB row for `dr-seed-1`: `'dr-seed-1\|ACTIVE'` |
| 25.1.a | Health/readiness during outage | **PASS** | `/health` during outage=200 (app itself stays up), `/ready` during outage=503, detection_time=2.64s |
| 25.1.b | Requests fail gracefully during outage | **PASS** | 3 requests: statuses=[500, 500, 500], latencies=[2086, 2049, 2041]ms, every body a clean `{"error_code":"internal_error",...}` JSON payload — no traceback, no `psycopg`/driver text leaked |
| 25.1.c | Application process survives the outage | **PASS** | `/health` immediately after the 3 failing requests=200 — no crash, no restart needed |
| 25.1.d | Readiness recovery after DB restart | **PASS** | postgres `pg_isready` recovered=True, `/ready` recovered=200, **readiness_recovery_time=0.51s**, **failure_duration=9.33s** |
| 25.1.e | Requests succeed again after recovery | **PASS** | post-recovery `/generate`=200, **request_recovery_latency=80.2ms** |
| 25.1.f | Data consistency across the outage | **PASS** | `dr-seed-1` row before outage: `'dr-seed-1\|ACTIVE'`, after recovery: `'dr-seed-1\|ACTIVE'` — byte-identical |

**Pre-failure state:** `dr-seed-1` session row present, `ACTIVE`, `/ready`=200.
**Failure injected:** real `docker stop docker-postgres-1`.
**Observed behavior:** `/health` (no DB dependency) stayed up throughout; `/ready` correctly flipped to 503; three `/generate` calls during the outage each got a real, clean 5xx (the global exception handler working as designed) rather than a leaked traceback, a hang, or a crash.
**Recovery behavior:** `docker start`, readiness returned within 0.51s of the container reporting healthy, a fresh request succeeded in 80ms, and the pre-outage session row was verified unchanged.

### 5.2 Container (Application) Restart

**Disclosed scope narrowing:** no application Docker image exists locally (`docker images` confirmed), and building one (this codebase installs `torch`/`transformers`/etc.) was judged out of scope for this pass — building and testing it is a reasonable follow-up, not silently substituted here. "Container restart" is therefore tested as a full in-process application restart against the real database: tearing down one live server instance and constructing a brand-new `ConversationManager` from scratch via `build_conversation_manager()`, which exercises the exact same construction path (including the synchronous `Database.health_check()` inside `resolve_persistence_repositories()`) a real process/container restart would hit.

| # | Check | Result | Evidence |
|---|---|---|---|
| 25.2.a | Cold-start construction succeeds while DB is healthy | **PASS** | `build_conversation_manager()` succeeded, construction_time=0.07s |
| 25.2.b | Startup/readiness immediately after restart | **PASS** | `/health`=200, `/ready`=200 |
| 25.2.c | Database connectivity from the new instance | **PASS** | post-restart `/generate`=200 |
| 25.2.d | Sessions created before the restart are still present | **PASS** | pre-restart request=200, row `dr-restart-1` found after restart |
| 25.2.e | No unexpected state corruption | **PASS** | `sessions` row count before=2, after=4, delta=2 (exactly this test's own 2 new requests — `dr-restart-1` + `dr-restart-2` — never an unexplained loss or duplication) |
| 25.2.f | **Cold start during a DB outage fails closed** | **PASS** | Raised `DatabaseUnavailableError` as required: `"Database health check failed... OperationalError"`, detection_time=2.05s. **Did not silently fall back to in-memory storage.** |

25.2.f is the single most important result in this scenario: plan.md/Phase 12.10's explicit persistence requirement is that a production deployment unable to reach its database must **fail to start**, not quietly run unpersisted. This was verified directly, not assumed — the container was genuinely stopped, and `build_conversation_manager()` was genuinely called against it.

### 5.3 Provider Outage (LOCAL/SIMULATED — no real Claude/Gemini/Groq call; no live credentials exist)

| # | Check | Result | Evidence |
|---|---|---|---|
| 25.3.a | Fallback routing on simulated provider outage | **PASS** | primary.call_count=1, fallback.call_count=1, `llm_failover_events_total`=1, `RETRY_ATTEMPT` audit event recorded, exactly one final response (`"I can still help."`) |
| 25.3.b | No duplicate tool execution during a provider outage | **PASS** | tool status=success, `TOOL_SUCCEEDED` audit events: before=0, after=1 (delta=1, exactly once), LLM invocation count during this tool call=0 — proves tool execution is structurally decoupled from LLM/provider health, not merely observed to behave that way once |
| 25.3.c | Recovery after the provider becomes available again | **PASS** | primary.call_count=1 (used directly), fallback.call_count=0 (not needed), response=`"Welcome back."` |

**Failure injected:** a primary provider (`MockLLMProvider` raising `LLMOverloadedError`, matching the exact exception `FallbackLLMProvider` is documented to fail over on) wrapped by the real `FallbackLLMProvider` class.
**Observed behavior:** exactly one failover event, one audit record, one response — never a duplicate or partial result. A tool-routed call (`ORDER_LOOKUP`) under the same failing-primary condition executed exactly once and never touched the LLM at all, confirming the architectural separation between tool execution and LLM generation (already known from Phase 4's design, re-confirmed under this specific failure condition).
**Recovery:** once a working primary was substituted, it was used directly on the next turn with zero fallback invocations.

### 5.4 Persistence / Recovery — Idempotency Across a Real Outage

The user's instructions specifically require verifying "no duplicated actions after recovery." An earlier draft of this scenario only re-sent the same natural-language message twice with no shared `request_id` — that proves nothing about idempotency (two separate messages with no shared key would simply create two separate real bookings). Corrected to directly exercise the actual idempotency mechanism (Phase 10/11: a repeated `request_id` is detected as a duplicate) across a **real** outage+restart, proving the guarantee is backed by the persisted `idempotency_records` table and not merely an in-memory dict a restart would have reset.

| # | Check | Result | Evidence |
|---|---|---|---|
| 25.4.a | First tool request succeeds and is persisted | **PASS** | invoke: success=True, status=success, `idempotency_records` row found for `dr-idempotency-shared-1` |
| 25.4.b | **Duplicate `request_id` after a real outage+recovery is detected as a duplicate, never re-executed** | **PASS** | postgres recovered=True, second invoke with the same `request_id`: success=False, status=`duplicate` |
| 25.4.c | Persisted audit trail remains consistent across the outage | **PASS** | `audit_events` count before=8, after=17 |

## 6. Resource Cleanup

| # | Check | Result | Evidence |
|---|---|---|---|
| 25.5.a | Background job resolves to a terminal state | **PASS** | final job status=`completed`, no hang |
| 25.5.b | No orphaned active-call state | **PASS** | `active_call_count`=0 |
| 25.5.c | WebSocket / Twilio Media Streams resource cleanup under real concurrent calls | **NOT RUN** | No real or synthetic Twilio Media Streams traffic was generated this pass — same disclosed gap as `PHASE_21_PERFORMANCE_REPORT.md` §21.5. Requires either real Twilio traffic (blocked on Phase 17/19) or a synthetic WS-frame harness this pass did not build. Stated plainly rather than inferred as passing. |

## 7. Test-Harness Bugs Found and Fixed During This Phase (not application defects)

Per the user's explicit instruction to reproduce/root-cause/fix/regression-test any real defect: three issues surfaced during this phase's own dry runs. All three were bugs in the **test script**, not the application — each is documented here with the evidence that distinguishes it from an application defect, rather than silently "fixed and moved on."

1. **`LLMOverloadedError` construction (script bug).** The script's own `LLMOverloadedError("simulated provider outage")` call was missing that exception's required `provider` argument — a `TypeError` in test setup, never reaching the application at all. Fixed: supplied `provider="fake-claude"`.
2. **Windows keep-alive connection artifact (test-harness bug, not app behavior).** On the first two dry runs, a request immediately following the 3 outage-triggered `/generate` calls (all ~2s each, all sharing one `httpx.Client`'s connection pool) intermittently failed at the TCP layer (`WinError 10053: An established connection was aborted by the software in your host machine`) rather than reaching the application. Diagnosis: this is consistent with a Windows-host-specific stale-keep-alive-socket artifact from reusing one persistent connection across several slow, DB-failure-triggered requests in the *test driver* — not a server-side hang or crash (the same scenario re-run cleanly multiple times afterward with `/health` immediately succeeding, and every graceful-5xx body in the final run is a clean, well-formed JSON error payload with real request/error IDs, proving the server itself handled every one of those requests correctly). Fixed at the test-harness level: `Connection: close` on the three outage-window requests, plus a `_get_resilient()` retry-on-transport-error-only helper (never retries on an HTTP status, so a genuine application hang would still surface as a failure) for the liveness checks immediately following. Re-ran clean three consecutive times after the fix.
3. **Weak idempotency check (test-design gap, described in §5.4).** The original scenario 4 asserted only "audit events grew" after sending the same message twice with no shared `request_id` — not a real idempotency proof. Rewritten to exercise the actual `request_id`-based duplicate-detection mechanism directly against `ToolOrchestrator`, across a real outage+restart.

**Reproduction discipline applied:** for #2 specifically, the failure was observed non-deterministically (present on 2 of 3 dry runs before the fix, absent afterward across 3 consecutive clean runs) — consistent with a connection-pool race, not a deterministic defect. The fix targets the identified mechanism (persistent-connection reuse across slow requests) rather than merely retrying blindly; `_get_resilient()`'s docstring states explicitly that it must NOT mask a genuine liveness failure, and does not (a real crash would still fail after exhausting retries against a fresh connection each time).

## 8. Recovery Measurements Summary

| Metric | Value | Source |
|---|---|---|
| Detection time (outage) | 2.64s | Time from `docker stop` to `/ready` first returning 503 |
| Failure duration | 9.33s | Time from `docker stop` to `docker start` completing + Postgres reporting ready |
| Readiness recovery time | 0.51s | Time from `docker start` to `/ready` first returning 200 |
| Request recovery time | 80.2ms | Latency of the first `/generate` call after recovery |
| Detection time (cold-start-during-outage) | 2.05s | Time for `build_conversation_manager()` to raise `DatabaseUnavailableError` |
| Data consistency | **Confirmed** | Pre- and post-outage row content byte-identical (25.1.f); idempotency guarantee survived a real restart (25.4.b) |
| Error rate during DB outage | 3/3 (100%, by design) | Every request during a real, total DB outage correctly failed — this is the expected, fail-closed behavior, not a defect |

## 9. Defects Found in Application Code

**None.** Every scenario's application-level behavior (graceful degradation, fail-closed cold start, provider failover, tool-execution isolation, idempotency persistence, background-job resolution) passed on its merits, verified with real evidence, not inferred from an absence of errors. The three issues found and fixed (Section 7) were entirely in this phase's own test script.

## 10. Tests Added

None to the `tests/` suite — this phase is an infrastructure-behavior verification pass against real Docker/Postgres, matching Phase 16/21's own precedent of a standalone `scripts/` harness rather than `pytest`-collected tests (real container stop/start cycles are not appropriate for the unit/integration suite that runs on every commit). `scripts/disaster_recovery_test.py`'s filename does not match pytest's default discovery globs (`test_*.py`/`*_test.py`), so it is not accidentally swept into `pytest -q` the way Phase 21's script briefly was (Phase 22's fix, re-confirmed not to recur here).

## 11. Final Test Result

`pytest tests/ -q` (the same CI-matching invocation every phase since 18 has used): **924 passed, 0 failed**, 52 subtests passed — the exact same count as Phase 24 concluded with. Baseline preserved; nothing weakened or disabled.

## 12. Known Limitations

- "Container restart" tested the application's own construction/startup logic against a real database, not a literal Docker-level restart of a built application image (no such image exists locally; building one is a reasonable, separately-scoped follow-up).
- WebSocket/Twilio Media Streams resource cleanup under real concurrent calls remains genuinely untested (§6, 25.5.c) — the same disclosed gap carried from Phase 21, requiring either live Twilio traffic (blocked on Phase 17/19) or a synthetic WS-frame harness not built in this pass.
- Provider-outage scenarios are LOCAL/SIMULATED against `FallbackLLMProvider`'s real mechanics with fake provider objects — not a real Claude/Gemini API outage, since no live credentials exist (same disclosed methodology as every phase since 16).
- Only a single, total database outage was tested (full stop/start) — partial degradation (e.g. slow queries, connection pool exhaustion without a full outage) was not exercised.
- Redis/cache failure was not tested — confirmed in Phase 24's data-lifecycle audit that no cache layer exists anywhere in this codebase, so this is not applicable rather than skipped.

## 13. Remaining Risks

- The STT-reconnect and duplicate-Twilio-START-frame risks disclosed in Phase 16 §5 (items 2–3) remain unaddressed and unre-tested here — genuinely out of this phase's HTTP/database-focused scope.
- Container-level (not just application-level) restart behavior for a real deployed image remains unverified.
- No sustained/soak disaster-recovery testing (e.g., repeated outage/recovery cycles under concurrent load) was performed — this pass verified single-cycle recovery correctness, not resilience under repeated or compounding failures.

## 14. Final Status

`PHASE 25 COMPLETE`

## 15. Next Phase

Phase 26 (Production Deployment) requires real production infrastructure, secrets, DNS, and a canary rollout decision — none of which exist or can be provisioned autonomously, and several of its tasks are exactly the kind of "required external credentials are unavailable" / "irreversible business decision" conditions this controller's own rules require a stop for. Recommending a check-in with the user before Phase 26, consistent with the same reasoning already applied before this phase.

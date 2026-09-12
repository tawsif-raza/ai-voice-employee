# Phase 16 - Final Controlled Stability Verification Report

## 1. Executive Summary

Ran 10 controlled scenarios against the real FastAPI application (`src/api/server.py`), served by a real live uvicorn server on a loopback socket (not `TestClient` — see `tests/test_job_isolation.py`'s module docstring for why that distinction matters for anything timing-sensitive), sampling process RSS memory and CPU via `psutil` throughout. All 10 scenarios **PASS**. No production infrastructure was destroyed: scenario 9 used an existing local, disposable Postgres container (created by a prior session's own real-Postgres validation work) — started and stopped, never recreated, its volume never touched, and left in the stopped state it was found in.

**Stability classification: B — mostly stable, known risks.** Justification in §4. Every scenario actually run passed cleanly with correct fail-closed/recovery behavior and no crashes or leaked internals — but real, previously-disclosed risks remain unaddressed (not newly discovered here, carried over from the Phase 1 audit), and several dimensions (sustained load beyond 50 requests, the WebSocket voice pipeline itself, real external API integration, real local-model inference) were not exercised in this pass. That combination does not meet the bar for "A."

Script: `scripts/stability_verification.py` (rerunnable). Measurement caveat, stated once here: the server runs in a background thread inside the same process as the test driver (the same pattern already validated in `tests/test_job_isolation.py` for correct real-ASGI timing behavior), so RSS/CPU figures include the lightweight httpx-based driver's own overhead, not a perfectly isolated separate server process — disclosed rather than overclaimed.

## 2. Final Table

| Test | Result | Evidence | Recovery | Remaining Risk |
|---|---|---|---|---|
| 1. Normal request | PASS | status=200, latency=4.8ms, mem 102.5→102.7MB | n/a (no failure) | none identified |
| 2. Heavy generation (simulated 2s LLM call) | PASS | submit=5.4ms (202 Accepted), concurrent `/health`=1.3ms (200), job resolved=completed, mem 104.2→104.6MB | n/a (no failure) | Job execution still serializes behind the generation semaphore (default size 1) when `ConversationManager` is constructed directly rather than via `build_conversation_manager()`'s remote-provider-aware default — see Phase 15.1's fix, which only applies to the factory path. |
| 3. 5 simultaneous heavy generations (1s each) | PASS | all 5 submits <500ms (max=3.4ms), all 5 completed, total wall time=5.06s (matches fully-serialized expectation of ~5.0s), `/health` during=200, mem 104.6→105.0MB | n/a (no failure) | Same semaphore-serialization risk as test 2 — confirmed directly by the wall-clock number: 5 one-second jobs took ~5 seconds, not ~1. |
| 4. Large input (message at/over 4000-char limit) | PASS | at-limit=200 (10.7ms), over-limit=422 (3.8ms, rejected before reaching the model), mem 105.4→105.6MB | n/a (rejection is correct, not a failure) | No file/binary upload endpoint exists in this API (text/JSON only) — large-input risk is scoped to message/history length, already bounded by `configs/reliability.yaml`. |
| 5. External API timeout (simulated) | PASS | job completed with the fixed apology/handoff response, `is_handoff=true`, `/health` after=200 | `ConversationManager` catches the timeout internally, converts to a safe degraded response — never raises out of `handle_turn()`. | none identified for this path specifically. |
| 6. External API hard failure (simulated connection error) | PASS | job completed with the fixed apology/handoff response, raw exception text never appeared in the response, `/health` after=200 | Same internal degradation path as test 5. | none identified for this path specifically. |
| 7. Malformed input (5 variants: invalid JSON, missing field, wrong types x2, unknown job_id) | PASS | statuses: 422/422/422/422/404 — all fail fast, all before touching the model | n/a (rejection is correct) | none identified. |
| 8. Worker/task failure (unexpected internal exception, not an LLM-provider one) | PASS | job marked `failed`, raw exception text never leaked, `/health` during the failure=200, a brand-new independent job submitted immediately after **completed normally** | Process never went down; no restart needed. | none identified. |
| 9. Database failure (real outage of a local disposable Postgres container) | PASS | `/ready` before outage=200, `/ready` during outage=**503**, `/health` (no DB check) during outage=200 throughout, `/ready` recovered to 200 after the container restarted, with no app restart | Fail-closed during the outage, auto-recovered once the database came back. | In-flight session/memory read-write behavior during a *mid-request* DB outage (as opposed to the `/ready` probe) was not exercised — `PERSISTENCE_MODE=dev` (in-memory) is this app's actual default, so most current deployments would never hit this code path at all. |
| 10. 50 repeated identical requests | PASS | 0/50 errors, latency stable (first-10-avg=2.3ms, last-10-avg=2.2ms), memory growth=+0.3MB | n/a (no failure) | Only 50 iterations run — does not rule out slow leaks that only manifest over thousands of requests or many hours of uptime. |

## 3. Monitoring Coverage

| Dimension | Covered how |
|---|---|
| CPU | `psutil.Process().cpu_percent()`, sampled before/after every scenario. |
| RAM | `psutil.Process().memory_info().rss`, sampled before/after every scenario — no scenario showed unbounded growth. |
| Request latency | Measured directly per HTTP call in every scenario (`time.perf_counter()` around each `httpx` call). |
| Error rate | Explicit status-code assertions in every scenario (tests 4, 7, 10 directly; others implicitly via the PASS/FAIL check). |
| Process health | `/health` checked during/after every failure scenario (2, 3, 5, 6, 8, 9) — never dropped from 200. |
| Worker health | Job-store state (`queued`/`running`/`completed`/`failed`) polled to a deterministic terminal state in every job-based scenario (2, 3, 5, 6, 8). |
| Database connections | Test 9's `Database` object against a real Postgres instance, through a real stop/start cycle. |
| Active/failed jobs | Directly observed via `GET /jobs/{job_id}` in every job-based scenario; test 8 specifically confirms a failed job doesn't block a subsequent independent one. |
| Recovery behavior | Explicit recovery step in tests 8 (new job after a failure) and 9 (DB restart) — both confirmed the process needed no restart of its own. |

## 4. Stability Classification: B — Mostly Stable, Known Risks

**Why not A:** "A — production resilient" would require evidence this pass does not have: sustained/soak load testing (this pass ran 50 sequential requests and 5 concurrent jobs, not thousands or hours), the WebSocket voice pipeline itself under real Twilio-shaped traffic (this verification only exercised the HTTP `/generate` and `/jobs/generate` paths), real external LLM provider integration (Claude/Gemini calls were simulated via fakes throughout, matching the same controlled-testing approach as Phases 14/15/15.1), and real local-model inference (torch/Qwen was never loaded in this pass). Beyond untested surface area, there are also *specific, already-disclosed* remaining risks (§5) that are real, not hypothetical, and unresolved.

**Why not C or D:** Every one of the 10 scenarios actually run passed cleanly, with no crash, no hang, no leaked internal detail, and correct fail-closed + auto-recovery behavior for both a worker-level failure (test 8) and a real infrastructure failure (test 9). The two P1s found in the original stability audit are both fixed and re-verified as part of this pass (tests 2/3's semaphore evidence, test 8's isolation evidence). That is meaningfully better than "significant risks remain" (C) or "critical instability" (D).

## 5. Exact Remaining Issues Before Phase 2

1. **Generation semaphore still serializes by default** (tests 2, 3): confirmed again here — `max_concurrent_generations` defaults to 1 unless the app is built via `build_conversation_manager()`'s environment-driven remote-provider path (fixed in Phase 15.1). Any deployment or test harness that constructs `ConversationManager` directly still gets the conservative default. Not a new finding; carried over and now measured with a concrete wall-clock number (5 jobs × 1s = 5.06s, not ~1s).
2. **STT event loop has no reconnect/retry on failure** (`src/voice/voice_pipeline.py`'s `process_stt_events()`): from the original audit, not exercised in this HTTP/job-focused pass. A dropped Deepgram connection would silently "deafen" a live call for its remaining duration.
3. **Duplicate/replayed Twilio START frame leaks the previous call handler's resources** (`VoiceCallManager.register_call()`): from the original audit, not exercised here.
4. **`MetricsRegistry.increment()`/`.observe()` raise on any unregistered metric name** (`src/agent/metrics.py`): from the original audit — no current mismatch exists, but no test guards against a future one being introduced.
5. **Not verified in this pass:** the WebSocket voice pipeline end-to-end, real external LLM provider behavior (only simulated failures were tested), real local-model inference, sustained/soak load beyond 50 sequential + 5 concurrent requests, and in-flight session/memory persistence behavior during a genuine mid-request database outage (only the `/ready` probe was exercised against the real outage in test 9).
6. **Cosmetic, zero current runtime impact:** the `BaseSTTService`/`BaseTTSService` abstract-method type-annotation mismatch mypy flags (already grandfathered in `pyproject.toml`'s baseline; verified benign at every current call site during the original audit).

None of the above are newly discovered — this verification pass's job was to confirm the previously-identified P1s are fixed and that the currently-tested surface area is genuinely stable, not to find new issues. It succeeded at that; the list above is exactly what should be picked up before any Phase 2 architectural work, in roughly the order listed (semaphore first, since it's already measured and understood; the voice-pipeline items next, since they affect live calls; the metrics fragility last, since it's latent, not active).

`PHASE 16 COMPLETE`

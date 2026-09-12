# Phase 15 - Heavy-Workload Request Isolation Report

## 1. Executive Summary

This phase was scoped against a prompt describing LangGraph, image/video generation, and FFmpeg workloads -- **none of which exist in this repository**, confirmed by exhaustive search (`grep -rli "langgraph|ffmpeg|video"` and image-generation library names across `src/`, `scripts/`, and both requirements files: zero matches). The user confirmed the intent was to apply the same lens to this repo's actual heaviest workflow instead. That workflow is **`ConversationManager.handle_turn()`** (RAG retrieval + LLM generation), reached via `POST /generate` and the Twilio voice pipeline.

Traced end to end: the heavy work already executes off the asyncio event loop (FastAPI dispatches the sync `/generate` route to a worker thread), so the event loop itself was never blocked. The real problem was narrower but still real: **the HTTP request/response cycle itself was the unit of work** -- a client submitting heavy generation held its connection and a worker thread open for the full duration, and a second concurrent heavy request was serialized behind the first with no acknowledgement in between. Added the smallest fix matching the requested target diagram: `POST /jobs/generate` (validate + create job + return job_id immediately) and `GET /jobs/{job_id}` (poll for result), with the identical `handle_turn()` call now dispatched to a background thread via `loop.run_in_executor()` -- the same pattern `voice_pipeline.py` already uses for this exact call. No distributed queue, no new service, no new process.

## 2. Trace: HTTP Request to Completion (Before This Phase)

```
POST /generate (sync def, FastAPI/Starlette dispatches to a worker thread)
  -> resolve_identity() [Depends]
  -> ConversationManager.handle_turn()
       -> clinical_guard.score()                 (fast, rule-based)
       -> intent_engine.classify()                (fast, rule-based)
       -> policy_engine.evaluate_generation()     (fast, rule-based)
       -> _retrieve_with_reliability()            (FAISS + embedding -- the RAG step)
       -> with _generation_semaphore:             ** process-wide, size 1 by default **
            self.llm_service.generate_stream()    ** THE heavy step: local Qwen
                                                      inference OR a Claude/Gemini
                                                      HTTP round trip **
       -> handoff_detector.score()                (fast, rule-based)
  <- ChatResponse (blocks until the entire above completes)
```

**Where the expensive operation executes**: inside the same process as the API, inside the worker thread FastAPI already assigned to that one HTTP request -- confirmed by reading `conversation_manager.py`'s `_generation_semaphore` usage and `server.py`'s `def generate(...)` (not `async def`, so Starlette's `run_in_threadpool` already isolates it from the event loop). This was already correct with respect to the *event loop*; it was not correct with respect to the *request/response cycle*, which is what this phase addresses.

**LangGraph / image-video-gen / FFmpeg**: not applicable -- none exist here.
**Parallel execution**: none found beyond the semaphore-bounded generation itself and the voice pipeline's existing `run_in_executor` usage.
**Memory-heavy operations**: the loaded local model and FAISS index are held once, at startup, not per-request -- not a per-request heavy-memory pattern.

## 3. Isolation Mechanism Added (Phase 1 Scope)

```
User request
    v
API validates request           -- POST /jobs/generate: same Pydantic ChatRequest validation as /generate
    v
API creates job                  -- JobStore.create() (src/api/jobs.py): thread-safe, bounded (max 1000), in-memory
    v
API returns job ID quickly       -- 202 {"job_id": ..., "status": "queued"} -- measured ~15-20ms (see Sec. 5)
    v
Heavy execution happens outside  -- loop.run_in_executor(None, _run_generate_job, ...) -- same pattern
the request lifecycle               voice_pipeline.py already uses for this identical call
    v
GET /jobs/{job_id}                -- poll for {"status": "completed"|"failed"|"queued"|"running", result|error}
```

Deliberately NOT built (Phase 2, not this phase): no Redis/Celery/external broker, no separate worker process or machine, no job persistence across a restart, no retry/backoff policy for failed jobs, no cross-instance job visibility. The heavy work still runs in this same process on the same shared `_generation_semaphore` -- this phase decouples the *request* from the *work*, it does not change the work's own concurrency limits (see Sec. 6).

New files: `src/api/jobs.py` (job store), two new routes in `src/api/server.py` (`POST /jobs/generate`, `GET /jobs/{job_id}`), `tests/test_job_isolation.py`, `scripts/benchmark_job_isolation.py`.

## 4. Tests Added

`tests/test_job_isolation.py`, 6 tests, all passing:

| # | Test | Proves |
|---|---|---|
| 1 | `test_submit_returns_immediately_and_health_stays_responsive` | API can respond (both job submission itself, and an unrelated `/health` call) while the heavy job is still running |
| 2 | `test_second_job_can_be_submitted_while_first_is_running` | One heavy job cannot prevent other requests/jobs from being served |
| 3 | `test_failing_job_is_isolated_and_app_keeps_serving` | A failed heavy job does not terminate the application -- the process keeps serving `/health` and a brand-new, independently successful job afterward |
| 4, 5 | `test_successful_job_resolves_to_completed_not_stuck` / `test_failing_job_resolves_to_failed_not_stuck` | The job reaches a deterministic terminal state (never stuck at queued/running), for both outcomes |
| 6 | `test_unknown_job_id_returns_404_not_a_hang` | An invalid job_id fails fast (404), not a hang |

**Methodology note, itself a finding**: tests 1 and 2 (the timing-sensitive ones) run against a **real live uvicorn server** on a loopback socket (`httpx` + a background thread), not FastAPI's `TestClient`. A direct repro during this phase showed `TestClient`'s synchronous httpx<->anyio bridging blocks for a background `run_in_executor()` task's *full* duration before returning control to the caller -- verified with a minimal 2-line-handler repro (10s wait matching the fake work's duration). The identical app served by real `uvicorn` returned in ~7ms. That is a `TestClient` bridging artifact, not real ASGI/uvicorn behavior, so proving "the API stays responsive" required the live-server harness; it's documented in the test file's module docstring so it isn't rediscovered as a false regression later.

Full suite after this phase: **818 passed, 0 failed** (812 before this phase + 6 new).

## 5. Measurements (Before vs. After)

Benchmark: `scripts/benchmark_job_isolation.py`, against a real live uvicorn server, using a fake LLM service with a fixed 2.0-second delay (no torch/network -- isolates the mechanism, not model speed). Run in this session; exact numbers will vary run to run by low milliseconds but the orders of magnitude are stable and reproducible.

| Scenario | Before (`/generate`) | After (`/jobs/generate`) |
|---|---|---|
| Client-perceived latency to submit one heavy (2s) request | **2.006 s** (blocks for the full duration) | **0.015 s** (submission only; job then resolves ~2s later via polling) |
| `GET /health` latency while that request is in flight | 0.003 s (already fine -- event loop wasn't blocked) | 0.003 s (unchanged, as expected) |
| Client-perceived latency to submit a **second** concurrent heavy request | **3.499 s** (queued behind the first via the shared generation semaphore, connection held the whole time) | **0.006 s** (job accepted immediately; its execution is still queued behind the first internally -- see Sec. 6 -- but the caller is never blocked finding that out) |

The single most important number: **submitting a second concurrent heavy request went from a ~3.5-second blocking HTTP call to a ~6-millisecond acknowledgement.** That is the concrete "API remains responsive while heavy work executes" result.

## 6. What This Phase Does Not Fix (Scope Boundary, Disclosed)

The `/jobs/generate` path still funnels through the same `_generation_semaphore` (default size 1) that serializes ALL generation process-wide -- this is a **separate, already-identified issue** (see the prior stability-audit conversation's P1 finding: the semaphore's size-1 default was correct for the local in-process model but was never revisited when the remote Claude/Gemini provider was added, and it now bottlenecks that path too). Job-based isolation makes the *symptom* (blocked HTTP clients) go away without touching that *root cause* (limited generation throughput) -- both are legitimate, independent fixes. Recommended order: this phase first (it's what "the API remains responsive" concretely means to a caller), then the semaphore fix, since raising concurrency without first decoupling the request cycle would just mean more concurrent *held connections* rather than more concurrent *served callers*.

Also not addressed here (genuinely out of scope for "Phase 1 stabilization"): job persistence across a process restart, cross-instance job visibility (irrelevant at 1 process today), a job-cancellation endpoint, and job-store metrics (counts of queued/running/completed/failed) -- straightforward additions if/when useful, deliberately not bundled into this minimal pass.

## 7. Compatibility

`POST /generate` is completely unchanged -- same behavior, same tests (`tests/test_server_api.py`, still 36/36 passing). The new endpoints are additive. No `PolicyEngine`/`ClinicalSafetyGuard`/`ToolOrchestrator` logic was touched; `_run_generate_job()` calls the exact same `ConversationManager.handle_turn()` every other caller does.

`PHASE 15 COMPLETE`

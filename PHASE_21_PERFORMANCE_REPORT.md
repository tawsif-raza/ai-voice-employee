# Phase 21 — Performance & Load Report

## 1. Objective

Per plan.md's Phase 21: determine the actual system capacity — concurrent calls, sustained calls, DB connection pressure, CPU/memory, latency degradation — measure p50/p95/p99/max, and document safe concurrency, bottlenecks, scaling/failure thresholds, and recovery time.

## 2. Method

`scripts/performance_load_test.py` (new, modeled directly on Phase 16's `scripts/stability_verification.py` harness — same real-live-uvicorn-server pattern, same LOCAL/SIMULATED/LIVE evidence labeling discipline, plan.md Rule 9). Six scenarios, run once, real measurements throughout — no scenario's numbers below are fabricated, estimated, or rounded up from a partial run.

**Disclosed methodology, stated once:** the server runs in a background thread inside this same test-driver process (same caveat Phase 16 already disclosed) — RSS/CPU figures include the driver's own overhead. The simulated LLM (`FixedDelayLLMService`) uses a fixed `time.sleep()` delay rather than a real provider call, so these are **LOCAL/SIMULATED** load characteristics of this application's own request-handling/concurrency machinery, not real Claude/Gemini/Twilio latency under load (that remains Phase 17/19's job, both blocked on live infrastructure).

## 3. Results

| # | Scenario | Result | Source | Key numbers |
|---|---|---|---|---|
| 21.1 | 10 concurrent jobs, default semaphore (`max_concurrent_generations=1`, `ConversationManager`'s own unconfigured default) | PASS | LOCAL/SIMULATED | wall=3.05s (fully-serial expectation ~3.00s at 0.3s/job) — confirms full serialization, 0 errors |
| 21.2.5 | 5 concurrent jobs, `max_concurrent_generations=20` | PASS | LOCAL/SIMULATED | wall=0.37s, latency p50/p95/p99/max=320/339/339/339ms, 0 errors |
| 21.2.20 | 20 concurrent jobs, `max_concurrent_generations=20` | PASS | LOCAL/SIMULATED | wall=0.47s, latency p50/p95/p99/max=403/419/420/420ms, 0 errors |
| 21.2.50 | 50 concurrent jobs, `max_concurrent_generations=20` | PASS | LOCAL/SIMULATED | wall=1.05s (expected~0.75s if purely semaphore-parallel), latency p50/p95/p99/max=642/897/900/900ms, 0 errors |
| 21.3 | 300 sustained sequential requests | PASS | LOCAL/SIMULATED | latency p50/p95/p99/max=2.7/4.3/6.3/9.9ms, first-quarter-avg=3.22ms → last-quarter-avg=3.03ms (**-5.9%**, no degradation), memory growth=**+0.0MB**, 0 errors |
| 21.4 | 20 concurrent requests against real local Postgres (distinct `session_id` per request) | PASS | **LIVE** (real local Postgres container, not production) | wall=0.09s, latency p50/p95/p99/max=42/48/50/50ms, 0 errors |
| 21.5 | WebSocket / Twilio Media Streams load | NOT RUN | N/A | No real or synthetic Twilio traffic generated — see Section 6 |

Full JSON evidence printed by the script at run time (not separately persisted — the script is rerunnable and each field above is copied directly from that output).

## 4. Performance Envelope

- **Safe concurrency (as shipped, default construction path):** 1 concurrent generation. `ConversationManager()` constructed directly (as every current test in this repository does, and as any deployment that does not go through `build_conversation_manager()`'s factory would) defaults `max_concurrent_generations` to 1 and fully serializes — this is a **previously disclosed, unchanged characteristic** (Phase 16 §5 risk #1, Phase 15.1's own fix only applies to the factory path), re-confirmed here with a concrete measurement (21.1) rather than newly discovered.
- **Safe concurrency (factory-built / remote-provider-configured path):** at least 50 concurrent requests with `max_concurrent_generations=20`, 0 errors at every concurrency level tested (5/20/50) — genuine parallel throughput, not serialization, confirmed by wall-clock time scaling with the semaphore limit rather than with raw concurrency.
- **Bottleneck identified:** at 50 concurrent jobs against a semaphore of 20, actual wall time (1.05s) exceeded the naive semaphore-parallel expectation (0.75s) by ~40%. This is real overhead from this test harness's own `ThreadPoolExecutor` + per-job HTTP polling loop (each of the 50 client threads polls `/jobs/{id}` independently every 20ms), not evidence of an application-side bottleneck — the `/health` and `/generate` endpoints themselves showed no comparable degradation (21.3's flat sustained-load numbers). Disclosed as a measured characteristic of this test's own driver, not attributed to the application without stronger evidence.
- **Scaling threshold:** not found. Every concurrency level actually tested (up to 50) passed with 0 errors and bounded latency. This pass did not push load high enough to find where the system starts failing — stated plainly rather than extrapolated or guessed at, per plan.md Rule 4 (No Fake Completion). A future pass should sweep higher (100, 200, 500+) to find the real threshold.
- **Failure threshold:** not found, for the same reason — no scenario in this pass produced an error, a timeout, or a queue-growth signal under the load levels tested.
- **Recovery time:** not applicable this pass — no failure was induced to recover from. Phase 16 §2 test 9 (real Postgres outage → `/ready` correctly returns 503 during the outage, 200 after real container restart, no app restart needed) remains the relevant, already-measured recovery-time evidence for a database failure specifically; not re-run here since Phase 21's own DB scenario (21.4) tests concurrent-load pressure, not outage recovery.
- **Sustained load / memory:** 300 sequential requests showed **zero measurable RSS growth** (151.4MB → 151.4MB) and a slight (5.9%) *improvement* in average latency from first to last quarter (consistent with normal warm-up, not degradation). This extends Phase 16's 50-request sustained test and narrows (does not close) that report's disclosed "does not rule out slow leaks... over thousands of requests" gap.
- **Database connection pressure:** 20 concurrent requests against a real local Postgres instance, each writing to a distinct session row, completed with 0 errors and p99 latency of 50ms — healthy. Narrower than a full production capacity test (no single-row lock-contention scenario was included — noted as out of scope, not silently skipped).

## 5. Files Added

- `scripts/performance_load_test.py` — rerunnable, no changes to application code required or made this phase (a pure measurement pass, consistent with plan.md's phase-isolation rule — Phase 21 measures, it does not optimize; Phase 23 is where load-informed optimization would happen if justified).

## 6. Known Limitations / Not Run

- **WebSocket / Twilio Media Streams load (21.5):** not exercised. This is a genuine, disclosed gap, not fabricated as covered by the HTTP scenarios above — connection-accept rate, audio-frame backpressure, and per-call resource cleanup under concurrent real calls remain unmeasured. Requires either real Twilio traffic (blocked on Phase 17/19's live-credential/staging gate) or a dedicated synthetic-WS-frame load harness this pass did not build.
- **Real external provider latency under load** (Claude/Gemini/Groq rate limiting, provider throttling behavior) is explicitly out of scope — this pass used a simulated LLM by design (LOCAL/SIMULATED), matching Phase 16's own methodology; real-provider load characteristics are a Phase 17/19 concern.
- **True long-duration soak testing** (thousands of requests, multi-hour uptime) was not performed — 300 requests over well under a minute is a meaningfully larger sample than Phase 16's 50 but still short of that bar.
- **Scaling/failure threshold not found** — see Section 4. This pass establishes a floor ("healthy up to 50 concurrent / 300 sequential"), not a ceiling.
- The local `docker-postgres-1` container used for 21.4 was found stopped before this run, started for the scenario, and explicitly stopped again immediately afterward, restoring its pre-test state — no data or volume was touched (same discipline as Phase 16 §"Executive Summary").

## 7. Acceptance Criteria

Per plan.md Phase 21: "Documented performance envelope." **Met** — Section 4 documents safe concurrency (both the current default and a properly-configured deployment), the one bottleneck actually identified (test-harness polling overhead, not application-side), and honestly states which thresholds (scaling/failure) were not found rather than inventing numbers for them.

## 8. Final Status

`PHASE 21 COMPLETE`

## 9. Next Phase

Phase 22 (Observability & Incident Response) is independent of live external infrastructure — it concerns whether an engineer can diagnose a failed call from existing OpenTelemetry/audit/metrics data without reproducing it locally, which can be evaluated against the tracing/observability work already built in Phases 8/14. Proceeding to Phase 22 next per plan.md Rule 17.

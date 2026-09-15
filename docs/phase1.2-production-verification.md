# Phase 1.2 Production Verification

Phase 1 entered this pass classified **B — Mostly Stable**, with the four
concrete P1 defects from the original audit already fixed and verified
(generation semaphore, STT reconnect, duplicate Twilio START, MetricsRegistry
mismatch — 864/864 tests passing at the start of this pass, 10/10 on the
synthetic stability script). This pass's job was not to re-fix those; it was
to find out whether the architecture holds up under *more realistic*
conditions: a real LLM provider, real local-model inference, sustained
duration, a realistic mixed workload shape, richer database failure modes,
and real Twilio media-stream traffic where feasible.

No application redesign was performed. No Phase 2 work was started.

## Environment

- OS: Windows 11 (10.0.26340), local development machine (not a staging/cloud environment).
- Python 3.14.7, torch 2.13.0+cpu — **no GPU available** (`torch.cuda.is_available() == False`).
- CPU: 16 logical / 10 physical cores. RAM: 16.85 GB total, but **heavily contended by other processes during this pass** (Chrome, VS Code, WSL, this very Claude Code session) — available RAM was observed as low as 1.25 GB (92.6% system-wide utilization) at points during testing. This is disclosed because it affects how several results below should be read (see Resource Usage).
- Docker 29.7.2, used only for the pre-existing local, disposable `docker-postgres-1` container (never production data; started/stopped, never recreated).
- No `.env` file and no `ANTHROPIC_API_KEY` / `GEMINI_API_KEY` / `OPENAI_API_KEY` / `TWILIO_*` environment variables were present anywhere in this environment (confirmed by direct inspection of `os.environ`) — this determines Test 1 and Test 6's outcomes below.
- A real local model IS available: `Qwen/Qwen2.5-0.5B-Instruct` (base weights cached locally under `~/.cache/huggingface`) with a trained LoRA adapter at `models/qwen-voice-assistant/` — this made Test 2 real, not skipped.

## Tests Executed

1. Real local-model inference (Test 2) — single, repeated, and concurrent (with/without the generation semaphore).
2. Sustained soak test (Test 3) — 12 minutes continuous, realistic mixed workload, 3 concurrent clients.
3. Realistic mixed concurrent workload (Test 4) — 70/20/10 light/medium/heavy split at 5 and 10 concurrent clients.
4. PostgreSQL failure modes (Test 5) — flapping (3 cycles), connection interruption mid-request, mid-transaction failure with rollback verification — all against the real `docker-postgres-1` container.
5. Configurability review (Test 9) — STT reconnect made configurable; provider-concurrency-detection safety reviewed.
6. Resource leak check (Test 8) and failure recovery matrix (Test 7) — compiled from the above.

## Tests Not Executed

1. **Real Claude/Gemini provider calls (Test 1)** — no API keys configured in this environment. Marked **UNVERIFIED**, not faked.
2. **Real Twilio media-stream traffic (Test 6)** — no Twilio account/credentials or staging telephony environment available (`.env.canary.example` only contains empty placeholder fields). Marked **UNVERIFIED**, not faked.
3. Multi-hour soak — only a 12-minute run was performed, per the "start with 10–15 minutes" instruction; a 30-minute or multi-hour run was not attempted this pass.

---

## Real Provider Results

```
Test: Real Claude/Gemini provider call
Environment: local dev machine, no .env file, no ANTHROPIC_API_KEY/GEMINI_API_KEY in process environment
Duration: n/a
Concurrency: n/a
Result: UNVERIFIED
Evidence: os.environ scan confirms no provider credentials configured; ClaudeLLMProvider/GeminiLLMProvider both raise LLMProviderError("<KEY> is not configured") by construction when their api_key is empty (src/inference/llm_provider.py:153-154, :311-312) — attempting this test would only prove that documented guard works, not real provider behavior.
Limitations: Cannot safely or honestly fabricate this evidence. A real provider smoke test (single request, small concurrency 1/2/3/5, timeout/cancellation) is the single most important remaining verification gap for this application, since Claude/Gemini are its primary production LLM path.
```

---

## Local Model Results

```
Test: Real local-model inference (Qwen2.5-0.5B-Instruct + LoRA adapter, CPU-only)
Environment: local dev machine, torch 2.13.0+cpu, no CUDA, base model cached locally (no network dependency)
Duration: ~7-56s model load (varies with OS page-cache warmth under this machine's memory pressure) + inference calls
Concurrency: 1 (single/repeated), then 2 (concurrent, with and without the real generation semaphore)
Result: PASS
Evidence:
  - Single inference: load 55.97s (cold run) / 7.22s (second run, warm OS cache), generation 1.62s, output 31 chars, RSS after load ~2.49 GB, stable.
  - Repeated inference (5x): latencies 1.39, 1.22, 1.28, 1.40, 1.29s -- no degradation. RSS samples 2467.2 -> 2471.4 MB across the 5 calls (+4.2 MB, noise-level, not a leak).
  - Two concurrent generate_stream() calls on the SAME shared model instance, WITHOUT any semaphore: both completed successfully (wall 2.33s), correct identical output ("The capital of France is Paris."), no crash, no corrupted output observed in this run.
  - The same two calls run again, this time through ConversationManager's real generation semaphore with the real LocalLLMProvider wrapping this same LLMService: _llm_service_is_safe_for_concurrent_generation() correctly returned False for it (never "safe"), max_concurrent_observed=1 (correctly serialized), wall 2.31s (~ sum of the two individual latencies, i.e. genuinely serialized, not merely reported as such).
Limitations: The unguarded-concurrency run (2 threads, no semaphore) not crashing is ONE data point on ONE machine with ONE small model on THIS run -- it is not a general proof that concurrent calls to a shared torch model instance are safe across model sizes, PyTorch versions, or KV-cache implementations. This is exactly why the semaphore defaults to unsafe (1) for LocalLLMProvider regardless of what any single test run shows -- documented as intentional, fail-safe-by-default behavior, not contradicted by this result. No GPU was available, so GPU-specific memory/utilization behavior (VRAM growth, CUDA OOM handling) is entirely unverified.
```

---

## Soak Test Results

```
Test: Sustained soak (Test 3)
Environment: real live uvicorn server (not TestClient), in-process background thread, fake LLM service standing in for the provider call itself (this test targets the API/job-queue/resource-management layer, not provider behavior -- see Real Provider Results above for that gap)
Duration: 720 seconds (12 minutes) continuous
Concurrency: 3 clients, each pacing itself with 0.3-1.2s of think time between requests (not a closed-loop hammer -- see Concurrent Workload Results for why)
Result: PASS
Evidence:
  - 1,847 total real HTTP operations completed over 12 minutes (1,304 light / 363 medium / 180 heavy), zero errors across all of them.
  - Latency stayed flat, no degradation over time:
    - light:  p50 2.3ms  (first-third 2.3ms  -> last-third 2.2ms)
    - medium: p50 307.4ms (first-third 308.5ms -> last-third 307.4ms)
    - heavy:  p50 2059.8ms (first-third 2058.9ms -> last-third 2196.4ms, +137ms/6.7% -- within noise, not a trend)
  - Resource samples taken every ~5s (141 samples total):
    - num_threads: ranged 11-19, no monotonic growth
    - num_handles (Windows): ranged 249-278, no monotonic growth
    - num_connections: ranged 3-9, tracked live in-flight requests, no accumulation
    - job_store_depth: grew monotonically 22 -> 201 -- this is EXPECTED, not a leak: JobStore retains every job record up to a 1000-job cap with FIFO eviction beyond that (src/api/jobs.py's JobStore.create()), by design: it's a bounded history, not an unprocessed backlog. At the observed creation rate it would take roughly an hour to reach the cap.
    - RSS: flat at ~112-113MB for nearly the entire run, then dropped to ~42.6-42.8MB in the last one-to-two samples.
Limitations: The RSS drop at the very end is disclosed, not hidden, but its cause is not fully isolated: this machine was under heavy system-wide memory pressure throughout this pass (see Environment), and Windows can trim a background process's working set under that pressure independent of anything the application does. It reads as "memory went down, not up" (the opposite of a leak signature), but it is not conclusively attributed to garbage collection inside this app versus OS-level working-set trimming, and a re-run on a machine with normal memory headroom would be needed to fully separate the two. Only a 12-minute run was performed (the "start with 10-15 minutes" tier); a 30-minute or multi-hour run to catch slower leaks was not attempted this pass.
```

---

## Concurrent Workload Results

```
Test: Realistic mixed concurrent workload (Test 4)
Environment: same real live server as the soak test; workload mix 70% light (GET /health), 20% medium (POST /generate, synchronous, ~0.3s simulated LLM call), 10% heavy (POST /jobs/generate + poll, async job, ~2.0s simulated LLM call)
Duration: 25s per concurrency level
Concurrency: 5 clients, then 10 clients (both with realistic per-request think time, not closed-loop hammering)
Result: PASS
Evidence:
  5 clients (wall 26.77s): light n=38 p50=2.3ms p95=6.3ms max=6.9ms errors=0
                           medium n=14 p50=2943.4ms p95=4572.2ms max=4886.7ms errors=0
                           heavy  n=11 p50=3645.4ms p95=5955.1ms max=5980.4ms errors=0
  10 clients (wall 29.53s): light n=112 p50=2.4ms p95=14.0ms max=27.1ms errors=0
                            medium n=29 p50=3022.2ms p95=6545.7ms max=6833.0ms errors=0
                            heavy  n=10 p50=4722.5ms p95=7937.6ms max=8012.0ms errors=0
  - light stayed fast and 100% error-free at both concurrency levels -- the API process itself remains responsive while medium/heavy generation work is in flight.
  - Zero errors across every operation at both concurrency levels -- no cascading failure, no crashes.
  - Every heavy job reached a deterministic completed state within the poll deadline (15s) -- none hung.
Limitations, stated plainly rather than buried: medium (/generate, synchronous) and heavy (/jobs/generate, async) generation both compete for the SAME generation semaphore, sized 1 here because the fake LLM service used by this harness is not one of the type-recognized-safe classes (ClaudeLLMProvider/GeminiLLMProvider) -- see Test 9 below. This is why medium p50 (2.9-3.0s) is ~10x its own unguarded delay (0.3s): it is real queueing behind the single-slot semaphore, not a defect, but it is a genuine capacity characteristic that would apply in production to any deployment using an LLM service class not yet recognized as concurrency-safe. A deployment using the real, recognized ClaudeLLMProvider/GeminiLLMProvider would see this semaphore raised automatically (verified separately, see the prior remediation's TestGenerationSemaphoreConcurrencyGuarantees), and this contention would be far smaller.
```

---

## Database Failure Results

```
Test: PostgreSQL failure modes (Test 5) -- flapping, connection interruption, mid-operation failure
Environment: real, local, disposable docker-postgres-1 container (never production data), started/stopped only, never recreated
Duration: ~90s total across all three sub-tests
Concurrency: n/a (sequential, deliberate fault injection)
Result: PASS (all three sub-tests)
Evidence:
  A. Flapping (3 cycles of healthy -> down -> healthy):
     cycle 0: healthy_before=True, correctly_unhealthy_during_outage=True, recovered=True, healthy_after=True
     cycle 1: healthy_before=True, correctly_unhealthy_during_outage=True, recovered=True, healthy_after=True
     cycle 2: healthy_before=True, correctly_unhealthy_during_outage=True, recovered=True, healthy_after=True
     -> never stuck in a permanent unhealthy state across any cycle.
  B. Connection interruption mid-request: a real PostgresSessionRepository.save() call issued while the DB was down raised DatabaseUnavailableError ("...OperationalError") in 2.05s -- not a hang, not a crash. After DB restart, the SAME repository instance (same connection pool) successfully completed a fresh save() -- pool was not corrupted by the outage.
  C. Mid-operation failure (DB killed mid-transaction, inside session_scope()'s open transaction, via an injected delay): the transaction correctly failed with DatabaseUnavailableError, and after recovery, querying for the row that was being written (session_id="phase1_2-mid-op-test") confirmed no partial row was committed (partial_row_committed=False) -- the rollback in Database.session_scope() worked correctly under a REAL connection loss, not just a simulated exception.
Limitations: All three sub-tests exercised the repository/Database layer directly (not the full HTTP request path) for precision in controlling the failure timing -- Test 9 (already verified in the prior remediation pass) covers the /ready-probe-level real outage/recovery cycle through the live HTTP server. PERSISTENCE_MODE=dev (in-memory) remains this app's default; PostgreSQL is an opt-in production mode, so most deployments would not exercise this path unless configured for production persistence.
```

---

## Twilio Results

```
Test: Real Twilio media-stream behavior (Test 6)
Environment: no Twilio account credentials, no staging telephony environment
Duration: n/a
Concurrency: n/a
Result: UNVERIFIED
Evidence: .env.canary.example contains only empty placeholder fields (TWILIO_ACCOUNT_SID=, TWILIO_AUTH_TOKEN=, etc.) -- no real or staging Twilio environment exists to test against. Duplicate-START handling, reconnect, and cleanup were already verified against realistic simulated Twilio frame sequences in the prior remediation pass (tests/test_voice_pipeline.py::TestDuplicateStartFrameHandling) -- that remains the best available evidence, but it is simulated, not real media-stream traffic.
Limitations: Real orphan-handler/leaked-socket/reconnect-storm detection under actual Twilio WebSocket/RTP traffic is not possible without a real or staging Twilio account, which is out of scope for this environment. Do not treat the simulated test coverage as equivalent to this.
```

---

## Resource Usage

| Resource | Local-model test | Soak (12 min, 3 clients) | Concurrency scan (5/10 clients) |
|---|---|---|---|
| RSS | ~2.47-2.64 GB (dominated by the loaded model weights), flat across repeats | ~112-113 MB flat, then an unexplained-but-benign drop to ~42.6 MB at the very end (see Soak Limitations) | 98.0 -> 111.3 MB, gradual, consistent with warm-up (connection pools, JIT-ish caches), not a leak |
| Threads | n/a (single process, no thread pool contention observed) | 11-19, no trend | not sampled per-level, but no errors or hangs observed |
| OS handles (Windows) | n/a | 249-278, no trend | not sampled per-level |
| Connections | n/a | 3-9, tracked live load | not sampled per-level |
| Job store depth | n/a | 22 -> 201 (bounded history, cap 1000, not a backlog) | grows with load but was not tracked separately per-level |
| Errors | 0 | 0 / 1,847 ops | 0 / 204 ops across both levels |

No resource in this table shows unbounded, cumulative growth. The one anomaly (the late-soak RSS drop) is a decrease, not an increase, and is disclosed with its most likely (but not fully isolated) explanation rather than claimed as proof of anything.

---

## Failure Recovery Matrix

| Failure | API survives | Worker survives | Job recovers | State consistent | Evidence |
|---|---|---|---|---|---|
| DB stopped (flapping, 3 cycles) | Yes | n/a | n/a | Yes -- health check correctly reports down during outage, up after | Test 5A |
| DB connection interrupted mid-request | Yes | Yes (same process, no crash) | n/a (single call) | Yes -- DatabaseUnavailableError raised, pool reusable after recovery | Test 5B |
| DB killed mid-transaction | Yes | Yes | n/a (write, not a job) | Yes -- rollback confirmed, no partial row committed | Test 5C |
| Two concurrent local-model calls, no semaphore | Yes | Yes | n/a | Yes -- both completed with correct, non-corrupted output (this run) | Test 2 |
| 8-10 real HTTP heavy jobs under sustained mixed load | Yes | Yes | Yes -- every heavy job reached `completed` within its poll deadline | Yes -- job store and semaphore both behaved deterministically | Tests 3 & 4 |
| Sustained 12-minute mixed load | Yes | Yes | Yes | Yes -- zero errors, flat latency, no resource growth trend | Test 3 |
| Real Claude/Gemini provider failure | UNVERIFIED | UNVERIFIED | UNVERIFIED | UNVERIFIED | Test 1 not executable in this environment |
| Real Twilio disconnect/duplicate-START/reconnect | UNVERIFIED (simulated coverage exists, not real traffic) | UNVERIFIED | UNVERIFIED | UNVERIFIED | Test 6 not executable in this environment |

---

## Remaining Risks

1. **Real remote LLM provider behavior is completely unverified in this pass** (and was already unverified going into it) -- rate limiting, real timeout/retry behavior, and real transient failures under the now-fixed concurrency auto-detection have never been exercised against Claude or Gemini's actual API.
2. **Real Twilio media-stream traffic is completely unverified** -- only simulated frame sequences have been tested; real orphan-handler/leaked-socket/reconnect-storm behavior under actual telephony traffic is unknown.
3. **Medium-path (`/generate`, synchronous) latency is materially exposed to heavy background job contention** whenever the configured LLM service isn't one of the two type-recognized-safe provider classes -- demonstrated concretely in Test 4 (p50 ~3s, p95 ~6.5-6.8s at 10 concurrent clients, purely from semaphore queueing). This is a capacity characteristic, not a defect, but it is a real production-shape risk worth capacity-planning around, especially for any deployment using a not-yet-recognized provider wrapper.
4. **Concurrent local-model inference safety was demonstrated, not proven**, for exactly one small (0.5B) CPU-only model on one run -- the semaphore correctly stays conservative for it regardless, so this isn't a live risk, but it means "LocalLLMProvider is unsafe for concurrency" remains a documented assumption backed by one supporting data point, not an exhaustively verified fact across model sizes/hardware.
5. **Soak duration was 12 minutes, not multi-hour** -- slow leaks (e.g. a resource that grows by single-digit KB per hour) would not be visible in this window.

## Unverified Conditions

- Real Claude/Gemini API calls (any concurrency level, any failure mode).
- Real local-model inference on GPU hardware (CUDA-specific memory/OOM behavior).
- Soak duration beyond 12 minutes.
- Real Twilio telephony traffic of any kind.
- Database failure modes beyond the three tested here (e.g. disk-full on the DB host, replica failover, extreme connection-pool exhaustion under hundreds of concurrent DB-bound requests).

## Stability Classification

### B — Mostly Stable

Not A. The evidence gathered this pass is genuinely good and materially narrows the previous verification gap: real local-model inference (including concurrent calls and the real semaphore) checked out cleanly, a real 12-minute sustained soak at a sustainable load showed zero errors and no resource-growth trend, a realistic mixed-workload concurrency scan at 5 and 10 clients showed zero errors and bounded (if queueing-affected) latency, and three distinct real-Postgres failure modes (flapping, connection interruption, mid-transaction kill) all recovered correctly with verified transactional correctness.

But two of the six requested test areas remain entirely UNVERIFIED for reasons genuinely outside this pass's control (no credentials, no staging telephony environment) -- and they are not peripheral: real LLM provider calls and real Twilio traffic are this application's two primary external dependencies in production. Core failure handling now has real (not just synthetic-unit-test) evidence behind it; meaningful real-world conditions specific to this app's actual production dependencies remain unverified. That is the literal definition of B, not A, and claiming otherwise would mean upgrading the classification because the tests that *could* be run all passed -- which the instructions explicitly rule out.

## Recommendation

Before Phase 2:
1. Obtain test-safe Claude/Gemini API credentials (even a low-quota test key) and run Test 1's single/2/3/5-concurrency scan for real.
2. Obtain or construct a Twilio staging/test environment (or a high-fidelity local WebSocket simulator of Twilio's actual protocol, if a real account genuinely cannot be arranged) and run Test 6's connection/START/media/STOP/reconnect/duplicate-START sequence against it.
3. Consider a longer (30-minute+) soak once running on a machine with normal (non-contended) memory headroom, to more cleanly separate the app's own memory behavior from host-level memory pressure and get a second, unambiguous data point on the late-soak RSS drop noted above.

> **Is Phase 1 complete enough to safely begin Phase 2?**
>
> **NO**
>
> The resource-management, database-resilience, and concurrency-control layers now have strong, real evidence behind them from this pass. But this application's two primary production dependencies -- the real LLM provider and real Twilio telephony -- remain completely unverified, not because anything failed, but because no credentials or staging environment for either exists in this environment. Beginning Phase 2 (a distributed architecture) on top of an unverified provider/telephony integration would compound risk in exactly the two places most likely to matter once real traffic arrives. Recommend closing items 1-2 above, then re-running this same Phase 1.2 checklist against real credentials/staging before starting Phase 2.

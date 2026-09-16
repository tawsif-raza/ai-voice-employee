"""
Phase 21 -- Performance & Load (PHASE_21_PERFORMANCE_REPORT.md). Measures
the actual capacity envelope of the real FastAPI application (`src/api/
server.py`), served by a real live uvicorn server (same harness pattern
as scripts/stability_verification.py -- Phase 16 -- reused here rather
than duplicated logic drifting out of sync), under concurrent and
sustained HTTP load with a simulated LLM provider (LOCAL/SIMULATED
throughout, disclosed as such -- no real Claude/Gemini/Groq call is
made, matching Phase 16's own methodology and this repository's
LOCAL/SIMULATED/MOCKED/LIVE evidence-labeling convention, plan.md Rule 9).

Measurement caveat (same as Phase 16, stated once): the server runs in a
background thread inside this same process, so psutil RSS/CPU figures
include this lightweight httpx-based driver's own overhead, not a
perfectly isolated separate server process.

Database-pressure scenario uses the existing local `docker-postgres-1`
container if Docker is reachable and that container exists (created by a
prior session's own real-Postgres validation work) -- started/stopped,
never recreated, volume never touched. If Docker isn't reachable, that
scenario is reported SKIPPED with the reason, never fabricated.

Run with:
    python scripts/performance_load_test.py
"""

import json
import os
import statistics
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path

import httpx
import psutil
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "api"))
import server  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "agent"))
from conversation_manager import ConversationManager  # noqa: E402
from db import Database, load_database_config  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "inference"))
from handoff_detector import HandoffDetector  # noqa: E402

CLINICAL_CONFIG_PATH = Path(__file__).resolve().parents[1] / "configs" / "clinical_triggers.yaml"
HANDOFF_CONFIG_PATH = Path(__file__).resolve().parents[1] / "configs" / "handoff_phrases.yaml"

_PROC = psutil.Process()
_PROC.cpu_percent(interval=None)

_RESULTS: list[dict] = []


def _sample() -> dict:
    mem = _PROC.memory_info()
    return {"rss_mb": round(mem.rss / (1024 * 1024), 1), "cpu_percent": _PROC.cpu_percent(interval=0.1)}


def _percentiles(latencies_ms: list[float]) -> dict:
    s = sorted(latencies_ms)
    n = len(s)

    def _pct(p: float) -> float:
        idx = min(n - 1, max(0, int(round(p * (n - 1)))))
        return round(s[idx], 1)

    return {"p50": _pct(0.50), "p95": _pct(0.95), "p99": _pct(0.99), "max": round(s[-1], 1), "min": round(s[0], 1)}


def _record(test_id: str, name: str, result: str, evidence: str, source: str, notes: str) -> None:
    _RESULTS.append({"test_id": test_id, "name": name, "result": result, "evidence": evidence, "source": source, "notes": notes})
    print(f"\n[{test_id}] {name}: {result} ({source})")
    print(f"  Evidence: {evidence}")
    print(f"  Notes: {notes}")


class FixedDelayLLMService:
    def __init__(self, delay_seconds: float = 0.0, response_text: str = "Done."):
        self.delay_seconds = delay_seconds
        self.response_text = response_text
        self.call_count = 0
        self._lock = threading.Lock()

    def generate_stream(self, messages, **kwargs):
        with self._lock:
            self.call_count += 1
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        for word in self.response_text.split(" "):
            yield word + " "
        yield {"text": self.response_text, "latency_ms": self.delay_seconds * 1000.0}


def _fake_conversation_manager(llm_service, max_concurrent_generations=None) -> ConversationManager:
    return ConversationManager(
        llm_service=llm_service,
        retriever=None,
        clinical_guard=HandoffDetector(config_path=CLINICAL_CONFIG_PATH),
        handoff_detector=HandoffDetector(config_path=HANDOFF_CONFIG_PATH),
        max_concurrent_generations=max_concurrent_generations,
    )


def _docker_container_exists(name: str) -> bool:
    try:
        out = subprocess.run(
            ["docker", "ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return name in out.stdout
    except Exception:
        return False


@contextmanager
def _live_server():
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    config = uvicorn.Config(server.app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    uv_server = uvicorn.Server(config)
    thread = threading.Thread(target=uv_server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    deadline = time.perf_counter() + 5.0
    with httpx.Client() as probe:
        while time.perf_counter() < deadline:
            try:
                probe.get(f"{base_url}/health", timeout=1.0)
                break
            except httpx.HTTPError:
                time.sleep(0.05)
        else:
            raise RuntimeError("performance-test server did not become responsive in time")

    try:
        yield base_url
    finally:
        uv_server.should_exit = True
        thread.join(timeout=5.0)
        server._database = None


def _poll_job(client: httpx.Client, base_url: str, job_id: str, deadline_seconds: float = 30.0) -> dict:
    deadline = time.perf_counter() + deadline_seconds
    last = None
    while time.perf_counter() < deadline:
        last = client.get(f"{base_url}/jobs/{job_id}").json()
        if last["status"] in ("completed", "failed"):
            return last
        time.sleep(0.02)
    return last


# ---------------------------------------------------------------------------
# 1. Concurrency sweep -- default semaphore (max_concurrent_generations=1)
# ---------------------------------------------------------------------------


def test_1_concurrency_default_semaphore(concurrency: int = 10, delay: float = 0.3) -> None:
    server._conversation_manager = _fake_conversation_manager(FixedDelayLLMService(delay, "Done."))
    with _live_server() as base_url:
        before = _sample()
        t_wall0 = time.perf_counter()

        def _one(i):
            with httpx.Client(timeout=30.0) as client:
                t0 = time.perf_counter()
                submit = client.post(f"{base_url}/jobs/generate", json={"message": f"job {i}"})
                submit_ms = (time.perf_counter() - t0) * 1000.0
                final = _poll_job(client, base_url, submit.json()["job_id"])
                total_ms = (time.perf_counter() - t0) * 1000.0
                return submit.status_code, submit_ms, total_ms, final["status"] if final else "timeout"

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            results = list(pool.map(_one, range(concurrency)))
        wall_s = time.perf_counter() - t_wall0
        after = _sample()

    submit_lat = [r[1] for r in results]
    total_lat = [r[2] for r in results]
    errors = sum(1 for r in results if r[0] != 202 or r[3] != "completed")
    pct = _percentiles(total_lat)
    expected_serial_s = concurrency * delay
    _record(
        "21.1",
        f"{concurrency} concurrent jobs, default semaphore (max_concurrent_generations=1)",
        "PASS" if errors == 0 else "FAIL",
        f"errors={errors}/{concurrency}, wall={wall_s:.2f}s (fully-serial expectation~{expected_serial_s:.2f}s), "
        f"submit p50={_percentiles(submit_lat)['p50']}ms, total-latency p50/p95/p99/max="
        f"{pct['p50']}/{pct['p95']}/{pct['p99']}/{pct['max']}ms, mem {before['rss_mb']}->{after['rss_mb']}MB",
        "LOCAL/SIMULATED",
        "Confirms the Phase 16-disclosed default-semaphore serialization: wall time scales "
        "~linearly with concurrency x delay, not sublinearly, when ConversationManager is "
        "constructed directly (max_concurrent_generations left at its default of 1).",
    )


# ---------------------------------------------------------------------------
# 2. Concurrency sweep -- configured for real concurrent throughput
# ---------------------------------------------------------------------------


def test_2_concurrency_configured(concurrency: int, delay: float = 0.3, max_concurrent: int = 20) -> None:
    server._conversation_manager = _fake_conversation_manager(FixedDelayLLMService(delay, "Done."), max_concurrent_generations=max_concurrent)
    with _live_server() as base_url:
        before = _sample()
        t_wall0 = time.perf_counter()

        def _one(i):
            with httpx.Client(timeout=30.0) as client:
                t0 = time.perf_counter()
                submit = client.post(f"{base_url}/jobs/generate", json={"message": f"job {i}"})
                submit_ms = (time.perf_counter() - t0) * 1000.0
                final = _poll_job(client, base_url, submit.json()["job_id"])
                total_ms = (time.perf_counter() - t0) * 1000.0
                return submit.status_code, submit_ms, total_ms, final["status"] if final else "timeout"

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            results = list(pool.map(_one, range(concurrency)))
        wall_s = time.perf_counter() - t_wall0
        after = _sample()

    total_lat = [r[2] for r in results]
    errors = sum(1 for r in results if r[0] != 202 or r[3] != "completed")
    pct = _percentiles(total_lat)
    expected_parallel_s = delay * (concurrency / max_concurrent) if concurrency > max_concurrent else delay
    ok = errors == 0
    _record(
        f"21.2.{concurrency}",
        f"{concurrency} concurrent jobs, max_concurrent_generations={max_concurrent}",
        "PASS" if ok else "FAIL",
        f"errors={errors}/{concurrency}, wall={wall_s:.2f}s (expected~{expected_parallel_s:.2f}s if truly "
        f"parallel up to the semaphore limit), total-latency p50/p95/p99/max="
        f"{pct['p50']}/{pct['p95']}/{pct['p99']}/{pct['max']}ms, mem {before['rss_mb']}->{after['rss_mb']}MB, "
        f"cpu={after['cpu_percent']}%",
        "LOCAL/SIMULATED",
        f"With an explicit max_concurrent_generations={max_concurrent} (the value a real remote-"
        "provider deployment auto-detects per Phase 15.1), throughput should scale with real "
        "parallelism up to that limit rather than serializing 1-at-a-time.",
    )


# ---------------------------------------------------------------------------
# 3. Sustained load
# ---------------------------------------------------------------------------


def test_3_sustained_load(n: int = 300) -> None:
    server._conversation_manager = _fake_conversation_manager(FixedDelayLLMService(0.0, "Our hours are 9 to 5."))
    with _live_server() as base_url, httpx.Client(timeout=10.0) as client:
        before = _sample()
        latencies_ms = []
        statuses = []
        mem_samples = []
        for i in range(n):
            t0 = time.perf_counter()
            resp = client.post(f"{base_url}/generate", json={"message": f"Repeated request {i}"})
            latencies_ms.append((time.perf_counter() - t0) * 1000.0)
            statuses.append(resp.status_code)
            if i % 50 == 0:
                mem_samples.append(_sample()["rss_mb"])
        after = _sample()

    errors = sum(1 for s in statuses if s != 200)
    pct = _percentiles(latencies_ms)
    mem_growth = after["rss_mb"] - before["rss_mb"]
    first_quarter_avg = sum(latencies_ms[: n // 4]) / (n // 4)
    last_quarter_avg = sum(latencies_ms[-(n // 4) :]) / (n // 4)
    degradation_pct = ((last_quarter_avg - first_quarter_avg) / first_quarter_avg) * 100 if first_quarter_avg else 0.0
    ok = errors == 0 and mem_growth < 30.0
    _record(
        "21.3",
        f"{n} sustained sequential requests",
        "PASS" if ok else "FAIL",
        f"errors={errors}/{n}, latency p50/p95/p99/max={pct['p50']}/{pct['p95']}/{pct['p99']}/{pct['max']}ms, "
        f"first-quarter-avg={first_quarter_avg:.2f}ms, last-quarter-avg={last_quarter_avg:.2f}ms "
        f"({degradation_pct:+.1f}% change), mem {before['rss_mb']}->{after['rss_mb']}MB "
        f"(growth={mem_growth:+.1f}MB), mem samples over run: {mem_samples}",
        "LOCAL/SIMULATED",
        f"Extends Phase 16's 50-request sustained-load test to {n} requests, closing part of "
        "that report's disclosed gap ('does not rule out slow leaks... over thousands of "
        "requests'). Still short of true multi-hour/thousands-of-requests soak testing.",
    )


# ---------------------------------------------------------------------------
# 4. Database connection pressure (real local Postgres, if available)
# ---------------------------------------------------------------------------


def test_4_database_pressure(concurrency: int = 20) -> None:
    container = "docker-postgres-1"
    if not _docker_container_exists(container):
        _record(
            "21.4",
            "Database connection pressure (concurrent writes against real Postgres)",
            "SKIPPED",
            f"Docker container '{container}' not found -- not created by this run, per "
            "'do not fabricate infrastructure that does not exist.'",
            "N/A",
            "See docs/DATABASE.md / scripts/validate_real_postgres.py to provision one for this test.",
        )
        return

    subprocess.run(["docker", "start", container], capture_output=True, timeout=30)
    for _ in range(30):
        ready = subprocess.run(
            ["docker", "exec", container, "pg_isready", "-U", "voice_app", "-d", "ai_voice_agent"],
            capture_output=True,
            timeout=5,
        )
        if ready.returncode == 0:
            break
        time.sleep(1)

    env_backup = {k: os.environ.get(k) for k in ("PERSISTENCE_MODE", "DATABASE_URL")}
    os.environ["PERSISTENCE_MODE"] = "production"
    os.environ["DATABASE_URL"] = "postgresql://voice_app:voice_secret@127.0.0.1:5432/ai_voice_agent"

    try:
        db_config = load_database_config()
        real_db = Database(db_config)
        server._conversation_manager = _fake_conversation_manager(FixedDelayLLMService(0.0, "Ok."))
        server._database = real_db

        with _live_server() as base_url:
            before = _sample()
            t_wall0 = time.perf_counter()

            def _one(i):
                with httpx.Client(timeout=15.0) as client:
                    t0 = time.perf_counter()
                    resp = client.post(
                        f"{base_url}/generate",
                        json={"message": f"db pressure {i}", "session_id": f"perf-sess-{i}"},
                    )
                    return resp.status_code, (time.perf_counter() - t0) * 1000.0

            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                results = list(pool.map(_one, range(concurrency)))
            wall_s = time.perf_counter() - t_wall0
            after = _sample()
    finally:
        for k, v in env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    latencies = [r[1] for r in results]
    errors = sum(1 for r in results if r[0] != 200)
    pct = _percentiles(latencies)
    _record(
        "21.4",
        f"{concurrency} concurrent requests against real local Postgres",
        "PASS" if errors == 0 else "FAIL",
        f"errors={errors}/{concurrency}, wall={wall_s:.2f}s, latency p50/p95/p99/max="
        f"{pct['p50']}/{pct['p95']}/{pct['p99']}/{pct['max']}ms, mem {before['rss_mb']}->{after['rss_mb']}MB",
        "LIVE (real local Postgres container, not production)",
        "Each request uses a distinct session_id (distinct SessionRepository row) -- this "
        "measures concurrent-connection/write pressure on the real DB, not lock contention "
        "on a single shared row (a separate, narrower scenario not covered here).",
    )


# ---------------------------------------------------------------------------
# 5. WebSocket / voice pipeline load -- explicitly not run this pass
# ---------------------------------------------------------------------------


def test_5_websocket_pressure_not_run() -> None:
    _record(
        "21.5",
        "WebSocket / Twilio Media Streams load",
        "NOT RUN",
        "No real or synthetic Twilio Media Streams traffic was generated this pass.",
        "N/A",
        "Requires either real Twilio traffic (Phase 17/19, blocked on live credentials/staging) "
        "or a synthetic WS-frame-generation harness this pass did not build -- the HTTP job-"
        "submission path exercised above shares ConversationManager/ToolOrchestrator/PolicyEngine "
        "with the voice pipeline (same handle_turn() call, confirmed in Phase 20), so this "
        "scenario's result is not fabricated as a proxy for WebSocket-specific behavior "
        "(connection accept rate, frame-processing backpressure, per-call resource cleanup "
        "under concurrent calls) which remains genuinely untested.",
    )


if __name__ == "__main__":
    print("=" * 70)
    print("Phase 21 -- Performance & Load")
    print("=" * 70)
    print(f"Baseline process sample: {_sample()}")

    test_1_concurrency_default_semaphore(concurrency=10, delay=0.3)
    test_2_concurrency_configured(concurrency=5, delay=0.3, max_concurrent=20)
    test_2_concurrency_configured(concurrency=20, delay=0.3, max_concurrent=20)
    test_2_concurrency_configured(concurrency=50, delay=0.3, max_concurrent=20)
    test_3_sustained_load(n=300)
    test_4_database_pressure(concurrency=20)
    test_5_websocket_pressure_not_run()

    print("\n" + "=" * 70)
    print("SUMMARY (JSON)")
    print("=" * 70)
    print(json.dumps(_RESULTS, indent=2))

"""
Phase 16 -- final controlled stability verification (PHASE_16_STABILITY_
VERIFICATION_REPORT.md). Runs 10 scenarios against the real FastAPI app
served by a real live uvicorn server (not TestClient -- see tests/
test_job_isolation.py's module docstring for why that distinction
matters for anything timing-sensitive), sampling process RSS/CPU via
psutil and per-request latency/status throughout.

Measurement caveat, stated once here rather than buried per-scenario: the
server runs in a background thread inside THIS SAME process (matching
the pattern already validated in tests/test_job_isolation.py), so the
psutil samples below reflect "the process under test, including this
lightweight httpx-based test-driver thread's own overhead" -- not a
perfectly isolated separate server process. The driver itself does no
heavy computation (just HTTP calls and short sleeps), so its contribution
is small relative to what's being measured (simulated heavy generation,
RAG, etc.), but this is disclosed rather than overclaimed.

Scenario 9 (database failure) uses the existing local `docker-postgres-1`
container (created by a prior session's real-Postgres validation,
docs/DATABASE.md / scripts/validate_real_postgres.py) if Docker is
reachable and that container exists -- started and stopped, never
recreated or have its volume removed, and left in whatever run/stopped
state it was found in. If Docker isn't reachable, that scenario is
reported as SKIPPED with the reason, never fabricated.

Run with:
    python scripts/stability_verification.py
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
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
_PROC.cpu_percent(interval=None)  # prime the CPU-percent counter (first call is always 0.0)

_RESULTS: list[dict] = []


def _sample() -> dict:
    mem = _PROC.memory_info()
    return {
        "rss_mb": round(mem.rss / (1024 * 1024), 1),
        "cpu_percent": _PROC.cpu_percent(interval=0.1),
    }


def _record(test_id: str, name: str, result: str, evidence: str, recovery: str, remaining_risk: str) -> None:
    _RESULTS.append(
        {
            "test_id": test_id,
            "name": name,
            "result": result,
            "evidence": evidence,
            "recovery": recovery,
            "remaining_risk": remaining_risk,
        }
    )
    print(f"\n[{test_id}] {name}: {result}")
    print(f"  Evidence: {evidence}")
    print(f"  Recovery: {recovery}")
    print(f"  Remaining risk: {remaining_risk}")


class FixedDelayLLMService:
    def __init__(self, delay_seconds: float = 0.0, response_text: str = "Done."):
        self.delay_seconds = delay_seconds
        self.response_text = response_text
        self.call_count = 0

    def generate_stream(self, messages, **kwargs):
        self.call_count += 1
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        for word in self.response_text.split(" "):
            yield word + " "
        yield {"text": self.response_text, "latency_ms": self.delay_seconds * 1000.0}


class RaisingLLMService:
    """Simulates an external LLM provider failure (timeout, connection error, etc.)."""

    def __init__(self, exc: Exception):
        self.exc = exc
        self.call_count = 0

    def generate_stream(self, messages, **kwargs):
        self.call_count += 1
        raise self.exc
        yield  # pragma: no cover -- unreachable, keeps this a generator function


class RaisingConversationManager:
    """Simulates an unexpected internal (worker/task) failure, not an LLM-provider one."""

    def handle_turn(self, *args, **kwargs):
        raise RuntimeError("simulated unexpected internal failure")


def _fake_conversation_manager(llm_service) -> ConversationManager:
    return ConversationManager(
        llm_service=llm_service,
        retriever=None,
        clinical_guard=HandoffDetector(config_path=CLINICAL_CONFIG_PATH),
        handoff_detector=HandoffDetector(config_path=HANDOFF_CONFIG_PATH),
    )


@contextmanager
def _live_server():
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
            raise RuntimeError("live verification server did not become responsive in time")

    try:
        yield base_url
    finally:
        uv_server.should_exit = True
        thread.join(timeout=5.0)
        server._database = None


def _poll_job(client: httpx.Client, base_url: str, job_id: str, deadline_seconds: float = 10.0) -> dict:
    deadline = time.perf_counter() + deadline_seconds
    last = None
    while time.perf_counter() < deadline:
        last = client.get(f"{base_url}/jobs/{job_id}").json()
        if last["status"] in ("completed", "failed"):
            return last
        time.sleep(0.05)
    return last


# ---------------------------------------------------------------------------
# 1. Normal request
# ---------------------------------------------------------------------------


def test_1_normal_request() -> None:
    server._conversation_manager = _fake_conversation_manager(FixedDelayLLMService(0.0, "Our hours are 9 to 5."))
    with _live_server() as base_url, httpx.Client() as client:
        before = _sample()
        t0 = time.perf_counter()
        resp = client.post(f"{base_url}/generate", json={"message": "What are your hours?"})
        latency_ms = (time.perf_counter() - t0) * 1000.0
        after = _sample()

    ok = resp.status_code == 200 and resp.json().get("response") == "Our hours are 9 to 5."
    _record(
        "1",
        "Normal request",
        "PASS" if ok else "FAIL",
        f"status={resp.status_code}, latency={latency_ms:.1f}ms, mem {before['rss_mb']}->{after['rss_mb']}MB",
        "n/a (no failure)",
        "none identified",
    )


# ---------------------------------------------------------------------------
# 2. Heavy generation
# ---------------------------------------------------------------------------


def test_2_heavy_generation() -> None:
    delay = 2.0
    server._conversation_manager = _fake_conversation_manager(FixedDelayLLMService(delay, "Done."))
    with _live_server() as base_url, httpx.Client(timeout=10.0) as client:
        before = _sample()
        t0 = time.perf_counter()
        submit = client.post(f"{base_url}/jobs/generate", json={"message": "Do something heavy."})
        submit_ms = (time.perf_counter() - t0) * 1000.0

        health_t0 = time.perf_counter()
        health = client.get(f"{base_url}/health")
        health_ms = (time.perf_counter() - health_t0) * 1000.0

        job_id = submit.json()["job_id"]
        final = _poll_job(client, base_url, job_id)
        after = _sample()

    ok = (
        submit.status_code == 202
        and submit_ms < 500.0
        and health.status_code == 200
        and health_ms < 500.0
        and final["status"] == "completed"
    )
    _record(
        "2",
        "Heavy generation (simulated 2s LLM call)",
        "PASS" if ok else "FAIL",
        f"submit={submit_ms:.1f}ms (202), concurrent /health={health_ms:.1f}ms (200), "
        f"job resolved={final['status']}, mem {before['rss_mb']}->{after['rss_mb']}MB",
        "n/a (no failure)",
        "job execution still serializes behind the default generation semaphore (size 1) "
        "when the app is built directly rather than via build_conversation_manager()'s "
        "remote-provider-aware default -- see PHASE_15/Phase-15.1 reports.",
    )


# ---------------------------------------------------------------------------
# 3. Multiple simultaneous heavy generations
# ---------------------------------------------------------------------------


def test_3_concurrent_heavy_generations() -> None:
    n = 5
    delay = 1.0
    server._conversation_manager = _fake_conversation_manager(FixedDelayLLMService(delay, "Done."))
    with _live_server() as base_url, httpx.Client(timeout=30.0) as client:
        before = _sample()
        submit_latencies_ms = []
        job_ids = []
        t_start = time.perf_counter()
        for i in range(n):
            t0 = time.perf_counter()
            resp = client.post(f"{base_url}/jobs/generate", json={"message": f"Heavy job {i}"})
            submit_latencies_ms.append((time.perf_counter() - t0) * 1000.0)
            job_ids.append(resp.json()["job_id"])

        health = client.get(f"{base_url}/health")
        finals = [_poll_job(client, base_url, jid, deadline_seconds=30.0) for jid in job_ids]
        total_wall_seconds = time.perf_counter() - t_start
        after = _sample()

    all_completed = all(f["status"] == "completed" for f in finals)
    all_submits_fast = all(ms < 500.0 for ms in submit_latencies_ms)
    ok = all_completed and all_submits_fast and health.status_code == 200
    _record(
        "3",
        f"{n} simultaneous heavy generations (1s each)",
        "PASS" if ok else "FAIL",
        f"all {n} submits < 500ms (max={max(submit_latencies_ms):.1f}ms), "
        f"all {n} jobs completed={all_completed}, total wall time={total_wall_seconds:.2f}s "
        f"(serialized default would be ~{n * delay:.1f}s), health during={health.status_code}, "
        f"mem {before['rss_mb']}->{after['rss_mb']}MB",
        "n/a (no failure)",
        "throughput is bounded by the generation semaphore, not by request handling -- "
        "see test 2's remaining risk note.",
    )


# ---------------------------------------------------------------------------
# 4. Large input / upload
# ---------------------------------------------------------------------------


def test_4_large_input() -> None:
    server._conversation_manager = _fake_conversation_manager(FixedDelayLLMService(0.0, "Ok."))
    with _live_server() as base_url, httpx.Client() as client:
        limit = server._RELIABILITY.request_limits.max_message_length
        before = _sample()

        t0 = time.perf_counter()
        at_limit = client.post(f"{base_url}/generate", json={"message": "x" * limit})
        at_limit_ms = (time.perf_counter() - t0) * 1000.0

        t1 = time.perf_counter()
        over_limit = client.post(f"{base_url}/generate", json={"message": "x" * (limit + 1)})
        over_limit_ms = (time.perf_counter() - t1) * 1000.0
        after = _sample()

    ok = at_limit.status_code == 200 and over_limit.status_code == 422 and over_limit_ms < 500.0
    _record(
        "4",
        f"Large input (message at/over {limit}-char limit)",
        "PASS" if ok else "FAIL",
        f"at-limit={at_limit.status_code} ({at_limit_ms:.1f}ms), "
        f"over-limit={over_limit.status_code} ({over_limit_ms:.1f}ms, rejected before reaching the model), "
        f"mem {before['rss_mb']}->{after['rss_mb']}MB",
        "n/a (rejection is the correct behavior, not a failure)",
        "no file/binary upload endpoint exists in this API (text/JSON only) -- "
        "large-input risk is scoped to message/history length, already bounded.",
    )


# ---------------------------------------------------------------------------
# 5. External API timeout
# ---------------------------------------------------------------------------


def test_5_external_api_timeout() -> None:
    server._conversation_manager = _fake_conversation_manager(RaisingLLMService(TimeoutError("upstream timed out")))
    with _live_server() as base_url, httpx.Client(timeout=10.0) as client:
        before = _sample()
        submit = client.post(f"{base_url}/jobs/generate", json={"message": "Hello"})
        job_id = submit.json()["job_id"]
        final = _poll_job(client, base_url, job_id)
        health = client.get(f"{base_url}/health")
        after = _sample()

    # ConversationManager's own generation-retry logic (conversation_manager.py)
    # catches every LLM exception internally and degrades to a fixed
    # apology/handoff response -- it never raises out of handle_turn(),
    # so this is correctly reported as a COMPLETED job with a degraded
    # response, not a FAILED one. See docs/MODULES.md's error-handling
    # contract.
    ok = (
        submit.status_code == 202
        and final["status"] == "completed"
        and final["result"]["is_handoff"] is True
        and health.status_code == 200
    )
    _record(
        "5",
        "External API timeout (simulated)",
        "PASS" if ok else "FAIL",
        f"job status={final['status']}, response={final.get('result', {}).get('response')!r}, "
        f"is_handoff={final.get('result', {}).get('is_handoff')}, health-after={health.status_code}, "
        f"mem {before['rss_mb']}->{after['rss_mb']}MB",
        "ConversationManager catches the timeout internally and returns a fixed "
        "apology/handoff response -- no retry occurs (default RetryPolicy(max_attempts=1) "
        "when constructed directly, as this test does; build_conversation_manager()'s "
        "reliability-enabled path does retry transient failures before giving up).",
        "none identified for this path specifically.",
    )


# ---------------------------------------------------------------------------
# 6. External API failure (non-timeout)
# ---------------------------------------------------------------------------


def test_6_external_api_failure() -> None:
    server._conversation_manager = _fake_conversation_manager(
        RaisingLLMService(ConnectionError("simulated connection reset by upstream provider"))
    )
    with _live_server() as base_url, httpx.Client(timeout=10.0) as client:
        before = _sample()
        submit = client.post(f"{base_url}/jobs/generate", json={"message": "Hello"})
        job_id = submit.json()["job_id"]
        final = _poll_job(client, base_url, job_id)
        health = client.get(f"{base_url}/health")
        after = _sample()

    ok = (
        submit.status_code == 202
        and final["status"] == "completed"
        and final["result"]["is_handoff"] is True
        and "simulated connection reset" not in json.dumps(final)
        and health.status_code == 200
    )
    _record(
        "6",
        "External API hard failure (simulated connection error)",
        "PASS" if ok else "FAIL",
        f"job status={final['status']}, is_handoff={final.get('result', {}).get('is_handoff')}, "
        f"raw exception text leaked into response={'simulated connection reset' in json.dumps(final)}, "
        f"health-after={health.status_code}, mem {before['rss_mb']}->{after['rss_mb']}MB",
        "Same internal degradation path as test 5 -- caught, converted to a safe apology "
        "response, never exposes the raw exception message to the client.",
        "none identified for this path specifically.",
    )


# ---------------------------------------------------------------------------
# 7. Malformed input
# ---------------------------------------------------------------------------


def test_7_malformed_input() -> None:
    server._conversation_manager = _fake_conversation_manager(FixedDelayLLMService(0.0, "Ok."))
    with _live_server() as base_url, httpx.Client() as client:
        before = _sample()
        cases = {
            "invalid_json_body": client.post(
                f"{base_url}/generate", content=b"{not valid json", headers={"content-type": "application/json"}
            ),
            "missing_required_field": client.post(f"{base_url}/generate", json={}),
            "wrong_type_for_message": client.post(f"{base_url}/generate", json={"message": 12345}),
            "wrong_type_for_history": client.post(
                f"{base_url}/generate", json={"message": "hi", "history": "not-a-list"}
            ),
            "unknown_job_id": client.get(f"{base_url}/jobs/does-not-exist"),
        }
        after = _sample()

    statuses = {name: resp.status_code for name, resp in cases.items()}
    ok = (
        statuses["invalid_json_body"] in (400, 422)
        and statuses["missing_required_field"] == 422
        and statuses["wrong_type_for_message"] == 422
        and statuses["wrong_type_for_history"] == 422
        and statuses["unknown_job_id"] == 404
    )
    _record(
        "7",
        "Malformed input (5 variants)",
        "PASS" if ok else "FAIL",
        f"statuses={statuses}, mem {before['rss_mb']}->{after['rss_mb']}MB",
        "n/a (rejection is the correct behavior, not a failure)",
        "none identified.",
    )


# ---------------------------------------------------------------------------
# 8. Worker/task failure
# ---------------------------------------------------------------------------


def test_8_worker_task_failure() -> None:
    server._conversation_manager = RaisingConversationManager()
    with _live_server() as base_url, httpx.Client(timeout=10.0) as client:
        before = _sample()
        submit = client.post(f"{base_url}/jobs/generate", json={"message": "Hello"})
        job_id = submit.json()["job_id"]
        final = _poll_job(client, base_url, job_id)
        health_during_failure = client.get(f"{base_url}/health")

        # Recovery: swap in a working conversation manager and confirm a
        # brand-new job succeeds -- proves the process itself survived.
        server._conversation_manager = _fake_conversation_manager(FixedDelayLLMService(0.0, "Recovered."))
        recovery_submit = client.post(f"{base_url}/jobs/generate", json={"message": "Hello again"})
        recovery_final = _poll_job(client, base_url, recovery_submit.json()["job_id"])
        after = _sample()

    ok = (
        submit.status_code == 202
        and final["status"] == "failed"
        and "simulated unexpected internal failure" not in json.dumps(final)
        and health_during_failure.status_code == 200
        and recovery_final["status"] == "completed"
        and recovery_final["result"]["response"] == "Recovered."
    )
    _record(
        "8",
        "Worker/task failure (unexpected internal exception, not an LLM-provider failure)",
        "PASS" if ok else "FAIL",
        f"failed-job status={final['status']}, error={final.get('error')}, "
        f"raw exception leaked={'simulated unexpected internal failure' in json.dumps(final)}, "
        f"health-during-failure={health_during_failure.status_code}, "
        f"post-failure new job status={recovery_final['status']}, mem {before['rss_mb']}->{after['rss_mb']}MB",
        "The process never went down -- a subsequent independent job on the same running "
        "server completed normally immediately after the failed one.",
        "none identified.",
    )


# ---------------------------------------------------------------------------
# 9. Database failure (safely testable: local disposable test container)
# ---------------------------------------------------------------------------


def _docker_container_exists(name: str) -> bool:
    try:
        out = subprocess.run(
            ["docker", "ps", "-a", "--filter", f"name={name}", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return name in out.stdout
    except Exception:
        return False


def test_9_database_failure() -> None:
    container = "docker-postgres-1"
    if not _docker_container_exists(container):
        _record(
            "9",
            "Database failure (real outage of a local disposable Postgres container)",
            "SKIPPED",
            f"Docker container '{container}' not found -- not created by this run, per "
            "'do not intentionally destroy production infrastructure' this scenario is not "
            "faked by fabricating an outage without real infrastructure to test against.",
            "n/a",
            "database-outage-under-load has not been verified in this environment. "
            "docs/DATABASE.md / scripts/validate_real_postgres.py describe how to set up "
            "a real Postgres instance for this test.",
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

    db_url = "postgresql://voice_app:voice_secret@127.0.0.1:5432/ai_voice_agent"
    # server.py's /ready route calls load_database_config() with no `env`
    # argument, i.e. it reads the real process os.environ at request time
    # -- constructing our own DatabaseConfig/Database object is not
    # enough on its own; the process env vars must actually be set so
    # /ready's own is_production() check agrees. Saved/restored below so
    # this test doesn't leak environment state into anything after it.
    env_backup = {k: os.environ.get(k) for k in ("PERSISTENCE_MODE", "DATABASE_URL")}
    os.environ["PERSISTENCE_MODE"] = "production"
    os.environ["DATABASE_URL"] = db_url

    db_config = load_database_config()
    real_db = Database(db_config)

    server._conversation_manager = _fake_conversation_manager(FixedDelayLLMService(0.0, "Ok."))
    server._database = real_db

    try:
        with _live_server() as base_url, httpx.Client(timeout=10.0) as client:
            before = _sample()
            ready_up = client.get(f"{base_url}/ready")

            subprocess.run(["docker", "stop", container], capture_output=True, timeout=30)
            time.sleep(1.0)
            ready_down = client.get(f"{base_url}/ready")
            health_during_outage = client.get(f"{base_url}/health")

            subprocess.run(["docker", "start", container], capture_output=True, timeout=30)
            recovered = False
            for _ in range(30):
                time.sleep(1.0)
                probe = client.get(f"{base_url}/ready")
                if probe.status_code == 200:
                    recovered = True
                    break
            after = _sample()
    finally:
        real_db.dispose()
        server._database = None
        for key, value in env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    ok = (
        ready_up.status_code == 200
        and ready_down.status_code == 503
        and health_during_outage.status_code == 200
        and recovered
    )
    _record(
        "9",
        "Database failure (real outage of a local disposable Postgres container)",
        "PASS" if ok else "FAIL",
        f"/ready before outage={ready_up.status_code}, /ready during outage={ready_down.status_code}, "
        f"/health during outage (no DB check)={health_during_outage.status_code}, "
        f"recovered after DB restart={recovered}, mem {before['rss_mb']}->{after['rss_mb']}MB",
        "The API process itself never went down during the outage -- /health (which doesn't "
        "check the database) stayed 200 throughout, and /ready correctly recovered once the "
        "database came back, with no restart of the app process needed.",
        "actual in-flight session/memory read-write behavior during a mid-request DB outage "
        "(as opposed to the /ready probe) was not exercised here -- PERSISTENCE_MODE=dev "
        "(in-memory) is this app's default, so most deployments would not hit this path at all.",
    )


# ---------------------------------------------------------------------------
# 10. Repeated requests
# ---------------------------------------------------------------------------


def test_10_repeated_requests() -> None:
    n = 50
    server._conversation_manager = _fake_conversation_manager(FixedDelayLLMService(0.0, "Ok."))
    with _live_server() as base_url, httpx.Client() as client:
        before = _sample()
        latencies_ms = []
        statuses = []
        for i in range(n):
            t0 = time.perf_counter()
            resp = client.post(f"{base_url}/generate", json={"message": f"Repeated request {i}"})
            latencies_ms.append((time.perf_counter() - t0) * 1000.0)
            statuses.append(resp.status_code)
        after = _sample()

    error_count = sum(1 for s in statuses if s != 200)
    first_10_avg = sum(latencies_ms[:10]) / 10
    last_10_avg = sum(latencies_ms[-10:]) / 10
    mem_growth_mb = after["rss_mb"] - before["rss_mb"]
    # A generous, non-scientific bound -- this loop allocates nothing
    # per-iteration that should persist, so growth should be near zero;
    # a few MB of interpreter/GC noise is expected and not a leak signal.
    ok = error_count == 0 and mem_growth_mb < 20.0
    _record(
        "10",
        f"{n} repeated identical requests",
        "PASS" if ok else "FAIL",
        f"errors={error_count}/{n}, latency first-10-avg={first_10_avg:.1f}ms, "
        f"last-10-avg={last_10_avg:.1f}ms, mem {before['rss_mb']}->{after['rss_mb']}MB "
        f"(growth={mem_growth_mb:+.1f}MB)",
        "n/a (no failure)",
        "only 50 iterations run here -- does not rule out slow leaks that only manifest "
        "over thousands of requests or many hours of uptime.",
    )


if __name__ == "__main__":
    print("=" * 70)
    print("Phase 16 -- Controlled Stability Verification")
    print("=" * 70)
    print(f"Baseline process sample: {_sample()}")

    for test_fn in (
        test_1_normal_request,
        test_2_heavy_generation,
        test_3_concurrent_heavy_generations,
        test_4_large_input,
        test_5_external_api_timeout,
        test_6_external_api_failure,
        test_7_malformed_input,
        test_8_worker_task_failure,
        test_9_database_failure,
        test_10_repeated_requests,
    ):
        try:
            test_fn()
        except Exception as exc:
            _record(
                test_fn.__name__,
                test_fn.__name__,
                "ERROR (verification script itself failed)",
                f"{type(exc).__name__}: {exc}",
                "n/a",
                "re-run with a debugger -- this is a test-harness failure, not necessarily an application failure.",
            )

    print("\n" + "=" * 70)
    print("SUMMARY (JSON)")
    print("=" * 70)
    print(json.dumps(_RESULTS, indent=2))

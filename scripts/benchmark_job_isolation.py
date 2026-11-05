"""
Before/after benchmark for Phase 15's heavy-work isolation (PHASE_15_
REQUEST_ISOLATION_REPORT.md). Measures, against a real live uvicorn
server (not TestClient -- see tests/test_job_isolation.py's module
docstring for why that distinction matters):

  BEFORE: POST /generate  -- blocks for the full duration of a simulated
          "heavy" LLM call, holding the HTTP connection + a worker thread
          the whole time.
  AFTER:  POST /jobs/generate -- returns almost immediately regardless of
          how long the underlying work takes; the caller polls
          GET /jobs/{job_id} separately.

Also measures whether an unrelated endpoint (/health) stays responsive
while the heavy work is in flight, under both the old and new endpoints.

Uses a fake LLM service (fixed-duration sleep, no torch/transformers, no
network) injected the same way tests/test_job_isolation.py and
test_server_api.py do -- this measures the isolation mechanism itself,
not model inference speed.

Run with:
    python scripts/benchmark_job_isolation.py
"""

import os
import socket
import sys
import threading
import time
from pathlib import Path

import httpx
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "api"))
# Local harness: development posture (anonymous text API), like tests/conftest.py.
os.environ.setdefault("APP_ENV", "dev")
import server  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "agent"))
from conversation_manager import ConversationManager  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "inference"))
from handoff_detector import HandoffDetector  # noqa: E402

CLINICAL_CONFIG_PATH = Path(__file__).resolve().parents[1] / "configs" / "clinical_triggers.yaml"
HANDOFF_CONFIG_PATH = Path(__file__).resolve().parents[1] / "configs" / "handoff_phrases.yaml"

HEAVY_WORK_SECONDS = 2.0


class FixedDelayLLMService:
    """Simulates a heavy LLM call of a fixed, known duration."""

    def __init__(self, delay_seconds: float, response_text: str = "Done."):
        self.delay_seconds = delay_seconds
        self.response_text = response_text

    def generate_stream(self, messages, **kwargs):
        time.sleep(self.delay_seconds)
        for word in self.response_text.split(" "):
            yield word + " "
        yield {"text": self.response_text, "latency_ms": self.delay_seconds * 1000.0}


def _fake_conversation_manager() -> ConversationManager:
    return ConversationManager(
        llm_service=FixedDelayLLMService(HEAVY_WORK_SECONDS),
        retriever=None,
        clinical_guard=HandoffDetector(config_path=CLINICAL_CONFIG_PATH),
        handoff_detector=HandoffDetector(config_path=HANDOFF_CONFIG_PATH),
    )


def _start_live_server() -> tuple[str, uvicorn.Server, threading.Thread]:
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
            raise RuntimeError("live benchmark server did not become responsive in time")
    return base_url, uv_server, thread


def _stop_live_server(uv_server: uvicorn.Server, thread: threading.Thread) -> None:
    uv_server.should_exit = True
    thread.join(timeout=5.0)


def measure_before() -> dict:
    """POST /generate: the client-perceived latency for submitting heavy work."""
    server._conversation_manager = _fake_conversation_manager()
    base_url, uv_server, thread = _start_live_server()
    try:
        with httpx.Client(timeout=HEAVY_WORK_SECONDS + 5.0) as client:
            t0 = time.perf_counter()
            resp = client.post(f"{base_url}/generate", json={"message": "Hello"})
            submit_elapsed = time.perf_counter() - t0
            return {
                "endpoint": "POST /generate (before)",
                "status_code": resp.status_code,
                "submit_call_elapsed_seconds": round(submit_elapsed, 3),
            }
    finally:
        _stop_live_server(uv_server, thread)


def measure_before_concurrent_health() -> dict:
    """
    While /generate is mid-flight in one thread, measure /health latency
    from another -- proving whether the OLD endpoint's long-held request
    starves an unrelated concurrent request of a thread/response.
    """
    server._conversation_manager = _fake_conversation_manager()
    base_url, uv_server, thread = _start_live_server()
    try:
        health_result = {}

        def _submit_heavy():
            with httpx.Client(timeout=HEAVY_WORK_SECONDS + 5.0) as client:
                client.post(f"{base_url}/generate", json={"message": "Hello"})

        heavy_thread = threading.Thread(target=_submit_heavy)
        heavy_thread.start()
        time.sleep(HEAVY_WORK_SECONDS / 2)  # ensure the heavy request is mid-flight

        with httpx.Client(timeout=5.0) as client:
            t0 = time.perf_counter()
            resp = client.get(f"{base_url}/health")
            health_result["elapsed_seconds"] = round(time.perf_counter() - t0, 3)
            health_result["status_code"] = resp.status_code
        heavy_thread.join(timeout=HEAVY_WORK_SECONDS + 5.0)
        return {"endpoint": "GET /health concurrently with POST /generate (before)", **health_result}
    finally:
        _stop_live_server(uv_server, thread)


def measure_after() -> dict:
    """POST /jobs/generate: the client-perceived latency for submitting the SAME heavy work."""
    server._conversation_manager = _fake_conversation_manager()
    base_url, uv_server, thread = _start_live_server()
    try:
        with httpx.Client(timeout=10.0) as client:
            t0 = time.perf_counter()
            resp = client.post(f"{base_url}/jobs/generate", json={"message": "Hello"})
            submit_elapsed = time.perf_counter() - t0
            job_id = resp.json().get("job_id")

            # Confirm /health stays fast while that job is still running.
            time.sleep(HEAVY_WORK_SECONDS / 2)
            t1 = time.perf_counter()
            health_resp = client.get(f"{base_url}/health")
            health_elapsed = time.perf_counter() - t1

            # Poll to completion and report total wall-clock time.
            t2 = time.perf_counter()
            while True:
                status = client.get(f"{base_url}/jobs/{job_id}").json()
                if status["status"] in ("completed", "failed"):
                    break
                time.sleep(0.05)
            poll_to_completion_elapsed = time.perf_counter() - t2

            return {
                "endpoint": "POST /jobs/generate (after)",
                "submit_status_code": resp.status_code,
                "submit_call_elapsed_seconds": round(submit_elapsed, 3),
                "concurrent_health_status_code": health_resp.status_code,
                "concurrent_health_elapsed_seconds": round(health_elapsed, 3),
                "job_final_status": status["status"],
                "poll_to_completion_elapsed_seconds": round(poll_to_completion_elapsed, 3),
            }
    finally:
        _stop_live_server(uv_server, thread)


def measure_before_second_generate_queued() -> dict:
    """
    A second POST /generate issued while the first is still mid-flight:
    this codebase's generation semaphore (conversation_manager.py's
    _generation_semaphore, default size 1 -- see
    PHASE_STABILITY_AUDIT findings) serializes them, so the SECOND
    caller's connection is held open for up to the full combined
    duration, on top of whatever else it was already waiting for.
    """
    server._conversation_manager = _fake_conversation_manager()
    base_url, uv_server, thread = _start_live_server()
    try:
        first_elapsed = {}

        def _submit_first():
            with httpx.Client(timeout=HEAVY_WORK_SECONDS * 3) as client:
                t0 = time.perf_counter()
                client.post(f"{base_url}/generate", json={"message": "First"})
                first_elapsed["seconds"] = round(time.perf_counter() - t0, 3)

        first_thread = threading.Thread(target=_submit_first)
        first_thread.start()
        time.sleep(HEAVY_WORK_SECONDS / 4)  # ensure the first request holds the semaphore

        with httpx.Client(timeout=HEAVY_WORK_SECONDS * 3) as client:
            t0 = time.perf_counter()
            resp = client.post(f"{base_url}/generate", json={"message": "Second"})
            second_elapsed = round(time.perf_counter() - t0, 3)

        first_thread.join(timeout=HEAVY_WORK_SECONDS * 3)
        return {
            "endpoint": "POST /generate x2 concurrent (before)",
            "first_call_elapsed_seconds": first_elapsed.get("seconds"),
            "second_call_elapsed_seconds": second_elapsed,
            "second_status_code": resp.status_code,
        }
    finally:
        _stop_live_server(uv_server, thread)


def measure_after_second_job_queued() -> dict:
    """
    The same scenario via POST /jobs/generate: the SECOND submission
    still returns fast even though its underlying work is queued behind
    the first by the same semaphore -- the client is never blocked
    waiting to find that out.
    """
    server._conversation_manager = _fake_conversation_manager()
    base_url, uv_server, thread = _start_live_server()
    try:
        with httpx.Client(timeout=10.0) as client:
            first_resp = client.post(f"{base_url}/jobs/generate", json={"message": "First"})
            time.sleep(HEAVY_WORK_SECONDS / 4)

            t0 = time.perf_counter()
            second_resp = client.post(f"{base_url}/jobs/generate", json={"message": "Second"})
            second_elapsed = round(time.perf_counter() - t0, 3)

            return {
                "endpoint": "POST /jobs/generate x2 concurrent (after)",
                "first_submit_status_code": first_resp.status_code,
                "second_submit_status_code": second_resp.status_code,
                "second_submit_call_elapsed_seconds": second_elapsed,
            }
    finally:
        _stop_live_server(uv_server, thread)


if __name__ == "__main__":
    print(f"Simulated heavy LLM call duration: {HEAVY_WORK_SECONDS}s\n")

    print("=== BEFORE: POST /generate ===")
    print(measure_before())
    print()

    print("=== BEFORE: GET /health while POST /generate is in flight ===")
    print(measure_before_concurrent_health())
    print()

    print("=== AFTER: POST /jobs/generate ===")
    print(measure_after())
    print()

    print("=== BEFORE: two concurrent POST /generate (semaphore-queued) ===")
    print(measure_before_second_generate_queued())
    print()

    print("=== AFTER: two concurrent POST /jobs/generate (semaphore-queued underneath) ===")
    print(measure_after_second_job_queued())

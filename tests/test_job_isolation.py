"""
Tests for Phase 15's heavy-work/request isolation (src/api/jobs.py,
POST /jobs/generate + GET /jobs/{job_id} in src/api/server.py).

Proves, against the real FastAPI app, with no torch/transformers needed
(same fake-LLM-injection pattern as test_server_api.py):

1. the API stays responsive while a heavy job is still running
2. one heavy/blocked job cannot prevent other requests/jobs from being served
3. a failed heavy job does not terminate the application
4. every job reaches a deterministic terminal state (completed or failed),
   never stuck at queued/running

Timing-sensitive tests (1 and 2) run against a REAL live uvicorn server on
a loopback socket, not FastAPI's TestClient -- TestClient's synchronous
httpx<->anyio bridging waits for a request's `loop.run_in_executor()`
future to fully finish before returning control to the calling thread
(verified directly: a minimal repro shows TestClient blocking for a
background task's full duration, while the identical app served by real
uvicorn returns in ~7ms). That's a TestClient bridging artifact, not real
ASGI/uvicorn behavior, so proving "the API stays responsive" requires an
actual live server and real HTTP requests -- exactly what production
traffic looks like.

Run with:
    python -m pytest tests/test_job_isolation.py -v
"""

import socket
import sys
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path

import httpx
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "api"))
import server  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "agent"))
from conversation_manager import ConversationManager  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "inference"))
from fastapi.testclient import TestClient  # noqa: E402
from handoff_detector import HandoffDetector  # noqa: E402

CLINICAL_CONFIG_PATH = Path(__file__).resolve().parents[1] / "configs" / "clinical_triggers.yaml"
HANDOFF_CONFIG_PATH = Path(__file__).resolve().parents[1] / "configs" / "handoff_phrases.yaml"

# Generous but bounded -- these tests must never hang forever even if the
# isolation mechanism is broken; a real regression should fail fast, not
# time out the whole suite.
_POLL_DEADLINE_SECONDS = 5.0
_POLL_INTERVAL_SECONDS = 0.05


class BlockingLLMService:
    """
    Stands in for a slow/heavy LLM call: generate_stream() blocks on an
    Event the test controls, so "the job is still running" is a precise,
    non-flaky condition rather than a fixed sleep duration.
    """

    def __init__(self, release_event: threading.Event, response_text: str = "Done."):
        self.release_event = release_event
        self.response_text = response_text
        self.call_count = 0

    def generate_stream(self, messages, **kwargs):
        self.call_count += 1
        # Bounded wait -- if a regression breaks release semantics, this
        # test fails on a timeout rather than hanging the suite forever.
        self.release_event.wait(timeout=10.0)
        for word in self.response_text.split(" "):
            yield word + " "
        yield {"text": self.response_text, "latency_ms": 1.0}


class RaisingConversationManager:
    """
    A ConversationManager stand-in whose handle_turn() always raises --
    simulating an unexpected internal failure that is NOT one of
    ConversationManager's own already-handled degraded paths (clinical
    guard, RAG failure, LLM failure all already convert to a safe
    response internally -- see conversation_manager.py). This targets
    _run_generate_job()'s own exception handling specifically.
    """

    def handle_turn(self, *args, **kwargs):
        raise RuntimeError("simulated unexpected internal failure")


def _fake_conversation_manager(llm_service) -> ConversationManager:
    return ConversationManager(
        llm_service=llm_service,
        retriever=None,
        clinical_guard=HandoffDetector(config_path=CLINICAL_CONFIG_PATH),
        handoff_detector=HandoffDetector(config_path=HANDOFF_CONFIG_PATH),
    )


def _poll_until_terminal_httpx(
    client: httpx.Client, base_url: str, job_id: str, deadline_seconds: float = _POLL_DEADLINE_SECONDS
) -> dict:
    deadline = time.perf_counter() + deadline_seconds
    last = None
    while time.perf_counter() < deadline:
        resp = client.get(f"{base_url}/jobs/{job_id}")
        last = resp.json()
        if last["status"] in ("completed", "failed"):
            return last
        time.sleep(_POLL_INTERVAL_SECONDS)
    return last


def _poll_until_terminal(client: TestClient, job_id: str, deadline_seconds: float = _POLL_DEADLINE_SECONDS) -> dict:
    deadline = time.perf_counter() + deadline_seconds
    last = None
    while time.perf_counter() < deadline:
        resp = client.get(f"/jobs/{job_id}")
        last = resp.json()
        if last["status"] in ("completed", "failed"):
            return last
        time.sleep(_POLL_INTERVAL_SECONDS)
    return last


@contextmanager
def _live_server():
    """
    Runs the real src/api/server.py FastAPI app on a real loopback socket
    via uvicorn, in a background thread -- genuine ASGI server behavior,
    not TestClient's synchronous bridging. Yields the base URL once the
    server is confirmed responsive.
    """
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    # lifespan="off": these tests inject a fake ConversationManager
    # directly (server._conversation_manager) the same way
    # test_server_api.py does -- the real lifespan's model load
    # (torch/transformers, potentially a slow or network-dependent
    # download) must never run and overwrite it.
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
            raise RuntimeError("live test server did not become responsive in time")

    try:
        yield base_url
    finally:
        uv_server.should_exit = True
        thread.join(timeout=5.0)


class TestApiRespondsWhileJobRuns(unittest.TestCase):
    """Property 1: the API stays responsive while a heavy job is still running."""

    def test_submit_returns_immediately_and_health_stays_responsive(self):
        release_event = threading.Event()
        server._conversation_manager = _fake_conversation_manager(BlockingLLMService(release_event))

        with _live_server() as base_url, httpx.Client() as client:
            try:
                t0 = time.perf_counter()
                submit_resp = client.post(f"{base_url}/jobs/generate", json={"message": "Hello"})
                submit_elapsed_ms = (time.perf_counter() - t0) * 1000.0

                self.assertEqual(submit_resp.status_code, 202)
                self.assertLess(submit_elapsed_ms, 500.0, "job submission must return almost immediately")
                job_id = submit_resp.json()["job_id"]
                self.assertEqual(submit_resp.json()["status"], "queued")

                # Give the background executor thread a moment to
                # actually start and block inside generate_stream().
                time.sleep(0.2)

                status_resp = client.get(f"{base_url}/jobs/{job_id}")
                self.assertIn(status_resp.json()["status"], ("queued", "running"))

                # The API must still serve OTHER requests while that job
                # sits blocked -- this is the core property under test.
                t1 = time.perf_counter()
                health_resp = client.get(f"{base_url}/health")
                health_elapsed_ms = (time.perf_counter() - t1) * 1000.0
                self.assertEqual(health_resp.status_code, 200)
                self.assertLess(health_elapsed_ms, 500.0, "unrelated endpoints must stay responsive")
            finally:
                release_event.set()

            final = _poll_until_terminal_httpx(client, base_url, job_id)
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["result"]["response"], "Done.")


class TestOneHeavyJobCannotBlockOthers(unittest.TestCase):
    """Property 2: one heavy job cannot prevent other requests/jobs from being served."""

    def test_second_job_can_be_submitted_while_first_is_running(self):
        release_event = threading.Event()
        server._conversation_manager = _fake_conversation_manager(BlockingLLMService(release_event))

        with _live_server() as base_url, httpx.Client() as client:
            try:
                first = client.post(f"{base_url}/jobs/generate", json={"message": "First"})
                self.assertEqual(first.status_code, 202)
                first_job_id = first.json()["job_id"]

                time.sleep(0.2)  # let the first job actually start blocking

                # Submitting a second job must not be blocked by the
                # first one still running (job creation never touches
                # the LLM).
                t0 = time.perf_counter()
                second = client.post(f"{base_url}/jobs/generate", json={"message": "Second"})
                second_elapsed_ms = (time.perf_counter() - t0) * 1000.0
                self.assertEqual(second.status_code, 202)
                self.assertLess(second_elapsed_ms, 500.0)
                second_job_id = second.json()["job_id"]
                self.assertNotEqual(first_job_id, second_job_id)

                # The server process itself is still healthy throughout.
                self.assertEqual(client.get(f"{base_url}/health").status_code, 200)
            finally:
                release_event.set()

            # Both jobs eventually resolve once the shared LLM is
            # released (this codebase's generation semaphore serializes
            # them -- see PHASE_15_REQUEST_ISOLATION_REPORT.md -- but
            # neither is lost or left permanently stuck).
            first_final = _poll_until_terminal_httpx(client, base_url, first_job_id)
            second_final = _poll_until_terminal_httpx(client, base_url, second_job_id)
        self.assertEqual(first_final["status"], "completed")
        self.assertEqual(second_final["status"], "completed")


class TestFailedJobDoesNotTerminateApp(unittest.TestCase):
    """Property 3: a failed heavy job does not terminate the application."""

    def test_failing_job_is_isolated_and_app_keeps_serving(self):
        server._conversation_manager = RaisingConversationManager()
        client = TestClient(server.app)

        submit_resp = client.post("/jobs/generate", json={"message": "Hello"})
        self.assertEqual(submit_resp.status_code, 202)
        job_id = submit_resp.json()["job_id"]

        final = _poll_until_terminal(client, job_id)
        self.assertEqual(final["status"], "failed")
        self.assertIsNotNone(final.get("error"))
        # Never the raw exception message -- see _run_generate_job()'s
        # "no internal failure detail exposed" comment.
        self.assertNotIn("simulated unexpected internal failure", final["error"])

        # The application process itself must still be fully functional
        # after a failed job -- prove it with both an unrelated endpoint
        # and a brand new, independently successful job.
        self.assertEqual(client.get("/health").status_code, 200)

        release_event = threading.Event()
        release_event.set()
        server._conversation_manager = _fake_conversation_manager(BlockingLLMService(release_event, "Recovered."))
        second = client.post("/jobs/generate", json={"message": "Hello again"})
        self.assertEqual(second.status_code, 202)
        second_final = _poll_until_terminal(client, second.json()["job_id"])
        self.assertEqual(second_final["status"], "completed")
        self.assertEqual(second_final["result"]["response"], "Recovered.")


class TestJobReachesDeterministicTerminalState(unittest.TestCase):
    """Property 4: every job reaches a deterministic terminal state."""

    def test_successful_job_resolves_to_completed_not_stuck(self):
        release_event = threading.Event()
        release_event.set()  # let it run immediately -- fastest possible resolution
        server._conversation_manager = _fake_conversation_manager(BlockingLLMService(release_event, "Ok."))
        client = TestClient(server.app)

        job_id = client.post("/jobs/generate", json={"message": "Hello"}).json()["job_id"]
        final = _poll_until_terminal(client, job_id, deadline_seconds=3.0)

        self.assertIsNotNone(final, "job must reach a terminal state within the deadline, not stay queued/running")
        self.assertEqual(final["status"], "completed")

    def test_failing_job_resolves_to_failed_not_stuck(self):
        server._conversation_manager = RaisingConversationManager()
        client = TestClient(server.app)

        job_id = client.post("/jobs/generate", json={"message": "Hello"}).json()["job_id"]
        final = _poll_until_terminal(client, job_id, deadline_seconds=3.0)

        self.assertIsNotNone(final, "job must reach a terminal state within the deadline, not stay queued/running")
        self.assertEqual(final["status"], "failed")

    def test_unknown_job_id_returns_404_not_a_hang(self):
        client = TestClient(server.app)
        resp = client.get("/jobs/does-not-exist")
        self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main()

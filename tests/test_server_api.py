"""
End-to-end HTTP contract tests for src/api/server.py's /health and
/generate routes, after the Conversation Manager extraction
(docs/IMPLEMENTATION_ROADMAP.md milestone M2).

Injects a ConversationManager built from a fake LLM service directly into
server._conversation_manager, bypassing the real lifespan model load, so
these run without torch/transformers/a real checkpoint -- while still
exercising the actual FastAPI route handlers, request validation, and
NDJSON streaming framing. This is the strongest available check that
FastAPI -> ConversationManager -> {Safety, RAG, LLM, Handoff} -> Response
(the target flow) preserves the exact HTTP contract clients depended on
before the extraction.

Run with:
    python -m unittest tests.test_server_api -v
"""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "api"))
import server  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "agent"))
from conversation_manager import ConversationManager  # noqa: E402
from db import DatabaseUnavailableError  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "inference"))
from fastapi.testclient import TestClient  # noqa: E402
from handoff_detector import HandoffDetector  # noqa: E402

CLINICAL_CONFIG_PATH = Path(__file__).resolve().parents[1] / "configs" / "clinical_triggers.yaml"
HANDOFF_CONFIG_PATH = Path(__file__).resolve().parents[1] / "configs" / "handoff_phrases.yaml"


class FakeLLMService:
    def __init__(self, response_text: str = "Sure, here is the answer.", latency_ms: float = 10.0):
        self.response_text = response_text
        self.latency_ms = latency_ms

    def generate_stream(self, messages, **kwargs):
        for word in self.response_text.split(" "):
            yield word + " "
        yield {"text": self.response_text, "latency_ms": self.latency_ms}


def _build_fake_conversation_manager(response_text: str = "Sure, here is the answer.") -> ConversationManager:
    return ConversationManager(
        llm_service=FakeLLMService(response_text=response_text),
        retriever=None,
        clinical_guard=HandoffDetector(config_path=CLINICAL_CONFIG_PATH),
        handoff_detector=HandoffDetector(config_path=HANDOFF_CONFIG_PATH),
    )


class TestResourceLimits(unittest.TestCase):
    """Phase 10, plan.md Step 10.16 — oversized requests are rejected before reaching ConversationManager."""

    def test_message_over_limit_rejected_with_422(self):
        server._conversation_manager = _build_fake_conversation_manager()
        client = TestClient(server.app)
        oversized = "x" * (server._RELIABILITY.request_limits.max_message_length + 1)
        resp = client.post("/generate", json={"message": oversized})
        self.assertEqual(resp.status_code, 422)

    def test_message_at_limit_accepted(self):
        recorder = RecordingConversationManager()
        server._conversation_manager = recorder
        client = TestClient(server.app)
        at_limit = "x" * server._RELIABILITY.request_limits.max_message_length
        resp = client.post("/generate", json={"message": at_limit})
        self.assertEqual(resp.status_code, 200)

    def test_too_many_history_turns_rejected(self):
        server._conversation_manager = _build_fake_conversation_manager()
        client = TestClient(server.app)
        too_many = [{"role": "user", "content": "hi"}] * (server._RELIABILITY.request_limits.max_history_turns + 1)
        resp = client.post("/generate", json={"message": "hi", "history": too_many})
        self.assertEqual(resp.status_code, 422)

    def test_history_turn_content_over_limit_rejected(self):
        server._conversation_manager = _build_fake_conversation_manager()
        client = TestClient(server.app)
        oversized_turn = [
            {"role": "user", "content": "x" * (server._RELIABILITY.request_limits.max_history_turn_length + 1)}
        ]
        resp = client.post("/generate", json={"message": "hi", "history": oversized_turn})
        self.assertEqual(resp.status_code, 422)

    def test_rejection_increments_metric(self):
        server._conversation_manager = _build_fake_conversation_manager()
        server._metrics._counters["request_rejections_total"] = 0
        client = TestClient(server.app)
        oversized = "x" * (server._RELIABILITY.request_limits.max_message_length + 1)
        client.post("/generate", json={"message": oversized})
        self.assertEqual(server._metrics.get_counter("request_rejections_total"), 1)

    def test_rejection_response_exposes_no_internal_detail(self):
        server._conversation_manager = _build_fake_conversation_manager()
        client = TestClient(server.app)
        oversized = "x" * (server._RELIABILITY.request_limits.max_message_length + 1)
        resp = client.post("/generate", json={"message": oversized})
        body = resp.json()
        self.assertNotIn("Traceback", str(body))


class TestGracefulShutdown(unittest.TestCase):
    """
    plan.md Step 10.18. `lifespan()`'s startup half calls
    build_conversation_manager(), which loads a real model (torch) --
    not available in this offline test environment -- so the full async
    context manager can't be exercised end to end here. This instead
    verifies the two concrete, checkable facts: the shutdown code path
    exists and emits GRACEFUL_SHUTDOWN, and uvicorn.run() is configured
    with a bounded (not infinite) graceful-shutdown timeout.
    """

    def test_lifespan_source_emits_graceful_shutdown_after_yield(self):
        import inspect

        source = inspect.getsource(server.lifespan)
        after_yield = source.split("yield", 1)[1]
        self.assertIn("GRACEFUL_SHUTDOWN", after_yield)

    def test_uvicorn_run_uses_bounded_graceful_shutdown_timeout(self):
        import inspect

        source = inspect.getsource(server)
        self.assertIn("timeout_graceful_shutdown", source)
        self.assertGreater(server._RELIABILITY.graceful_shutdown_timeout_seconds, 0)


class TestHealthEndpoint(unittest.TestCase):
    def test_health_before_model_loaded(self):
        server._conversation_manager = None
        client = TestClient(server.app)
        resp = client.get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "ok", "model_loaded": False})

    def test_health_after_model_loaded(self):
        server._conversation_manager = _build_fake_conversation_manager()
        client = TestClient(server.app)
        resp = client.get("/health")
        self.assertEqual(resp.json(), {"status": "ok", "model_loaded": True})


class TestReadyEndpoint(unittest.TestCase):
    """
    Phase 8, plan.md Step 8.14; updated Phase 13 Step 13.1 — /ready reports readiness:
    - In dev mode: model loaded -> 200, model missing -> 503.
    - In production mode: model loaded + DB healthy -> 200; DB unhealthy or missing -> 503.
    - Error responses never leak internal detail or connection strings.
    """

    def setUp(self):
        self._prior_pm = os.environ.get("PERSISTENCE_MODE")
        self._prior_db_url = os.environ.get("DATABASE_URL")
        self._prior_cm = server._conversation_manager
        self._prior_db = server._database

    def tearDown(self):
        if self._prior_pm is None:
            os.environ.pop("PERSISTENCE_MODE", None)
        else:
            os.environ["PERSISTENCE_MODE"] = self._prior_pm
        if self._prior_db_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = self._prior_db_url
        server._conversation_manager = self._prior_cm
        server._database = self._prior_db

    def test_not_ready_before_model_loaded(self):
        os.environ.pop("PERSISTENCE_MODE", None)
        server._conversation_manager = None
        client = TestClient(server.app)
        resp = client.get("/ready")
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json(), {"ready": False})

    def test_ready_after_model_loaded_dev_mode(self):
        os.environ.pop("PERSISTENCE_MODE", None)
        server._conversation_manager = _build_fake_conversation_manager()
        client = TestClient(server.app)
        resp = client.get("/ready")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"ready": True})

    def test_ready_response_exposes_no_internal_detail(self):
        os.environ.pop("PERSISTENCE_MODE", None)
        server._conversation_manager = None
        client = TestClient(server.app)
        resp = client.get("/ready")
        self.assertEqual(set(resp.json().keys()), {"ready"})

    def test_production_mode_ready_when_database_healthy(self):
        os.environ["PERSISTENCE_MODE"] = "production"
        os.environ["DATABASE_URL"] = "postgresql://user:pass@localhost:5432/db"
        server._conversation_manager = _build_fake_conversation_manager()
        mock_db = MagicMock()
        mock_db.health_check.return_value = True
        server._database = mock_db

        client = TestClient(server.app)
        resp = client.get("/ready")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"ready": True})
        mock_db.health_check.assert_called_once()

    def test_production_mode_not_ready_when_database_health_check_returns_false(self):
        os.environ["PERSISTENCE_MODE"] = "production"
        os.environ["DATABASE_URL"] = "postgresql://user:pass@localhost:5432/db"
        server._conversation_manager = _build_fake_conversation_manager()
        mock_db = MagicMock()
        mock_db.health_check.return_value = False
        server._database = mock_db

        client = TestClient(server.app)
        resp = client.get("/ready")
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json(), {"ready": False})

    def test_production_mode_not_ready_when_database_health_check_raises(self):
        os.environ["PERSISTENCE_MODE"] = "production"
        os.environ["DATABASE_URL"] = "postgresql://user:pass@localhost:5432/db"
        server._conversation_manager = _build_fake_conversation_manager()
        mock_db = MagicMock()
        mock_db.health_check.side_effect = DatabaseUnavailableError("DB unreachable")
        server._database = mock_db

        client = TestClient(server.app)
        resp = client.get("/ready")
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json(), {"ready": False})

    def test_production_mode_not_ready_when_no_database_available(self):
        os.environ["PERSISTENCE_MODE"] = "production"
        os.environ["DATABASE_URL"] = "postgresql://user:pass@localhost:5432/db"
        server._conversation_manager = _build_fake_conversation_manager()
        server._database = None

        client = TestClient(server.app)
        resp = client.get("/ready")
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json(), {"ready": False})

    def test_production_mode_unhealthy_response_exposes_no_internal_detail(self):
        os.environ["PERSISTENCE_MODE"] = "production"
        os.environ["DATABASE_URL"] = "postgresql://secret_user:super_secret_password@localhost:5432/secret_db"
        server._conversation_manager = _build_fake_conversation_manager()
        mock_db = MagicMock()
        mock_db.health_check.side_effect = DatabaseUnavailableError("secret_user:super_secret_password failed")
        server._database = mock_db

        client = TestClient(server.app)
        resp = client.get("/ready")
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(set(resp.json().keys()), {"ready"})
        self.assertNotIn("secret", str(resp.json()))
        self.assertNotIn("Traceback", str(resp.json()))


class TestRequestIdMiddleware(unittest.TestCase):
    """Phase 8, plan.md Step 8.3 — correlation ID assigned per request and echoed back."""

    def test_response_carries_a_request_id_header(self):
        server._conversation_manager = _build_fake_conversation_manager()
        client = TestClient(server.app)
        resp = client.get("/health")
        self.assertIn("x-request-id", resp.headers)
        self.assertTrue(resp.headers["x-request-id"])

    def test_incoming_request_id_header_is_reused_not_replaced(self):
        server._conversation_manager = _build_fake_conversation_manager()
        client = TestClient(server.app)
        resp = client.get("/health", headers={"X-Request-ID": "req_caller_supplied"})
        self.assertEqual(resp.headers["x-request-id"], "req_caller_supplied")

    def test_generate_call_receives_the_middleware_request_id(self):
        recorder = RecordingConversationManager()
        server._conversation_manager = recorder
        client = TestClient(server.app)
        resp = client.post("/generate", json={"message": "hi"}, headers={"X-Request-ID": "req_fixed"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(recorder.calls[0]["request_id"], "req_fixed")


class TestSafeExceptionHandler(unittest.TestCase):
    """Phase 8, plan.md Step 8.15 — an unhandled exception never reaches the client as a raw traceback."""

    def test_unhandled_exception_returns_safe_generic_body(self):
        class CrashingConversationManager:
            def handle_turn(self, *args, **kwargs):
                raise RuntimeError("boom: internal secret detail")
                yield  # pragma: no cover -- keeps this a generator

        server._conversation_manager = CrashingConversationManager()
        client = TestClient(server.app, raise_server_exceptions=False)
        resp = client.post("/generate", json={"message": "hi"})
        self.assertEqual(resp.status_code, 500)
        body = resp.json()
        self.assertEqual(set(body.keys()), {"request_id", "error_id", "error_code", "message"})
        self.assertNotIn("boom", str(body))
        self.assertNotIn("RuntimeError", str(body))


class RecordingConversationManager:
    """
    Records every handle_turn() call's kwargs (Phase 7) so tests can
    assert on exactly what identity/session_id server.py resolved and
    passed through — without needing the real ConversationManager's
    internal orchestration logic for these identity-boundary tests.
    """

    def __init__(self, response_text: str = "Sure, here is the answer."):
        self.response_text = response_text
        self.calls: list[dict] = []

    def handle_turn(self, message, history=None, auth=None, confirmed=False, session_id=None, request_id=None):
        self.calls.append(
            {
                "message": message,
                "auth": auth,
                "session_id": session_id,
                "confirmed": confirmed,
                "request_id": request_id,
            }
        )
        yield self.response_text
        yield {
            "response": self.response_text,
            "is_handoff": False,
            "handoff_confidence": 0.0,
            "latency_ms": 5.0,
            "retrieved_chunks": [],
            "clinical_guard_triggered": False,
            "degraded": False,
            "error": None,
            "intent": None,
            "policy": None,
            "tool": None,
        }


class TestAuthenticationBoundary(unittest.TestCase):
    """Phase 7 — Authorization header -> AuthContext, and identity-spoofing regression tests (Step 7.11/7.15)."""

    def setUp(self):
        self.recorder = RecordingConversationManager()
        server._conversation_manager = self.recorder
        self.client = TestClient(server.app)

    def test_no_authorization_header_uses_anonymous_identity(self):
        resp = self.client.post("/generate", json={"message": "Hello"})
        self.assertEqual(resp.status_code, 200)
        auth = self.recorder.calls[0]["auth"]
        self.assertFalse(auth.authenticated)

    def test_valid_bearer_token_resolves_real_identity(self):
        resp = self.client.post(
            "/generate",
            json={"message": "Hello"},
            headers={"Authorization": "Bearer test-user-token"},
        )
        self.assertEqual(resp.status_code, 200)
        auth = self.recorder.calls[0]["auth"]
        self.assertTrue(auth.authenticated)
        self.assertEqual(auth.user_id, "test-user-1")

    def test_invalid_bearer_token_returns_401(self):
        resp = self.client.post(
            "/generate",
            json={"message": "Hello"},
            headers={"Authorization": "Bearer not-a-real-token"},
        )
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(self.recorder.calls, [])  # handle_turn() never reached

    def test_malformed_authorization_scheme_returns_401(self):
        resp = self.client.post(
            "/generate",
            json={"message": "Hello"},
            headers={"Authorization": "NotBearer test-user-token"},
        )
        self.assertEqual(resp.status_code, 401)

    def test_401_response_does_not_echo_submitted_token(self):
        secret_looking_token = "sk-super-secret-value-12345"
        resp = self.client.post(
            "/generate",
            json={"message": "Hello"},
            headers={"Authorization": f"Bearer {secret_looking_token}"},
        )
        self.assertEqual(resp.status_code, 401)
        self.assertNotIn(secret_looking_token, resp.text)

    def test_session_id_is_passed_through(self):
        self.client.post("/generate", json={"message": "Hello", "session_id": "sess-42"})
        self.assertEqual(self.recorder.calls[0]["session_id"], "sess-42")

    def test_client_supplied_identity_in_body_is_ignored(self):
        """
        Step 7.11 — attack: a client sends {"user_id": "admin"} (or any
        identity-shaped field) in the request body. ChatRequest has no
        such field at all, so Pydantic silently drops it (default
        extra="ignore") — it can never reach or influence the resolved
        AuthContext, which comes exclusively from the Authorization
        header via resolve_identity().
        """
        resp = self.client.post(
            "/generate",
            json={"message": "Hello", "user_id": "admin", "role": "ADMIN", "authenticated": True},
        )
        self.assertEqual(resp.status_code, 200)
        auth = self.recorder.calls[0]["auth"]
        self.assertNotEqual(auth.user_id, "admin")
        self.assertFalse(auth.authenticated)  # no Authorization header was sent -- still anonymous

    def test_dev_auth_provider_can_be_disabled(self):
        original = server._authentication_provider.enabled
        server._authentication_provider.enabled = False
        try:
            resp = self.client.post(
                "/generate",
                json={"message": "Hello"},
                headers={"Authorization": "Bearer test-user-token"},  # a token that would otherwise be valid
            )
            self.assertEqual(resp.status_code, 401)
        finally:
            server._authentication_provider.enabled = original


class TestGenerateEndpoint(unittest.TestCase):
    def setUp(self):
        server._conversation_manager = _build_fake_conversation_manager("Our return window is thirty days.")
        self.client = TestClient(server.app)

    def test_non_streaming_response_shape_unchanged(self):
        resp = self.client.post("/generate", json={"message": "What's your return policy?"})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        # Exactly the pre-extraction ChatResponse contract -- no internal
        # ConversationManager metadata (degraded/error) leaks into the
        # HTTP response.
        self.assertEqual(set(body.keys()), {"response", "is_handoff", "latency_ms"})
        self.assertEqual(body["response"], "Our return window is thirty days.")
        self.assertFalse(body["is_handoff"])

    def test_streaming_response_is_ndjson(self):
        resp = self.client.post("/generate", json={"message": "What's your return policy?", "stream": True})
        self.assertEqual(resp.status_code, 200)
        lines = [json.loads(line) for line in resp.text.strip().split("\n") if line]
        self.assertTrue(any("token" in line for line in lines), "expected at least one token line")
        done_lines = [line for line in lines if line.get("done")]
        self.assertEqual(len(done_lines), 1)
        self.assertEqual(done_lines[0]["response"], "Our return window is thirty days.")
        self.assertIn("is_handoff", done_lines[0])
        self.assertIn("latency_ms", done_lines[0])

    def test_streaming_done_event_excludes_internal_metadata(self):
        """
        Phase 6 (plan.md Step 6.12): the NDJSON "done" event must not leak
        ConversationManager's internal policy/intent reasoning, or a raw
        tool result payload, to API clients.
        """
        resp = self.client.post("/generate", json={"message": "What's your return policy?", "stream": True})
        lines = [json.loads(line) for line in resp.text.strip().split("\n") if line]
        done_event = next(line for line in lines if line.get("done"))
        for internal_key in ("policy", "intent", "degraded", "error", "tool"):
            self.assertNotIn(internal_key, done_event)

    def test_clinical_question_returns_handoff(self):
        resp = self.client.post("/generate", json={"message": "How many mg of ibuprofen should I take?"})
        self.assertTrue(resp.json()["is_handoff"])

    def test_empty_message_does_not_500(self):
        resp = self.client.post("/generate", json={"message": ""})
        self.assertEqual(resp.status_code, 200)

    def test_model_not_loaded_returns_503(self):
        server._conversation_manager = None
        resp = self.client.post("/generate", json={"message": "Hello"})
        self.assertEqual(resp.status_code, 503)


class TestCredentialReadinessLogging(unittest.TestCase):
    """
    Phase 1.4 (docs/phase1.4-external-integration-report.md Section 3):
    _log_credential_readiness() must report WHICH credentials are missing,
    and must NEVER print a credential's value -- these tests use obviously
    fake values precisely so a leak would be caught by string search below.
    """

    _CRED_NAMES = (
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "GROQ_API_KEY",
        "DEEPGRAM_API_KEY",
        "ELEVENLABS_API_KEY",
        "TWILIO_ACCOUNT_SID",
        "TWILIO_AUTH_TOKEN",
    )

    def setUp(self):
        self._backup = {name: os.environ.get(name) for name in self._CRED_NAMES}

    def tearDown(self):
        for name, value in self._backup.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_missing_credentials_are_named_without_leaking_values(self):
        for name in self._CRED_NAMES:
            os.environ.pop(name, None)
        with self.assertLogs("ai_voice_agent.startup", level="INFO") as captured:
            server._log_credential_readiness()
        joined = " ".join(captured.output)
        self.assertIn("ANTHROPIC_API_KEY", joined)
        self.assertIn("TWILIO_AUTH_TOKEN", joined)
        self.assertIn(f"{len(self._CRED_NAMES)}/{len(self._CRED_NAMES)}", joined)

    def test_configured_credentials_are_not_leaked_by_value(self):
        fake_values = {name: f"OBVIOUSLY-FAKE-SECRET-{name}" for name in self._CRED_NAMES}
        for name, value in fake_values.items():
            os.environ[name] = value
        with self.assertLogs("ai_voice_agent.startup", level="INFO") as captured:
            server._log_credential_readiness()
        joined = " ".join(captured.output)
        self.assertIn(f"all {len(self._CRED_NAMES)}", joined)
        for value in fake_values.values():
            self.assertNotIn(value, joined)

    def test_partial_configuration_reports_only_the_missing_ones(self):
        os.environ["ANTHROPIC_API_KEY"] = "OBVIOUSLY-FAKE-SECRET-ANTHROPIC"
        for name in self._CRED_NAMES:
            if name != "ANTHROPIC_API_KEY":
                os.environ.pop(name, None)
        with self.assertLogs("ai_voice_agent.startup", level="INFO") as captured:
            server._log_credential_readiness()
        joined = " ".join(captured.output)
        self.assertNotIn("ANTHROPIC_API_KEY (Claude", joined)
        self.assertIn("GEMINI_API_KEY", joined)
        self.assertIn(f"{len(self._CRED_NAMES) - 1}/{len(self._CRED_NAMES)}", joined)


if __name__ == "__main__":
    unittest.main()

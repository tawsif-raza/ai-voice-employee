"""
Server behaviour under the strict (non-dev) APP_ENV posture
(docs/MASTER_PROJECT_PLAN.md H2; F-03, F-04, F-18).

The in-process tests swap server._SECURITY for a production posture and
the authentication provider for a small fake, so they exercise the real
routes without an OIDC provider. Startup refusal is tested by importing
the real server module in a subprocess, because the posture is enforced
at import time.
"""

import dataclasses
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src" / "agent"))
sys.path.insert(0, str(_ROOT / "src" / "inference"))
sys.path.insert(0, str(_ROOT / "src" / "api"))

import server  # noqa: E402
from action_models import AuthContext  # noqa: E402
from conversation_manager import build_conversation_manager  # noqa: E402
from fastapi import WebSocketDisconnect  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from identity import AuthenticationError, Role, permissions_for_roles  # noqa: E402
from llm_provider import BaseLLMProvider  # noqa: E402
from runtime_env import SecurityPostureError, load_security_settings  # noqa: E402

PRODUCTION = {"APP_ENV": "production", "AUTH_MODE": "production"}
TOKENS = {"token-alice": "alice", "token-bob": "bob"}


class StaticLLM(BaseLLMProvider):
    provider_name = "static"

    def generate_stream(self, messages, **kwargs):
        yield "We open at nine."
        yield {"text": "We open at nine.", "latency_ms": 1.0}


class FakeOIDCProvider:
    def authenticate(self, credentials, client_identifier=None):
        user_id = TOKENS.get(credentials.get("token"))
        if user_id is None:
            raise AuthenticationError("Invalid or missing credentials.")
        return AuthContext(
            user_id=user_id,
            authenticated=True,
            roles=(Role.USER.value,),
            permissions=permissions_for_roles((Role.USER,)),
            authentication_method="oidc",
        )


@pytest.fixture
def production_client(monkeypatch):
    monkeypatch.setattr(server, "_SECURITY", load_security_settings({**PRODUCTION, "RATE_LIMIT_BURST": "1000"}))
    monkeypatch.setattr(server, "_authentication_provider", FakeOIDCProvider())
    monkeypatch.setattr(
        server,
        "_conversation_manager",
        build_conversation_manager(llm_provider=StaticLLM(), rag_enabled=False, persistence_enabled=False),
    )
    return TestClient(server.app)


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


# Must reach the LLM: a greeting or verified-FAQ question is answered by the
# decision router (docs/DECISION_ROUTING.md) without calling the stub below.
BODY = {"message": "What is your return policy for online orders?"}


@pytest.mark.parametrize("path", ["/generate", "/jobs/generate"])
@pytest.mark.parametrize(
    "headers",
    [{}, _auth("not-a-real-token"), {"Authorization": "Basic abc"}, _auth("test-admin-token")],
    ids=["no-header", "bad-token", "wrong-scheme", "dev-admin-token"],
)
def test_text_api_rejects_unauthenticated_requests_outside_dev(production_client, path, headers):
    response = production_client.post(path, json=BODY, headers=headers)
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or missing credentials."}


def test_text_api_serves_authenticated_requests_outside_dev(production_client):
    response = production_client.post("/generate", json=BODY, headers=_auth("token-alice"))
    assert response.status_code == 200
    assert response.json()["response"] == "We open at nine."


@pytest.mark.parametrize(
    "message,rule",
    [
        ("Should I increase my dose?", "MEDICAL_DOSAGE"),
        ("I can't breathe after taking my pills", "URGENT_MEDICAL_RISK"),
    ],
)
@pytest.mark.parametrize("stream", [False, True])
def test_clinical_safety_holds_under_the_production_posture(monkeypatch, message, rule, stream):
    # docs/CLINICAL_SAFETY.md: the same boundary in production as in dev,
    # through the real authenticated route, and the LLM is never called.
    calls = []

    class SpyLLM(StaticLLM):
        def generate_stream(self, messages, **kwargs):
            calls.append(messages)
            yield from super().generate_stream(messages, **kwargs)

    monkeypatch.setattr(server, "_SECURITY", load_security_settings({**PRODUCTION, "RATE_LIMIT_BURST": "1000"}))
    monkeypatch.setattr(server, "_authentication_provider", FakeOIDCProvider())
    manager = build_conversation_manager(llm_provider=SpyLLM(), rag_enabled=False, persistence_enabled=False)
    monkeypatch.setattr(server, "_conversation_manager", manager)

    response = TestClient(server.app).post(
        "/generate", json={"message": message, "stream": stream}, headers=_auth("token-alice")
    )

    assert response.status_code == 200
    expected = manager.URGENT_SAFETY_RESPONSE if rule == "URGENT_MEDICAL_RISK" else manager.CLINICAL_HANDOFF_RESPONSE
    if stream:
        assert '"done": true' in response.text and '"is_handoff": true' in response.text
    else:
        assert response.json()["response"] == expected and response.json()["is_handoff"] is True
    assert calls == []


def test_streaming_generate_also_requires_auth(production_client):
    assert production_client.post("/generate", json={**BODY, "stream": True}).status_code == 401
    ok = production_client.post("/generate", json={**BODY, "stream": True}, headers=_auth("token-alice"))
    assert ok.status_code == 200 and '"done": true' in ok.text


def _wait_for_job(client, job_id, headers):
    for _ in range(100):
        body = client.get(f"/jobs/{job_id}", headers=headers).json()
        if body.get("status") in ("completed", "failed"):
            return body
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def test_jobs_are_only_visible_to_their_owner(production_client):
    submitted = production_client.post("/jobs/generate", json=BODY, headers=_auth("token-alice"))
    assert submitted.status_code == 202
    job_id = submitted.json()["job_id"]

    assert _wait_for_job(production_client, job_id, _auth("token-alice"))["status"] == "completed"
    assert production_client.get(f"/jobs/{job_id}", headers=_auth("token-bob")).status_code == 404
    assert production_client.get(f"/jobs/{job_id}").status_code == 401


def test_lifespan_refuses_to_serve_without_clinical_guard(monkeypatch):
    manager = build_conversation_manager(llm_provider=StaticLLM(), rag_enabled=False, persistence_enabled=False)
    manager.clinical_guard = None
    monkeypatch.setattr(server, "_SECURITY", load_security_settings(PRODUCTION))
    monkeypatch.setattr(server, "build_conversation_manager", lambda **kwargs: manager)
    monkeypatch.setattr(server, "MERGED_MODEL_DIR", str(_ROOT / "does-not-exist"))

    with pytest.raises(SecurityPostureError, match="clinical safety guard"):
        with TestClient(server.app):
            pass


# ── Voice endpoints ─────────────────────────────────────────────────────────

CONNECTED_FRAME = '{"event": "connected", "protocol": "Call", "version": "1.0.0"}'


@pytest.fixture
def voice_server(monkeypatch):
    from voice_pipeline import VoiceCallManager

    manager = build_conversation_manager(llm_provider=StaticLLM(), rag_enabled=False, persistence_enabled=False)
    monkeypatch.setattr(server, "_conversation_manager", manager)
    monkeypatch.setattr(server, "_voice_call_manager", VoiceCallManager(conversation_manager=manager))
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)

    def use(settings_env, **overrides):
        settings = dataclasses.replace(load_security_settings(settings_env), **overrides)
        monkeypatch.setattr(server, "_SECURITY", settings)
        return TestClient(server.app)

    return use


def test_unsigned_twiml_webhook_is_rejected_outside_dev(voice_server):
    client = voice_server(PRODUCTION)
    assert client.post("/twiml/inbound-call", data={"CallSid": "CA1"}).status_code == 403


def test_unsigned_media_stream_is_rejected_outside_dev(voice_server):
    client = voice_server(PRODUCTION)
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws/call"):
            pass
    assert exc_info.value.code == 1008


def test_unsigned_voice_endpoints_still_work_in_dev(voice_server):
    client = voice_server({"APP_ENV": "dev"})
    assert client.post("/twiml/inbound-call", data={"CallSid": "CA1"}).status_code == 200
    with client.websocket_connect("/ws/call") as ws:
        ws.send_text(CONNECTED_FRAME)


def test_concurrent_call_limit_rejects_extra_connections(voice_server):
    client = voice_server({"APP_ENV": "dev"}, max_concurrent_calls=1)
    with client.websocket_connect("/ws/call") as first:
        first.send_text(CONNECTED_FRAME)
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/ws/call"):
                pass
        assert exc_info.value.code == 1013
    # The slot is released once the first call ends.
    with client.websocket_connect("/ws/call") as again:
        again.send_text(CONNECTED_FRAME)


def test_call_is_ended_at_max_duration(voice_server):
    client = voice_server({"APP_ENV": "dev"}, max_call_duration_seconds=1)
    started = time.monotonic()
    with client.websocket_connect("/ws/call") as ws:
        # Keep the stream alive the way Twilio does (a frame every 20 ms in
        # practice); the server must still end the call at the deadline.
        # (TestClient's receive() blocks a worker thread, so the idle-call
        # path -- wait_for timing out with no frames -- only fires under a
        # real ASGI server; see H2 validation in the master plan.)
        while time.monotonic() - started < 1.3:
            ws.send_text(CONNECTED_FRAME)
            time.sleep(0.1)
        ws.send_text(CONNECTED_FRAME)
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_text()
    assert exc_info.value.code == 1000
    assert time.monotonic() - started < 10


# ── Startup refusal (real import in a subprocess) ───────────────────────────

_SCRUBBED = (
    "APP_ENV",
    "AUTH_MODE",
    "DEV_AUTH_ENABLED",
    "VOICE_MOCK_SERVICES",
    "TELEPHONY_MOCK_PIN",
    "OIDC_ISSUER_URL",
    "OIDC_AUDIENCE",
    "OIDC_JWKS_URL",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
)


def _import_server(extra_env):
    env = {k: v for k, v in os.environ.items() if k not in _SCRUBBED}
    env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-c", "import sys; sys.path.insert(0, 'src/api'); import server"],
        cwd=_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.mark.parametrize(
    "extra_env, fragment",
    [
        ({}, "AUTH_MODE"),
        ({"APP_ENV": "production", "AUTH_MODE": "dev", "DEV_AUTH_ENABLED": "true"}, "AUTH_MODE"),
        ({"APP_ENV": "staging", "AUTH_MODE": "production", "VOICE_MOCK_SERVICES": "true"}, "VOICE_MOCK_SERVICES"),
        ({"APP_ENV": "production", "AUTH_MODE": "production", "TELEPHONY_MOCK_PIN": "1234"}, "TELEPHONY_MOCK_PIN"),
        ({"APP_ENV": "prod"}, "not recognised"),
    ],
    ids=["app-env-unset", "dev-auth", "mock-voice", "mock-pin", "unknown-app-env"],
)
def test_server_refuses_to_start_with_dev_conveniences_outside_dev(extra_env, fragment):
    result = _import_server(extra_env)
    assert result.returncode != 0
    assert "SecurityPostureError" in result.stderr and fragment in result.stderr


def test_server_starts_with_production_auth_configured():
    result = _import_server(
        {
            "APP_ENV": "production",
            "AUTH_MODE": "production",
            "OIDC_ISSUER_URL": "https://issuer.example.test/",
            "OIDC_AUDIENCE": "ai-voice-agent",
            "OIDC_JWKS_URL": "https://issuer.example.test/.well-known/jwks.json",
        }
    )
    assert result.returncode == 0, result.stderr[-2000:]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

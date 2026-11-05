"""
Regression tests: the Gemini API key must never appear in exceptions, logs,
fallback errors, or API responses (docs/MASTER_PROJECT_PLAN.md finding F-06).

GeminiLLMProvider used to put the key in the request URL (`?key=...`).
`requests` includes the URL in connection/timeout exception text, the
provider wrapped that text into LLMProviderError, and FallbackLLMProvider
logged it on failover -- so any network error wrote the key to the
application log (CloudWatch in the Phase 26 deployment). OpenTelemetry's
requests instrumentation records the URL as well. Error bodies returned by
the API were also copied into exception messages unfiltered.

These tests use a sentinel key and a closed local port (or a stubbed HTTP
response) so they never touch the real Gemini API.
"""

import logging
import sys
from pathlib import Path

import pytest
import requests

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src" / "agent"))
sys.path.insert(0, str(_ROOT / "src" / "inference"))
sys.path.insert(0, str(_ROOT / "src" / "api"))

from conversation_manager import build_conversation_manager  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from llm_provider import BaseLLMProvider, FallbackLLMProvider, GeminiLLMProvider, LLMProviderError  # noqa: E402

SENTINEL_KEY = "AIzaSENTINEL_do_not_log_0123456789"
UNREACHABLE_BASE = "http://127.0.0.1:9/v1beta/models"


def _unreachable_gemini() -> GeminiLLMProvider:
    provider = GeminiLLMProvider(api_key=SENTINEL_KEY, timeout_seconds=2)
    provider.API_BASE = UNREACHABLE_BASE
    return provider


def _exception_chain_text(exc: BaseException) -> str:
    parts = []
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        parts.append(f"{exc!s} {exc!r}")
        exc = exc.__cause__ or exc.__context__
    return " ".join(parts)


class StaticLLM(BaseLLMProvider):
    provider_name = "static"

    def generate_stream(self, messages, **kwargs):
        yield "Fallback reply."
        yield {"text": "Fallback reply.", "latency_ms": 1.0}


class _FakeResponse:
    def __init__(self, status_code: int, text: str, sse_lines=()):
        self.status_code = status_code
        self.text = text
        self._sse_lines = list(sse_lines)

    def json(self):
        return {"error": {"message": self.text}}

    def iter_lines(self, decode_unicode=True):
        return iter(self._sse_lines)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_key_is_sent_in_header_not_url(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, **kwargs):
        captured["url"] = url
        captured["headers"] = headers or {}
        # A realistic one-chunk Gemini SSE stream (an empty 200 is itself an
        # error since H3 -- see test_voice_deadlines.py).
        return _FakeResponse(200, "", sse_lines=['data: {"candidates": [{"content": {"parts": [{"text": "Hi."}]}}]}'])

    monkeypatch.setattr(requests, "post", fake_post)
    list(GeminiLLMProvider(api_key=SENTINEL_KEY).generate_stream([{"role": "user", "content": "hi"}]))

    assert SENTINEL_KEY not in captured["url"]
    assert captured["headers"].get("x-goog-api-key") == SENTINEL_KEY


def test_network_error_exception_chain_does_not_contain_key():
    with pytest.raises(LLMProviderError) as exc_info:
        list(_unreachable_gemini().generate_stream([{"role": "user", "content": "hi"}]))

    assert SENTINEL_KEY not in _exception_chain_text(exc_info.value)


@pytest.mark.parametrize("status_code", [400, 403, 429, 500])
def test_error_body_echoing_key_is_redacted(monkeypatch, status_code):
    # Some proxies/gateways echo the request URL or headers in error
    # bodies; the provider copies the body into its exception message.
    body = f"request rejected for key={SENTINEL_KEY}"
    monkeypatch.setattr(requests, "post", lambda *a, **k: _FakeResponse(status_code, body))

    with pytest.raises(LLMProviderError) as exc_info:
        list(GeminiLLMProvider(api_key=SENTINEL_KEY).generate_stream([{"role": "user", "content": "hi"}]))

    assert SENTINEL_KEY not in _exception_chain_text(exc_info.value)


def test_failover_logs_do_not_contain_key(caplog):
    provider = FallbackLLMProvider(primary=_unreachable_gemini(), fallback=StaticLLM())

    with caplog.at_level(logging.DEBUG):
        items = list(provider.generate_stream([{"role": "user", "content": "hi"}]))

    assert items[-1]["fallback_used"] is True
    assert caplog.records, "failover should have been logged"
    for record in caplog.records:
        assert SENTINEL_KEY not in record.getMessage()
        if record.exc_info:
            assert SENTINEL_KEY not in _exception_chain_text(record.exc_info[1])


def test_api_response_and_logs_do_not_contain_key_when_gemini_fails(caplog):
    # Control: this path already returned only the generic failure text
    # before the fix; kept so a future change can't start echoing
    # provider errors to clients.
    import server

    manager = build_conversation_manager(
        llm_provider=_unreachable_gemini(), rag_enabled=False, persistence_enabled=False, reliability_enabled=False
    )
    previous = server._conversation_manager
    server._conversation_manager = manager
    try:
        with caplog.at_level(logging.DEBUG):
            client = TestClient(server.app)
            blocking = client.post("/generate", json={"message": "What are your opening hours?"})
            streaming = client.post("/generate", json={"message": "What are your opening hours?", "stream": True})
    finally:
        server._conversation_manager = previous

    assert blocking.status_code == 200
    assert blocking.json()["response"] == manager.LLM_FAILURE_RESPONSE
    assert SENTINEL_KEY not in blocking.text
    assert SENTINEL_KEY not in streaming.text
    for record in caplog.records:
        assert SENTINEL_KEY not in record.getMessage()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

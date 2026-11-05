"""
Text-API rate limiting (docs/MASTER_PROJECT_PLAN.md H2, F-04):
src/api/rate_limiter.py in isolation, then through the real routes.
"""

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src" / "agent"))
sys.path.insert(0, str(_ROOT / "src" / "inference"))
sys.path.insert(0, str(_ROOT / "src" / "api"))

import server  # noqa: E402
from conversation_manager import build_conversation_manager  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from llm_provider import BaseLLMProvider  # noqa: E402
from rate_limiter import TokenBucketRateLimiter  # noqa: E402
from test_production_posture import FakeOIDCProvider  # noqa: E402


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_burst_then_refill():
    clock = FakeClock()
    limiter = TokenBucketRateLimiter(per_minute=60, burst=3, clock=clock)

    assert [limiter.check("u")[0] for _ in range(4)] == [True, True, True, False]
    assert limiter.check("u") == (False, 1)
    clock.now += 1.0  # 60/min refills one token per second
    assert limiter.check("u") == (True, 0)
    assert limiter.check("u")[0] is False


def test_keys_are_independent():
    limiter = TokenBucketRateLimiter(per_minute=60, burst=1, clock=FakeClock())
    assert limiter.check("alice")[0] is True
    assert limiter.check("alice")[0] is False
    assert limiter.check("bob")[0] is True


def test_retry_after_reflects_refill_rate():
    limiter = TokenBucketRateLimiter(per_minute=6, burst=1, clock=FakeClock())  # one token per 10 s
    limiter.check("u")
    assert limiter.check("u") == (False, 10)


def test_tracked_keys_are_bounded():
    limiter = TokenBucketRateLimiter(per_minute=60, burst=1, max_keys=100, clock=FakeClock())
    for i in range(1000):
        limiter.check(f"user-{i}")
    assert limiter.tracked_keys() == 100


def test_invalid_configuration_is_rejected():
    with pytest.raises(ValueError):
        TokenBucketRateLimiter(per_minute=0, burst=1)


class StaticLLM(BaseLLMProvider):
    provider_name = "static"

    def generate_stream(self, messages, **kwargs):
        yield "ok"
        yield {"text": "ok", "latency_ms": 1.0}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(server, "_authentication_provider", FakeOIDCProvider())
    monkeypatch.setattr(
        server,
        "_conversation_manager",
        build_conversation_manager(llm_provider=StaticLLM(), rag_enabled=False, persistence_enabled=False),
    )
    monkeypatch.setattr(server, "_rate_limiter", TokenBucketRateLimiter(per_minute=60, burst=2))
    return TestClient(server.app)


BODY = {"message": "hello"}


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_generate_is_rate_limited_per_user(client):
    codes = [client.post("/generate", json=BODY, headers=_auth("token-alice")).status_code for _ in range(3)]
    assert codes == [200, 200, 429]

    limited = client.post("/generate", json=BODY, headers=_auth("token-alice"))
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) >= 1
    assert client.post("/generate", json=BODY, headers=_auth("token-bob")).status_code == 200


def test_job_submission_shares_the_users_budget(client):
    assert client.post("/generate", json=BODY, headers=_auth("token-alice")).status_code == 200
    assert client.post("/jobs/generate", json=BODY, headers=_auth("token-alice")).status_code == 202
    assert client.post("/jobs/generate", json=BODY, headers=_auth("token-alice")).status_code == 429


def test_rejected_credentials_do_not_consume_budget(client):
    for _ in range(5):
        assert client.post("/generate", json=BODY, headers=_auth("bad")).status_code == 401
    assert client.post("/generate", json=BODY, headers=_auth("token-alice")).status_code == 200


def test_rate_limit_is_off_by_default_in_dev():
    assert server._SECURITY.is_dev and server._rate_limiter is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

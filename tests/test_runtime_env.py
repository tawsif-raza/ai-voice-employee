"""
Unit tests for src/agent/runtime_env.py -- the APP_ENV security posture
(docs/MASTER_PROJECT_PLAN.md H2, F-03/F-04). Pure: no server import.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "agent"))

from runtime_env import AppEnv, SecurityPostureError, load_security_settings  # noqa: E402

PRODUCTION_OK = {"APP_ENV": "production", "AUTH_MODE": "production"}


def test_unset_app_env_means_production():
    with pytest.raises(SecurityPostureError, match="AUTH_MODE"):
        load_security_settings({})  # AUTH_MODE defaults to dev -> refused


def test_unknown_app_env_is_refused():
    with pytest.raises(SecurityPostureError, match="not recognised"):
        load_security_settings({"APP_ENV": "prod"})


@pytest.mark.parametrize("app_env", ["production", "staging", "PRODUCTION "])
def test_non_dev_requires_production_auth(app_env):
    with pytest.raises(SecurityPostureError, match="AUTH_MODE"):
        load_security_settings({"APP_ENV": app_env, "AUTH_MODE": "dev"})


@pytest.mark.parametrize(
    "extra, fragment",
    [
        ({"VOICE_MOCK_SERVICES": "true"}, "VOICE_MOCK_SERVICES"),
        ({"TELEPHONY_MOCK_PIN": "1234"}, "TELEPHONY_MOCK_PIN"),
    ],
)
def test_non_dev_refuses_dev_conveniences(extra, fragment):
    with pytest.raises(SecurityPostureError, match=fragment):
        load_security_settings({**PRODUCTION_OK, **extra})


def test_all_violations_are_reported_together():
    with pytest.raises(SecurityPostureError) as exc_info:
        load_security_settings({"APP_ENV": "staging", "VOICE_MOCK_SERVICES": "1", "TELEPHONY_MOCK_PIN": "9"})
    message = str(exc_info.value)
    assert "AUTH_MODE" in message and "VOICE_MOCK_SERVICES" in message and "TELEPHONY_MOCK_PIN" in message


@pytest.mark.parametrize("auth_mode", ["production", "oidc"])
def test_production_posture_is_strict(auth_mode):
    settings = load_security_settings({"APP_ENV": "production", "AUTH_MODE": auth_mode})
    assert settings.app_env is AppEnv.PRODUCTION
    assert settings.allow_anonymous_text_api is False
    assert settings.require_twilio_signature is True
    assert settings.rate_limit_per_minute > 0


def test_dev_posture_keeps_conveniences():
    settings = load_security_settings(
        {"APP_ENV": "dev", "AUTH_MODE": "dev", "VOICE_MOCK_SERVICES": "true", "TELEPHONY_MOCK_PIN": "1234"}
    )
    assert settings.is_dev
    assert settings.allow_anonymous_text_api is True
    assert settings.require_twilio_signature is False
    assert settings.rate_limit_per_minute == 0


def test_numeric_overrides_and_validation():
    settings = load_security_settings(
        {**PRODUCTION_OK, "RATE_LIMIT_REQUESTS_PER_MINUTE": "5", "MAX_CONCURRENT_CALLS": "3"}
    )
    assert settings.rate_limit_per_minute == 5 and settings.max_concurrent_calls == 3
    with pytest.raises(SecurityPostureError, match="MAX_CONCURRENT_CALLS"):
        load_security_settings({**PRODUCTION_OK, "MAX_CONCURRENT_CALLS": "0"})
    with pytest.raises(SecurityPostureError, match="RATE_LIMIT_BURST"):
        load_security_settings({**PRODUCTION_OK, "RATE_LIMIT_BURST": "many"})


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

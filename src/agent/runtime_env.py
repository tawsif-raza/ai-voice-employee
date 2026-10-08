"""
Deployment environment and security posture (docs/MASTER_PROJECT_PLAN.md
H2; findings F-03, F-04).

One switch, `APP_ENV`, decides whether development conveniences are
available. It is deliberately closed by default: an unset `APP_ENV` means
`production`, so a container or host that forgets to configure it gets the
strict posture rather than the permissive one.

  dev         -- local development and the test suite. Anonymous text-API
                 access, the dev bearer-token table, mock voice services,
                 the telephony mock PIN, and unsigned Twilio endpoints are
                 all permitted (subject to their own existing flags).
  staging /   -- every one of those conveniences is refused. Staging is a
  production     separate label only so logs/metrics can tell them apart.

`load_security_settings()` is a pure function of an environment mapping so
the rules can be unit-tested without importing the server; the server
calls it once at import time and fails to start on a forbidden
combination, the same fail-closed behaviour as a missing OIDC config.
"""

import os
from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Optional


class AppEnv(str, Enum):
    DEV = "dev"
    STAGING = "staging"
    PRODUCTION = "production"


class SecurityPostureError(RuntimeError):
    """Raised at startup when configuration violates the environment's posture."""


PRODUCTION_AUTH_MODES = frozenset({"production", "oidc"})

# Text-API rate limit defaults (requests per identity). Off in dev so local
# work and the test suite are unaffected unless explicitly configured.
_DEFAULT_RATE_LIMIT_PER_MINUTE = {AppEnv.DEV: 0, AppEnv.STAGING: 60, AppEnv.PRODUCTION: 60}
_DEFAULT_RATE_LIMIT_BURST = 10
_DEFAULT_MAX_CONCURRENT_CALLS = 10
_DEFAULT_MAX_CALL_DURATION_SECONDS = 1800


@dataclass(frozen=True)
class SecuritySettings:
    app_env: AppEnv
    auth_mode: str
    allow_anonymous_text_api: bool
    require_twilio_signature: bool
    rate_limit_per_minute: int
    rate_limit_burst: int
    max_concurrent_calls: int
    max_call_duration_seconds: int

    @property
    def is_dev(self) -> bool:
        return self.app_env is AppEnv.DEV


def _truthy(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _int_setting(environ: Mapping[str, str], name: str, default: int, minimum: int) -> int:
    raw = (environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SecurityPostureError(f"{name} must be an integer, got {raw!r}.") from None
    if value < minimum:
        raise SecurityPostureError(f"{name} must be >= {minimum}, got {value}.")
    return value


def resolve_app_env(environ: Mapping[str, str]) -> AppEnv:
    raw = (environ.get("APP_ENV") or "").strip().lower()
    if not raw:
        return AppEnv.PRODUCTION
    try:
        return AppEnv(raw)
    except ValueError:
        allowed = ", ".join(e.value for e in AppEnv)
        raise SecurityPostureError(f"APP_ENV={raw!r} is not recognised (expected one of: {allowed}).") from None


def load_security_settings(environ: Optional[Mapping[str, str]] = None) -> SecuritySettings:
    """
    Resolves and validates the security posture. Raises SecurityPostureError
    listing every violation (not just the first) for a non-dev environment
    that has a development convenience switched on.
    """
    env = os.environ if environ is None else environ
    app_env = resolve_app_env(env)
    auth_mode = (env.get("AUTH_MODE") or "dev").strip().lower()

    if app_env is not AppEnv.DEV:
        violations = []
        if auth_mode not in PRODUCTION_AUTH_MODES:
            violations.append(
                f"AUTH_MODE={auth_mode!r} uses the development token table; set AUTH_MODE=production (OIDC)."
            )
        if _truthy(env.get("VOICE_MOCK_SERVICES")):
            violations.append("VOICE_MOCK_SERVICES=true replaces real STT/TTS with mocks.")
        if (env.get("TELEPHONY_MOCK_PIN") or "").strip():
            violations.append("TELEPHONY_MOCK_PIN is a single shared mock secret, not caller verification.")
        if violations:
            raise SecurityPostureError(
                f"APP_ENV={app_env.value} forbids development conveniences "
                "(set APP_ENV=dev only for local development):\n  - " + "\n  - ".join(violations)
            )

    return SecuritySettings(
        app_env=app_env,
        auth_mode=auth_mode,
        allow_anonymous_text_api=app_env is AppEnv.DEV,
        require_twilio_signature=app_env is not AppEnv.DEV,
        rate_limit_per_minute=_int_setting(
            env, "RATE_LIMIT_REQUESTS_PER_MINUTE", _DEFAULT_RATE_LIMIT_PER_MINUTE[app_env], minimum=0
        ),
        rate_limit_burst=_int_setting(env, "RATE_LIMIT_BURST", _DEFAULT_RATE_LIMIT_BURST, minimum=1),
        max_concurrent_calls=_int_setting(env, "MAX_CONCURRENT_CALLS", _DEFAULT_MAX_CONCURRENT_CALLS, minimum=1),
        max_call_duration_seconds=_int_setting(
            env, "MAX_CALL_DURATION_SECONDS", _DEFAULT_MAX_CALL_DURATION_SECONDS, minimum=1
        ),
    )

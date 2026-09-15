"""
Twilio webhook request signature validation (Phase 1.4 external integration
closure; docs/phase1.4-external-integration-report.md Section 2/3).

Implements Twilio's documented X-Twilio-Signature algorithm
(https://www.twilio.com/docs/usage/security#validating-requests) with no
third-party `twilio` SDK dependency -- this repository consistently
hand-rolls small, single-purpose validation utilities (see oidc_provider.py,
idempotency_repository.py) rather than pulling in a full SDK for one
HMAC check.

Never logs the auth token or the computed/received signature value -- only
ever returns a bool. Callers must not log those either.
"""

import hashlib
import hmac
from base64 import b64encode
from typing import Mapping


def compute_signature(auth_token: str, full_url: str, params: Mapping[str, str]) -> str:
    """
    Reproduces Twilio's expected signature for a request: the full URL
    (scheme + host + path + query string, exactly as Twilio saw it) with
    every param's key+value appended directly (no delimiter), params
    sorted by key in byte order, HMAC-SHA1'd with `auth_token`, base64-encoded.

    For a GET/WebSocket-upgrade request with no form body, pass an empty
    `params` mapping -- the signature then covers the URL alone.
    """
    data = full_url
    for key in sorted(params.keys()):
        data += f"{key}{params[key]}"
    digest = hmac.new(auth_token.encode("utf-8"), data.encode("utf-8"), hashlib.sha1).digest()
    return b64encode(digest).decode("utf-8")


def validate_signature(auth_token: str, full_url: str, params: Mapping[str, str], received_signature: str) -> bool:
    """
    Constant-time comparison against the expected signature. Returns False
    (never raises) for a missing/empty `received_signature` or `auth_token`
    -- an unconfigured caller should decide separately whether validation
    is even required (see is_validation_configured()), not get a crash here.
    """
    if not auth_token or not received_signature:
        return False
    expected = compute_signature(auth_token, full_url, params)
    return hmac.compare_digest(expected, received_signature)


def is_validation_configured(auth_token: str) -> bool:
    """
    Whether signature enforcement should be active at all. Matches this
    codebase's existing "presence of real config enables the real check"
    convention (VOICE_MOCK_SERVICES / DEEPGRAM_API_KEY / etc. in
    src/api/server.py) -- a dev/mock environment with no TWILIO_AUTH_TOKEN
    configured is never blocked by a check it can't satisfy.
    """
    return bool(auth_token)

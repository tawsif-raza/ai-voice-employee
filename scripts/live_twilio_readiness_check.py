"""
Phase 1.4 external integration test harness -- Twilio Media Streams
readiness check (docs/phase1.4-external-integration-report.md Section 2/3).

IMPORTANT SCOPE: this script can verify that the DEPLOYED infrastructure is
reachable and correctly configured (public HTTPS endpoint up, TwiML
response well-formed, WebSocket endpoint accepting connections, signature
enforcement active). It CANNOT place a real phone call -- that requires an
actual Twilio account with a phone number, a human or Twilio's API
originating a call to that number, and that number's voice webhook pointed
at this deployment. Every scenario that requires an actual inbound call
(START, media frames, STT, LLM, TTS, Twilio playback, disconnect, silence,
interruption, barge-in, malformed event, provider failure, session cleanup
under REAL Twilio traffic) is therefore always reported NOT RUN by this
script, with exact manual steps in the phase report -- never fabricated.

Opt-in contract (same shape as scripts/live_provider_verification.py):
  - Requires RUN_LIVE_TWILIO_TESTS=1.
  - Requires VOICE_PUBLIC_URL (or TWILIO_MEDIA_STREAM_URL) pointed at a
    real, reachable, non-localhost HTTPS deployment -- a local dev server
    is not "publicly reachable" and this script will mark the reachability
    scenarios UNVERIFIED with that exact reason rather than pretend
    localhost satisfies "publicly reachable HTTPS endpoint."
  - Never logs TWILIO_AUTH_TOKEN's value.

Usage:
    $env:RUN_LIVE_TWILIO_TESTS = "1"
    $env:VOICE_PUBLIC_URL = "https://<your-canary-host>"
    $env:TWILIO_AUTH_TOKEN = "<test-safe auth token>"   # optional, enables signature checks
    python scripts/live_twilio_readiness_check.py
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "api"))

RESULTS_PATH = Path(__file__).resolve().parents[1] / "docs" / "phase1.4-live-twilio-results.json"
_RESULTS: list[dict] = []


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record(
    scenario: str, result: str, source: str, latency_ms=None, error_category=None, remediation=None, note=None
) -> None:
    entry = {
        "provider": "twilio",
        "scenario": scenario,
        "timestamp": _now_iso(),
        "latency_ms": round(latency_ms, 1) if latency_ms is not None else None,
        "result": result,
        "source": source,
        "error_category": error_category,
        "remediation": remediation,
        "note": note,
    }
    _RESULTS.append(entry)
    print(f"[{source}] twilio/{scenario}: {result}")


_REAL_CALL_SCENARIOS_NOT_RUNNABLE_HERE = [
    "incoming_call_to_twiml",
    "media_stream_established",
    "audio_frames_received",
    "stt_transcription",
    "conversation_manager_turn",
    "llm_response",
    "tts_synthesis",
    "twilio_playback",
    "websocket_disconnect_real",
    "call_termination_real",
    "caller_silence_real",
    "caller_interruption_real",
    "barge_in_real",
    "malformed_event_real",
    "provider_failure_real",
    "session_cleanup_real",
]

_REMEDIATION_NEEDS_REAL_CALL = (
    "Requires a real Twilio phone number pointed at this deployment's "
    "/twiml/inbound-call and an actual inbound phone call -- cannot be "
    "produced by a script. See docs/phase1.4-external-integration-report.md "
    "Section 8 for the exact manual steps."
)


def main() -> int:
    if os.environ.get("RUN_LIVE_TWILIO_TESTS", "").strip() != "1":
        print(
            "RUN_LIVE_TWILIO_TESTS is not set to '1' -- refusing to run any live "
            "network check. Set it and provide a real, reachable VOICE_PUBLIC_URL "
            "to run this script's infra-readiness checks."
        )
        for scenario in [
            "public_endpoint_reachable",
            "twiml_response_valid",
            "websocket_endpoint_reachable",
            "signature_enforcement_active",
        ] + _REAL_CALL_SCENARIOS_NOT_RUNNABLE_HERE:
            _record(
                scenario, "NOT RUN", "LIVE", error_category="opt_in_not_set", remediation="Set RUN_LIVE_TWILIO_TESTS=1."
            )
        RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        RESULTS_PATH.write_text(json.dumps(_RESULTS, indent=2), encoding="utf-8")
        return 1

    account_sid = bool(os.environ.get("TWILIO_ACCOUNT_SID"))
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN", "")
    phone_number = bool(os.environ.get("TWILIO_PHONE_NUMBER"))
    print(f"TWILIO_ACCOUNT_SID configured: {account_sid}")
    print(f"TWILIO_AUTH_TOKEN configured: {bool(auth_token)}")
    print(f"TWILIO_PHONE_NUMBER configured: {phone_number}")

    public_url = (os.environ.get("VOICE_PUBLIC_URL") or os.environ.get("TWILIO_MEDIA_STREAM_URL") or "").strip()
    parsed = urlparse(public_url) if public_url else None
    is_https = bool(parsed and parsed.scheme in ("https", "wss"))
    is_localhost = bool(parsed and parsed.hostname in ("localhost", "127.0.0.1", "::1"))

    if not public_url or not is_https or is_localhost:
        reason = (
            "VOICE_PUBLIC_URL/TWILIO_MEDIA_STREAM_URL is not set to a real, "
            "reachable, non-localhost HTTPS URL -- a local dev server is not "
            "a 'publicly reachable HTTPS endpoint,' so this cannot be marked PASS."
        )
        for scenario in [
            "public_endpoint_reachable",
            "twiml_response_valid",
            "websocket_endpoint_reachable",
            "signature_enforcement_active",
        ]:
            _record(scenario, "UNVERIFIED", "LIVE", error_category="no_public_endpoint", remediation=reason)
    else:
        _run_infra_checks(public_url, auth_token)

    for scenario in _REAL_CALL_SCENARIOS_NOT_RUNNABLE_HERE:
        _record(
            scenario,
            "NOT RUN",
            "LIVE",
            error_category="requires_real_inbound_call",
            remediation=_REMEDIATION_NEEDS_REAL_CALL,
        )

    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(json.dumps(_RESULTS, indent=2), encoding="utf-8")
    print(f"\nWrote {len(_RESULTS)} result(s) to {RESULTS_PATH}")

    any_fail = any(r["result"] == "FAIL" for r in _RESULTS)
    return 1 if any_fail else 0


def _run_infra_checks(public_url: str, auth_token: str) -> None:
    import requests

    base = public_url.rstrip("/")
    http_base = base.replace("wss://", "https://").replace("ws://", "http://")

    t0 = time.perf_counter()
    try:
        resp = requests.get(f"{http_base}/health", timeout=10.0)
        latency_ms = (time.perf_counter() - t0) * 1000
        if resp.status_code == 200:
            _record("public_endpoint_reachable", "PASS", "LIVE", latency_ms)
        else:
            _record("public_endpoint_reachable", "FAIL", "LIVE", latency_ms, error_category=f"http_{resp.status_code}")
    except Exception as exc:
        _record(
            "public_endpoint_reachable",
            "FAIL",
            "LIVE",
            error_category=type(exc).__name__,
            remediation="Confirm the deployment is up and the URL/DNS/TLS/firewall are correct.",
        )
        # Nothing downstream of the endpoint can be checked if it's unreachable.
        _record("twiml_response_valid", "NOT RUN", "LIVE", error_category="endpoint_unreachable")
        _record("websocket_endpoint_reachable", "NOT RUN", "LIVE", error_category="endpoint_unreachable")
        _record("signature_enforcement_active", "NOT RUN", "LIVE", error_category="endpoint_unreachable")
        return

    t0 = time.perf_counter()
    try:
        if auth_token:
            from twilio_signature import compute_signature

            params = {"CallSid": "CA_PHASE1_4_READINESS_CHECK", "From": "+15550000000", "To": "+15550000001"}
            sig = compute_signature(auth_token, f"{http_base}/twiml/inbound-call", params)
            resp = requests.post(
                f"{http_base}/twiml/inbound-call", data=params, headers={"X-Twilio-Signature": sig}, timeout=10.0
            )
        else:
            resp = requests.post(f"{http_base}/twiml/inbound-call", timeout=10.0)
        latency_ms = (time.perf_counter() - t0) * 1000
        body = resp.text
        valid = resp.status_code == 200 and "<Stream" in body and ("wss://" in body or "ws://" in body)
        _record(
            "twiml_response_valid",
            "PASS" if valid else "FAIL",
            "LIVE",
            latency_ms,
            note="signed request (real X-Twilio-Signature computed against this deployment's real TWILIO_AUTH_TOKEN)"
            if auth_token
            else "TWILIO_AUTH_TOKEN not set -- signature enforcement not exercised here",
        )

        if auth_token:
            t0 = time.perf_counter()
            unsigned_resp = requests.post(f"{http_base}/twiml/inbound-call", data=params, timeout=10.0)
            latency_ms = (time.perf_counter() - t0) * 1000
            enforced = unsigned_resp.status_code == 403
            _record(
                "signature_enforcement_active",
                "PASS" if enforced else "FAIL",
                "LIVE",
                latency_ms,
                note=f"unsigned request got HTTP {unsigned_resp.status_code}, expected 403",
            )
        else:
            _record(
                "signature_enforcement_active",
                "UNVERIFIED",
                "LIVE",
                error_category="no_auth_token",
                remediation="Set TWILIO_AUTH_TOKEN to exercise signature enforcement.",
            )
    except Exception as exc:
        _record("twiml_response_valid", "FAIL", "LIVE", error_category=type(exc).__name__)
        _record("signature_enforcement_active", "NOT RUN", "LIVE", error_category=type(exc).__name__)

    _check_websocket(base)


def _check_websocket(public_url: str) -> None:
    ws_url = public_url.replace("https://", "wss://").replace("http://", "ws://")
    if not ws_url.endswith("/ws/call"):
        ws_url = ws_url.rstrip("/") + "/ws/call"

    t0 = time.perf_counter()
    try:
        import asyncio

        import websockets

        async def _try_connect():
            async with websockets.connect(ws_url, open_timeout=10.0):
                return True

        connected = asyncio.run(_try_connect())
        latency_ms = (time.perf_counter() - t0) * 1000
        _record("websocket_endpoint_reachable", "PASS" if connected else "FAIL", "LIVE", latency_ms)
    except Exception as exc:
        _record(
            "websocket_endpoint_reachable",
            "FAIL",
            "LIVE",
            error_category=type(exc).__name__,
            remediation="Confirm /ws/call is reachable through any reverse proxy/load balancer (WebSocket upgrade must be passed through, not just plain HTTP).",
        )


if __name__ == "__main__":
    sys.exit(main())

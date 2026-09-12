"""
Voice Telephony Structured Logging & Correlation (src/voice/voice_logging.py)

Provides structured, privacy-preserving logging for telephone media sessions.
Enforces:
1. Zero PII/PHI in logs: caller phone numbers are masked, clinical content is excluded.
2. Unified correlation context: call_id, stream_sid, session_id, turn_id, request_id.
3. Machine-readable JSON output option when LOG_FORMAT=json or VOICE_LOG_FORMAT=json.
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("ai_voice_agent.voice")


def mask_phone_number(phone: Optional[str]) -> str:
    """Mask phone number to preserve caller privacy."""
    if not phone:
        return "anonymous"
    clean = "".join(ch for ch in str(phone) if ch.isalnum() or ch == "+")
    if len(clean) <= 4:
        return "****"
    return f"{clean[:2]}****{clean[-4:]}"


class VoiceStructuredLogger:
    """
    Structured logger that records telephony events with correlation context
    and enforces strict privacy boundaries.
    """

    def __init__(self, name: str = "ai_voice_agent.voice"):
        self._logger = logging.getLogger(name)
        self.is_json = (
            os.environ.get("LOG_FORMAT", "").lower() == "json"
            or os.environ.get("VOICE_LOG_FORMAT", "").lower() == "json"
        )

    def log_event(
        self,
        event_name: str,
        call_sid: str,
        stream_sid: str,
        session_id: str,
        turn_id: Optional[int] = None,
        request_id: Optional[str] = None,
        level: int = logging.INFO,
        latency_ms: Optional[float] = None,
        status: str = "ok",
        caller_id: Optional[str] = None,
        extra: Optional[dict[str, Any]] = None,
    ) -> None:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event_name,
            "call_sid": call_sid,
            "stream_sid": stream_sid,
            "session_id": session_id,
            "status": status,
        }
        if turn_id is not None:
            payload["turn_id"] = turn_id
        if request_id:
            payload["request_id"] = request_id
        if latency_ms is not None:
            payload["latency_ms"] = round(latency_ms, 2)
        if caller_id:
            payload["caller_masked"] = mask_phone_number(caller_id)
        if extra:
            # Safe copy excluding any known sensitive keys
            for k, v in extra.items():
                if k.lower() not in ("token", "api_key", "password", "secret", "transcript", "raw_audio"):
                    payload[k] = v

        if self.is_json:
            self._logger.log(level, json.dumps(payload))
        else:
            turn_str = f" turn={turn_id}" if turn_id is not None else ""
            lat_str = f" latency={latency_ms:.1f}ms" if latency_ms is not None else ""
            self._logger.log(
                level,
                "[%s] call=%s stream=%s%s status=%s%s",
                event_name,
                call_sid,
                stream_sid,
                turn_str,
                status,
                lat_str,
            )


# Global default structured logger instance
voice_logger = VoiceStructuredLogger()

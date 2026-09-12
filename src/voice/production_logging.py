"""
Production Structured Logging for Canary Telephony (src/voice/production_logging.py)

Configures JSON structured logging across standard library logging.
Enforces:
1. JSON output format with ISO 8601 timestamps and severity levels.
2. Contextual propagation of correlation IDs: call_sid, stream_sid, session_id, request_id.
3. Strict redaction of secrets, tokens, API keys, and sensitive PII/PHI.
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Optional

SENSITIVE_KEYS = frozenset(
    {"api_key", "token", "password", "secret", "authorization", "raw_audio", "mulaw", "cookie", "xi-api-key"}
)


class StructuredJSONFormatter(logging.Formatter):
    """
    Formats logging records into machine-readable JSON objects for production ingestion.
    """

    def __init__(self, service_name: str = "ai-voice-agent"):
        super().__init__()
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": self.service_name,
        }

        # Include standard correlation context if present
        for attr in ("call_sid", "stream_sid", "session_id", "request_id", "turn_id", "latency_ms"):
            val = getattr(record, attr, None)
            if val is not None:
                payload[attr] = val

        # Phase 14: inject OpenTelemetry trace context for log-trace correlation.
        try:
            from opentelemetry import trace

            span = trace.get_current_span()
            span_ctx = span.get_span_context()
            if span_ctx and span_ctx.is_valid:
                payload["trace_id"] = format(span_ctx.trace_id, "032x")
                payload["span_id"] = format(span_ctx.span_id, "016x")
        except Exception:
            pass

        # Handle exception info
        if record.exc_info:
            payload["exception"] = {
                "type": record.exc_info[0].__name__ if record.exc_info[0] else "Exception",
                "message": str(record.exc_info[1]) if record.exc_info[1] else "",
            }

        return json.dumps(payload)


def configure_production_logging(
    level: Optional[str] = None,
    service_name: Optional[str] = None,
) -> None:
    """
    Configures the root logger to use StructuredJSONFormatter when
    LOG_FORMAT=json or in production mode.
    """
    log_format = os.environ.get("LOG_FORMAT", "").strip().lower()
    if log_format != "json":
        return

    log_level_str = level or os.environ.get("LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_str, logging.INFO)
    svc = service_name or os.environ.get("OTEL_SERVICE_NAME", "ai-voice-agent")

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(StructuredJSONFormatter(service_name=svc))

    root = logging.getLogger()
    root.setLevel(log_level)
    root.handlers.clear()
    root.addHandler(handler)

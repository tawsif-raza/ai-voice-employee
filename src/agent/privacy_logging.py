"""
Centralized, privacy-aware logging boundary (Phase 6; plan.md Step 6.8).

This repository has no logging framework anywhere in its live request
path today — every existing print() statement (src/inference/llm_service.py's
startup messages, src/api/server.py's model-loading messages,
src/inference/predict.py's interactive REPL echo, src/eval/evaluate.py's
benchmark progress) is either offline tooling or a CLI's own echo of a
conversation it's actively driving, not a persistent log sink, and none
of them log raw user_input/response content today (confirmed by
inspection — see PHASE_6 report Section "Data Flow Audit"). This module
exists so that the FIRST real structured logging this application adds
(here: one turn-completion log line in ConversationManager, wired in this
phase to prove the boundary actually works end to end) goes through
sanitization automatically, rather than requiring every future developer
to remember `redact(x)` before every log call (plan.md's explicit
instruction).

Log injection hardening (Phase 11; plan.md Step 11.17): no handler in
this repository currently formats `record.privacy_payload` into an
output sink (no `basicConfig()`/`addHandler()` exists anywhere), so
today's deployment has no live log-injection exposure through this path
-- but `conversation_manager.py`'s "conversation_turn_completed" log
call passes fully attacker-controlled `user_input` into that payload,
and the moment a future handler/formatter references it, an unescaped
newline (`"msg\nlevel=CRITICAL\nadmin=true"`) or ANSI escape sequence
could forge what looks like a separate, fabricated log entry in a raw
text sink. `PrivacySanitizingFilter` therefore neutralizes control
characters in every string it touches (message and payload alike),
independent of and in addition to its existing PII sanitization -- a
defense-in-depth fix for a latent rather than currently-exploited gap.
"""

import logging
import re
from typing import Optional

from privacy_service import PrivacyService

_LOGGER_NAME = "ai_voice_agent.privacy_aware"

# Matches CR/LF (log-line forging) and ANSI/terminal escape sequences
# (which could otherwise manipulate a terminal-based log viewer). Kept
# narrow and explicit rather than a generic "strip all control chars"
# rule, so ordinary printable text is never touched.
_LOG_INJECTION_PATTERN = re.compile(r"[\r\n]|\x1b\[[0-9;]*[a-zA-Z]")


def _neutralize_log_injection(value):
    """Recursively escapes CR/LF and ANSI escape sequences in strings so a single structured payload can never be split into forged, independent-looking log lines."""
    if isinstance(value, str):
        return _LOG_INJECTION_PATTERN.sub(lambda m: {"\r": "\\r", "\n": "\\n"}.get(m.group(0), ""), value)
    if isinstance(value, dict):
        return {k: _neutralize_log_injection(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_neutralize_log_injection(v) for v in value)
    return value


class PrivacySanitizingFilter(logging.Filter):
    """
    A logging.Filter that sanitizes a record's structured `extra` payload
    (stored under `record.privacy_payload` by log_event() below) through
    PrivacyService.sanitize() before the record reaches any handler.
    Operates on the LogRecord itself — the actual boundary point, not a
    helper function callers might forget to use (plan.md Step 6.13:
    "Do not merely test the redaction function. Test the actual logging
    boundary.").
    """

    def __init__(self, privacy_service: PrivacyService, context: str = "LOGGING"):
        super().__init__()
        self._privacy_service = privacy_service
        self._context = context

    def filter(self, record: logging.LogRecord) -> bool:
        payload = getattr(record, "privacy_payload", None)
        if payload is not None:
            sanitized = self._privacy_service.sanitize(payload, context=self._context)
            record.privacy_payload = _neutralize_log_injection(sanitized)
        if isinstance(record.msg, str):
            sanitized_msg = self._privacy_service.sanitize(record.msg, context=self._context)
            record.msg = _neutralize_log_injection(sanitized_msg)
        return True


def get_privacy_aware_logger(privacy_service: PrivacyService, name: str = _LOGGER_NAME) -> logging.Logger:
    """
    Returns a logger with a PrivacySanitizingFilter attached exactly
    once. Every message and every `privacy_payload` extra passed through
    log_event() below is sanitized before any handler (including a
    test's own captured-records handler) sees it.
    """
    logger = logging.getLogger(name)
    if not any(isinstance(f, PrivacySanitizingFilter) for f in logger.filters):
        logger.addFilter(PrivacySanitizingFilter(privacy_service))
    return logger


def log_event(logger: logging.Logger, message: str, payload: Optional[dict] = None, level: int = logging.INFO) -> None:
    """
    Emits one structured log record. `payload` (e.g. {"user_input": ...,
    "response": ...}) is attached as `record.privacy_payload`, which the
    logger's PrivacySanitizingFilter (if present) sanitizes before the
    record is handled — see get_privacy_aware_logger().
    """
    logger.log(level, message, extra={"privacy_payload": payload or {}})

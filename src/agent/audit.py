"""
AuditLogger, AuditRepository, and SecurityEventDetector (Phase 8; plan.md
Steps 8.5, 8.6, 8.16, 8.17).

Discipline every call site in this codebase follows (PolicyEngine,
ToolOrchestrator, SessionManager, MemoryManager, PrivacyService, identity.py):
observability code OBSERVES a decision that has already been made by the
real authoritative component (PolicyEngine.evaluate_*(), ToolOrchestrator's
actual execution, SessionManager's actual transition, etc.) and emits a
record of it. No audit-emission call ever runs *before* or *instead of*
the real decision, and nothing in this module has a return value any
caller uses to decide what to do next — audit() calls are fire-and-forget
by construction (they return None). This is what makes "observability
must not change business decisions" (plan.md Principle 1) structurally
true rather than just a convention: there is no code path back from this
module into a decision.

Failure posture (plan.md Steps 8.18, 8.21): recording an audit event is
best-effort — an internal AuditLogger failure (e.g. a corrupted
repository) is caught and swallowed, never propagated into the caller's
control flow. No audit event in this implementation is treated as
"security-critical to persist" in the sense of blocking the underlying
action if recording fails; this is a deliberate, documented scope choice
(see PHASE_8 report's Limitations) — a future phase could introduce a
durable, failure-visible audit sink for specific event types if that
becomes a real requirement.
"""

import logging
import threading
from collections import OrderedDict, deque
from typing import Optional

from observability_models import AuditEvent, EventType, SecurityEvent, Severity, new_event_id, now_utc
from privacy_logging import get_privacy_aware_logger, log_event

_AUDIT_LOGGER_NAME = "ai_voice_agent.audit"


class AuditRepository:
    """
    In-memory audit event storage (plan.md Step 8.16 — simplest
    architecture compatible with this project; every other Phase 5/6
    store in this codebase is in-memory too). Deliberately NOT the same
    storage as MemoryManager's user-editable memory — audit records are
    never exposed through, or writable via, any user/LLM-facing memory
    API (plan.md: "Audit records should not become user-editable
    memory"). Append-only from this class's own perspective: there is no
    update()/delete() method.

    Bounded: at most `max_events` audit events and `max_events` security
    events are retained, oldest evicted first. This is the live server's
    store whenever PostgreSQL persistence is off, so an unbounded list
    grew for the life of the process (docs/MASTER_PROJECT_PLAN.md F-08).
    """

    DEFAULT_MAX_EVENTS = 10_000

    def __init__(self, max_events: int = DEFAULT_MAX_EVENTS):
        if max_events < 1:
            raise ValueError("max_events must be >= 1")
        self.max_events = max_events
        self._lock = threading.Lock()
        self._events: deque[AuditEvent] = deque(maxlen=max_events)
        self._security_events: deque[SecurityEvent] = deque(maxlen=max_events)

    def append(self, event: AuditEvent) -> None:
        with self._lock:
            self._events.append(event)

    def append_security_event(self, event: SecurityEvent) -> None:
        with self._lock:
            self._security_events.append(event)

    def list_events(
        self,
        event_type: Optional[EventType] = None,
        request_id: Optional[str] = None,
        actor: Optional[str] = None,
        session_id: Optional[str] = None,
        start_time=None,
        end_time=None,
    ) -> list[AuditEvent]:
        """
        Phase 12.8 (plan.md's explicit required filter set: "correlation
        ID, user ID, session ID, event type, time range") added `actor`
        (the audit model's field for a safe user identifier — see
        AuditEvent's own docstring), `session_id`, and a `start_time`/
        `end_time` window, all additive/optional — every pre-Phase-12.8
        caller (event_type/request_id only) is unaffected. No new query
        capability beyond these five was added; nothing here becomes an
        unrestricted "fetch everything" method (still a filtered list,
        never exposed as a public API endpoint).
        """
        with self._lock:
            events = list(self._events)
        if event_type is not None:
            events = [e for e in events if e.event_type == event_type]
        if request_id is not None:
            events = [e for e in events if e.request_id == request_id]
        if actor is not None:
            events = [e for e in events if e.actor == actor]
        if session_id is not None:
            events = [e for e in events if e.session_id == session_id]
        if start_time is not None:
            events = [e for e in events if e.timestamp >= start_time]
        if end_time is not None:
            events = [e for e in events if e.timestamp <= end_time]
        return events

    def list_security_events(self) -> list[SecurityEvent]:
        with self._lock:
            return list(self._security_events)


class AuditLogger:
    """
    The single point every component routes an audit emission through.
    Structured (typed AuditEvent, not a free-form string), privacy-aware
    (metadata is sanitized via PrivacyService before storage/logging —
    Phase 6, reused, not duplicated), and best-effort (see module
    docstring).
    """

    def __init__(self, privacy_service=None, repository: Optional[AuditRepository] = None):
        self._has_explicit_repository = repository is not None
        self._repository = repository or AuditRepository()
        self._set_privacy_service(privacy_service)

    def _set_privacy_service(self, privacy_service) -> None:
        self._privacy_service = privacy_service
        self._logger = (
            get_privacy_aware_logger(privacy_service, name=_AUDIT_LOGGER_NAME)
            if privacy_service is not None
            else logging.getLogger(_AUDIT_LOGGER_NAME)
        )

    def attach_defaults(self, repository: Optional[AuditRepository] = None, privacy_service=None) -> None:
        """
        Fills in whatever this logger was constructed without -- the
        persisted repository and/or the privacy service -- and leaves
        anything it was explicitly given untouched. Called by
        build_conversation_manager() for a caller-supplied logger:
        src/api/server.py builds its logger before persistence is resolved
        (its auth boundary needs one at import time), so without this the
        live server's audit trail was never persisted or PII-sanitized
        (docs/MASTER_PROJECT_PLAN.md F-08). The object identity is kept so
        every component already holding this logger keeps sharing it.
        """
        if repository is not None and not self._has_explicit_repository:
            self._repository = repository
            self._has_explicit_repository = True
        if privacy_service is not None and self._privacy_service is None:
            self._set_privacy_service(privacy_service)

    def record(
        self,
        event_type: EventType,
        outcome: str,
        *,
        request_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        session_id: Optional[str] = None,
        actor: Optional[str] = None,
        action: Optional[str] = None,
        resource: Optional[str] = None,
        policy: Optional[str] = None,
        reason: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> Optional[AuditEvent]:
        """
        Records one audit event. Never raises — a failure here is logged
        best-effort and swallowed (see module docstring), returning None
        instead of an event so a caller can tell recording didn't
        succeed without that becoming an exception it has to handle.
        `metadata` is sanitized through PrivacyService (context
        "LOGGING") before storage, exactly like the demonstrative
        conversation-turn log call in conversation_manager.py — the same
        boundary, not a second one.
        """
        try:
            safe_metadata = metadata or {}
            if self._privacy_service is not None and safe_metadata:
                safe_metadata = self._privacy_service.sanitize(safe_metadata, context="LOGGING")

            # Phase 14: inject OpenTelemetry trace correlation into every
            # audit event.  Fire-and-forget — extraction failure is swallowed.
            _trace_id, _span_id = None, None
            try:
                from tracing import get_current_trace_context

                _trace_id, _span_id = get_current_trace_context()
            except Exception:
                pass

            event = AuditEvent(
                event_id=new_event_id(),
                timestamp=now_utc(),
                event_type=event_type,
                request_id=request_id,
                conversation_id=conversation_id,
                session_id=session_id,
                actor=actor,
                action=action,
                resource=resource,
                outcome=outcome,
                policy=policy,
                reason=reason,
                metadata=safe_metadata,
                trace_id=_trace_id,
                span_id=_span_id,
            )
            self._repository.append(event)
            log_event(self._logger, f"audit:{event_type.value}", event.to_dict())
            return event
        except Exception:
            return None


class SecurityEventDetector:
    """
    Lightweight, deterministic security-event detection (plan.md Step
    8.17) — explicitly not a SIEM. Reuses signals already produced by
    real components (AUTH_FAILURE events, AUTHZ_DENY events, a
    SessionManager/MemoryManager denial) rather than re-implementing any
    detection logic of its own.
    """

    DEFAULT_MAX_TRACKED_IDENTIFIERS = 10_000

    def __init__(
        self,
        audit_logger: AuditLogger,
        repeated_failure_threshold: int = 3,
        max_tracked_identifiers: int = DEFAULT_MAX_TRACKED_IDENTIFIERS,
    ):
        self._audit_logger = audit_logger
        self._threshold = repeated_failure_threshold
        # Keyed by a safe, non-secret identifier (e.g. request source), never
        # the token. Bounded LRU (H3): one entry per distinct client address
        # used to grow for the life of the process; the least recently seen
        # identifier is evicted first. Locked: requests run in a threadpool.
        self._max_tracked = max(1, max_tracked_identifiers)
        self._auth_failure_counts: OrderedDict[str, int] = OrderedDict()
        self._counts_lock = threading.Lock()

    def record_auth_failure(self, identifier: str, request_id: Optional[str] = None) -> None:
        """`identifier` MUST be a safe, non-secret reference (e.g. a client IP or a hashed value) — never the submitted token/credential."""
        with self._counts_lock:
            count = self._auth_failure_counts.pop(identifier, 0) + 1
            self._auth_failure_counts[identifier] = count
            while len(self._auth_failure_counts) > self._max_tracked:
                self._auth_failure_counts.popitem(last=False)
        if count >= self._threshold:
            self._emit(
                type_="REPEATED_AUTH_FAILURE",
                severity=Severity.MEDIUM,
                request_id=request_id,
                actor=identifier,
                resource="authentication",
                outcome="denied",
                reason=f"{count} consecutive authentication failures.",
            )

    def reset_auth_failures(self, identifier: str) -> None:
        with self._counts_lock:
            self._auth_failure_counts.pop(identifier, None)

    def tracked_identifier_count(self) -> int:
        with self._counts_lock:
            return len(self._auth_failure_counts)

    def record_cross_user_access_attempt(
        self, resource_type: str, actor: str, request_id: Optional[str] = None
    ) -> None:
        self._emit(
            type_="CROSS_USER_ACCESS_ATTEMPT",
            severity=Severity.HIGH,
            request_id=request_id,
            actor=actor,
            resource=resource_type,
            outcome="denied",
            reason=f"Identity attempted to access another user's {resource_type}.",
        )

    def record_unknown_tool_request(
        self, action_name: str, actor: Optional[str], request_id: Optional[str] = None
    ) -> None:
        self._emit(
            type_="UNKNOWN_TOOL_REQUEST",
            severity=Severity.MEDIUM,
            request_id=request_id,
            actor=actor,
            resource=action_name,
            outcome="denied",
            reason=f"Request referenced an unregistered tool action: '{action_name}'.",
        )

    def record_policy_bypass_attempt(
        self, description: str, actor: Optional[str], request_id: Optional[str] = None
    ) -> None:
        self._emit(
            type_="POLICY_BYPASS_ATTEMPT",
            severity=Severity.CRITICAL,
            request_id=request_id,
            actor=actor,
            resource=None,
            outcome="denied",
            reason=description,
        )

    def record_malformed_action_proposal(self, actor: Optional[str], request_id: Optional[str] = None) -> None:
        self._emit(
            type_="MALFORMED_ACTION_PROPOSAL",
            severity=Severity.LOW,
            request_id=request_id,
            actor=actor,
            resource=None,
            outcome="rejected",
            reason="Structurally invalid action proposal.",
        )

    def record_repeated_authorization_denial(
        self, actor: str, resource: str, count: int, request_id: Optional[str] = None
    ) -> None:
        self._emit(
            type_="REPEATED_AUTHORIZATION_DENIAL",
            severity=Severity.MEDIUM,
            request_id=request_id,
            actor=actor,
            resource=resource,
            outcome="denied",
            reason=f"{count} consecutive authorization denials.",
        )

    def _emit(self, type_: str, severity: Severity, request_id, actor, resource, outcome, reason) -> None:
        try:
            event = SecurityEvent(
                event_id=new_event_id(),
                timestamp=now_utc(),
                type=type_,
                severity=severity,
                request_id=request_id,
                actor=actor,
                resource=resource,
                outcome=outcome,
                reason=reason,
            )
            self._audit_logger._repository.append_security_event(event)
        except Exception:
            pass

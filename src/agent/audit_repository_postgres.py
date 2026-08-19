"""
PostgreSQL-backed AuditRepository (Phase 12; plan.md Step 12.8).

Implements exactly the interface audit.py's in-memory AuditRepository
already exposes (`append`/`append_security_event`/`list_events`/
`list_security_events`) — a drop-in replacement
(PHASE_12_1_PERSISTENCE_AUDIT.md §7). AuditLogger is NOT modified at all
in this step; it keeps calling `self._repository.append(event)` after it
has already sanitized `metadata` through PrivacyService, exactly as
before. This class only persists an already-sanitized, already-decided
AuditEvent/SecurityEvent — the same "observe after the fact" discipline
audit.py's own module docstring describes now extends to the database:

    Business Decision -> AuditLogger -> Privacy Sanitization
        -> AuditRepository -> PostgreSQL

Critical constraint honored: this module does NOT import or reference
PrivacyService anywhere. AuditLogger -> PrivacyService -> AuditLogger
(the Phase 8 recursion hazard `AuditLogger.record()`'s own docstring
warns about — PrivacyService.sanitize() would call decide(), and if that
path itself emitted an audit event, it would recurse forever) remains
structurally impossible: sanitization already happened in AuditLogger
before this repository ever sees the event, and this repository has no
path back into PrivacyService.

Append-only, matching the in-memory AuditRepository exactly: no
update()/delete() method exists here either. Every table (`audit_events`,
`security_events`) was already primary-keyed and indexed for the exact
query shapes below in Step 12.3 — no schema change was needed.
"""

from typing import Optional

from sqlalchemy import select

from db import Database
from db_models import AuditEventRow, SecurityEventRow
from observability_models import AuditEvent, EventType, SecurityEvent, Severity


def _aware(dt):
    from session_repository_postgres import _aware as _shared_aware  # single shared SQLite-naive-datetime fix

    return _shared_aware(dt)


def _row_to_event(row: AuditEventRow) -> AuditEvent:
    return AuditEvent(
        event_id=row.event_id, timestamp=_aware(row.timestamp), event_type=EventType(row.event_type),
        request_id=row.request_id, conversation_id=row.conversation_id, session_id=row.session_id,
        actor=row.actor, action=row.action, resource=row.resource, outcome=row.outcome,
        policy=row.policy, reason=row.reason, metadata=dict(row.metadata_ or {}),
    )


def _event_to_values(event: AuditEvent) -> dict:
    return {
        "event_id": event.event_id, "timestamp": event.timestamp, "event_type": event.event_type.value,
        "request_id": event.request_id, "conversation_id": event.conversation_id, "session_id": event.session_id,
        "actor": event.actor, "action": event.action, "resource": event.resource, "outcome": event.outcome,
        "policy": event.policy, "reason": event.reason, "metadata": dict(event.metadata),
    }


def _row_to_security_event(row: SecurityEventRow) -> SecurityEvent:
    return SecurityEvent(
        event_id=row.event_id, timestamp=_aware(row.timestamp), type=row.type, severity=Severity(row.severity),
        request_id=row.request_id, actor=row.actor, resource=row.resource, outcome=row.outcome, reason=row.reason,
    )


class PostgresAuditRepository:
    def __init__(self, database: Database):
        self._database = database

    def append(self, event: AuditEvent) -> None:
        values = _event_to_values(event)
        with self._database.session_scope() as db_session:
            db_session.execute(AuditEventRow.__table__.insert().values(**values))

    def append_security_event(self, event: SecurityEvent) -> None:
        values = {
            "event_id": event.event_id, "timestamp": event.timestamp, "type": event.type,
            "severity": event.severity.value, "request_id": event.request_id, "actor": event.actor,
            "resource": event.resource, "outcome": event.outcome, "reason": event.reason,
        }
        with self._database.session_scope() as db_session:
            db_session.execute(SecurityEventRow.__table__.insert().values(**values))

    def list_events(
        self, event_type: Optional[EventType] = None, request_id: Optional[str] = None,
        actor: Optional[str] = None, session_id: Optional[str] = None,
        start_time=None, end_time=None,
    ) -> list:
        table = AuditEventRow.__table__
        stmt = select(AuditEventRow)
        if event_type is not None:
            stmt = stmt.where(table.c.event_type == event_type.value)
        if request_id is not None:
            stmt = stmt.where(table.c.request_id == request_id)
        if actor is not None:
            stmt = stmt.where(table.c.actor == actor)
        if session_id is not None:
            stmt = stmt.where(table.c.session_id == session_id)
        if start_time is not None:
            stmt = stmt.where(table.c.timestamp >= start_time)
        if end_time is not None:
            stmt = stmt.where(table.c.timestamp <= end_time)
        with self._database.session_scope() as db_session:
            rows = db_session.execute(stmt).scalars().all()
            return [_row_to_event(row) for row in rows]

    def list_security_events(self) -> list:
        with self._database.session_scope() as db_session:
            rows = db_session.execute(select(SecurityEventRow)).scalars().all()
            return [_row_to_security_event(row) for row in rows]

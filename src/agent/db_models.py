"""
SQLAlchemy ORM table definitions (Phase 12; plan.md Step 12.3).

Schema-only, per this step's explicit scope: these classes are storage
shapes, not repositories. Nothing here is imported by SessionManager,
MemoryManager, AuditLogger, or ToolOrchestrator yet — that integration is
Steps 12.5-12.10. No table here makes, or is consulted for, a
business/policy decision (plan.md Phase 12.1 Architecture Constraints:
"Repositories must only provide persistence").

Every table traces directly to a real, existing in-memory structure
audited in PHASE_12_1_PERSISTENCE_AUDIT.md §5/§8 — no speculative field
was added. Column shapes mirror the corresponding dataclass's `to_dict()`
method exactly (session_models.SessionState, memory_models.MemoryRecord,
observability_models.AuditEvent/SecurityEvent) plus a Phase 12-only
idempotency_records table (§7 of the audit — no in-memory predecessor
class existed for this one; it was a bare `set()` on ToolOrchestrator).

Portability note: JSON columns use SQLAlchemy's generic `JSON` type
(not PostgreSQL's `JSONB`) so the exact same model definitions produce a
working schema on both PostgreSQL (production) and SQLite (this repo's
offline test convention — see PHASE_12_1 audit §13). No repository is
expected to ever query *inside* a JSON column (audit §16) — every access
is a whole-row read/write — so JSONB's indexing/containment-query
advantages are not needed here; switching to JSONB later, if a genuine
query need arises, would be a non-breaking column-type migration.
"""

from sqlalchemy import CheckConstraint, Column, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import declarative_base
from sqlalchemy.types import JSON

Base = declarative_base()

_SESSION_STATUSES = ("ACTIVE", "WAITING_FOR_INPUT", "WAITING_FOR_CONFIRMATION", "COMPLETED", "EXPIRED", "FAILED")
_MEMORY_CATEGORIES = ("PREFERENCE", "WORKFLOW_CONTEXT", "COMMUNICATION_PREFERENCE")


def _sql_in_list(values: tuple[str, ...]) -> str:
    """Builds a SQL `('A','B','C')` literal list for a CHECK constraint — explicit, not a reliance on Python's tuple repr happening to look like SQL."""
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


class SessionRow(Base):
    """Mirrors session_models.SessionState exactly (audit §8's `sessions` table)."""

    __tablename__ = "sessions"

    session_id = Column(String(64), primary_key=True)
    user_id = Column(String(128), nullable=True)
    status = Column(String(32), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    current_intent = Column(String(128), nullable=True)
    workflow_state = Column(String(64), nullable=True)
    pending_action = Column(String(64), nullable=True)
    pending_parameters = Column(JSON, nullable=False, default=dict)
    confirmation_state = Column(JSON, nullable=False, default=dict)
    metadata_ = Column("metadata", JSON, nullable=False, default=dict)
    version = Column(Integer, nullable=False, default=1, server_default="1")

    __table_args__ = (
        CheckConstraint(f"status IN {_sql_in_list(_SESSION_STATUSES)}", name="ck_sessions_status_valid"),
        Index("ix_sessions_user_id", "user_id"),
        Index("ix_sessions_expires_at", "expires_at"),
    )


class MemoryRecordRow(Base):
    """Mirrors memory_models.MemoryRecord exactly (audit §8's `memory_records` table)."""

    __tablename__ = "memory_records"

    id = Column(String(64), primary_key=True)
    user_id = Column(String(128), nullable=False)
    category = Column(String(32), nullable=False)
    key = Column(String(128), nullable=False)
    value = Column(Text, nullable=False)
    source = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    metadata_ = Column("metadata", JSON, nullable=False, default=dict)
    version = Column(Integer, nullable=False, default=1, server_default="1")

    __table_args__ = (
        CheckConstraint(f"category IN {_sql_in_list(_MEMORY_CATEGORIES)}", name="ck_memory_records_category_valid"),
        Index("ix_memory_records_user_id", "user_id"),
    )


class AuditEventRow(Base):
    """Mirrors observability_models.AuditEvent exactly (audit §8's `audit_events` table). Append-only — no update path is ever expected to touch this table."""

    __tablename__ = "audit_events"

    event_id = Column(String(64), primary_key=True)
    timestamp = Column(DateTime(timezone=True), nullable=False)
    event_type = Column(String(64), nullable=False)
    request_id = Column(String(64), nullable=True)
    conversation_id = Column(String(64), nullable=True)
    session_id = Column(String(64), nullable=True)
    actor = Column(String(128), nullable=True)
    action = Column(String(64), nullable=True)
    resource = Column(String(128), nullable=True)
    outcome = Column(String(32), nullable=False)
    policy = Column(String(64), nullable=True)
    reason = Column(Text, nullable=True)
    metadata_ = Column("metadata", JSON, nullable=False, default=dict)

    __table_args__ = (
        Index("ix_audit_events_event_type", "event_type"),
        Index("ix_audit_events_request_id", "request_id"),
    )


class SecurityEventRow(Base):
    """Mirrors observability_models.SecurityEvent exactly (audit §8's `security_events` table)."""

    __tablename__ = "security_events"

    event_id = Column(String(64), primary_key=True)
    timestamp = Column(DateTime(timezone=True), nullable=False)
    type = Column(String(64), nullable=False)
    severity = Column(String(16), nullable=False)
    request_id = Column(String(64), nullable=True)
    actor = Column(String(128), nullable=True)
    resource = Column(String(128), nullable=True)
    outcome = Column(String(32), nullable=False)
    reason = Column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint("severity IN ('INFO','LOW','MEDIUM','HIGH','CRITICAL')", name="ck_security_events_severity_valid"),
        Index("ix_security_events_type", "type"),
    )


class IdempotencyRecordRow(Base):
    """
    New in Phase 12 — no in-memory predecessor class existed (audit §7):
    ToolOrchestrator previously tracked this as a bare `set()` of
    request_ids with no action/outcome/timestamp/user attached.

    Primary key is composite `(user_id, request_id)`, not `request_id`
    alone (revised in Step 12.9 from this table's original Step 12.3
    design — see migration 20260819_2 in alembic/versions/): Step 12.9's
    explicit required test "same key + different user -> isolated" means
    two different users reusing the same idempotency-key string must NOT
    collide, which a single-column `request_id` primary key would make
    impossible (a second user's `INSERT` would violate the first user's
    uniqueness constraint before either user-level check ever ran). The
    composite key still gives `INSERT ... ON CONFLICT (user_id,
    request_id) DO NOTHING` (audit §10's concurrency-safe pattern) a real
    unique constraint to conflict against — same mechanism, correctly
    scoped. `user_id` is NOT NULL because ToolOrchestrator._invoke()'s
    step 2 (authentication) always runs before step 4 (idempotency) and
    already rejects an unauthenticated caller — an idempotency check is
    structurally unreachable without a real, authenticated user_id.

    `expires_at` (added alongside the composite-key migration, same
    Step 12.9): the default in-process `_executed_request_ids` set this
    table can replace has no expiration at all — a request_id is blocked
    forever for the life of the process. That's fine for a set that's
    wiped by every restart anyway; it is not fine for a table that
    persists indefinitely, and plan.md's own Step 12.9 explicitly
    requires "expired key -> correct expiration behavior." An expired row
    is treated as reservable again (see idempotency_repository_postgres.py's
    try_reserve()), not deleted by a background sweep — the same "lazy
    expiration on read/write, not a scheduled job" pattern
    SessionManager already uses (session_manager.py's own docstring).
    """

    __tablename__ = "idempotency_records"

    user_id = Column(String(128), primary_key=True)
    request_id = Column(String(64), primary_key=True)
    action = Column(String(64), nullable=False)
    result_status = Column(String(32), nullable=False)
    executed_at = Column(DateTime(timezone=True), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)


ALL_TABLES = (SessionRow, MemoryRecordRow, AuditEventRow, SecurityEventRow, IdempotencyRecordRow)

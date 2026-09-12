"""
PostgreSQL-backed SessionRepository (Phase 12; plan.md Steps 12.5, 12.6).

Implements exactly the interface session_manager.py's in-memory
SessionRepository already exposes (`get`/`save`/`delete`) — a drop-in
replacement (PHASE_12_1_PERSISTENCE_AUDIT.md §7). SessionManager itself
is NOT modified to know this class exists; it keeps depending on the
same three-method shape and remains the sole authority for identity,
ownership, session semantics, and expiration rules (this step's own
instruction). This class is responsible only for persistence, querying,
and transactions.

Step 12.6 (Persistent Pending Confirmations) is implemented here too,
not as a separate table: PHASE_12_1_PERSISTENCE_AUDIT.md §17 already
decided pending-action state stays on `sessions` (it's already additive
fields on SessionState, not a distinct entity) rather than introducing a
second `pending_actions` table that would just duplicate session
columns. What Step 12.6 actually requires — "Do not rely only on Python
locks because multiple application processes may exist" — is met by
`try_consume_pending_confirmation()` below: a single atomic UPDATE
statement (guarded by workflow_state/expiry/ownership in its WHERE
clause) rather than SessionManager's in-process RLock, which only
protects against races within one Python process.
"""

from datetime import datetime, timezone
from typing import Optional

from db import ConcurrentModificationError, Database
from db_models import SessionRow
from session_models import SessionState, SessionStatus
from sqlalchemy import insert, or_, select, update
from sqlalchemy.exc import IntegrityError


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """
    PostgreSQL's `TIMESTAMPTZ` round-trips a timezone-aware `datetime`
    exactly. SQLite has no native timezone-aware timestamp type — every
    `DateTime(timezone=True)` value SQLAlchemy reads back from it is
    naive, even though it was written as UTC-aware (this repository
    always writes UTC-aware values; see `SessionState`'s own
    `field(default_factory=lambda: datetime.now(timezone.utc))`). Without
    this normalization, comparing a value read back from SQLite against
    `datetime.now(timezone.utc)` elsewhere (e.g. `SessionState.is_expired()`)
    raises `TypeError: can't compare offset-naive and offset-aware
    datetimes`. Re-attaching UTC here (never any other zone — every write
    path in this codebase already normalizes to UTC before persisting)
    keeps this repository's dialect-portability promise
    (PHASE_12_1_PERSISTENCE_AUDIT.md §13) honest: the *values* behave
    identically across dialects, not just the schema.
    """
    if dt is None or dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=timezone.utc)


def _row_to_state(row: SessionRow) -> SessionState:
    return SessionState(
        session_id=row.session_id,
        user_id=row.user_id,
        status=SessionStatus(row.status),
        created_at=_aware(row.created_at),
        updated_at=_aware(row.updated_at),
        expires_at=_aware(row.expires_at),
        current_intent=row.current_intent,
        workflow_state=row.workflow_state,
        pending_action=row.pending_action,
        pending_parameters=dict(row.pending_parameters or {}),
        confirmation_state=dict(row.confirmation_state or {}),
        metadata=dict(row.metadata_ or {}),
        version=getattr(row, "version", 1) or 1,
    )


def _state_to_values(session: SessionState) -> dict:
    return {
        "session_id": session.session_id,
        "user_id": session.user_id,
        "status": session.status.value,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "expires_at": session.expires_at,
        "current_intent": session.current_intent,
        "workflow_state": session.workflow_state,
        "pending_action": session.pending_action,
        "pending_parameters": dict(session.pending_parameters),
        "confirmation_state": dict(session.confirmation_state),
        "metadata": dict(session.metadata),
        "version": getattr(session, "version", 1) or 1,
    }


class PostgresSessionRepository:
    def __init__(self, database: Database):
        self._database = database

    def get(self, session_id: str) -> Optional[SessionState]:
        with self._database.session_scope() as db_session:
            row = db_session.get(SessionRow, session_id)
            if row is None:
                return None
            return _row_to_state(row)

    def save(self, session: SessionState) -> None:
        table = SessionRow.__table__
        values = _state_to_values(session)
        expected_version = getattr(session, "version", 1) or 1
        new_version = expected_version + 1

        with self._database.session_scope() as db_session:
            update_values = {k: v for k, v in values.items() if k != "session_id"}
            update_values["version"] = new_version
            stmt = (
                update(table)
                .where(table.c.session_id == session.session_id)
                .where(table.c.version == expected_version)
                .values(**update_values)
            )
            res = db_session.execute(stmt)
            if res.rowcount == 0:
                existing_ver = db_session.execute(
                    select(table.c.version).where(table.c.session_id == session.session_id)
                ).scalar_one_or_none()
                if existing_ver is not None:
                    raise ConcurrentModificationError(
                        f"Concurrent modification detected for session '{session.session_id}': "
                        f"expected version {expected_version}, current version {existing_ver}"
                    )
                insert_values = dict(values)
                insert_values["version"] = expected_version
                try:
                    db_session.execute(insert(table).values(**insert_values))
                except IntegrityError:
                    raise ConcurrentModificationError(f"Concurrent insert detected for session '{session.session_id}'")
            else:
                session.version = new_version

    def delete(self, session_id: str) -> None:
        with self._database.session_scope() as db_session:
            row = db_session.get(SessionRow, session_id)
            if row is not None:
                db_session.delete(row)

    def try_consume_pending_confirmation(
        self,
        session_id: str,
        user_id: Optional[str] = None,
    ) -> Optional[tuple]:
        """
        Atomic compare-and-swap: transitions `workflow_state` away from
        AWAITING_CONFIRMATION and returns the pending action/parameters
        that were consumed, or None if there was nothing usable to
        consume (missing session, wrong owner, expired, or no pending
        confirmation) — same contract as
        SessionManager.try_consume_pending_confirmation(), enforced at
        the database layer instead of a Python lock.

        Two-statement, one-transaction implementation, not a bug: the
        first UPDATE's WHERE clause (workflow_state='AWAITING_CONFIRMATION')
        is the actual compare-and-swap gate and clears workflow_state
        immediately — a concurrent second caller (same or a different
        process) attempting the same UPDATE will either see 0 rows
        affected (already consumed) or block on PostgreSQL's row lock
        until this transaction commits, after which it also sees 0 rows.
        RETURNING in that first statement deliberately does NOT clear
        pending_action/pending_parameters in the same SET, because
        RETURNING reflects post-UPDATE values — clearing and returning
        the same column in one statement would return NULL, not the
        value the caller needs. The second UPDATE (clearing
        pending_action/pending_parameters for tidiness) runs only when
        the first one actually matched a row, inside the same
        transaction, so it carries no additional race exposure.
        """
        table = SessionRow.__table__
        now = datetime.now(timezone.utc)

        guard = (
            (table.c.session_id == session_id)
            & (table.c.workflow_state == "AWAITING_CONFIRMATION")
            & table.c.pending_action.is_not(None)
            & (table.c.expires_at > now)
        )
        if user_id is not None:
            guard = guard & or_(table.c.user_id.is_(None), table.c.user_id == user_id)

        consume_stmt = (
            update(table)
            .where(guard)
            .values(workflow_state=None, updated_at=now, version=table.c.version + 1)
            .returning(table.c.pending_action, table.c.pending_parameters)
        )

        with self._database.session_scope() as db_session:
            row = db_session.execute(consume_stmt).first()
            if row is None:
                return None
            action_name, params = row
            db_session.execute(
                update(table).where(table.c.session_id == session_id).values(pending_action=None, pending_parameters={})
            )
            return action_name, dict(params or {})

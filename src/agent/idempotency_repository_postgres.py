"""
PostgreSQL-backed IdempotencyRepository (Phase 12; plan.md Step 12.9).

Same (user_id, request_id)-scoped, reserve-before-execute interface as
idempotency_repository.InMemoryIdempotencyRepository — see that module's
docstring for the full rationale.

Concurrency (mandatory, per this step and audit §10): `try_reserve()`
uses a single atomic `INSERT ... ON CONFLICT (user_id, request_id) DO
UPDATE ... WHERE <existing row expired> ... RETURNING` statement. A row
lock taken by one transaction's insert/update branch blocks a concurrent
second transaction's identical statement until the first commits; the
second then re-evaluates the WHERE condition against the now-committed
row and gets nothing back. This is real database-level mutual exclusion
across processes, not a Python lock (which, per this step's explicit
instruction, cannot cover multiple application processes sharing the
same database) — the `RETURNING` clause's presence/absence of a row is
what tells each caller whether it won.

Expiration (Step 12.9: "expired key -> correct expiration behavior"):
the conflict branch is `DO UPDATE ... WHERE expires_at < now()`, not
`DO NOTHING` — an expired existing row is atomically reclaimed by
whichever caller's statement runs first, exactly like a genuinely
missing key would be. If the WHERE condition is false (the existing row
is NOT expired), PostgreSQL's and SQLite's UPSERT semantics both leave
the row untouched and omit it from RETURNING — so this caller correctly
sees "did not win," identical to the DO-NOTHING-on-a-fresh-conflict case.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from db import Database, DatabaseUnavailableError
from db_models import IdempotencyRecordRow
from idempotency_repository import DEFAULT_TTL
from sqlalchemy import delete, select, update


class PostgresIdempotencyRepository:
    def __init__(self, database: Database):
        self._database = database

    def try_reserve(self, request_id: str, *, user_id: str, action: str, ttl: timedelta = DEFAULT_TTL) -> bool:
        table = IdempotencyRecordRow.__table__
        now = datetime.now(timezone.utc)
        expires_at = now + ttl
        with self._database.session_scope() as db_session:
            dialect = db_session.connection().dialect.name
            if dialect == "postgresql":
                from sqlalchemy.dialects.postgresql import insert as _insert
            elif dialect == "sqlite":
                from sqlalchemy.dialects.sqlite import insert as _insert
            else:
                raise DatabaseUnavailableError(f"Unsupported database dialect for idempotency insert: {dialect}")

            stmt = (
                _insert(table)
                .values(
                    user_id=user_id,
                    request_id=request_id,
                    action=action,
                    result_status="in_progress",
                    executed_at=now,
                    expires_at=expires_at,
                )
                .on_conflict_do_update(
                    index_elements=["user_id", "request_id"],
                    set_={
                        "action": action,
                        "result_status": "in_progress",
                        "executed_at": now,
                        "expires_at": expires_at,
                    },
                    where=(table.c.expires_at < now),
                )
                .returning(table.c.user_id)
            )
            result = db_session.execute(stmt).first()
            return result is not None

    def update_result(self, request_id: str, *, user_id: str, result_status: str) -> None:
        table = IdempotencyRecordRow.__table__
        with self._database.session_scope() as db_session:
            db_session.execute(
                update(table)
                .where(table.c.user_id == user_id)
                .where(table.c.request_id == request_id)
                .values(result_status=result_status)
            )

    def release(self, request_id: str, *, user_id: str) -> None:
        """Removes the reservation on failure so a legitimate later retry with the same (user_id, request_id) can proceed — see idempotency_repository.py's release() docstring."""
        table = IdempotencyRecordRow.__table__
        with self._database.session_scope() as db_session:
            db_session.execute(delete(table).where(table.c.user_id == user_id).where(table.c.request_id == request_id))

    def has_executed(self, request_id: str, *, user_id: str, action: str) -> bool:
        """An expired row reports False, matching try_reserve()'s own expiration handling."""
        table = IdempotencyRecordRow.__table__
        now = datetime.now(timezone.utc)
        with self._database.session_scope() as db_session:
            row = db_session.execute(
                select(table.c.request_id)
                .where(table.c.user_id == user_id)
                .where(table.c.request_id == request_id)
                .where(table.c.expires_at > now)
            ).first()
            return row is not None

    def get_recorded_action(self, request_id: str, *, user_id: str) -> Optional[str]:
        table = IdempotencyRecordRow.__table__
        with self._database.session_scope() as db_session:
            row = db_session.execute(
                select(table.c.action).where(table.c.user_id == user_id).where(table.c.request_id == request_id)
            ).first()
            return row[0] if row else None

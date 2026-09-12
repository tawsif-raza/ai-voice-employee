"""
PostgreSQL-backed MemoryRepository (Phase 12; plan.md Step 12.7).

Implements exactly the interface memory_manager.py's in-memory
MemoryRepository already exposes (`get`/`save`/`delete`/`list_for_user`)
— a drop-in replacement (PHASE_12_1_PERSISTENCE_AUDIT.md §7).
MemoryManager is NOT modified to know this class exists (aside from
Step 12.7's own ownership-guard fix to persist_memory(), which is
backend-independent and lives in memory_manager.py, not here); it keeps
depending on the same four-method shape and remains solely responsible
for ownership, authorization, privacy, and retention semantics. This
class is responsible only for persistence and querying.

No raw/unscoped query method exists here, matching the audit's explicit
security requirement (§11): only `get(id)` and `list_for_user(user_id)`
— the exact same two access patterns the in-memory repository already
supports, never a `list_all()`/`query()`-shaped method that could let a
future caller bypass MemoryManager's ownership checks by reaching for
"the obviously more convenient" unscoped method.
"""

from typing import Optional

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

from db import ConcurrentModificationError, Database, upsert_row
from db_models import MemoryRecordRow
from memory_models import MemoryCategory, MemoryRecord


def _row_to_record(row: MemoryRecordRow) -> MemoryRecord:
    from session_repository_postgres import _aware  # reuse the same SQLite-naive-datetime fix; see that module's docstring

    return MemoryRecord(
        id=row.id, user_id=row.user_id, category=MemoryCategory(row.category), key=row.key, value=row.value,
        source=row.source, created_at=_aware(row.created_at), updated_at=_aware(row.updated_at),
        expires_at=_aware(row.expires_at), metadata=dict(row.metadata_ or {}),
        version=getattr(row, "version", 1) or 1,
    )


def _record_to_values(record: MemoryRecord) -> dict:
    return {
        "id": record.id, "user_id": record.user_id, "category": record.category.value, "key": record.key,
        "value": record.value, "source": record.source, "created_at": record.created_at,
        "updated_at": record.updated_at, "expires_at": record.expires_at, "metadata": dict(record.metadata),
        "version": getattr(record, "version", 1) or 1,
    }


class PostgresMemoryRepository:
    def __init__(self, database: Database):
        self._database = database

    def get(self, memory_id: str) -> Optional[MemoryRecord]:
        with self._database.session_scope() as db_session:
            row = db_session.get(MemoryRecordRow, memory_id)
            if row is None:
                return None
            return _row_to_record(row)

    def save(self, record: MemoryRecord) -> None:
        table = MemoryRecordRow.__table__
        values = _record_to_values(record)
        expected_version = getattr(record, "version", 1) or 1
        new_version = expected_version + 1

        with self._database.session_scope() as db_session:
            update_values = {k: v for k, v in values.items() if k != "id"}
            update_values["version"] = new_version
            stmt = (
                update(table)
                .where(table.c.id == record.id)
                .where(table.c.version == expected_version)
                .values(**update_values)
            )
            res = db_session.execute(stmt)
            if res.rowcount == 0:
                existing_ver = db_session.execute(
                    select(table.c.version).where(table.c.id == record.id)
                ).scalar_one_or_none()
                if existing_ver is not None:
                    raise ConcurrentModificationError(
                        f"Concurrent modification detected for memory '{record.id}': "
                        f"expected version {expected_version}, current version {existing_ver}"
                    )
                insert_values = dict(values)
                insert_values["version"] = expected_version
                try:
                    db_session.execute(insert(table).values(**insert_values))
                except IntegrityError:
                    raise ConcurrentModificationError(
                        f"Concurrent insert detected for memory '{record.id}'"
                    )

    def delete(self, memory_id: str) -> None:
        with self._database.session_scope() as db_session:
            row = db_session.get(MemoryRecordRow, memory_id)
            if row is not None:
                db_session.delete(row)

    def list_for_user(self, user_id: str) -> list:
        table = MemoryRecordRow.__table__
        with self._database.session_scope() as db_session:
            rows = db_session.execute(select(MemoryRecordRow).where(table.c.user_id == user_id)).scalars().all()
            return [_row_to_record(row) for row in rows]

"""
Optimistic Concurrency Tests (Phase 13; plan.md Step 13.2).

Verifies optimistic concurrency control (CAS) for session and memory writes:
1. Sequential writes from the same process succeed normally and increment the version column.
2. Simulated concurrent writes (stale version) are rejected deterministically with
   ConcurrentModificationError rather than silently overwritten or lost.
3. Both in-memory and PostgreSQL (SQLite stand-in) backends are verified.
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ALEMBIC_INI = _REPO_ROOT / "alembic.ini"
sys.path.insert(0, str(_REPO_ROOT / "src" / "agent"))

from alembic import command
from alembic.config import Config
from db import ConcurrentModificationError, Database, DatabaseConfig
from memory_manager import MemoryManager, MemoryRepository
from memory_models import MemoryCategory, MemoryRecord
from memory_repository_postgres import PostgresMemoryRepository
from session_manager import SessionManager, SessionRepository
from session_models import SessionState, SessionStatus
from session_repository_postgres import PostgresSessionRepository


class TestInMemorySessionOptimisticConcurrency(unittest.TestCase):
    def setUp(self):
        self.repo = SessionRepository()
        self.manager = SessionManager(repository=self.repo)

    def test_sequential_writes_succeed_and_increment_version(self):
        session = self.manager.create_session("sess-seq-1", user_id="user-1")
        self.assertEqual(session.version, 1)

        updated = self.manager.update_session("sess-seq-1", user_id="user-1", current_intent="BOOKING")
        self.assertEqual(updated.version, 2)

        transitioned = self.manager.transition_state("sess-seq-1", SessionStatus.WAITING_FOR_INPUT, user_id="user-1")
        self.assertEqual(transitioned.version, 3)

        stored = self.repo.get("sess-seq-1")
        self.assertIsNotNone(stored)
        self.assertEqual(stored.version, 3)
        self.assertEqual(stored.current_intent, "BOOKING")
        self.assertEqual(stored.status, SessionStatus.WAITING_FOR_INPUT)

    def test_stale_write_rejected_with_concurrent_modification_error(self):
        session = self.manager.create_session("sess-stale-1", user_id="user-1")
        self.assertEqual(session.version, 1)

        # First write updates version to 2
        self.manager.update_session("sess-stale-1", user_id="user-1", current_intent="INTENT_A")

        # Stale writer attempts to write using an old copy with version 1
        stale_copy = SessionState(
            session_id="sess-stale-1",
            user_id="user-1",
            version=1,
            current_intent="OVERWRITE_ATTEMPT",
        )
        with self.assertRaises(ConcurrentModificationError):
            self.repo.save(stale_copy)

        # Confirm data was not silently overwritten
        current = self.repo.get("sess-stale-1")
        self.assertEqual(current.version, 2)
        self.assertEqual(current.current_intent, "INTENT_A")


class TestInMemoryMemoryOptimisticConcurrency(unittest.TestCase):
    def setUp(self):
        self.repo = MemoryRepository()
        class DummyPolicy:
            def evaluate_privacy(self, key, operation="persist"):
                from dataclasses import dataclass
                @dataclass
                class D:
                    allowed: bool = True
                    reason: str = "ok"
                return D()
        self.manager = MemoryManager(policy_engine=DummyPolicy(), repository=self.repo)

    def test_sequential_writes_succeed_and_increment_version(self):
        record = self.manager.propose_memory(
            user_id="user-1", category=MemoryCategory.PREFERENCE, key="lang", value="en", source="user_explicit"
        )
        self.manager.persist_memory(record)
        stored1 = self.repo.get(record.id)
        self.assertEqual(stored1.version, 1)

        # Update record: save with expected version 1 -> increments to 2
        updated_rec = MemoryRecord(
            id=record.id, user_id="user-1", category=MemoryCategory.PREFERENCE, key="lang", value="es",
            source="user_explicit", created_at=record.created_at, updated_at=datetime.now(timezone.utc),
            version=1,
        )
        self.repo.save(updated_rec)
        stored2 = self.repo.get(record.id)
        self.assertEqual(stored2.version, 2)
        self.assertEqual(stored2.value, "es")

    def test_stale_write_rejected_with_concurrent_modification_error(self):
        record = self.manager.propose_memory(
            user_id="user-1", category=MemoryCategory.PREFERENCE, key="lang", value="en", source="user_explicit"
        )
        self.manager.persist_memory(record)

        # Valid update bumps version to 2
        updated_rec = MemoryRecord(
            id=record.id, user_id="user-1", category=MemoryCategory.PREFERENCE, key="lang", value="es",
            source="user_explicit", created_at=record.created_at, updated_at=datetime.now(timezone.utc),
            version=1,
        )
        self.repo.save(updated_rec)

        # Stale writer with version 1 tries to overwrite
        stale_rec = MemoryRecord(
            id=record.id, user_id="user-1", category=MemoryCategory.PREFERENCE, key="lang", value="fr",
            source="user_explicit", created_at=record.created_at, updated_at=datetime.now(timezone.utc),
            version=1,
        )
        with self.assertRaises(ConcurrentModificationError):
            self.repo.save(stale_rec)

        # Ensure no silent loss of value "es"
        current = self.repo.get(record.id)
        self.assertEqual(current.version, 2)
        self.assertEqual(current.value, "es")


class _PostgresBase(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db", prefix="phase13_opt_lock_")
        os.close(fd)
        os.remove(path)
        self.db_path = Path(path)
        self._prior_database_url = os.environ.get("DATABASE_URL")
        self._prior_persistence_mode = os.environ.get("PERSISTENCE_MODE")
        os.environ["DATABASE_URL"] = f"sqlite:///{self.db_path.as_posix()}"
        os.environ.pop("PERSISTENCE_MODE", None)

        config = Config(str(_ALEMBIC_INI))
        command.upgrade(config, "head")

        self.db_config = DatabaseConfig(
            url=f"sqlite:///{self.db_path.as_posix()}",
            safe_url="sqlite:///***",
            mode="production",
            pool_size=5,
            max_overflow=10,
            pool_timeout_seconds=30.0,
            pool_recycle_seconds=1800,
            echo=False,
        )
        self.database = Database(self.db_config)

    def tearDown(self):
        self.database.dispose()
        if self._prior_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = self._prior_database_url
        if self._prior_persistence_mode is None:
            os.environ.pop("PERSISTENCE_MODE", None)
        else:
            os.environ["PERSISTENCE_MODE"] = self._prior_persistence_mode
        if self.db_path.exists():
            try:
                self.db_path.unlink()
            except Exception:
                pass


class TestPostgresSessionOptimisticConcurrency(_PostgresBase):
    def setUp(self):
        super().setUp()
        self.repo = PostgresSessionRepository(self.database)
        self.manager = SessionManager(repository=self.repo)

    def test_postgres_sequential_writes_succeed_and_increment_version(self):
        session = self.manager.create_session("sess-pg-1", user_id="user-1")
        self.assertEqual(session.version, 1)

        updated = self.manager.update_session("sess-pg-1", user_id="user-1", current_intent="BOOKING")
        self.assertEqual(updated.version, 2)

        transitioned = self.manager.transition_state("sess-pg-1", SessionStatus.WAITING_FOR_INPUT, user_id="user-1")
        self.assertEqual(transitioned.version, 3)

        stored = self.repo.get("sess-pg-1")
        self.assertIsNotNone(stored)
        self.assertEqual(stored.version, 3)
        self.assertEqual(stored.current_intent, "BOOKING")

    def test_postgres_stale_write_rejected_with_concurrent_modification_error(self):
        session = self.manager.create_session("sess-pg-stale", user_id="user-1")
        self.assertEqual(session.version, 1)

        # Update bumps version to 2
        self.manager.update_session("sess-pg-stale", user_id="user-1", current_intent="FIRST_UPDATE")

        # Stale writer attempts to write with version 1
        stale_session = SessionState(
            session_id="sess-pg-stale",
            user_id="user-1",
            version=1,
            current_intent="STALE_OVERWRITE",
        )
        with self.assertRaises(ConcurrentModificationError):
            self.repo.save(stale_session)

        # Value in database remains intact
        current = self.repo.get("sess-pg-stale")
        self.assertEqual(current.version, 2)
        self.assertEqual(current.current_intent, "FIRST_UPDATE")

    def test_postgres_confirmation_consumption_bumps_version(self):
        session = self.manager.create_session("sess-conf-1", user_id="user-1")
        self.manager.update_session(
            "sess-conf-1", user_id="user-1",
            workflow_state="AWAITING_CONFIRMATION",
            pending_action="BOOK_APPOINTMENT",
            pending_parameters={"slot": "10:00"},
        )
        before_consume = self.repo.get("sess-conf-1")
        ver_before = before_consume.version

        consumed = self.repo.try_consume_pending_confirmation("sess-conf-1", user_id="user-1")
        self.assertIsNotNone(consumed)

        after_consume = self.repo.get("sess-conf-1")
        self.assertEqual(after_consume.version, ver_before + 1)

        # An attempt to save using before_consume (stale version) must fail
        before_consume.current_intent = "STALE"
        with self.assertRaises(ConcurrentModificationError):
            self.repo.save(before_consume)


class TestPostgresMemoryOptimisticConcurrency(_PostgresBase):
    def setUp(self):
        super().setUp()
        self.repo = PostgresMemoryRepository(self.database)

    def test_postgres_sequential_writes_succeed_and_increment_version(self):
        record = MemoryRecord(
            id="mem-pg-1", user_id="user-1", category=MemoryCategory.PREFERENCE, key="theme", value="dark",
            source="user_explicit", created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
            version=1,
        )
        self.repo.save(record)
        stored1 = self.repo.get("mem-pg-1")
        self.assertEqual(stored1.version, 1)

        # Sequential update with expected version 1
        updated = MemoryRecord(
            id="mem-pg-1", user_id="user-1", category=MemoryCategory.PREFERENCE, key="theme", value="light",
            source="user_explicit", created_at=record.created_at, updated_at=datetime.now(timezone.utc),
            version=1,
        )
        self.repo.save(updated)
        stored2 = self.repo.get("mem-pg-1")
        self.assertEqual(stored2.version, 2)
        self.assertEqual(stored2.value, "light")

    def test_postgres_stale_write_rejected_with_concurrent_modification_error(self):
        record = MemoryRecord(
            id="mem-pg-stale", user_id="user-1", category=MemoryCategory.PREFERENCE, key="theme", value="dark",
            source="user_explicit", created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
            version=1,
        )
        self.repo.save(record)

        # Update row to version 2
        updated = MemoryRecord(
            id="mem-pg-stale", user_id="user-1", category=MemoryCategory.PREFERENCE, key="theme", value="light",
            source="user_explicit", created_at=record.created_at, updated_at=datetime.now(timezone.utc),
            version=1,
        )
        self.repo.save(updated)

        # Stale write with version 1
        stale = MemoryRecord(
            id="mem-pg-stale", user_id="user-1", category=MemoryCategory.PREFERENCE, key="theme", value="neon",
            source="user_explicit", created_at=record.created_at, updated_at=datetime.now(timezone.utc),
            version=1,
        )
        with self.assertRaises(ConcurrentModificationError):
            self.repo.save(stale)

        # Data remains light at version 2
        current = self.repo.get("mem-pg-stale")
        self.assertEqual(current.version, 2)
        self.assertEqual(current.value, "light")


if __name__ == "__main__":
    unittest.main()

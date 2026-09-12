"""
Tests for PostgresMemoryRepository (Phase 12; plan.md Step 12.7):
src/agent/memory_repository_postgres.py, plus MemoryManager's new
ownership guard on persist_memory() (memory_manager.py's
MemoryOwnershipError — a backend-independent fix discovered while
building this step, exercised here against the persisted repository as
this step's required "User B attempts to modify it -> DENY" test).

Run against SQLite as this repository's dialect-portable test double
(PHASE_12_1_PERSISTENCE_AUDIT.md §13) — same convention as
test_session_repository_postgres.py.

Run with:
    python -m unittest tests.test_memory_repository_postgres -v
"""

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "agent"))

from db import Database, DatabaseUnavailableError, load_database_config  # noqa: E402
from db_models import Base  # noqa: E402
from memory_manager import MemoryManager, MemoryOwnershipError, MemoryPolicyDeniedError  # noqa: E402
from memory_models import MemoryCategory, MemoryRecord  # noqa: E402
from memory_repository_postgres import PostgresMemoryRepository  # noqa: E402
from policy_engine import PolicyEngine  # noqa: E402
from privacy_service import PrivacyService  # noqa: E402


def _fresh_database() -> Database:
    database = Database(load_database_config(env={"DATABASE_URL": "sqlite:///:memory:"}))
    Base.metadata.create_all(database.engine)
    return database


class TestRepositoryRoundTrip(unittest.TestCase):
    def setUp(self):
        self.database = _fresh_database()
        self.repo = PostgresMemoryRepository(self.database)

    def tearDown(self):
        self.database.dispose()

    def test_get_missing_returns_none(self):
        self.assertIsNone(self.repo.get("nope"))

    def test_save_then_get_round_trips_fields(self):
        now = datetime.now(timezone.utc)
        record = MemoryRecord(
            id="m1",
            user_id="u1",
            category=MemoryCategory.PREFERENCE,
            key="preferred_clinic",
            value="Downtown Clinic",
            source="user_explicit",
            created_at=now,
            updated_at=now,
            metadata={"channel": "voice"},
        )
        self.repo.save(record)
        fetched = self.repo.get("m1")
        self.assertEqual(fetched.user_id, "u1")
        self.assertEqual(fetched.category, MemoryCategory.PREFERENCE)
        self.assertEqual(fetched.value, "Downtown Clinic")
        self.assertEqual(fetched.metadata, {"channel": "voice"})

    def test_save_is_upsert(self):
        now = datetime.now(timezone.utc)
        self.repo.save(
            MemoryRecord(
                id="m2",
                user_id="u1",
                category=MemoryCategory.PREFERENCE,
                key="k",
                value="v1",
                source="s",
                created_at=now,
                updated_at=now,
            )
        )
        self.repo.save(
            MemoryRecord(
                id="m2",
                user_id="u1",
                category=MemoryCategory.PREFERENCE,
                key="k",
                value="v2",
                source="s",
                created_at=now,
                updated_at=now,
            )
        )
        self.assertEqual(self.repo.get("m2").value, "v2")

    def test_delete_removes_record(self):
        now = datetime.now(timezone.utc)
        self.repo.save(
            MemoryRecord(
                id="m3",
                user_id="u1",
                category=MemoryCategory.PREFERENCE,
                key="k",
                value="v",
                source="s",
                created_at=now,
                updated_at=now,
            )
        )
        self.repo.delete("m3")
        self.assertIsNone(self.repo.get("m3"))

    def test_delete_missing_does_not_raise(self):
        self.repo.delete("never-existed")

    def test_list_for_user_scoped_correctly(self):
        now = datetime.now(timezone.utc)
        self.repo.save(
            MemoryRecord(
                id="a1",
                user_id="user-a",
                category=MemoryCategory.PREFERENCE,
                key="k1",
                value="v",
                source="s",
                created_at=now,
                updated_at=now,
            )
        )
        self.repo.save(
            MemoryRecord(
                id="a2",
                user_id="user-a",
                category=MemoryCategory.PREFERENCE,
                key="k2",
                value="v",
                source="s",
                created_at=now,
                updated_at=now,
            )
        )
        self.repo.save(
            MemoryRecord(
                id="b1",
                user_id="user-b",
                category=MemoryCategory.PREFERENCE,
                key="k1",
                value="v",
                source="s",
                created_at=now,
                updated_at=now,
            )
        )
        user_a_records = self.repo.list_for_user("user-a")
        self.assertEqual({r.id for r in user_a_records}, {"a1", "a2"})

    def test_no_unscoped_list_all_method_exists(self):
        self.assertFalse(hasattr(self.repo, "list_all"))
        self.assertFalse(hasattr(self.repo, "query"))


class TestCrossUserSecurity(unittest.TestCase):
    """plan.md Step 12.7's mandatory security tests: User B read/modify/delete of User A's memory -> DENY."""

    def setUp(self):
        self.database = _fresh_database()
        policy_engine = PolicyEngine()
        self.manager = MemoryManager(policy_engine, repository=PostgresMemoryRepository(self.database))

    def tearDown(self):
        self.database.dispose()

    def test_user_b_cannot_read_user_a_memory(self):
        record = self.manager.propose_memory(
            user_id="user-a",
            category=MemoryCategory.PREFERENCE,
            key="preferred_clinic",
            value="Downtown Clinic",
            source="user_explicit",
        )
        self.manager.persist_memory(record)
        self.assertEqual(self.manager.list_allowed_memory("user-b"), [])

    def test_user_b_cannot_modify_user_a_memory(self):
        record = self.manager.propose_memory(
            user_id="user-a",
            category=MemoryCategory.PREFERENCE,
            key="preferred_clinic",
            value="Downtown Clinic",
            source="user_explicit",
        )
        self.manager.persist_memory(record)

        forged = MemoryRecord(
            id=record.id,
            user_id="user-b",
            category=MemoryCategory.PREFERENCE,
            key="preferred_clinic",
            value="Attacker Clinic",
            source="user_explicit",
            created_at=record.created_at,
            updated_at=record.updated_at,
        )
        with self.assertRaises(MemoryOwnershipError):
            self.manager.persist_memory(forged)
        # Original value must be untouched.
        self.assertEqual(self.manager.list_allowed_memory("user-a")[0].value, "Downtown Clinic")

    def test_user_b_cannot_delete_user_a_memory(self):
        record = self.manager.propose_memory(
            user_id="user-a",
            category=MemoryCategory.PREFERENCE,
            key="preferred_clinic",
            value="Downtown Clinic",
            source="user_explicit",
        )
        self.manager.persist_memory(record)
        self.assertFalse(self.manager.remove_memory(record.id, user_id="user-b"))
        self.assertEqual(len(self.manager.list_allowed_memory("user-a")), 1)

    def test_error_does_not_reveal_existence_via_return_type(self):
        # remove_memory() returns the same False for "doesn't exist" and
        # "exists but belongs to someone else" -- never a different
        # signal that would let a caller distinguish the two.
        self.assertFalse(self.manager.remove_memory("truly-does-not-exist", user_id="user-b"))

    def test_same_user_can_still_update_own_memory(self):
        # The ownership guard must not block legitimate same-user reuse
        # of an id (test_duplicate_memory_ids_do_not_raise's scenario,
        # re-verified here against the persisted repository).
        record = self.manager.propose_memory(
            user_id="user-a", category=MemoryCategory.PREFERENCE, key="k", value="v1", source="s"
        )
        self.manager.persist_memory(record)
        updated = MemoryRecord(
            id=record.id,
            user_id="user-a",
            category=MemoryCategory.PREFERENCE,
            key="k",
            value="v2",
            source="s",
            created_at=record.created_at,
            updated_at=record.updated_at,
        )
        self.manager.persist_memory(updated)  # must not raise
        self.assertEqual(self.manager.list_allowed_memory("user-a")[0].value, "v2")


class TestPrivacyNotBypassed(unittest.TestCase):
    """Persistence must not bypass PrivacyService/PolicyEngine — same restricted-field and PII rules apply against the persisted repository."""

    def setUp(self):
        self.database = _fresh_database()
        policy_engine = PolicyEngine()
        privacy_service = PrivacyService(policy_engine)
        self.manager = MemoryManager(
            policy_engine, repository=PostgresMemoryRepository(self.database), privacy_service=privacy_service
        )

    def tearDown(self):
        self.database.dispose()

    def test_restricted_field_denied_persistence_still_enforced(self):
        record = self.manager.propose_memory(
            user_id="u1",
            category=MemoryCategory.PREFERENCE,
            key="medical_condition",
            value="diabetes",
            source="user_explicit",
        )
        with self.assertRaises(MemoryPolicyDeniedError):
            self.manager.persist_memory(record)
        self.assertEqual(self.manager.list_allowed_memory("u1"), [])

    def test_pii_in_value_never_reaches_the_database(self):
        # configs/policies/privacy.yaml maps PHONE -> RESTRICT for the
        # MEMORY context, and PolicyEngine.evaluate_pii() treats any
        # non-ALLOW action (REDACT/RESTRICT/BLOCK alike) as
        # allowed=False -- identical to ToolOrchestrator's own PII-gate
        # semantics (tool_orchestrator.py step 2.5). persist_memory()
        # therefore denies this write outright rather than storing a
        # redacted copy -- an even stronger "never bypassed" guarantee
        # than redaction would be. This is pre-existing PolicyEngine
        # behavior (Phase 6), not something Phase 12 changed; this test
        # exists to prove persistence doesn't weaken it.
        record = self.manager.propose_memory(
            user_id="u1",
            category=MemoryCategory.PREFERENCE,
            key="notes",
            value="call me at 555-123-4567 please",
            source="user_explicit",
        )
        with self.assertRaises(MemoryPolicyDeniedError):
            self.manager.persist_memory(record)
        # "PostgreSQL is private infrastructure" is not a reason to skip
        # this check (plan.md's explicit instruction) -- confirm the raw
        # value truly never reached the repository at all.
        self.assertIsNone(PostgresMemoryRepository(self.database).get(record.id))


class TestRestartRecovery(unittest.TestCase):
    def setUp(self):
        import os
        import tempfile

        fd, path = tempfile.mkstemp(suffix=".db", prefix="phase12_memory_restart_")
        os.close(fd)
        os.remove(path)
        self.db_path = Path(path)
        self.url = f"sqlite:///{self.db_path.as_posix()}"
        self._databases = []
        database = self._new_database()
        Base.metadata.create_all(database.engine)

    def tearDown(self):
        for database in self._databases:
            database.dispose()
        if self.db_path.exists():
            self.db_path.unlink()

    def _new_database(self) -> Database:
        database = Database(load_database_config(env={"DATABASE_URL": self.url}))
        self._databases.append(database)
        return database

    def test_memory_survives_simulated_restart(self):
        policy_engine = PolicyEngine()
        manager_before = MemoryManager(policy_engine, repository=PostgresMemoryRepository(self._new_database()))
        record = manager_before.propose_memory(
            user_id="u1",
            category=MemoryCategory.PREFERENCE,
            key="preferred_language",
            value="English",
            source="user_explicit",
        )
        manager_before.persist_memory(record)

        # "Restart": brand-new Database/repository/manager against the
        # same underlying file.
        manager_after = MemoryManager(policy_engine, repository=PostgresMemoryRepository(self._new_database()))
        fetched = manager_after.list_allowed_memory("u1")
        self.assertEqual(len(fetched), 1)
        self.assertEqual(fetched[0].value, "English")


class TestDatabaseFailure(unittest.TestCase):
    def test_get_raises_database_unavailable(self):
        database = Database(
            load_database_config(
                env={
                    "PERSISTENCE_MODE": "production",
                    "DATABASE_URL": "postgresql+psycopg2://u:p@127.0.0.1:1/nope?connect_timeout=1",
                }
            )
        )
        repo = PostgresMemoryRepository(database)
        try:
            with self.assertRaises(DatabaseUnavailableError):
                repo.get("anything")
        finally:
            database.dispose()

    def test_save_raises_database_unavailable(self):
        database = Database(
            load_database_config(
                env={
                    "PERSISTENCE_MODE": "production",
                    "DATABASE_URL": "postgresql+psycopg2://u:p@127.0.0.1:1/nope?connect_timeout=1",
                }
            )
        )
        repo = PostgresMemoryRepository(database)
        now = datetime.now(timezone.utc)
        try:
            with self.assertRaises(DatabaseUnavailableError):
                repo.save(
                    MemoryRecord(
                        id="x",
                        user_id="u1",
                        category=MemoryCategory.PREFERENCE,
                        key="k",
                        value="v",
                        source="s",
                        created_at=now,
                        updated_at=now,
                    )
                )
        finally:
            database.dispose()

    def test_list_for_user_raises_database_unavailable(self):
        database = Database(
            load_database_config(
                env={
                    "PERSISTENCE_MODE": "production",
                    "DATABASE_URL": "postgresql+psycopg2://u:p@127.0.0.1:1/nope?connect_timeout=1",
                }
            )
        )
        repo = PostgresMemoryRepository(database)
        try:
            with self.assertRaises(DatabaseUnavailableError):
                repo.list_for_user("u1")
        finally:
            database.dispose()


if __name__ == "__main__":
    unittest.main()

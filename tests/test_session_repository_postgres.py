"""
Tests for PostgresSessionRepository (Phase 12; plan.md Steps 12.5, 12.6):
src/agent/session_repository_postgres.py.

Run against SQLite (a temp file per test class, or :memory: where no
cross-thread/cross-connection sharing is required) as this repository's
dialect-portable test double — see PHASE_12_1_PERSISTENCE_AUDIT.md §13.
The repository code itself is dialect-agnostic SQLAlchemy Core (db.py's
upsert_row() dispatches only between "postgresql" and "sqlite"), so a
passing SQLite run exercises the exact same code path PostgreSQL would.

SessionManager itself is exercised too (not just the repository in
isolation) for the ownership/ordering tests, since SessionManager is
what actually enforces the ownership/expiration rules this repository
must not weaken (plan.md Step 12.5: "Do not move authorization entirely
into SQL").

Run with:
    python -m unittest tests.test_session_repository_postgres -v
"""

import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "agent"))

from db import Database, DatabaseUnavailableError, load_database_config  # noqa: E402
from db_models import Base  # noqa: E402
from session_manager import SessionManager  # noqa: E402
from session_models import SessionState, SessionStatus  # noqa: E402
from session_repository_postgres import PostgresSessionRepository  # noqa: E402


def _fresh_database() -> Database:
    """One isolated in-memory SQLite database per test, schema created directly from db_models (no Alembic dependency for these unit tests — migration correctness is test_db_migrations.py's job)."""
    database = Database(load_database_config(env={"DATABASE_URL": "sqlite:///:memory:"}))
    Base.metadata.create_all(database.engine)
    return database


class TestRepositoryRoundTrip(unittest.TestCase):
    def setUp(self):
        self.database = _fresh_database()
        self.repo = PostgresSessionRepository(self.database)

    def tearDown(self):
        self.database.dispose()

    def test_get_missing_session_returns_none(self):
        self.assertIsNone(self.repo.get("does-not-exist"))

    def test_save_then_get_round_trips_all_fields(self):
        now = datetime.now(timezone.utc)
        session = SessionState(
            session_id="s1", user_id="user-1", status=SessionStatus.ACTIVE,
            created_at=now, updated_at=now, expires_at=now + timedelta(minutes=30),
            current_intent="BOOK_APPOINTMENT", workflow_state="AWAITING_CONFIRMATION",
            pending_action="CANCEL_APPOINTMENT", pending_parameters={"appointment_id": "123"},
            confirmation_state={"required": True}, metadata={"channel": "voice"},
        )
        self.repo.save(session)
        fetched = self.repo.get("s1")
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.session_id, "s1")
        self.assertEqual(fetched.user_id, "user-1")
        self.assertEqual(fetched.status, SessionStatus.ACTIVE)
        self.assertEqual(fetched.pending_action, "CANCEL_APPOINTMENT")
        self.assertEqual(fetched.pending_parameters, {"appointment_id": "123"})
        self.assertEqual(fetched.confirmation_state, {"required": True})
        self.assertEqual(fetched.metadata, {"channel": "voice"})

    def test_save_is_upsert_not_insert_only(self):
        now = datetime.now(timezone.utc)
        session = SessionState(session_id="s2", created_at=now, updated_at=now, expires_at=now + timedelta(minutes=30))
        self.repo.save(session)
        session.current_intent = "FAQ"
        session.status = SessionStatus.WAITING_FOR_INPUT
        self.repo.save(session)  # same session_id -- must update, not raise a duplicate-PK error
        fetched = self.repo.get("s2")
        self.assertEqual(fetched.current_intent, "FAQ")
        self.assertEqual(fetched.status, SessionStatus.WAITING_FOR_INPUT)

    def test_delete_removes_session(self):
        now = datetime.now(timezone.utc)
        self.repo.save(SessionState(session_id="s3", created_at=now, updated_at=now, expires_at=now + timedelta(minutes=30)))
        self.repo.delete("s3")
        self.assertIsNone(self.repo.get("s3"))

    def test_delete_missing_session_does_not_raise(self):
        self.repo.delete("never-existed")  # no exception


class TestSessionManagerOwnership(unittest.TestCase):
    """User A -> User B session -> DENY (plan.md Step 12.5's explicit required test), exercised through SessionManager, not just the repository, since ownership is SessionManager's responsibility (not SQL's)."""

    def setUp(self):
        self.database = _fresh_database()
        self.manager = SessionManager(repository=PostgresSessionRepository(self.database))

    def tearDown(self):
        self.database.dispose()

    def test_cross_user_session_access_denied(self):
        session = self.manager.create_session(user_id="user-a")
        # User B attempts to read User A's session.
        self.assertIsNone(self.manager.get_session(session.session_id, user_id="user-b"))

    def test_owning_user_can_access_own_session(self):
        session = self.manager.create_session(user_id="user-a")
        fetched = self.manager.get_session(session.session_id, user_id="user-a")
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.user_id, "user-a")

    def test_ownerless_session_is_accessible_by_any_caller(self):
        session = self.manager.create_session(user_id=None)
        self.assertIsNotNone(self.manager.get_session(session.session_id, user_id="anyone"))


class TestExpiration(unittest.TestCase):
    """Expired sessions must remain inaccessible even if no cleanup job has run (plan.md Step 12.5's explicit requirement) -- verified against the persisted repository, not just the in-memory one."""

    def setUp(self):
        self.database = _fresh_database()
        self.repo = PostgresSessionRepository(self.database)
        self.manager = SessionManager(repository=self.repo)

    def tearDown(self):
        self.database.dispose()

    def test_expired_session_is_inaccessible_without_a_cleanup_job(self):
        now = datetime.now(timezone.utc)
        # Directly persist an already-expired session -- simulates "no
        # cleanup job has run yet," exactly the scenario plan.md calls out.
        self.repo.save(SessionState(session_id="expired-1", created_at=now - timedelta(hours=1), updated_at=now - timedelta(hours=1), expires_at=now - timedelta(minutes=1)))
        self.assertIsNone(self.manager.get_session("expired-1"))

    def test_expired_pending_confirmation_cannot_be_consumed(self):
        now = datetime.now(timezone.utc)
        self.repo.save(SessionState(
            session_id="expired-2", created_at=now - timedelta(hours=1), updated_at=now - timedelta(hours=1),
            expires_at=now - timedelta(minutes=1), workflow_state="AWAITING_CONFIRMATION",
            pending_action="CANCEL_APPOINTMENT", pending_parameters={"appointment_id": "1"},
        ))
        self.assertIsNone(self.manager.try_consume_pending_confirmation("expired-2"))


class TestUpdateAndTransitions(unittest.TestCase):
    def setUp(self):
        self.database = _fresh_database()
        self.manager = SessionManager(repository=PostgresSessionRepository(self.database))

    def tearDown(self):
        self.database.dispose()

    def test_update_session_persists_through_repository(self):
        session = self.manager.create_session(user_id="u1")
        self.manager.update_session(session.session_id, user_id="u1", current_intent="BOOK_APPOINTMENT")
        fetched = self.manager.get_session(session.session_id, user_id="u1")
        self.assertEqual(fetched.current_intent, "BOOK_APPOINTMENT")

    def test_valid_transition_persists(self):
        session = self.manager.create_session(user_id="u1")
        self.manager.transition_state(session.session_id, SessionStatus.WAITING_FOR_CONFIRMATION, user_id="u1")
        fetched = self.manager.get_session(session.session_id, user_id="u1")
        self.assertEqual(fetched.status, SessionStatus.WAITING_FOR_CONFIRMATION)

    def test_delete_session_removes_it_from_persistent_storage(self):
        session = self.manager.create_session(user_id="u1")
        self.manager.delete_session(session.session_id)
        self.assertIsNone(self.manager.get_session(session.session_id, user_id="u1"))


class TestReplayProtectionAcrossPersistence(unittest.TestCase):
    """Confirmation -> execute -> same confirmation again must be denied the second time (mirrors Phase 11's in-memory replay-protection regression test, now against the persisted repository)."""

    def setUp(self):
        self.database = _fresh_database()
        self.manager = SessionManager(repository=PostgresSessionRepository(self.database))

    def tearDown(self):
        self.database.dispose()

    def test_second_consumption_of_same_confirmation_is_denied(self):
        session = self.manager.create_session(user_id="u1")
        self.manager.update_session(
            session.session_id, user_id="u1", workflow_state="AWAITING_CONFIRMATION",
            pending_action="CANCEL_APPOINTMENT", pending_parameters={"appointment_id": "1"},
        )
        first = self.manager.try_consume_pending_confirmation(session.session_id, user_id="u1")
        self.assertIsNotNone(first)
        self.assertEqual(first[0], "CANCEL_APPOINTMENT")

        second = self.manager.try_consume_pending_confirmation(session.session_id, user_id="u1")
        self.assertIsNone(second)


class TestRestartRecovery(unittest.TestCase):
    """
    plan.md Step 12.6: pending-confirmation state must survive a process
    restart. Simulated here by constructing a brand-new Database +
    PostgresSessionRepository + SessionManager against the SAME
    underlying SQLite file — i.e. nothing in-memory is reused, matching
    what a real process restart looks like for a file/network-backed
    database.
    """

    def setUp(self):
        import os

        fd, path = tempfile.mkstemp(suffix=".db", prefix="phase12_session_restart_")
        os.close(fd)
        os.remove(path)
        self.db_path = Path(path)
        self.url = f"sqlite:///{self.db_path.as_posix()}"
        self._databases = []
        database = self._new_database()
        Base.metadata.create_all(database.engine)

    def tearDown(self):
        # Windows holds an OS-level file lock on a SQLite file for as
        # long as any Engine has an open connection to it -- every
        # "restart" in this test opened a genuinely new Database/Engine,
        # so every one of them must be disposed before the file can be
        # deleted, not just the last one.
        for database in self._databases:
            database.dispose()
        if self.db_path.exists():
            self.db_path.unlink()

    def _new_database(self) -> Database:
        database = Database(load_database_config(env={"DATABASE_URL": self.url}))
        self._databases.append(database)
        return database

    def _new_manager(self) -> SessionManager:
        return SessionManager(repository=PostgresSessionRepository(self._new_database()))

    def test_pending_confirmation_survives_simulated_restart(self):
        manager_before_restart = self._new_manager()
        session = manager_before_restart.create_session(user_id="u1")
        manager_before_restart.update_session(
            session.session_id, user_id="u1", workflow_state="AWAITING_CONFIRMATION",
            pending_action="CANCEL_APPOINTMENT", pending_parameters={"appointment_id": "42"},
        )

        # "Restart": a completely fresh Database/repository/manager stack
        # against the same file -- no Python object from before is reused.
        manager_after_restart = self._new_manager()
        fetched = manager_after_restart.get_session(session.session_id, user_id="u1")
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.workflow_state, "AWAITING_CONFIRMATION")
        self.assertEqual(fetched.pending_action, "CANCEL_APPOINTMENT")

        consumed = manager_after_restart.try_consume_pending_confirmation(session.session_id, user_id="u1")
        self.assertEqual(consumed, ("CANCEL_APPOINTMENT", {"appointment_id": "42"}))


class TestConcurrentConfirmationConsumption(unittest.TestCase):
    """
    Mandatory (plan.md Step 12.6): two concurrent confirmation requests
    against the same pending action must result in exactly one execution.
    Uses a real file-based SQLite database (not :memory:) so each thread
    genuinely checks out its own pooled connection and the database's own
    locking -- not a shared Python object -- is what serializes the two
    UPDATE statements, actually exercising cross-connection atomicity
    rather than incidental single-connection serialization.
    """

    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db", prefix="phase12_session_concurrency_")
        import os
        os.close(fd)
        os.remove(path)
        self.db_path = Path(path)
        self.database = Database(load_database_config(env={"DATABASE_URL": f"sqlite:///{self.db_path.as_posix()}", "DB_POOL_SIZE": "10"}))
        Base.metadata.create_all(self.database.engine)
        self.manager = SessionManager(repository=PostgresSessionRepository(self.database))

    def tearDown(self):
        self.database.dispose()
        if self.db_path.exists():
            self.db_path.unlink()

    def test_concurrent_same_session_confirmation_executes_exactly_once(self):
        session = self.manager.create_session(user_id="u1")
        self.manager.update_session(
            session.session_id, user_id="u1", workflow_state="AWAITING_CONFIRMATION",
            pending_action="CANCEL_APPOINTMENT", pending_parameters={"appointment_id": "1"},
        )

        results = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(5)

        def _attempt():
            barrier.wait()
            outcome = self.manager.try_consume_pending_confirmation(session.session_id, user_id="u1")
            with results_lock:
                results.append(outcome)

        threads = [threading.Thread(target=_attempt) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        successes = [r for r in results if r is not None]
        self.assertEqual(len(successes), 1, f"expected exactly one successful consumption, got {len(successes)}: {results}")


class TestDuplicateConfirmationSubmission(unittest.TestCase):
    """A user (or a retried client request) submitting the same 'yes' twice, sequentially, must only ever execute once -- the sequential counterpart to TestConcurrentConfirmationConsumption's parallel version."""

    def setUp(self):
        self.database = _fresh_database()
        self.manager = SessionManager(repository=PostgresSessionRepository(self.database))

    def tearDown(self):
        self.database.dispose()

    def test_duplicate_sequential_confirmation_only_consumes_once(self):
        session = self.manager.create_session(user_id="u1")
        self.manager.update_session(
            session.session_id, user_id="u1", workflow_state="AWAITING_CONFIRMATION",
            pending_action="CANCEL_APPOINTMENT", pending_parameters={"appointment_id": "9"},
        )
        outcomes = [
            self.manager.try_consume_pending_confirmation(session.session_id, user_id="u1")
            for _ in range(3)
        ]
        self.assertEqual(sum(1 for o in outcomes if o is not None), 1)


class TestDatabaseFailure(unittest.TestCase):
    def test_get_raises_database_unavailable_when_engine_is_disposed_and_unreachable(self):
        database = Database(load_database_config(env={"PERSISTENCE_MODE": "production", "DATABASE_URL": "postgresql+psycopg2://u:p@127.0.0.1:1/nope?connect_timeout=1"}))
        repo = PostgresSessionRepository(database)
        try:
            with self.assertRaises(DatabaseUnavailableError):
                repo.get("anything")
        finally:
            database.dispose()

    def test_save_raises_database_unavailable_on_connection_failure(self):
        database = Database(load_database_config(env={"PERSISTENCE_MODE": "production", "DATABASE_URL": "postgresql+psycopg2://u:p@127.0.0.1:1/nope?connect_timeout=1"}))
        repo = PostgresSessionRepository(database)
        now = datetime.now(timezone.utc)
        try:
            with self.assertRaises(DatabaseUnavailableError):
                repo.save(SessionState(session_id="x", created_at=now, updated_at=now, expires_at=now + timedelta(minutes=1)))
        finally:
            database.dispose()

    def test_try_consume_pending_confirmation_raises_database_unavailable_on_connection_failure(self):
        database = Database(load_database_config(env={"PERSISTENCE_MODE": "production", "DATABASE_URL": "postgresql+psycopg2://u:p@127.0.0.1:1/nope?connect_timeout=1"}))
        repo = PostgresSessionRepository(database)
        try:
            with self.assertRaises(DatabaseUnavailableError):
                repo.try_consume_pending_confirmation("any-session", user_id="u1")
        finally:
            database.dispose()


if __name__ == "__main__":
    unittest.main()

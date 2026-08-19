"""
Persistence recovery verification (Phase 12; plan.md Step 12.12): proves
the persistent architecture actually survives process restarts.

Each test genuinely destroys and recreates the application-level
instances (Database, Engine, repository, manager) against the same
underlying SQLite file -- never reusing a Python object across the
"restart" boundary. This is real integration testing, not mocked restart
behavior: the only thing that persists across the boundary is what's
actually on disk.

Each of these 7 scenarios individually overlaps with a test already
written in Steps 12.5-12.9's own per-repository test files (see each
test's docstring for the specific prior test it consolidates) -- this
file exists because Step 12.12 explicitly asks for a single, dedicated,
holistic proof of all seven together, not because the underlying
capability was previously unverified.

Run with:
    python -m unittest tests.test_persistence_recovery -v
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "agent"))

from action_models import AuthContext, ToolRequest  # noqa: E402
from audit import AuditLogger  # noqa: E402
from audit_repository_postgres import PostgresAuditRepository  # noqa: E402
from db import Database, load_database_config  # noqa: E402
from db_models import Base  # noqa: E402
from identity import Role, permissions_for_roles  # noqa: E402
from idempotency_repository_postgres import PostgresIdempotencyRepository  # noqa: E402
from memory_manager import MemoryManager  # noqa: E402
from memory_models import MemoryCategory  # noqa: E402
from memory_repository_postgres import PostgresMemoryRepository  # noqa: E402
from mock_tools import MockAppointmentStore, build_default_tool_registry  # noqa: E402
from observability_models import EventType  # noqa: E402
from policy_engine import PolicyEngine  # noqa: E402
from session_manager import SessionManager  # noqa: E402
from session_repository_postgres import PostgresSessionRepository  # noqa: E402
from tool_orchestrator import ToolOrchestrator  # noqa: E402

AUTH = AuthContext(
    user_id="user-1", authenticated=True, roles=(Role.USER.value,),
    permissions=permissions_for_roles((Role.USER,)), authentication_method="test",
)


class RecoveryTestCase(unittest.TestCase):
    """Base: one throwaway SQLite file per test, with a helper that constructs a genuinely fresh Database instance each time it's called -- the "destroy and recreate the application instance" step."""

    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db", prefix="phase12_recovery_")
        os.close(fd)
        os.remove(path)
        self.db_path = Path(path)
        self.url = f"sqlite:///{self.db_path.as_posix()}"
        self._databases = []
        bootstrap = self._new_database()
        Base.metadata.create_all(bootstrap.engine)

    def tearDown(self):
        for database in self._databases:
            database.dispose()
        if self.db_path.exists():
            self.db_path.unlink()

    def _new_database(self) -> Database:
        """Each call is a genuine "new application instance" -- a fresh Database/Engine, never a reused Python object."""
        database = Database(load_database_config(env={"DATABASE_URL": self.url}))
        self._databases.append(database)
        return database


class TestSessionRecovery(RecoveryTestCase):
    """Test 1 — Session: create -> persist -> destroy -> create new instance -> retrieve. Consolidates test_session_repository_postgres.py's TestRestartRecovery."""

    def test_session_survives_restart(self):
        manager1 = SessionManager(repository=PostgresSessionRepository(self._new_database()))
        session = manager1.create_session(user_id="user-1")
        del manager1  # destroy application instance

        manager2 = SessionManager(repository=PostgresSessionRepository(self._new_database()))
        recovered = manager2.get_session(session.session_id, user_id="user-1")
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered.session_id, session.session_id)


class TestConfirmationRecovery(RecoveryTestCase):
    """Test 2 — Confirmation: request -> confirmation required -> persist -> restart -> confirm. Expected: action executes exactly once."""

    def test_confirmation_executes_exactly_once_after_restart(self):
        appointments = MockAppointmentStore()
        booked = appointments.book({"doctor_id": "d1", "date": "2026-08-18", "time": "17:00"})
        registry = build_default_tool_registry(appointment_store=appointments)

        manager1 = SessionManager(repository=PostgresSessionRepository(self._new_database()))
        session = manager1.create_session(user_id="user-1")
        manager1.update_session(
            session.session_id, user_id="user-1", workflow_state="AWAITING_CONFIRMATION",
            pending_action="CANCEL_APPOINTMENT", pending_parameters={"appointment_id": booked["appointment_id"]},
        )
        del manager1  # destroy application instance

        manager2 = SessionManager(repository=PostgresSessionRepository(self._new_database()))
        orchestrator2 = ToolOrchestrator(registry, PolicyEngine(), idempotency_repository=PostgresIdempotencyRepository(self._new_database()))
        consumed = manager2.try_consume_pending_confirmation(session.session_id, user_id="user-1")
        self.assertIsNotNone(consumed)
        action_name, params = consumed
        result = orchestrator2.invoke(ToolRequest(action=action_name, params=params, confirmed=True, request_id="recovery-req-1"), auth=AUTH)
        self.assertTrue(result.success)

        # exactly once: a repeat with the same request_id (post-restart) is denied
        result_again = orchestrator2.invoke(ToolRequest(action=action_name, params=params, confirmed=True, request_id="recovery-req-1"), auth=AUTH)
        self.assertFalse(result_again.success)
        self.assertEqual(result_again.status, "duplicate")


class TestReplayRecovery(RecoveryTestCase):
    """Test 3 — Replay: execute -> restart -> replay same confirmation. Expected: DENY."""

    def test_replayed_confirmation_denied_after_restart(self):
        manager1 = SessionManager(repository=PostgresSessionRepository(self._new_database()))
        session = manager1.create_session(user_id="user-1")
        manager1.update_session(
            session.session_id, user_id="user-1", workflow_state="AWAITING_CONFIRMATION",
            pending_action="CANCEL_APPOINTMENT", pending_parameters={"appointment_id": "1"},
        )
        first = manager1.try_consume_pending_confirmation(session.session_id, user_id="user-1")
        self.assertIsNotNone(first)
        del manager1  # destroy application instance -- confirmation was already consumed before restart

        manager2 = SessionManager(repository=PostgresSessionRepository(self._new_database()))
        replay = manager2.try_consume_pending_confirmation(session.session_id, user_id="user-1")
        self.assertIsNone(replay, "a replayed confirmation must be denied even after a restart")


class TestMemoryRecovery(RecoveryTestCase):
    """Test 4 — Memory: create memory -> restart -> retrieve. Consolidates test_memory_repository_postgres.py's TestRestartRecovery."""

    def test_memory_survives_restart(self):
        policy_engine = PolicyEngine()
        manager1 = MemoryManager(policy_engine, repository=PostgresMemoryRepository(self._new_database()))
        record = manager1.propose_memory(user_id="user-1", category=MemoryCategory.PREFERENCE, key="preferred_language", value="English", source="user_explicit")
        manager1.persist_memory(record)
        del manager1  # destroy application instance

        manager2 = MemoryManager(policy_engine, repository=PostgresMemoryRepository(self._new_database()))
        recovered = manager2.list_allowed_memory("user-1")
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].value, "English")


class TestAuditRecovery(RecoveryTestCase):
    """Test 5 — Audit: generate event -> restart -> retrieve event. Consolidates test_audit_repository_postgres.py's TestRestartRecovery."""

    def test_audit_event_survives_restart(self):
        logger1 = AuditLogger(repository=PostgresAuditRepository(self._new_database()))
        logger1.record(EventType.SESSION_CREATED, outcome="success", actor="user-1", session_id="sess-1")
        del logger1  # destroy application instance

        repo2 = PostgresAuditRepository(self._new_database())
        events = repo2.list_events(event_type=EventType.SESSION_CREATED)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].actor, "user-1")


class TestIdempotencyRecovery(RecoveryTestCase):
    """Test 6 — Idempotency: execute operation -> restart -> repeat same operation. Expected: no duplicate execution. Consolidates test_idempotency_repository_postgres.py's restart test."""

    def test_no_duplicate_execution_after_restart(self):
        appointments = MockAppointmentStore()
        booked = appointments.book({"doctor_id": "d1", "date": "2026-08-18", "time": "17:00"})
        registry = build_default_tool_registry(appointment_store=appointments)

        orchestrator1 = ToolOrchestrator(registry, PolicyEngine(), idempotency_repository=PostgresIdempotencyRepository(self._new_database()))
        request = ToolRequest(action="CANCEL_APPOINTMENT", params={"appointment_id": booked["appointment_id"]}, confirmed=True, request_id="recovery-idem-1")
        first = orchestrator1.invoke(request, auth=AUTH)
        self.assertTrue(first.success)
        del orchestrator1  # destroy application instance

        orchestrator2 = ToolOrchestrator(registry, PolicyEngine(), idempotency_repository=PostgresIdempotencyRepository(self._new_database()))
        second = orchestrator2.invoke(request, auth=AUTH)
        self.assertFalse(second.success)
        self.assertEqual(second.status, "duplicate")


class TestCrossUserRecovery(RecoveryTestCase):
    """Test 7 — Cross-user: User A state -> restart -> User B attempts access. Expected: DENY."""

    def test_cross_user_session_access_denied_after_restart(self):
        manager1 = SessionManager(repository=PostgresSessionRepository(self._new_database()))
        session = manager1.create_session(user_id="user-a")
        del manager1  # destroy application instance

        manager2 = SessionManager(repository=PostgresSessionRepository(self._new_database()))
        self.assertIsNone(manager2.get_session(session.session_id, user_id="user-b"))

    def test_cross_user_memory_access_denied_after_restart(self):
        policy_engine = PolicyEngine()
        manager1 = MemoryManager(policy_engine, repository=PostgresMemoryRepository(self._new_database()))
        record = manager1.propose_memory(user_id="user-a", category=MemoryCategory.PREFERENCE, key="preferred_clinic", value="Downtown Clinic", source="user_explicit")
        manager1.persist_memory(record)
        del manager1  # destroy application instance

        manager2 = MemoryManager(policy_engine, repository=PostgresMemoryRepository(self._new_database()))
        self.assertEqual(manager2.list_allowed_memory("user-b"), [])
        self.assertFalse(manager2.remove_memory(record.id, user_id="user-b"))


if __name__ == "__main__":
    unittest.main()

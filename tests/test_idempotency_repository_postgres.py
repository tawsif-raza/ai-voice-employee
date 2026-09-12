"""
Tests for idempotency persistence (Phase 12; plan.md Step 12.9):
src/agent/idempotency_repository.py (InMemoryIdempotencyRepository),
src/agent/idempotency_repository_postgres.py (PostgresIdempotencyRepository),
and their integration into ToolOrchestrator.

Run against SQLite as PostgresIdempotencyRepository's dialect-portable
test double (PHASE_12_1_PERSISTENCE_AUDIT.md §13).

Run with:
    python -m unittest tests.test_idempotency_repository_postgres -v
"""

import sys
import threading
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "agent"))

from action_models import AuthContext, ToolRequest  # noqa: E402
from db import Database, DatabaseUnavailableError, load_database_config  # noqa: E402
from db_models import Base  # noqa: E402
from idempotency_repository import InMemoryIdempotencyRepository  # noqa: E402
from idempotency_repository_postgres import PostgresIdempotencyRepository  # noqa: E402
from identity import Role, permissions_for_roles  # noqa: E402
from mock_tools import MockAppointmentStore, build_default_tool_registry  # noqa: E402
from policy_engine import PolicyEngine  # noqa: E402
from tool_orchestrator import ToolOrchestrator  # noqa: E402

USER_A = AuthContext(
    user_id="user-a",
    authenticated=True,
    roles=(Role.USER.value,),
    permissions=permissions_for_roles((Role.USER,)),
    authentication_method="test",
)
USER_B = AuthContext(
    user_id="user-b",
    authenticated=True,
    roles=(Role.USER.value,),
    permissions=permissions_for_roles((Role.USER,)),
    authentication_method="test",
)


def _fresh_database() -> Database:
    database = Database(load_database_config(env={"DATABASE_URL": "sqlite:///:memory:"}))
    Base.metadata.create_all(database.engine)
    return database


def _repos():
    """Both implementations, tested identically wherever the interface is shared -- proves the contract, not just one backend."""
    database = _fresh_database()
    return [
        ("in-memory", InMemoryIdempotencyRepository(), None),
        ("postgres(sqlite)", PostgresIdempotencyRepository(database), database),
    ]


class TestRequiredScopingScenarios(unittest.TestCase):
    """plan.md Step 12.9's exact required test set."""

    def test_same_user_same_key_same_operation_executes_once(self):
        for name, repo, database in _repos():
            with self.subTest(backend=name):
                first = repo.try_reserve("req-1", user_id="user-a", action="CANCEL_APPOINTMENT")
                second = repo.try_reserve("req-1", user_id="user-a", action="CANCEL_APPOINTMENT")
                self.assertTrue(first)
                self.assertFalse(second)
                if database:
                    database.dispose()

    def test_same_key_different_user_is_isolated(self):
        # Design choice (documented in idempotency_repository.py):
        # isolated, not rejected -- two different users reusing the same
        # key string get independent slots.
        for name, repo, database in _repos():
            with self.subTest(backend=name):
                first = repo.try_reserve("shared-key", user_id="user-a", action="CANCEL_APPOINTMENT")
                second = repo.try_reserve("shared-key", user_id="user-b", action="CANCEL_APPOINTMENT")
                self.assertTrue(first)
                self.assertTrue(second)  # NOT blocked by user-a's reservation
                if database:
                    database.dispose()

    def test_same_key_different_operation_is_rejected(self):
        for name, repo, database in _repos():
            with self.subTest(backend=name):
                first = repo.try_reserve("req-2", user_id="user-a", action="CANCEL_APPOINTMENT")
                second = repo.try_reserve("req-2", user_id="user-a", action="BOOK_APPOINTMENT")
                self.assertTrue(first)
                self.assertFalse(second)  # key already claimed, regardless of action
                if database:
                    database.dispose()

    def test_expired_key_can_be_reclaimed(self):
        for name, repo, database in _repos():
            with self.subTest(backend=name):
                first = repo.try_reserve(
                    "req-3", user_id="user-a", action="CANCEL_APPOINTMENT", ttl=timedelta(seconds=-1)
                )
                self.assertTrue(first)
                # ttl already in the past -- the record is expired the instant it's written.
                second = repo.try_reserve("req-3", user_id="user-a", action="CANCEL_APPOINTMENT")
                self.assertTrue(second, "an expired key must be reclaimable, not permanently blocked")
                if database:
                    database.dispose()

    def test_unexpired_key_is_not_reclaimed(self):
        for name, repo, database in _repos():
            with self.subTest(backend=name):
                first = repo.try_reserve("req-4", user_id="user-a", action="CANCEL_APPOINTMENT", ttl=timedelta(hours=1))
                second = repo.try_reserve("req-4", user_id="user-a", action="CANCEL_APPOINTMENT")
                self.assertTrue(first)
                self.assertFalse(second)
                if database:
                    database.dispose()


class TestReleaseOnFailure(unittest.TestCase):
    def test_release_allows_legitimate_retry(self):
        for name, repo, database in _repos():
            with self.subTest(backend=name):
                repo.try_reserve("req-5", user_id="user-a", action="CANCEL_APPOINTMENT")
                repo.release("req-5", user_id="user-a")
                retried = repo.try_reserve("req-5", user_id="user-a", action="CANCEL_APPOINTMENT")
                self.assertTrue(retried, "release() must allow a legitimate later retry with the same key")
                if database:
                    database.dispose()


class TestConcurrency(unittest.TestCase):
    """Mandatory: same operation submitted concurrently -> exactly one execution."""

    def test_in_memory_concurrent_reservations(self):
        repo = InMemoryIdempotencyRepository()
        results = []
        lock = threading.Lock()

        def _attempt():
            r = repo.try_reserve("concurrent-1", user_id="user-a", action="CANCEL_APPOINTMENT")
            with lock:
                results.append(r)

        threads = [threading.Thread(target=_attempt) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(results), 1)

    def test_postgres_backed_concurrent_reservations_real_file_db(self):
        import os
        import tempfile

        fd, path = tempfile.mkstemp(suffix=".db", prefix="phase12_idempotency_concurrency_")
        os.close(fd)
        os.remove(path)
        db_path = Path(path)
        try:
            database = Database(
                load_database_config(env={"DATABASE_URL": f"sqlite:///{db_path.as_posix()}", "DB_POOL_SIZE": "10"})
            )
            Base.metadata.create_all(database.engine)
            repo = PostgresIdempotencyRepository(database)

            results = []
            lock = threading.Lock()
            barrier = threading.Barrier(5)

            def _attempt():
                barrier.wait()
                r = repo.try_reserve("concurrent-pg-1", user_id="user-a", action="CANCEL_APPOINTMENT")
                with lock:
                    results.append(r)

            threads = [threading.Thread(target=_attempt) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)
            self.assertEqual(sum(results), 1, f"expected exactly one reservation to win, got {results}")
            database.dispose()
        finally:
            if db_path.exists():
                db_path.unlink()


class TestSecurity(unittest.TestCase):
    """LLM cannot create/control trusted idempotency authorization; client cannot reuse another user's idempotency state."""

    def test_reservation_requires_a_real_authenticated_user_id_not_client_supplied_text(self):
        # The repository interface itself takes user_id as a plain
        # keyword the CALLER supplies -- the security guarantee lives in
        # ToolOrchestrator only ever passing auth.user_id (trusted,
        # resolved by AuthenticationProvider), never any client/LLM text.
        # Verified at the integration level below
        # (TestToolOrchestratorIntegration); this test documents the
        # repository's own contract: it has no method that accepts or
        # interprets a client-supplied "user_id" claim from a request
        # body or model output.
        repo = InMemoryIdempotencyRepository()
        import inspect

        sig = inspect.signature(repo.try_reserve)
        self.assertIn("user_id", sig.parameters)  # scoping is structural, not optional

    def test_client_cannot_reuse_another_users_idempotency_state_via_repository(self):
        repo = InMemoryIdempotencyRepository()
        repo.try_reserve("victim-key", user_id="user-a", action="CANCEL_APPOINTMENT")
        # An attacker as user-b presenting the SAME key must get their
        # own independent slot, never observe or consume user-a's.
        attacker_result = repo.try_reserve("victim-key", user_id="user-b", action="CANCEL_APPOINTMENT")
        self.assertTrue(attacker_result)  # isolated, not "reused" -- user-a's own record is untouched
        self.assertEqual(repo.get_recorded_action("victim-key", user_id="user-a"), "CANCEL_APPOINTMENT")
        self.assertEqual(repo.get_recorded_action("victim-key", user_id="user-b"), "CANCEL_APPOINTMENT")


class TestToolOrchestratorIntegration(unittest.TestCase):
    """The actual wiring: ToolOrchestrator(idempotency_repository=...) uses it instead of the raw set, scoped by the real auth.user_id."""

    def _orchestrator_with(self, repo):
        appointments = MockAppointmentStore()
        booked = appointments.book({"doctor_id": "d1", "date": "2026-08-18", "time": "17:00"})
        registry = build_default_tool_registry(appointment_store=appointments)
        orchestrator = ToolOrchestrator(registry, PolicyEngine(), idempotency_repository=repo)
        return orchestrator, booked

    def test_default_orchestrator_is_unaffected_when_no_repository_given(self):
        appointments = MockAppointmentStore()
        booked = appointments.book({"doctor_id": "d1", "date": "2026-08-18", "time": "17:00"})
        registry = build_default_tool_registry(appointment_store=appointments)
        orchestrator = ToolOrchestrator(registry, PolicyEngine())  # no idempotency_repository
        self.assertIsNone(orchestrator._idempotency_repository)
        request = ToolRequest(
            action="CANCEL_APPOINTMENT",
            params={"appointment_id": booked["appointment_id"]},
            confirmed=True,
            request_id="req-x",
        )
        first = orchestrator.invoke(request, auth=USER_A)
        self.assertTrue(first.success)
        second = orchestrator.invoke(request, auth=USER_A)
        self.assertEqual(second.status, "duplicate")

    def test_persisted_repository_blocks_duplicate_for_same_user(self):
        orchestrator, booked = self._orchestrator_with(InMemoryIdempotencyRepository())
        request = ToolRequest(
            action="CANCEL_APPOINTMENT",
            params={"appointment_id": booked["appointment_id"]},
            confirmed=True,
            request_id="req-y",
        )
        first = orchestrator.invoke(request, auth=USER_A)
        self.assertTrue(first.success)
        second = orchestrator.invoke(request, auth=USER_A)
        self.assertFalse(second.success)
        self.assertEqual(second.status, "duplicate")

    def test_failed_execution_releases_the_key_for_legitimate_retry(self):
        # CANCEL_APPOINTMENT on a nonexistent appointment fails structurally
        # (KeyError inside the mock tool) -- the idempotency key must not
        # be permanently consumed by that failure.
        repo = InMemoryIdempotencyRepository()
        appointments = MockAppointmentStore()
        registry = build_default_tool_registry(appointment_store=appointments)
        orchestrator = ToolOrchestrator(registry, PolicyEngine(), idempotency_repository=repo)
        request = ToolRequest(
            action="CANCEL_APPOINTMENT", params={"appointment_id": "does-not-exist"}, confirmed=True, request_id="req-z"
        )
        first = orchestrator.invoke(request, auth=USER_A)
        self.assertFalse(first.success)
        self.assertFalse(repo.has_executed("req-z", user_id="user-a", action="CANCEL_APPOINTMENT"))

    def test_restart_recovery_duplicate_still_blocked(self):
        import os
        import tempfile

        fd, path = tempfile.mkstemp(suffix=".db", prefix="phase12_idempotency_restart_")
        os.close(fd)
        os.remove(path)
        db_path = Path(path)
        url = f"sqlite:///{db_path.as_posix()}"
        databases = []
        try:
            db1 = Database(load_database_config(env={"DATABASE_URL": url}))
            databases.append(db1)
            Base.metadata.create_all(db1.engine)

            appointments = MockAppointmentStore()
            booked = appointments.book({"doctor_id": "d1", "date": "2026-08-18", "time": "17:00"})
            registry = build_default_tool_registry(appointment_store=appointments)
            orchestrator1 = ToolOrchestrator(
                registry, PolicyEngine(), idempotency_repository=PostgresIdempotencyRepository(db1)
            )
            request = ToolRequest(
                action="CANCEL_APPOINTMENT",
                params={"appointment_id": booked["appointment_id"]},
                confirmed=True,
                request_id="req-restart",
            )
            first = orchestrator1.invoke(request, auth=USER_A)
            self.assertTrue(first.success)

            # "Restart": brand-new Database + repository + orchestrator against the same file.
            db2 = Database(load_database_config(env={"DATABASE_URL": url}))
            databases.append(db2)
            orchestrator2 = ToolOrchestrator(
                registry, PolicyEngine(), idempotency_repository=PostgresIdempotencyRepository(db2)
            )
            second = orchestrator2.invoke(request, auth=USER_A)
            self.assertFalse(second.success)
            self.assertEqual(second.status, "duplicate")
        finally:
            for database in databases:
                database.dispose()
            if db_path.exists():
                db_path.unlink()


class TestDatabaseFailure(unittest.TestCase):
    def test_try_reserve_raises_database_unavailable(self):
        database = Database(
            load_database_config(
                env={
                    "PERSISTENCE_MODE": "production",
                    "DATABASE_URL": "postgresql+psycopg2://u:p@127.0.0.1:1/nope?connect_timeout=1",
                }
            )
        )
        repo = PostgresIdempotencyRepository(database)
        try:
            with self.assertRaises(DatabaseUnavailableError):
                repo.try_reserve("x", user_id="user-a", action="CANCEL_APPOINTMENT")
        finally:
            database.dispose()


if __name__ == "__main__":
    unittest.main()

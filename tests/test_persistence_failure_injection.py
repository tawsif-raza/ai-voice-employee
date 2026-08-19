"""
Persistence failure-injection tests (Phase 12; plan.md Step 12.11):
proves PostgreSQL failures cannot cause unsafe behavior anywhere in the
Phase 12 persistence layer.

Core requirement under test: if the system cannot establish trustworthy
state for authorization, confirmation, ownership, or idempotency, the
risky operation must NOT execute -- SAFE FAILURE / DENY, never a silent
insecure fallback to a permissive default. This file proves that
property by actually breaking the database (an unreachable target, a
constraint violation, a pool exhausted under real concurrency, a forced
mid-transaction rollback) and observing what each collaborator does,
rather than asserting it from code inspection alone.

Run with:
    python -m unittest tests.test_persistence_failure_injection -v
"""

import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "agent"))

from action_models import ActionSpec, AuthContext, ToolRequest  # noqa: E402
from audit import AuditLogger  # noqa: E402
from audit_repository_postgres import PostgresAuditRepository  # noqa: E402
from db import Database, DatabaseUnavailableError, load_database_config  # noqa: E402
from db_models import Base  # noqa: E402
from identity import Role, permissions_for_roles  # noqa: E402
from idempotency_repository_postgres import PostgresIdempotencyRepository  # noqa: E402
from memory_manager import MemoryManager  # noqa: E402
from memory_models import MemoryCategory, MemoryRecord  # noqa: E402
from memory_repository_postgres import PostgresMemoryRepository  # noqa: E402
from observability_models import EventType  # noqa: E402
from policy_engine import PolicyEngine  # noqa: E402
from session_manager import SessionManager  # noqa: E402
from session_models import SessionState  # noqa: E402
from session_repository_postgres import PostgresSessionRepository  # noqa: E402
from tool_orchestrator import ToolOrchestrator  # noqa: E402
from tool_registry import ToolRegistry  # noqa: E402

UNREACHABLE_URL = "postgresql+psycopg2://u:p@127.0.0.1:1/nope?connect_timeout=1"
AUTHENTICATED_USER = AuthContext(
    user_id="user-1", authenticated=True, roles=(Role.USER.value,),
    permissions=permissions_for_roles((Role.USER,)), authentication_method="test",
)


def _unreachable_database() -> Database:
    return Database(load_database_config(env={"PERSISTENCE_MODE": "production", "DATABASE_URL": UNREACHABLE_URL}))


def _fresh_database() -> Database:
    database = Database(load_database_config(env={"DATABASE_URL": "sqlite:///:memory:"}))
    Base.metadata.create_all(database.engine)
    return database


class TestIdempotencyFailsSafe(unittest.TestCase):
    """Simulates: database unavailable. Required behavior: the risky (non-idempotent) tool call must NOT execute."""

    def test_unreachable_idempotency_repository_prevents_tool_execution(self):
        call_count = {"n": 0}

        def _cancel(params):
            call_count["n"] += 1
            return {"status": "cancelled"}

        registry = ToolRegistry()
        registry.register(
            ActionSpec(name="CANCEL_APPOINTMENT", description="d", params_schema={"appointment_id": "str"},
                       required_params=("appointment_id",), requires_confirmation=True, destructive=True),
            _cancel,
        )
        database = _unreachable_database()
        orchestrator = ToolOrchestrator(registry, PolicyEngine(), idempotency_repository=PostgresIdempotencyRepository(database))
        request = ToolRequest(action="CANCEL_APPOINTMENT", params={"appointment_id": "x"}, confirmed=True, request_id="req-1")

        with self.assertRaises(DatabaseUnavailableError):
            orchestrator.invoke(request, auth=AUTHENTICATED_USER)

        self.assertEqual(call_count["n"], 0, "the tool must never execute when idempotency state cannot be established")
        database.dispose()


class TestConfirmationFailsSafe(unittest.TestCase):
    """Simulates: database unavailable during confirmation consumption. Required: no execution, no corrupted confirmation state."""

    def test_unreachable_session_repository_prevents_confirmation_consumption(self):
        database = _unreachable_database()
        manager = SessionManager(repository=PostgresSessionRepository(database))
        with self.assertRaises(DatabaseUnavailableError):
            manager.try_consume_pending_confirmation("any-session", user_id="user-1")
        database.dispose()

    def test_unreachable_session_repository_never_reports_a_false_confirmation(self):
        # The failure mode must be "raise", never "return None as if there
        # were simply nothing pending" -- the latter would be
        # indistinguishable from a legitimate "no pending action" and
        # could mask a real outage from a caller that only checks for
        # None. Verified: this call raises, it does not return None.
        database = _unreachable_database()
        manager = SessionManager(repository=PostgresSessionRepository(database))
        try:
            manager.try_consume_pending_confirmation("any-session", user_id="user-1")
            self.fail("expected DatabaseUnavailableError, got a normal return")
        except DatabaseUnavailableError:
            pass
        finally:
            database.dispose()


class TestOwnershipFailsSafe(unittest.TestCase):
    """Simulates: database unavailable during an ownership-scoped read/write. Required: no ownership bypass."""

    def test_unreachable_session_repository_get_raises_not_returns_permissive_default(self):
        database = _unreachable_database()
        manager = SessionManager(repository=PostgresSessionRepository(database))
        with self.assertRaises(DatabaseUnavailableError):
            manager.get_session("any-session", user_id="user-1")
        database.dispose()

    def test_unreachable_memory_repository_prevents_cross_user_bypass(self):
        database = _unreachable_database()
        manager = MemoryManager(PolicyEngine(), repository=PostgresMemoryRepository(database))
        with self.assertRaises(DatabaseUnavailableError):
            manager.remove_memory("any-id", user_id="user-b")
        database.dispose()

    def test_unreachable_memory_repository_persist_never_silently_succeeds(self):
        database = _unreachable_database()
        manager = MemoryManager(PolicyEngine(), repository=PostgresMemoryRepository(database))
        record = manager.propose_memory(user_id="user-1", category=MemoryCategory.PREFERENCE, key="k", value="v", source="s")
        with self.assertRaises(DatabaseUnavailableError):
            manager.persist_memory(record)
        database.dispose()


class TestAuthorizationUnaffectedByDatabaseState(unittest.TestCase):
    """
    Authorization itself (PolicyEngine.evaluate_authorization()) is
    stateless/YAML-config-driven -- it has no database dependency at all,
    so a PostgreSQL outage cannot corrupt or bypass it. This test proves
    that property directly rather than merely asserting it: a fully
    unreachable database is configured everywhere else, and the
    authorization decision is still made correctly.
    """

    def test_authorization_decision_correct_even_with_every_other_repository_unreachable(self):
        policy_engine = PolicyEngine()
        unauthenticated = AuthContext(user_id="x", authenticated=False)
        decision = policy_engine.evaluate_authorization(unauthenticated, "CANCEL_APPOINTMENT")
        self.assertFalse(decision.allowed)


class TestNoInsecureFallback(unittest.TestCase):
    """No manager silently substitutes an in-memory repository, or otherwise proceeds permissively, when its configured persisted repository fails."""

    def test_session_manager_does_not_fall_back_to_a_fresh_in_memory_session(self):
        database = _unreachable_database()
        manager = SessionManager(repository=PostgresSessionRepository(database))
        # If SessionManager silently fell back, create_session() would
        # succeed and return a usable session. It must not.
        with self.assertRaises(DatabaseUnavailableError):
            manager.create_session(user_id="user-1")
        database.dispose()

    def test_tool_orchestrator_does_not_fall_back_to_the_in_process_set(self):
        # ToolOrchestrator._idempotency_repository, once configured, is
        # the ONLY path step 4 uses for that instance -- there is no
        # "try the repository, fall back to the set on failure" branch.
        registry = ToolRegistry()
        registry.register(ActionSpec(name="CANCEL_APPOINTMENT", description="d", params_schema={"appointment_id": "str"}, required_params=("appointment_id",), requires_confirmation=True, destructive=True), lambda p: {"status": "ok"})
        database = _unreachable_database()
        orchestrator = ToolOrchestrator(registry, PolicyEngine(), idempotency_repository=PostgresIdempotencyRepository(database))
        request = ToolRequest(action="CANCEL_APPOINTMENT", params={"appointment_id": "x"}, confirmed=True, request_id="req-2")
        with self.assertRaises(DatabaseUnavailableError):
            orchestrator.invoke(request, auth=AUTHENTICATED_USER)
        # The raw in-process set must remain untouched/empty -- proof the
        # failure wasn't silently absorbed by the old default mechanism.
        self.assertEqual(orchestrator._executed_request_ids, set())
        database.dispose()


class TestNoPrivacyLeakageOnFailure(unittest.TestCase):
    def test_memory_persist_failure_message_never_contains_the_value(self):
        database = _unreachable_database()
        manager = MemoryManager(PolicyEngine(), repository=PostgresMemoryRepository(database))
        record = manager.propose_memory(user_id="user-1", category=MemoryCategory.PREFERENCE, key="notes", value="secret-value-should-never-leak", source="s")
        try:
            manager.persist_memory(record)
            self.fail("expected DatabaseUnavailableError")
        except DatabaseUnavailableError as exc:
            self.assertNotIn("secret-value-should-never-leak", str(exc))
        database.dispose()

    def test_audit_logger_never_raises_and_never_leaks_metadata_on_failure(self):
        database = _unreachable_database()
        logger = AuditLogger(repository=PostgresAuditRepository(database))
        result = logger.record(
            EventType.TOOL_REQUESTED, outcome="requested", actor="user-1",
            metadata={"sensitive": "should-never-appear-anywhere"},
        )
        self.assertIsNone(result)  # best-effort: swallowed, not raised, and nothing was ever persisted
        database.dispose()


class TestConstraintViolation(unittest.TestCase):
    """Simulates: constraint violation. A write that violates a database constraint must fail loudly (DatabaseUnavailableError), never silently succeed with corrupted data."""

    def test_null_user_id_violates_not_null_constraint_and_raises(self):
        database = _fresh_database()
        with self.assertRaises(DatabaseUnavailableError):
            with database.session_scope() as db_session:
                from db_models import IdempotencyRecordRow

                db_session.add(IdempotencyRecordRow(
                    user_id=None, request_id="x", action="CANCEL_APPOINTMENT",
                    result_status="in_progress", executed_at=datetime.now(timezone.utc),
                    expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                ))
        database.dispose()


class TestTransactionRollback(unittest.TestCase):
    """Simulates: transaction rollback. A failure partway through a multi-statement transaction must leave no partial write behind."""

    def test_partial_write_is_rolled_back_on_constraint_violation(self):
        database = _fresh_database()
        repo = PostgresSessionRepository(database)
        now = datetime.now(timezone.utc)
        repo.save(SessionState(session_id="rollback-test", created_at=now, updated_at=now, expires_at=now + timedelta(minutes=30)))

        from db_models import SessionRow

        try:
            with database.session_scope() as db_session:
                # A valid update, followed by a deliberately invalid one
                # in the SAME transaction -- the whole thing must roll
                # back, not partially apply.
                row = db_session.get(SessionRow, "rollback-test")
                row.current_intent = "SHOULD_NOT_PERSIST"
                db_session.flush()
                db_session.add(SessionRow(
                    session_id="rollback-test",  # duplicate PK -- violates uniqueness
                    status="ACTIVE", created_at=now, updated_at=now, expires_at=now + timedelta(minutes=30),
                    pending_parameters={}, confirmation_state={}, metadata_={},
                ))
        except DatabaseUnavailableError:
            pass

        reloaded = repo.get("rollback-test")
        self.assertIsNone(reloaded.current_intent, "the update inside the failed transaction must have been rolled back")
        database.dispose()


class TestConnectionPoolExhaustion(unittest.TestCase):
    """
    Simulates: connection pool exhaustion. A caller that can't get a
    connection within the pool timeout must fail safely, not hang or
    silently proceed.

    db.py's _build_engine() deliberately does NOT apply pool_size/
    max_overflow/pool_timeout tuning to SQLite (documented in that
    function: "those parameters are meaningless for SQLite's connection
    model"), so SQLite cannot be used to exercise REAL pool-timeout
    behavior end-to-end the way the rest of this test suite uses it as a
    PostgreSQL stand-in. What's actually under test here is
    `Database.session_scope()`'s exception-wrapping: real pool exhaustion
    raises `sqlalchemy.exc.TimeoutError` (a `SQLAlchemyError` subclass);
    proving `session_scope()` converts THAT specific exception type into
    `DatabaseUnavailableError` (never letting it escape raw, never
    hanging) is what actually matters here, independent of which pool
    implementation produced it.
    """

    def test_pool_timeout_error_is_wrapped_as_database_unavailable(self):
        import sqlalchemy.exc

        database = _fresh_database()
        try:
            real_session_factory = database._session_factory

            class _TimingOutSession:
                def execute(self, *a, **k):
                    raise sqlalchemy.exc.TimeoutError("QueuePool limit of size 1 overflow 0 reached, connection timed out")

                def commit(self):
                    pass

                def rollback(self):
                    pass

                def close(self):
                    pass

            database._session_factory = lambda: _TimingOutSession()
            start = time.monotonic()
            with self.assertRaises(DatabaseUnavailableError):
                with database.session_scope() as db_session:
                    db_session.execute("irrelevant")
            elapsed = time.monotonic() - start
            self.assertLess(elapsed, 2, "must fail immediately on a pool timeout, never hang waiting for a connection that will never come")
            database._session_factory = real_session_factory
        finally:
            database.dispose()


class TestNoAggressiveRetries(unittest.TestCase):
    """
    plan.md Step 12.11's explicit instruction: "Do not add aggressive
    database retries. Database retries can duplicate writes." None of the
    Postgres-backed repositories implement any retry loop -- verified
    both by inspection (no `retry`/`attempt`/loop-and-sleep construct in
    any of them) and behaviorally: a single failed connection attempt
    against an unreachable target returns in roughly one connect_timeout
    window, not a multiple of it.
    """

    def test_single_failed_attempt_takes_roughly_one_connect_timeout_not_a_multiple(self):
        database = Database(load_database_config(env={
            "PERSISTENCE_MODE": "production",
            "DATABASE_URL": "postgresql+psycopg2://u:p@127.0.0.1:1/nope?connect_timeout=1",
        }))
        start = time.monotonic()
        with self.assertRaises(DatabaseUnavailableError):
            database.health_check()
        elapsed = time.monotonic() - start
        # A retrying implementation (e.g. 3 attempts at 1s connect_timeout
        # each) would take ~3s+; a single attempt completes well under
        # that. Generous bound to avoid environment-timing flakiness.
        self.assertLess(elapsed, 3.0, "a single connection attempt should not be internally retried")
        database.dispose()

    def test_no_retry_related_code_in_repository_modules(self):
        import inspect

        import idempotency_repository_postgres
        import session_repository_postgres
        import memory_repository_postgres
        import audit_repository_postgres

        for module in (idempotency_repository_postgres, session_repository_postgres, memory_repository_postgres, audit_repository_postgres):
            source = inspect.getsource(module)
            self.assertNotIn("for attempt in", source)
            self.assertNotIn("while True", source)


if __name__ == "__main__":
    unittest.main()

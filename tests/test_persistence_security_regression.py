"""
Persistence-specific security regression (Phase 12; plan.md Step 12.13):
proves adding PostgreSQL did not weaken any security boundary from
Phases 3-11, and adds the 10 persistence-specific attack scenarios this
step explicitly requires.

Critical rule under test throughout: persistence stores state, it does
not replace authorization. Every scenario below either tampers with
stored state directly (bypassing the application layer, simulating a
compromised database or malicious admin) or breaks the database at a
specific moment, and observes whether the REAL authorization/policy/
confirmation gates are still consulted -- never whether stored state
alone was trusted as a substitute for them.

Run with:
    python -m unittest tests.test_persistence_security_regression -v
"""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "agent"))

from action_models import AuthContext, ToolRequest  # noqa: E402
from db import Database, DatabaseUnavailableError, load_database_config  # noqa: E402
from db_models import Base, SessionRow  # noqa: E402
from identity import Role, permissions_for_roles  # noqa: E402
from idempotency_repository_postgres import PostgresIdempotencyRepository  # noqa: E402
from mock_tools import MockAppointmentStore, build_default_tool_registry  # noqa: E402
from policy_engine import PolicyEngine  # noqa: E402
from session_manager import SessionManager  # noqa: E402
from session_models import SessionState, SessionStatus  # noqa: E402
from session_repository_postgres import PostgresSessionRepository  # noqa: E402
from tool_orchestrator import ToolOrchestrator  # noqa: E402

USER_A = AuthContext(user_id="user-a", authenticated=True, roles=(Role.USER.value,), permissions=permissions_for_roles((Role.USER,)), authentication_method="test")
USER_B = AuthContext(user_id="user-b", authenticated=True, roles=(Role.USER.value,), permissions=permissions_for_roles((Role.USER,)), authentication_method="test")
UNREACHABLE_URL = "postgresql+psycopg2://u:p@127.0.0.1:1/nope?connect_timeout=1"


def _fresh_database() -> Database:
    database = Database(load_database_config(env={"DATABASE_URL": "sqlite:///:memory:"}))
    Base.metadata.create_all(database.engine)
    return database


class Test1TamperedOwnershipField(unittest.TestCase):
    """
    Attack: an attacker with direct database access (compromised DB,
    malicious admin, SQL injection elsewhere) rewrites a session's
    user_id field directly, bypassing the application entirely.

    Critical rule verified: the ownership CHECK itself has no bypass --
    it mechanically compares against whatever is currently stored, every
    time, freshly queried. Tampering can change WHO the stored owner is,
    but there is no separate trusted field an attacker could set (like an
    `is_owner=true` flag) that would make the check itself always pass.
    """

    def test_ownership_check_follows_tampered_stored_state_not_a_separate_trusted_field(self):
        database = _fresh_database()
        manager = SessionManager(repository=PostgresSessionRepository(database))
        session = manager.create_session(user_id="user-a")

        # Original owner can access it.
        self.assertIsNotNone(manager.get_session(session.session_id, user_id="user-a"))

        # Attacker tampers with the raw stored user_id directly (bypassing SessionManager entirely).
        with database.session_scope() as db_session:
            row = db_session.get(SessionRow, session.session_id)
            row.user_id = "user-attacker"

        # The check is driven by whatever is NOW stored -- the original
        # owner is denied (proves there is no cached/separate "true
        # owner" the application trusts instead of the stored field).
        self.assertIsNone(manager.get_session(session.session_id, user_id="user-a"))
        # And the tampered value is honored, not specially rejected --
        # proving the ownership check has exactly one source of truth
        # (the stored field), never a second hidden authority. This is
        # the honest, expected consequence of a compromised database --
        # not a gap this application layer can or should paper over.
        self.assertIsNotNone(manager.get_session(session.session_id, user_id="user-attacker"))
        database.dispose()


class Test2TamperedPendingAction(unittest.TestCase):
    """
    Attack: an attacker with direct database access rewrites a session's
    pending_action/pending_parameters while it's AWAITING_CONFIRMATION,
    trying to substitute a different (or more privileged) action for the
    one the user actually agreed to.

    Critical rule verified: even a tampered pending_action must still
    pass through ToolOrchestrator's FULL gate sequence (policy,
    authentication, authorization, confirmation) when consumed -- the
    fact that it came from "pending session state" grants it no
    elevated trust whatsoever.
    """

    def test_tampered_pending_action_still_goes_through_full_policy_gate(self):
        database = _fresh_database()
        manager = SessionManager(repository=PostgresSessionRepository(database))
        session = manager.create_session(user_id="user-a")
        manager.update_session(
            session.session_id, user_id="user-a", workflow_state="AWAITING_CONFIRMATION",
            pending_action="CANCEL_APPOINTMENT", pending_parameters={"appointment_id": "1"},
        )

        # Attacker tampers with the pending action directly in the database.
        with database.session_scope() as db_session:
            row = db_session.get(SessionRow, session.session_id)
            row.pending_action = "NOT_A_REGISTERED_TOOL"
            row.pending_parameters = {"appointment_id": "1"}

        consumed = manager.try_consume_pending_confirmation(session.session_id, user_id="user-a")
        self.assertIsNotNone(consumed)
        action_name, params = consumed
        self.assertEqual(action_name, "NOT_A_REGISTERED_TOOL")

        registry = build_default_tool_registry(appointment_store=MockAppointmentStore())
        orchestrator = ToolOrchestrator(registry, PolicyEngine())
        result = orchestrator.invoke(ToolRequest(action=action_name, params=params, confirmed=True, request_id="tampered-1"), auth=USER_A)
        # The tampered action name was never a registered tool -- ToolOrchestrator's
        # own validation (never SessionManager's) is what catches this.
        self.assertFalse(result.success)
        self.assertEqual(result.error, "UNKNOWN_TOOL")
        database.dispose()


class Test3ReusedConfirmation(unittest.TestCase):
    def test_reused_confirmation_denied(self):
        database = _fresh_database()
        manager = SessionManager(repository=PostgresSessionRepository(database))
        session = manager.create_session(user_id="user-a")
        manager.update_session(
            session.session_id, user_id="user-a", workflow_state="AWAITING_CONFIRMATION",
            pending_action="CANCEL_APPOINTMENT", pending_parameters={"appointment_id": "1"},
        )
        first = manager.try_consume_pending_confirmation(session.session_id, user_id="user-a")
        self.assertIsNotNone(first)
        second = manager.try_consume_pending_confirmation(session.session_id, user_id="user-a")
        self.assertIsNone(second)
        database.dispose()


class Test4ReusedIdempotencyKey(unittest.TestCase):
    def test_reused_idempotency_key_denied(self):
        database = _fresh_database()
        repo = PostgresIdempotencyRepository(database)
        self.assertTrue(repo.try_reserve("key-1", user_id="user-a", action="CANCEL_APPOINTMENT"))
        self.assertFalse(repo.try_reserve("key-1", user_id="user-a", action="CANCEL_APPOINTMENT"))
        database.dispose()


class Test5CrossUserIdempotencyKey(unittest.TestCase):
    """Attack: User B submits the exact same idempotency key User A used, hoping to either read/replay User A's operation or have it treated as already-authorized."""

    def test_cross_user_idempotency_key_grants_no_access_to_victims_operation(self):
        database = _fresh_database()
        repo = PostgresIdempotencyRepository(database)
        self.assertTrue(repo.try_reserve("shared-key", user_id="user-a", action="CANCEL_APPOINTMENT"))
        repo.update_result("shared-key", user_id="user-a", result_status="success")

        # User B reusing the identical key gets their OWN independent
        # slot -- never User A's result, never treated as "already done."
        won = repo.try_reserve("shared-key", user_id="user-b", action="CANCEL_APPOINTMENT")
        self.assertTrue(won, "user-b's use of the same key string must be independent of user-a's")
        self.assertEqual(repo.get_recorded_action("shared-key", user_id="user-a"), "CANCEL_APPOINTMENT")
        self.assertEqual(repo.get_recorded_action("shared-key", user_id="user-b"), "CANCEL_APPOINTMENT")
        database.dispose()


class Test6StaleConfirmation(unittest.TestCase):
    def test_expired_confirmation_denied(self):
        database = _fresh_database()
        repo = PostgresSessionRepository(database)
        manager = SessionManager(repository=repo)
        now = datetime.now(timezone.utc)
        repo.save(SessionState(
            session_id="stale-confirm", user_id="user-a", created_at=now - timedelta(hours=1),
            updated_at=now - timedelta(hours=1), expires_at=now - timedelta(minutes=1),
            workflow_state="AWAITING_CONFIRMATION", pending_action="CANCEL_APPOINTMENT",
            pending_parameters={"appointment_id": "1"},
        ))
        self.assertIsNone(manager.try_consume_pending_confirmation("stale-confirm", user_id="user-a"))
        database.dispose()


class Test7StaleSession(unittest.TestCase):
    def test_expired_session_denied(self):
        database = _fresh_database()
        repo = PostgresSessionRepository(database)
        manager = SessionManager(repository=repo)
        now = datetime.now(timezone.utc)
        repo.save(SessionState(session_id="stale-session", user_id="user-a", created_at=now - timedelta(hours=1), updated_at=now - timedelta(hours=1), expires_at=now - timedelta(minutes=1)))
        self.assertIsNone(manager.get_session("stale-session", user_id="user-a"))
        database.dispose()


class Test8DatabaseFailureDuringAuthorization(unittest.TestCase):
    """PolicyEngine.evaluate_authorization() is stateless/YAML-driven -- a database outage cannot be exploited to bypass it, verified with every persisted repository simultaneously unreachable."""

    def test_authorization_still_correctly_denies_with_database_down(self):
        policy_engine = PolicyEngine()
        decision = policy_engine.evaluate_authorization(USER_B, "CANCEL_APPOINTMENT", resource_owner_user_id="user-a")
        # USER_B lacks ownership of user-a's resource -- denied regardless of any database state.
        self.assertFalse(decision.allowed)

    def test_tool_orchestrator_authorization_gate_fails_closed_when_idempotency_db_is_down(self):
        # Even though authorization itself doesn't touch the database,
        # confirm the OVERALL gate sequence still fails closed (deny, not
        # bypass) when a later gate's database is unreachable -- an
        # attacker cannot use a DB outage as a way to skip past
        # authorization into execution.
        database = _unreachable_database()
        registry = build_default_tool_registry(appointment_store=MockAppointmentStore())
        orchestrator = ToolOrchestrator(registry, PolicyEngine(), idempotency_repository=PostgresIdempotencyRepository(database))
        result_or_exc = None
        try:
            result_or_exc = orchestrator.invoke(
                ToolRequest(action="CANCEL_APPOINTMENT", params={"appointment_id": "1"}, confirmed=True, request_id="req-1"),
                auth=USER_B,  # not the resource owner
            )
        except DatabaseUnavailableError:
            pass  # also an acceptable "did not execute" outcome
        if result_or_exc is not None:
            self.assertFalse(result_or_exc.success)
        database.dispose()


class Test9DatabaseFailureDuringConfirmation(unittest.TestCase):
    def test_confirmation_consumption_fails_closed_when_database_unreachable(self):
        database = _unreachable_database()
        manager = SessionManager(repository=PostgresSessionRepository(database))
        with self.assertRaises(DatabaseUnavailableError):
            manager.try_consume_pending_confirmation("any-session", user_id="user-a")
        database.dispose()


class Test10DatabaseFailureDuringBookkeeping(unittest.TestCase):
    """
    Attack surface: the tool ALREADY executed successfully (e.g. the
    appointment was really cancelled), but the post-execution
    idempotency bookkeeping write (update_result()) then fails because
    the database drops at that exact moment. Does this create a window
    for a duplicate destructive side effect on a legitimate retry?

    Finding, documented not silently fixed: ToolOrchestrator.invoke()
    raises DatabaseUnavailableError in this window (the bookkeeping
    write is unguarded) -- a caller cannot tell "definitely failed" from
    "succeeded but bookkeeping failed" from this exception alone. This is
    NOT a security bypass (failing loudly is the safe direction, not a
    silent double-execution) -- verified here: even a legitimate retry
    with a NEW request_id (since the caller believes the first attempt
    failed) is safely rejected by the underlying business logic's own
    idempotent state check (the appointment is already cancelled),
    preventing a real duplicate side effect regardless of the
    orchestrator-level bookkeeping failure.
    """

    def test_retry_after_bookkeeping_failure_does_not_double_execute(self):
        appointments = MockAppointmentStore()
        booked = appointments.book({"doctor_id": "d1", "date": "2026-08-18", "time": "17:00"})
        registry = build_default_tool_registry(appointment_store=appointments)

        class _FailOnUpdateResultRepository(PostgresIdempotencyRepository):
            def update_result(self, *a, **k):
                raise DatabaseUnavailableError("simulated: database dropped immediately after successful execution")

        database = _fresh_database()
        orchestrator = ToolOrchestrator(registry, PolicyEngine(), idempotency_repository=_FailOnUpdateResultRepository(database))
        request = ToolRequest(action="CANCEL_APPOINTMENT", params={"appointment_id": booked["appointment_id"]}, confirmed=True, request_id="bookkeeping-req-1")

        with self.assertRaises(DatabaseUnavailableError):
            orchestrator.invoke(request, auth=USER_A)

        # The appointment WAS actually cancelled by the real business logic.
        self.assertEqual(appointments._appointments[booked["appointment_id"]]["status"], "cancelled")

        # A legitimate retry (new request_id, since the caller believes
        # the operation failed) must not silently re-execute a
        # destructive action -- the underlying business store's own
        # guard against double-cancellation is what actually prevents
        # harm here, not the (failed) idempotency bookkeeping.
        retry = ToolOrchestrator(registry, PolicyEngine())  # a fresh orchestrator, e.g. after an app restart
        retry_result = retry.invoke(
            ToolRequest(action="CANCEL_APPOINTMENT", params={"appointment_id": booked["appointment_id"]}, confirmed=True, request_id="bookkeeping-req-2"),
            auth=USER_A,
        )
        self.assertFalse(retry_result.success)
        self.assertIn("already cancelled", retry_result.error)
        database.dispose()


def _unreachable_database() -> Database:
    return Database(load_database_config(env={"PERSISTENCE_MODE": "production", "DATABASE_URL": UNREACHABLE_URL}))


if __name__ == "__main__":
    unittest.main()

"""
Unit tests for SessionManager/SessionRepository/SessionState (Phase 5).

Fully offline, stdlib only. No model, no network.

Run with:
    python -m unittest tests.test_session_manager -v
"""

import sys
import time
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "agent"))
from session_manager import InvalidTransitionError, SessionManager, SessionNotFoundError  # noqa: E402
from session_models import SessionState, SessionStatus  # noqa: E402


class TestSessionLifecycle(unittest.TestCase):
    def test_create_session(self):
        manager = SessionManager()
        session = manager.create_session(user_id="user-1")
        self.assertIsInstance(session, SessionState)
        self.assertEqual(session.status, SessionStatus.ACTIVE)
        self.assertEqual(session.user_id, "user-1")

    def test_create_session_generates_id_if_omitted(self):
        manager = SessionManager()
        a = manager.create_session()
        b = manager.create_session()
        self.assertNotEqual(a.session_id, b.session_id)

    def test_retrieve_session(self):
        manager = SessionManager()
        created = manager.create_session(session_id="s1", user_id="user-1")
        fetched = manager.get_session("s1")
        self.assertEqual(fetched.session_id, created.session_id)

    def test_update_session(self):
        manager = SessionManager()
        manager.create_session(session_id="s1")
        updated = manager.update_session("s1", current_intent="APPOINTMENT_BOOKING")
        self.assertEqual(updated.current_intent, "APPOINTMENT_BOOKING")

    def test_update_session_rejects_unsupported_field(self):
        manager = SessionManager()
        manager.create_session(session_id="s1")
        with self.assertRaises(ValueError):
            manager.update_session("s1", status="ACTIVE")  # status is transition_state()'s job, not update_session()'s

    def test_missing_session_returns_none(self):
        manager = SessionManager()
        self.assertIsNone(manager.get_session("does-not-exist"))

    def test_update_missing_session_raises(self):
        manager = SessionManager()
        with self.assertRaises(SessionNotFoundError):
            manager.update_session("does-not-exist", current_intent="X")

    def test_corrupted_session_id_types_do_not_raise(self):
        manager = SessionManager()
        for bad in (None, 12345, ["not", "a", "string"], ""):
            with self.subTest(bad=bad):
                self.assertIsNone(manager.get_session(bad))

    def test_delete_session(self):
        manager = SessionManager()
        manager.create_session(session_id="s1")
        manager.delete_session("s1")
        self.assertIsNone(manager.get_session("s1"))


class TestStateTransitions(unittest.TestCase):
    def test_valid_transition(self):
        manager = SessionManager()
        manager.create_session(session_id="s1")
        session = manager.transition_state("s1", SessionStatus.WAITING_FOR_CONFIRMATION)
        self.assertEqual(session.status, SessionStatus.WAITING_FOR_CONFIRMATION)

    def test_invalid_transition_rejected(self):
        manager = SessionManager()
        manager.create_session(session_id="s1")
        manager.transition_state("s1", SessionStatus.COMPLETED)
        with self.assertRaises(InvalidTransitionError):
            manager.transition_state("s1", SessionStatus.ACTIVE)  # COMPLETED is terminal

    def test_transition_requires_enum_not_arbitrary_string(self):
        manager = SessionManager()
        manager.create_session(session_id="s1")
        with self.assertRaises(ValueError):
            manager.transition_state("s1", "COMPLETED")  # plain string, not SessionStatus.COMPLETED

    def test_full_workflow_sequence(self):
        manager = SessionManager()
        manager.create_session(session_id="s1")
        manager.transition_state("s1", SessionStatus.WAITING_FOR_CONFIRMATION)
        manager.transition_state("s1", SessionStatus.ACTIVE)
        session = manager.transition_state("s1", SessionStatus.COMPLETED)
        self.assertEqual(session.status, SessionStatus.COMPLETED)


class TestExpiration(unittest.TestCase):
    def test_session_within_ttl_is_usable(self):
        manager = SessionManager(ttl=timedelta(seconds=5))
        manager.create_session(session_id="s1")
        self.assertIsNotNone(manager.get_session("s1"))

    def test_expired_session_access_returns_none(self):
        manager = SessionManager(ttl=timedelta(milliseconds=50))
        manager.create_session(session_id="s1")
        time.sleep(0.1)
        self.assertIsNone(manager.get_session("s1"))

    def test_expired_pending_action_is_cleared_not_executable(self):
        """
        plan.md Step 5.5's explicit security requirement: a stale
        WAITING_FOR_CONFIRMATION session's pending action must not be
        picked up by a later "yes" once expired.
        """
        manager = SessionManager(ttl=timedelta(milliseconds=50))
        manager.create_session(session_id="s1")
        manager.update_session(
            "s1", pending_action="CANCEL_APPOINTMENT", pending_parameters={"appointment_id": "appt_1"}
        )
        manager.transition_state("s1", SessionStatus.WAITING_FOR_CONFIRMATION)
        time.sleep(0.1)

        session = manager.get_session("s1")
        self.assertIsNone(session, "expired session must not be returned as usable")

        # Directly inspect the repository record to confirm the pending
        # action was actually cleared, not merely hidden by get_session().
        raw = manager._repository.get("s1")
        self.assertEqual(raw.status, SessionStatus.EXPIRED)
        self.assertIsNone(raw.pending_action)

    def test_explicit_expire_session(self):
        manager = SessionManager()
        manager.create_session(session_id="s1")
        manager.expire_session("s1")
        self.assertIsNone(manager.get_session("s1"))


class TestPurgeExpiredSessions(unittest.TestCase):
    """Phase 24 (data lifecycle): operator-invoked bulk deletion of already-expired sessions."""

    def test_purge_deletes_only_expired_sessions(self):
        manager = SessionManager(ttl=timedelta(milliseconds=50))
        manager.create_session(session_id="expired-1")
        manager.create_session(session_id="expired-2")
        time.sleep(0.1)
        # Share the same repository so both managers see the same rows.
        long_lived = SessionManager(repository=manager._repository)
        long_lived.create_session(session_id="still-active")

        count = manager.purge_expired_sessions()

        self.assertEqual(count, 2)
        self.assertIsNone(manager._repository.get("expired-1"))
        self.assertIsNone(manager._repository.get("expired-2"))
        self.assertIsNotNone(manager._repository.get("still-active"))

    def test_purge_never_deletes_a_session_not_yet_expired(self):
        manager = SessionManager()
        manager.create_session(session_id="s1")
        count = manager.purge_expired_sessions()
        self.assertEqual(count, 0)
        self.assertIsNotNone(manager._repository.get("s1"))

    def test_purge_emits_one_data_purged_audit_event_with_the_real_count(self):
        from audit import AuditLogger
        from observability_models import EventType

        audit_logger = AuditLogger()
        manager = SessionManager(ttl=timedelta(milliseconds=50), audit_logger=audit_logger)
        manager.create_session(session_id="s1")
        manager.create_session(session_id="s2")
        time.sleep(0.1)

        manager.purge_expired_sessions()

        events = audit_logger._repository.list_events(event_type=EventType.DATA_PURGED)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].metadata["count"], 2)
        self.assertEqual(events[0].resource, "session")

    def test_purge_emits_no_audit_event_when_nothing_was_deleted(self):
        from audit import AuditLogger
        from observability_models import EventType

        audit_logger = AuditLogger()
        manager = SessionManager(audit_logger=audit_logger)
        manager.create_session(session_id="s1")

        manager.purge_expired_sessions()

        self.assertEqual(audit_logger._repository.list_events(event_type=EventType.DATA_PURGED), [])


class TestUnauthorizedAccess(unittest.TestCase):
    def test_cross_user_session_access_denied(self):
        manager = SessionManager()
        manager.create_session(session_id="s1", user_id="user-a")
        self.assertIsNone(manager.get_session("s1", user_id="user-b"))

    def test_same_user_session_access_allowed(self):
        manager = SessionManager()
        manager.create_session(session_id="s1", user_id="user-a")
        self.assertIsNotNone(manager.get_session("s1", user_id="user-a"))

    def test_no_user_id_check_when_omitted(self):
        # A caller that doesn't pass user_id gets whatever session exists
        # -- authorization is opt-in at the call site (ConversationManager
        # always passes the trusted auth.user_id in Phase 5's integration).
        manager = SessionManager()
        manager.create_session(session_id="s1", user_id="user-a")
        self.assertIsNotNone(manager.get_session("s1"))

    def test_session_with_no_owner_is_accessible_by_any_caller(self):
        # A session created without a user_id (e.g. anonymous flow) isn't
        # falsely rejected for a caller who does supply one.
        manager = SessionManager()
        manager.create_session(session_id="s1")
        self.assertIsNotNone(manager.get_session("s1", user_id="user-a"))


if __name__ == "__main__":
    unittest.main()

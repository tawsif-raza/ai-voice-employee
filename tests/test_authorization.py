"""
Unit and integration tests for the authorization boundary (Phase 7):
PolicyEngine.evaluate_authorization(), its ToolOrchestrator integration
(resource ownership), and the mandatory Step 7.15 LLM/client identity-
spoofing regression tests.

Fully offline -- only needs PyYAML (via PolicyEngine's config loading).

Run with:
    python -m unittest tests.test_authorization -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "agent"))
from action_models import ActionProposal, AuthContext, ToolRequest  # noqa: E402
from identity import Role, permissions_for_roles  # noqa: E402
from mock_tools import MockAppointmentStore, build_default_tool_registry  # noqa: E402
from policy_engine import Action, PolicyEngine  # noqa: E402
from tool_orchestrator import ToolOrchestrator  # noqa: E402


def _user(user_id: str, roles=(Role.USER,)) -> AuthContext:
    return AuthContext(
        user_id=user_id, authenticated=True, roles=tuple(r.value for r in roles),
        permissions=permissions_for_roles(roles), authentication_method="test",
    )


USER_A = _user("user-a")
USER_B = _user("user-b")
ADMIN = _user("admin-1", roles=(Role.ADMIN,))
UNAUTHENTICATED = AuthContext(user_id="anon", authenticated=False)


class TestEvaluateAuthorization(unittest.TestCase):
    def test_unauthenticated_identity_denied(self):
        engine = PolicyEngine()
        decision = engine.evaluate_authorization(UNAUTHENTICATED, "BOOK_APPOINTMENT")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.rule, "AUTHENTICATION_REQUIRED")

    def test_none_identity_denied(self):
        engine = PolicyEngine()
        decision = engine.evaluate_authorization(None, "BOOK_APPOINTMENT")
        self.assertFalse(decision.allowed)

    def test_valid_permission_allowed(self):
        engine = PolicyEngine()
        decision = engine.evaluate_authorization(USER_A, "BOOK_APPOINTMENT")
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.action, Action.ALLOW)

    def test_missing_permission_denied(self):
        engine = PolicyEngine()
        decision = engine.evaluate_authorization(USER_A, "ADMIN_OPERATIONS")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.rule, "INSUFFICIENT_PERMISSIONS")

    def test_admin_has_admin_operations_permission(self):
        engine = PolicyEngine()
        decision = engine.evaluate_authorization(ADMIN, "ADMIN_OPERATIONS")
        self.assertTrue(decision.allowed)

    def test_own_resource_allowed(self):
        engine = PolicyEngine()
        decision = engine.evaluate_authorization(USER_A, "CANCEL_APPOINTMENT", resource_owner_user_id="user-a")
        self.assertTrue(decision.allowed)

    def test_another_users_resource_denied(self):
        engine = PolicyEngine()
        decision = engine.evaluate_authorization(USER_A, "CANCEL_APPOINTMENT", resource_owner_user_id="user-b")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.rule, "NOT_RESOURCE_OWNER")

    def test_admin_can_access_another_users_resource(self):
        engine = PolicyEngine()
        decision = engine.evaluate_authorization(ADMIN, "CANCEL_APPOINTMENT", resource_owner_user_id="user-a")
        self.assertTrue(decision.allowed)

    def test_no_resource_owner_specified_skips_ownership_check(self):
        engine = PolicyEngine()
        decision = engine.evaluate_authorization(USER_A, "BOOK_APPOINTMENT", resource_owner_user_id=None)
        self.assertTrue(decision.allowed)

    def test_least_privilege_user_lacks_admin_operations_by_default(self):
        engine = PolicyEngine()
        self.assertFalse(USER_A.has_permission("ADMIN_OPERATIONS"))
        decision = engine.evaluate_authorization(USER_A, "ADMIN_OPERATIONS")
        self.assertFalse(decision.allowed)


class TestAuthorizationPrecedence(unittest.TestCase):
    def test_authorization_denial_wins_over_tool_and_confirmation_allow(self):
        from policy_engine import PRECEDENCE
        self.assertIn("authorization", PRECEDENCE)
        self.assertLess(PRECEDENCE.index("authorization"), PRECEDENCE.index("tool"))
        self.assertLess(PRECEDENCE.index("authorization"), PRECEDENCE.index("confirmation"))


class TestToolOrchestratorResourceOwnership(unittest.TestCase):
    """
    Step 7.6/7.9's worked example, exercised through the real
    ToolOrchestrator + PolicyEngine stack: USER + CANCEL_APPOINTMENT +
    own appointment -> ALLOW; USER + CANCEL_APPOINTMENT + another user's
    appointment -> DENY.
    """

    def test_cancel_own_appointment_allowed(self):
        store = MockAppointmentStore()
        booked = store.book({"doctor_id": "d1", "date": "2026-08-18", "time": "17:00", "owner_user_id": "user-a"})
        orchestrator = ToolOrchestrator(build_default_tool_registry(appointment_store=store), PolicyEngine())

        request = ToolRequest(
            action="CANCEL_APPOINTMENT", params={"appointment_id": booked["appointment_id"]},
            confirmed=True, resource_owner_user_id=store.get_owner(booked["appointment_id"]),
        )
        result = orchestrator.invoke(request, auth=USER_A)
        self.assertTrue(result.success)

    def test_cancel_another_users_appointment_denied(self):
        store = MockAppointmentStore()
        booked = store.book({"doctor_id": "d1", "date": "2026-08-18", "time": "17:00", "owner_user_id": "user-a"})
        orchestrator = ToolOrchestrator(build_default_tool_registry(appointment_store=store), PolicyEngine())

        request = ToolRequest(
            action="CANCEL_APPOINTMENT", params={"appointment_id": booked["appointment_id"]},
            confirmed=True, resource_owner_user_id=store.get_owner(booked["appointment_id"]),
        )
        result = orchestrator.invoke(request, auth=USER_B)  # different user
        self.assertFalse(result.success)
        self.assertEqual(result.error, "NOT_RESOURCE_OWNER")

    def test_admin_can_cancel_another_users_appointment(self):
        store = MockAppointmentStore()
        booked = store.book({"doctor_id": "d1", "date": "2026-08-18", "time": "17:00", "owner_user_id": "user-a"})
        orchestrator = ToolOrchestrator(build_default_tool_registry(appointment_store=store), PolicyEngine())

        request = ToolRequest(
            action="CANCEL_APPOINTMENT", params={"appointment_id": booked["appointment_id"]},
            confirmed=True, resource_owner_user_id=store.get_owner(booked["appointment_id"]),
        )
        result = orchestrator.invoke(request, auth=ADMIN)
        self.assertTrue(result.success)

    def test_unauthenticated_user_cannot_read_order(self):
        orchestrator = ToolOrchestrator(build_default_tool_registry(), PolicyEngine())
        request = ToolRequest(action="ORDER_LOOKUP", params={"order_id": "order_1001"}, confirmed=True)
        result = orchestrator.invoke(request, auth=UNAUTHENTICATED)
        self.assertFalse(result.success)


class TestLLMTrustBoundaryIdentitySpoofing(unittest.TestCase):
    """Step 7.15 — mandatory security regression tests."""

    def test_attack_1_fake_identity_in_proposal_is_ignored(self):
        # "LLM output": {"user_id": "admin"} -- ActionProposal has no
        # `user_id` field at all; even embedded in `parameters`, it's
        # just an unexpected/ignored key, never read as identity.
        orchestrator = ToolOrchestrator(build_default_tool_registry(), PolicyEngine())
        proposal = ActionProposal(action="ORDER_LOOKUP", parameters={"order_id": "order_1001", "user_id": "admin"})
        with self.assertRaises(Exception):
            orchestrator.validate_proposal(proposal)  # "user_id" is an unexpected parameter -> rejected

        # Even if somehow smuggled through as an ordinary param the
        # schema allowed, the identity used for authorization is always
        # the `auth` argument to invoke() -- never anything from params.
        request = ToolRequest(action="ORDER_LOOKUP", params={"order_id": "order_1001"}, confirmed=True)
        result = orchestrator.invoke(request, auth=USER_A)  # real, trusted identity
        self.assertTrue(result.success)
        self.assertEqual(USER_A.user_id, "user-a")  # not "admin" -- the claim had no effect

    def test_attack_2_fake_authenticated_flag_is_ignored(self):
        # "LLM output": {"authenticated": true}; actual state: authenticated=False.
        engine = PolicyEngine()
        forged_claim = {"authenticated": True}  # never passed to evaluate_authorization
        decision = engine.evaluate_authorization(UNAUTHENTICATED, "BOOK_APPOINTMENT")
        self.assertFalse(decision.allowed)
        self.assertIsInstance(forged_claim, dict)  # existed, had zero effect

    def test_attack_3_fake_role_is_ignored(self):
        # "LLM output": {"role": "ADMIN"}; actual role: USER.
        engine = PolicyEngine()
        decision = engine.evaluate_authorization(USER_A, "ADMIN_OPERATIONS")
        self.assertFalse(decision.allowed)
        self.assertNotIn(Role.ADMIN.value, USER_A.roles)

    def test_attack_4_fake_permission_grants_nothing(self):
        # "LLM output": {"permissions": ["ADMIN_OPERATIONS"]} -- evaluate_authorization()
        # only ever reads `identity.permissions` from the trusted AuthContext object,
        # never from any dict a caller might construct to look like one.
        engine = PolicyEngine()
        forged_permissions_claim = {"permissions": ["ADMIN_OPERATIONS"]}
        decision = engine.evaluate_authorization(USER_A, "ADMIN_OPERATIONS")
        self.assertFalse(decision.allowed)
        self.assertNotIn("ADMIN_OPERATIONS", USER_A.permissions)
        self.assertIsInstance(forged_permissions_claim, dict)  # had zero effect

    def test_attack_5_cross_user_memory_access_denied(self):
        from memory_manager import MemoryManager
        from memory_models import MemoryCategory

        manager = MemoryManager(PolicyEngine())
        record = manager.propose_memory(
            user_id="user-a", category=MemoryCategory.PREFERENCE, key="preferred_clinic",
            value="Downtown", source="user_explicit",
        )
        manager.persist_memory(record)
        # "LLM requests another user's memory" -- MemoryManager's own
        # user_id-scoped read path (Phase 5, unmodified) denies it.
        self.assertEqual(manager.list_allowed_memory("user-b"), [])
        self.assertFalse(manager.remove_memory(record.id, user_id="user-b"))

    def test_attack_6_cross_user_session_access_denied(self):
        from session_manager import SessionManager

        manager = SessionManager()
        manager.create_session(session_id="s1", user_id="user-a")
        # "LLM requests another user's session" -- SessionManager's own
        # user_id-scoped read path (Phase 5, unmodified) denies it.
        self.assertIsNone(manager.get_session("s1", user_id="user-b"))


if __name__ == "__main__":
    unittest.main()

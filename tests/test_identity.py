"""
Unit tests for the identity/authentication boundary (Phase 7):
src/agent/identity.py, and AuthContext's Phase 7 extensions in
src/agent/action_models.py.

Fully offline, stdlib only.

Run with:
    python -m unittest tests.test_identity -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "agent"))
from action_models import ANONYMOUS_CONTEXT, AuthContext  # noqa: E402
from identity import (  # noqa: E402
    AuthenticationError, AuthenticationProvider, DevelopmentAuthenticationProvider,
    Permission, ROLE_PERMISSIONS, Role, permissions_for_roles,
)


class TestAuthContextExtensions(unittest.TestCase):
    def test_default_context_has_no_permissions(self):
        self.assertEqual(ANONYMOUS_CONTEXT.permissions, ())
        self.assertFalse(ANONYMOUS_CONTEXT.authenticated)

    def test_has_permission(self):
        ctx = AuthContext(user_id="u1", authenticated=True, permissions=("BOOK_APPOINTMENT",))
        self.assertTrue(ctx.has_permission("BOOK_APPOINTMENT"))
        self.assertFalse(ctx.has_permission("ADMIN_OPERATIONS"))

    def test_authentication_method_defaults_to_none(self):
        self.assertEqual(ANONYMOUS_CONTEXT.authentication_method, "none")

    def test_context_is_immutable(self):
        ctx = AuthContext(user_id="u1", authenticated=True)
        with self.assertRaises(Exception):
            ctx.authenticated = False


class TestRolesAndPermissions(unittest.TestCase):
    def test_every_role_has_a_permission_set(self):
        for role in Role:
            self.assertIn(role, ROLE_PERMISSIONS)

    def test_admin_has_admin_operations(self):
        self.assertIn(Permission.ADMIN_OPERATIONS, ROLE_PERMISSIONS[Role.ADMIN])

    def test_user_does_not_have_admin_operations(self):
        self.assertNotIn(Permission.ADMIN_OPERATIONS, ROLE_PERMISSIONS[Role.USER])

    def test_no_do_everything_wildcard_permission(self):
        all_values = {p.value for p in Permission}
        self.assertNotIn("DO_EVERYTHING", all_values)

    def test_permissions_for_roles_is_deterministic_union(self):
        result = permissions_for_roles((Role.USER,))
        self.assertEqual(result, tuple(sorted(result)))  # deterministic ordering
        self.assertIn(Permission.BOOK_APPOINTMENT.value, result)
        self.assertNotIn(Permission.ADMIN_OPERATIONS.value, result)

    def test_permissions_for_roles_empty_for_no_roles(self):
        self.assertEqual(permissions_for_roles(()), ())

    def test_permissions_for_multiple_roles_is_union(self):
        result = permissions_for_roles((Role.USER, Role.ADMIN))
        self.assertIn(Permission.ADMIN_OPERATIONS.value, result)
        self.assertIn(Permission.BOOK_APPOINTMENT.value, result)


class TestDevelopmentAuthenticationProvider(unittest.TestCase):
    def test_valid_user_token_authenticates(self):
        provider = DevelopmentAuthenticationProvider()
        identity = provider.authenticate({"token": "test-user-token"})
        self.assertTrue(identity.authenticated)
        self.assertEqual(identity.user_id, "test-user-1")
        self.assertIn(Role.USER.value, identity.roles)
        self.assertIn(Permission.BOOK_APPOINTMENT.value, identity.permissions)
        self.assertEqual(identity.authentication_method, "development_test_provider")

    def test_valid_admin_token_authenticates_with_admin_permissions(self):
        provider = DevelopmentAuthenticationProvider()
        identity = provider.authenticate({"token": "test-admin-token"})
        self.assertIn(Role.ADMIN.value, identity.roles)
        self.assertIn(Permission.ADMIN_OPERATIONS.value, identity.permissions)

    def test_invalid_token_raises(self):
        provider = DevelopmentAuthenticationProvider()
        with self.assertRaises(AuthenticationError):
            provider.authenticate({"token": "not-a-real-token"})

    def test_missing_token_raises(self):
        provider = DevelopmentAuthenticationProvider()
        with self.assertRaises(AuthenticationError):
            provider.authenticate({})

    def test_malformed_credentials_do_not_crash(self):
        provider = DevelopmentAuthenticationProvider()
        for bad in (None, "not a dict", 12345, ["token", "x"]):
            with self.subTest(bad=bad):
                with self.assertRaises(AuthenticationError):
                    provider.authenticate(bad)

    def test_disabled_provider_always_rejects(self):
        provider = DevelopmentAuthenticationProvider(enabled=False)
        with self.assertRaises(AuthenticationError):
            provider.authenticate({"token": "test-user-token"})  # even a valid token

    def test_get_identity_is_alias_for_authenticate(self):
        provider = DevelopmentAuthenticationProvider()
        a = provider.authenticate({"token": "test-user-token"})
        b = provider.get_identity({"token": "test-user-token"})
        self.assertEqual(a.user_id, b.user_id)

    def test_error_message_never_echoes_submitted_token(self):
        provider = DevelopmentAuthenticationProvider()
        secret_looking_token = "sk-super-secret-value-12345"
        try:
            provider.authenticate({"token": secret_looking_token})
            self.fail("expected AuthenticationError")
        except AuthenticationError as exc:
            self.assertNotIn(secret_looking_token, str(exc))

    def test_abstract_provider_raises_not_implemented(self):
        provider = AuthenticationProvider()
        with self.assertRaises(NotImplementedError):
            provider.authenticate({"token": "x"})


if __name__ == "__main__":
    unittest.main()

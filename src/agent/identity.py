"""
Authentication provider interface, roles, and permissions (Phase 7;
plan.md Steps 7.3, 7.4, 7.5).

Trust boundary: this module is the ONLY place an AuthContext (this
codebase's IdentityContext — see action_models.py's reconciliation note)
is ever constructed from credentials. ConversationManager, ToolOrchestrator,
SessionManager, and MemoryManager all *consume* an already-resolved
AuthContext; none of them call authenticate() themselves (plan.md Step
7.13: "Keep authentication at the API/security boundary" —
src/api/server.py is the only caller of AuthenticationProvider.authenticate()).
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from action_models import AuthContext


class Role(str, Enum):
    USER = "USER"
    STAFF = "STAFF"
    ADMIN = "ADMIN"


class Permission(str, Enum):
    READ_OWN_SESSION = "READ_OWN_SESSION"
    READ_OWN_MEMORY = "READ_OWN_MEMORY"
    WRITE_OWN_MEMORY = "WRITE_OWN_MEMORY"
    BOOK_APPOINTMENT = "BOOK_APPOINTMENT"
    CANCEL_APPOINTMENT = "CANCEL_APPOINTMENT"
    READ_ORDER = "READ_ORDER"
    ADMIN_OPERATIONS = "ADMIN_OPERATIONS"


# Least-privilege role -> permission mapping. STAFF is defined (plan.md's
# requested role) but, per "do not create permissions that are not
# required," has no capability beyond USER today — there is no
# staff-only tool or action anywhere in this system yet. Adding one
# later only requires extending this table, not the role/permission
# taxonomy itself.
ROLE_PERMISSIONS: dict[Role, tuple[Permission, ...]] = {
    Role.USER: (
        Permission.READ_OWN_SESSION,
        Permission.READ_OWN_MEMORY,
        Permission.WRITE_OWN_MEMORY,
        Permission.BOOK_APPOINTMENT,
        Permission.CANCEL_APPOINTMENT,
        Permission.READ_ORDER,
    ),
    Role.STAFF: (
        Permission.READ_OWN_SESSION,
        Permission.READ_OWN_MEMORY,
        Permission.WRITE_OWN_MEMORY,
        Permission.BOOK_APPOINTMENT,
        Permission.CANCEL_APPOINTMENT,
        Permission.READ_ORDER,
    ),
    Role.ADMIN: tuple(Permission),  # every permission, including ADMIN_OPERATIONS
}


def permissions_for_roles(roles: tuple[Role, ...]) -> tuple[str, ...]:
    """Deterministic union of every permission granted by the given roles. No implicit DO_EVERYTHING wildcard."""
    granted: set[str] = set()
    for role in roles:
        for permission in ROLE_PERMISSIONS.get(role, ()):
            granted.add(permission.value)
    return tuple(sorted(granted))


class AuthenticationError(Exception):
    """Raised by AuthenticationProvider.authenticate() on missing/invalid credentials. Never raised with credential material in its message."""


class AuthenticationProvider:
    """
    Abstract authentication boundary (plan.md Step 7.3). FastAPI ->
    AuthenticationProvider -> AuthContext -> ConversationManager — no
    caller downstream of this interface parses credentials itself.
    """

    def authenticate(self, credentials: dict) -> AuthContext:
        raise NotImplementedError

    def get_identity(self, credentials: dict) -> AuthContext:
        """Alias for authenticate() — plan.md Step 7.3's requested second method name for the same resolution."""
        return self.authenticate(credentials)


@dataclass(frozen=True)
class _TestIdentity:
    user_id: str
    roles: tuple[Role, ...]


class DevelopmentAuthenticationProvider(AuthenticationProvider):
    """
    A deterministic, TEST-ONLY authentication provider (plan.md Step 7.4).

    This is explicitly NOT production-grade authentication: it recognizes
    a small, fixed table of non-secret bearer tokens and maps them to
    canned identities (TEST_USER, TEST_ADMIN). There is no password
    hashing, no external identity provider, no token expiry, and no
    cryptographic verification of any kind — it exists solely to let the
    rest of this system's authorization/ownership architecture be built
    and tested against a real AuthenticationProvider implementation
    before a production identity provider (OAuth/OIDC/etc.) is
    integrated, which this phase deliberately does not attempt (plan.md:
    "Do not implement production OAuth/OIDC integration unless an
    existing provider is already configured" — none is).

    `enabled=False` lets a production configuration disable this provider
    outright, so it can never become an accidental production
    authentication mechanism (plan.md's explicit requirement) — see
    src/api/server.py's wiring, which reads this from an environment
    variable defaulting to enabled only in the absence of production
    configuration.
    """

    _TOKENS: dict[str, _TestIdentity] = {
        "test-user-token": _TestIdentity(user_id="test-user-1", roles=(Role.USER,)),
        "test-admin-token": _TestIdentity(user_id="test-admin-1", roles=(Role.ADMIN,)),
    }

    def __init__(self, enabled: bool = True, audit_logger=None, security_detector=None):
        self.enabled = enabled
        # Phase 8, both optional -- None preserves exact Phase 7 behavior.
        # This IS "the authentication boundary" plan.md Step 8.10 says
        # AUTH_SUCCESS/AUTH_FAILURE events must come from -- emitted here,
        # nowhere else, and strictly *after* the real accept/reject
        # decision above/below has already been made.
        self._audit_logger = audit_logger
        self._security_detector = security_detector

    def authenticate(self, credentials: dict, client_identifier: Optional[str] = None) -> AuthContext:
        """
        `client_identifier` (Phase 8, optional) is a safe, non-secret
        reference used only for repeated-failure security-event detection
        (e.g. a caller's IP, supplied by src/api/server.py) — never
        logged as, or treated as, an identity claim.
        """
        safe_actor = client_identifier or "unknown"
        if not self.enabled:
            self._record_failure(safe_actor, "Development authentication provider is disabled.")
            raise AuthenticationError("Development authentication provider is disabled.")
        if not isinstance(credentials, dict):
            self._record_failure(safe_actor, "Invalid credentials format.")
            raise AuthenticationError("Invalid credentials format.")
        token = credentials.get("token")
        if not isinstance(token, str) or token not in self._TOKENS:
            # Deliberately generic -- never echoes the invalid token back
            # (plan.md Step 7.12: never log/expose credential material).
            self._record_failure(safe_actor, "Invalid or missing credentials.")
            raise AuthenticationError("Invalid or missing credentials.")

        identity = self._TOKENS[token]
        role_values = tuple(r.value for r in identity.roles)
        if self._security_detector is not None:
            self._security_detector.reset_auth_failures(safe_actor)
        if self._audit_logger is not None:
            from observability_models import EventType

            self._audit_logger.record(
                EventType.AUTH_SUCCESS,
                outcome="success",
                actor=identity.user_id,
                reason="Credentials accepted by development authentication provider.",
            )
        return AuthContext(
            user_id=identity.user_id,
            authenticated=True,
            roles=role_values,
            permissions=permissions_for_roles(identity.roles),
            authentication_method="development_test_provider",
        )

    def _record_failure(self, actor: str, reason: str) -> None:
        if self._audit_logger is not None:
            from observability_models import EventType

            self._audit_logger.record(EventType.AUTH_FAILURE, outcome="denied", actor=actor, reason=reason)
        if self._security_detector is not None:
            self._security_detector.record_auth_failure(actor)

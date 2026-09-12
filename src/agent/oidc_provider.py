"""
Production OIDC/JWT authentication provider (Phase 9; plan.md Steps
9.2-9.10).

Extends the same `AuthenticationProvider` interface `identity.py`'s
`DevelopmentAuthenticationProvider` already implements -- this is the
*second* implementation of that interface, not a second authentication
system. `ConversationManager`/`ToolOrchestrator`/`SessionManager`/
`MemoryManager` are unaware which provider produced a given `AuthContext`
and never change behavior based on `authentication_method`.

Trust boundary (plan.md's non-negotiable principles): this module
answers WHO IS THIS?, never WHAT CAN THEY DO? -- role/permission mapping
here only maps an already-verified JWT claim to this application's
existing `Role` taxonomy (`identity.py`); `PolicyEngine` remains the
sole authority for what a role/permission is allowed to do.

Verification uses PyJWT (a maintained security library) exclusively --
this module never implements signature verification, base64/JSON
parsing of the token, or key-matching logic itself (plan.md Step 9.5:
"Do NOT write cryptographic verification manually"). JWKS fetching/
caching/kid-resolution is `jwt.PyJWKClient`'s (plan.md Step 9.6), not
reimplemented here; `signing_key_resolver` is an injection point purely
so tests can supply a static, offline key set without a real HTTPS JWKS
endpoint -- production callers never need to touch it.

Fail-closed by construction: `load_oidc_config()` raises
`AuthConfigurationError` immediately if required configuration is
missing, rather than returning a permissive default silently accepted
by the rest of the system -- this is deliberately DIFFERENT from
PolicyEngine's/PrivacyService's config loaders, which fall back to safe
built-in defaults when their YAML is missing. Authentication config is
not "safe to default" the same way: a missing issuer/audience/jwks_url
must stop the provider from being constructed at all (plan.md Principle
32; wired into src/api/server.py's startup so production never silently
runs with broken/absent authentication).
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import jwt
import yaml
from identity import AuthContext, AuthenticationError, AuthenticationProvider, Role, permissions_for_roles

_CONFIG_PATH = Path(__file__).resolve().parents[2] / "configs" / "auth.yaml"

_DEFAULT_ROLE_MAPPING: dict[str, Role] = {"admin": Role.ADMIN, "staff": Role.STAFF, "user": Role.USER}


class AuthConfigurationError(Exception):
    """
    Raised by load_oidc_config()/OIDCAuthenticationProvider construction
    when required production authentication configuration is missing or
    invalid. Deliberately NOT a subclass of AuthenticationError -- this
    is a startup/configuration failure, not a per-request authentication
    decision, and must never be caught by request-handling code and
    silently treated as "deny this one request."
    """


@dataclass(frozen=True)
class OIDCConfig:
    issuer: str
    audience: str
    jwks_url: str
    algorithms: tuple[str, ...] = ("RS256",)
    clock_skew_seconds: int = 60
    required_claims: tuple[str, ...] = ("sub", "iss", "aud", "exp")
    role_claim: str = "roles"
    role_mapping: dict = field(default_factory=lambda: dict(_DEFAULT_ROLE_MAPPING))
    jwks_timeout_seconds: float = 10.0


def load_oidc_config(config_path: Optional[str] = None) -> OIDCConfig:
    """
    Loads configs/auth.yaml, then applies environment-variable overrides
    for the three deployment-specific fields (OIDC_ISSUER_URL/
    OIDC_AUDIENCE/OIDC_JWKS_URL take priority over the YAML file, same
    convention as src/api/server.py's MERGED_MODEL_DIR/BASE_MODEL_NAME).
    Raises AuthConfigurationError if, after that, any of the three is
    still missing, or if the configured algorithm list includes "none"
    (defense-in-depth alongside PyJWT's own algorithm allow-listing --
    plan.md Principle 17: "Never accept an algorithm downgrade").
    """
    path = Path(config_path) if config_path else _CONFIG_PATH
    data: dict = {}
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    auth_cfg = data.get("authentication", {}) or {}

    issuer = os.environ.get("OIDC_ISSUER_URL") or auth_cfg.get("issuer_url")
    audience = os.environ.get("OIDC_AUDIENCE") or auth_cfg.get("audience")
    jwks_url = os.environ.get("OIDC_JWKS_URL") or auth_cfg.get("jwks_url")

    missing = [
        name for name, value in (("issuer_url", issuer), ("audience", audience), ("jwks_url", jwks_url)) if not value
    ]
    if missing:
        raise AuthConfigurationError(f"Missing required OIDC configuration: {', '.join(missing)}")

    algorithms = tuple(auth_cfg.get("algorithms") or ("RS256",))
    if any(a.strip().lower() == "none" for a in algorithms):
        raise AuthConfigurationError("Configured algorithm list must not include 'none'.")
    if not algorithms:
        raise AuthConfigurationError("At least one signing algorithm must be configured.")

    role_mapping_cfg = auth_cfg.get("role_mapping") or {}
    role_mapping: dict[str, Role] = {}
    for claim_value, role_name in role_mapping_cfg.items():
        try:
            role_mapping[claim_value] = Role(role_name)
        except ValueError:
            raise AuthConfigurationError(f"Unknown role in role_mapping: '{role_name}'")
    if not role_mapping:
        role_mapping = dict(_DEFAULT_ROLE_MAPPING)

    required_claims = tuple(auth_cfg.get("required_claims") or ("sub", "iss", "aud", "exp"))
    clock_skew = int(auth_cfg.get("clock_skew_seconds", 60))
    if clock_skew < 0:
        raise AuthConfigurationError("clock_skew_seconds must not be negative.")

    return OIDCConfig(
        issuer=issuer,
        audience=audience,
        jwks_url=jwks_url,
        algorithms=algorithms,
        clock_skew_seconds=clock_skew,
        required_claims=required_claims,
        role_claim=auth_cfg.get("role_claim", "roles"),
        role_mapping=role_mapping,
        jwks_timeout_seconds=float(auth_cfg.get("jwks_timeout_seconds", 10.0)),
    )


class OIDCAuthenticationProvider(AuthenticationProvider):
    """
    Authorization header -> Bearer token -> JWT signature/claims
    validation (PyJWT) -> trusted AuthContext. Every failure path denies
    with the same generic client-facing message identity.py's
    DevelopmentAuthenticationProvider already uses (plan.md Step 7.12/
    9.25/9.26: never echo token material or internal validation detail
    to the caller) while recording a specific, safe `reason` string
    internally via `audit_logger`/`security_detector`, the same pattern
    DevelopmentAuthenticationProvider's `_record_failure()` uses -- here
    named `_deny()` and additionally raising itself (rather than the
    caller raising afterward) purely to avoid repeating `raise
    AuthenticationError(...)` at every one of this method's ~10 denial
    points.
    """

    def __init__(
        self,
        config: OIDCConfig,
        signing_key_resolver: Optional[Callable[[str], object]] = None,
        audit_logger=None,
        security_detector=None,
    ):
        self._config = config
        if signing_key_resolver is not None:
            self._signing_key_resolver = signing_key_resolver
        else:
            # Deferred import: jwt.PyJWKClient performs no network I/O at
            # construction time, only on first get_signing_key_from_jwt()
            # call (with its own caching -- plan.md Step 9.6), so building
            # it eagerly here is safe and keeps this provider's own
            # constructor free of network calls.
            resolver = jwt.PyJWKClient(config.jwks_url, cache_jwk_set=True, timeout=config.jwks_timeout_seconds)
            self._signing_key_resolver = resolver.get_signing_key_from_jwt
        self._audit_logger = audit_logger
        self._security_detector = security_detector

    def authenticate(self, credentials: dict, client_identifier: Optional[str] = None) -> AuthContext:
        safe_actor = client_identifier or "unknown"

        if not isinstance(credentials, dict):
            self._deny(safe_actor, "Invalid credentials format.")
        token = credentials.get("token")
        if not isinstance(token, str) or not token.strip():
            self._deny(safe_actor, "Missing bearer token.")

        try:
            signing_key = self._signing_key_resolver(token)
        except jwt.PyJWKClientError:
            self._deny(safe_actor, "Unable to resolve a signing key for this token.")
        except jwt.DecodeError:
            self._deny(safe_actor, "Malformed token.")
        except Exception:
            # JWKS unavailable/malformed, or any other unexpected key-
            # resolution failure -- plan.md Step 9.6: "If JWKS cannot be
            # safely resolved, authentication must fail closed."
            self._deny(safe_actor, "Signing key resolution failed.")

        key = signing_key.key if hasattr(signing_key, "key") else signing_key

        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=list(self._config.algorithms),
                audience=self._config.audience,
                issuer=self._config.issuer,
                leeway=self._config.clock_skew_seconds,
                options={"require": list(self._config.required_claims)},
            )
        except jwt.ExpiredSignatureError:
            self._deny(safe_actor, "Token expired.")
        except jwt.ImmatureSignatureError:
            self._deny(safe_actor, "Token not yet valid (nbf).")
        except jwt.InvalidIssuerError:
            self._deny(safe_actor, "Invalid issuer.")
        except jwt.InvalidAudienceError:
            self._deny(safe_actor, "Invalid audience.")
        except jwt.InvalidAlgorithmError:
            self._deny(safe_actor, "Unsupported or disallowed algorithm.")
        except jwt.InvalidSignatureError:
            self._deny(safe_actor, "Invalid signature.")
        except jwt.MissingRequiredClaimError as exc:
            self._deny(safe_actor, f"Missing required claim: {exc}.")
        except jwt.PyJWTError:
            self._deny(safe_actor, "Token failed validation.")

        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject.strip():
            self._deny(safe_actor, "Missing subject claim.")

        roles = self._map_roles(claims)
        if self._security_detector is not None:
            self._security_detector.reset_auth_failures(safe_actor)
        if self._audit_logger is not None:
            from observability_models import EventType

            self._audit_logger.record(
                EventType.AUTH_SUCCESS,
                outcome="success",
                actor=subject,
                reason="OIDC token accepted.",
            )
        return AuthContext(
            user_id=subject,
            authenticated=True,
            roles=tuple(r.value for r in roles),
            permissions=permissions_for_roles(roles),
            authentication_method="oidc",
        )

    def _map_roles(self, claims: dict) -> tuple[Role, ...]:
        """
        Maps only the explicitly configured role claim (plan.md Step
        9.9/9.10) -- an unconfigured claim name or an unmapped claim
        value contributes nothing and this identity falls back to the
        least-privilege USER role, never an implicit elevated one.
        """
        raw = claims.get(self._config.role_claim)
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            return (Role.USER,)
        mapped = tuple(self._config.role_mapping[value] for value in raw if value in self._config.role_mapping)
        return mapped or (Role.USER,)

    def _deny(self, actor: str, reason: str) -> None:
        if self._audit_logger is not None:
            from observability_models import EventType

            self._audit_logger.record(EventType.AUTH_FAILURE, outcome="denied", actor=actor, reason=reason)
        if self._security_detector is not None:
            self._security_detector.record_auth_failure(actor)
        raise AuthenticationError("Invalid or missing credentials.")

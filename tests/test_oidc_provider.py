"""
Unit and security tests for the production OIDC/JWT authentication
provider (Phase 9): src/agent/oidc_provider.py.

Uses PyJWT + a locally-generated RSA keypair -- no real network call, no
real identity provider. A `_StaticKeyResolver` test double mirrors
jwt.PyJWKClient's `get_signing_key_from_jwt(token) -> object with .key`
interface exactly, so these tests exercise the same code path
OIDCAuthenticationProvider uses against a real JWKS endpoint, just
without the HTTP round-trip.

Run with:
    python -m unittest tests.test_oidc_provider -v
"""

import sys
import time
import unittest
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "agent"))
from audit import AuditLogger, AuditRepository, SecurityEventDetector  # noqa: E402
from identity import AuthenticationError, Role  # noqa: E402
from observability_models import EventType  # noqa: E402
from oidc_provider import AuthConfigurationError, OIDCAuthenticationProvider, OIDCConfig, load_oidc_config  # noqa: E402

ISSUER = "https://issuer.example.test/"
AUDIENCE = "test-api"


def _generate_keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return private_pem, key.public_key()


_PRIVATE_KEY, _PUBLIC_KEY = _generate_keypair()
_OTHER_PRIVATE_KEY, _OTHER_PUBLIC_KEY = _generate_keypair()
_KID = "test-kid-1"


class _StaticKeyResolver:
    """Test double for jwt.PyJWKClient -- same `get_signing_key_from_jwt(token) -> object with .key` contract, no network I/O."""

    class _Key:
        def __init__(self, key):
            self.key = key

    def __init__(self, keys_by_kid: dict):
        self._keys = keys_by_kid

    def get_signing_key_from_jwt(self, token):
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")
        if kid not in self._keys:
            raise jwt.PyJWKClientError(f"Unable to find a signing key that matches: '{kid}'")
        return self._Key(self._keys[kid])


def _resolver(keys_by_kid=None):
    return _StaticKeyResolver(keys_by_kid or {_KID: _PUBLIC_KEY}).get_signing_key_from_jwt


def _make_token(
    *,
    private_key=_PRIVATE_KEY,
    kid=_KID,
    sub="user-123",
    iss=ISSUER,
    aud=AUDIENCE,
    exp_delta=3600,
    iat_delta=0,
    nbf_delta=None,
    roles=None,
    algorithm="RS256",
    extra_claims=None,
):
    now = int(time.time())
    claims = {"sub": sub, "iss": iss, "aud": aud, "exp": now + exp_delta, "iat": now + iat_delta}
    if nbf_delta is not None:
        claims["nbf"] = now + nbf_delta
    if roles is not None:
        claims["roles"] = roles
    if extra_claims:
        claims.update(extra_claims)
    if sub is None:
        claims.pop("sub", None)
    if iss is None:
        claims.pop("iss", None)
    if aud is None:
        claims.pop("aud", None)
    headers = {"kid": kid} if kid is not None else {}
    return jwt.encode(claims, private_key, algorithm=algorithm, headers=headers)


def _config(**overrides) -> OIDCConfig:
    defaults = dict(
        issuer=ISSUER,
        audience=AUDIENCE,
        algorithms=("RS256",),
        clock_skew_seconds=60,
        jwks_url="https://issuer.example.test/jwks.json",
    )
    defaults.update(overrides)
    return OIDCConfig(**defaults)


def _provider(config=None, keys_by_kid=None, **kwargs) -> OIDCAuthenticationProvider:
    return OIDCAuthenticationProvider(config or _config(), signing_key_resolver=_resolver(keys_by_kid), **kwargs)


class TestValidToken(unittest.TestCase):
    def test_valid_token_authenticates(self):
        provider = _provider()
        ctx = provider.authenticate({"token": _make_token()})
        self.assertTrue(ctx.authenticated)
        self.assertEqual(ctx.user_id, "user-123")
        self.assertEqual(ctx.authentication_method, "oidc")

    def test_no_role_claim_defaults_to_user_role(self):
        provider = _provider()
        ctx = provider.authenticate({"token": _make_token()})
        self.assertEqual(ctx.roles, (Role.USER.value,))

    def test_admin_role_claim_maps_to_admin_role(self):
        provider = _provider()
        ctx = provider.authenticate({"token": _make_token(roles=["admin"])})
        self.assertEqual(ctx.roles, (Role.ADMIN.value,))
        self.assertIn("ADMIN_OPERATIONS", ctx.permissions)

    def test_unmapped_role_claim_falls_back_to_user(self):
        provider = _provider()
        ctx = provider.authenticate({"token": _make_token(roles=["superhacker"])})
        self.assertEqual(ctx.roles, (Role.USER.value,))


class TestTokenValidationFailures(unittest.TestCase):
    def test_wrong_issuer_denied(self):
        provider = _provider()
        with self.assertRaises(AuthenticationError):
            provider.authenticate({"token": _make_token(iss="https://wrong-issuer.example/")})

    def test_wrong_audience_denied(self):
        provider = _provider()
        with self.assertRaises(AuthenticationError):
            provider.authenticate({"token": _make_token(aud="wrong-audience")})

    def test_expired_token_denied(self):
        provider = _provider()
        with self.assertRaises(AuthenticationError):
            provider.authenticate({"token": _make_token(exp_delta=-3600)})

    def test_not_yet_valid_token_denied(self):
        provider = _provider()
        with self.assertRaises(AuthenticationError):
            provider.authenticate({"token": _make_token(nbf_delta=3600)})

    def test_missing_subject_denied(self):
        provider = _provider()
        with self.assertRaises(AuthenticationError):
            provider.authenticate({"token": _make_token(sub=None)})

    def test_invalid_signature_denied(self):
        """A token signed with a DIFFERENT private key than the one the resolver serves for this kid must be rejected."""
        provider = _provider()
        forged = _make_token(private_key=_OTHER_PRIVATE_KEY)
        with self.assertRaises(AuthenticationError):
            provider.authenticate({"token": forged})

    def test_unknown_kid_denied(self):
        provider = _provider()
        with self.assertRaises(AuthenticationError):
            provider.authenticate({"token": _make_token(kid="never-registered-kid")})

    def test_key_resolution_exception_denied(self):
        def _raising_resolver(token):
            raise RuntimeError("JWKS endpoint unreachable")

        provider = OIDCAuthenticationProvider(_config(), signing_key_resolver=_raising_resolver)
        with self.assertRaises(AuthenticationError):
            provider.authenticate({"token": _make_token()})

    def test_malformed_token_denied(self):
        provider = _provider()
        with self.assertRaises(AuthenticationError):
            provider.authenticate({"token": "not-a-jwt-at-all"})

    def test_missing_token_denied(self):
        provider = _provider()
        with self.assertRaises(AuthenticationError):
            provider.authenticate({})

    def test_non_dict_credentials_denied(self):
        provider = _provider()
        with self.assertRaises(AuthenticationError):
            provider.authenticate("not-a-dict")

    def test_empty_string_token_denied(self):
        provider = _provider()
        with self.assertRaises(AuthenticationError):
            provider.authenticate({"token": "   "})


class TestAlgorithmAttacks(unittest.TestCase):
    def test_alg_none_rejected(self):
        now = int(time.time())
        claims = {"sub": "user-123", "iss": ISSUER, "aud": AUDIENCE, "exp": now + 3600}
        forged = jwt.encode(claims, key="", algorithm="none")
        provider = _provider()
        with self.assertRaises(AuthenticationError):
            provider.authenticate({"token": forged})

    def test_algorithm_not_in_allowed_list_rejected(self):
        """
        A token signed with HS256 (using the RSA public key's PEM bytes
        as an HMAC secret -- the classic RS256->HS256 confusion attack)
        must be rejected when only RS256 is configured. PyJWT's own
        encode() refuses to treat an asymmetric key as an HMAC secret, so
        the forged token is built manually here (base64url header/payload
        + raw HMAC-SHA256 signature) to actually simulate the attack
        payload an adversary would send.
        """
        import base64
        import hashlib
        import hmac
        import json

        provider = _provider()
        public_pem = _PUBLIC_KEY.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        now = int(time.time())
        header = {"alg": "HS256", "typ": "JWT", "kid": _KID}
        payload = {"sub": "user-123", "iss": ISSUER, "aud": AUDIENCE, "exp": now + 3600}

        def _b64(data: bytes) -> str:
            return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

        signing_input = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(payload).encode())}"
        signature = hmac.new(public_pem, signing_input.encode(), hashlib.sha256).digest()
        confusion_token = f"{signing_input}.{_b64(signature)}"

        with self.assertRaises(AuthenticationError):
            provider.authenticate({"token": confusion_token})


class TestClockSkew(unittest.TestCase):
    def test_expired_within_tolerance_accepted(self):
        provider = _provider(_config(clock_skew_seconds=60))
        token = _make_token(exp_delta=-30)
        ctx = provider.authenticate({"token": token})
        self.assertTrue(ctx.authenticated)

    def test_expired_beyond_tolerance_denied(self):
        provider = _provider(_config(clock_skew_seconds=5))
        token = _make_token(exp_delta=-30)
        with self.assertRaises(AuthenticationError):
            provider.authenticate({"token": token})


class TestLLMTrustBoundaryTokenTampering(unittest.TestCase):
    """A forged claim inside a token's payload has zero effect unless the token is validly re-signed by the real key -- proves the signature, not the payload text, is authoritative."""

    def test_tampered_payload_without_resigning_is_rejected(self):
        provider = _provider()
        token = _make_token(sub="user-123")
        header_b64, payload_b64, sig_b64 = token.split(".")
        import base64
        import json

        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "=="))
        payload["sub"] = "admin-1"
        payload["roles"] = ["admin"]
        tampered_payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
        tampered_token = f"{header_b64}.{tampered_payload_b64}.{sig_b64}"
        with self.assertRaises(AuthenticationError):
            provider.authenticate({"token": tampered_token})

    def test_claim_shaped_text_in_credentials_dict_has_no_effect(self):
        """Extra, unrecognized keys in the credentials dict (e.g. a forged {"role": "admin"}) are simply ignored -- only `token` is ever read."""
        provider = _provider()
        ctx = provider.authenticate(
            {"token": _make_token(), "role": "admin", "authenticated": True, "user_id": "admin-1"}
        )
        self.assertEqual(ctx.user_id, "user-123")
        self.assertNotIn(Role.ADMIN.value, ctx.roles)


class TestObservabilityIntegration(unittest.TestCase):
    def test_success_emits_auth_success(self):
        repo = AuditRepository()
        provider = _provider(audit_logger=AuditLogger(repository=repo))
        provider.authenticate({"token": _make_token()}, client_identifier="client-1")
        events = repo.list_events(event_type=EventType.AUTH_SUCCESS)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].actor, "user-123")

    def test_failure_emits_auth_failure_never_success(self):
        repo = AuditRepository()
        provider = _provider(audit_logger=AuditLogger(repository=repo))
        with self.assertRaises(AuthenticationError):
            provider.authenticate({"token": _make_token(exp_delta=-3600)}, client_identifier="client-1")
        self.assertEqual(len(repo.list_events(event_type=EventType.AUTH_FAILURE)), 1)
        self.assertEqual(len(repo.list_events(event_type=EventType.AUTH_SUCCESS)), 0)

    def test_failure_reason_never_contains_raw_token(self):
        repo = AuditRepository()
        provider = _provider(audit_logger=AuditLogger(repository=repo))
        token = _make_token(exp_delta=-3600)
        with self.assertRaises(AuthenticationError):
            provider.authenticate({"token": token}, client_identifier="client-1")
        event = repo.list_events(event_type=EventType.AUTH_FAILURE)[0]
        self.assertNotIn(token, str(event.to_dict()))

    def test_repeated_failures_trigger_security_event(self):
        repo = AuditRepository()
        logger = AuditLogger(repository=repo)
        detector = SecurityEventDetector(logger, repeated_failure_threshold=3)
        provider = _provider(audit_logger=logger, security_detector=detector)
        for _ in range(3):
            with self.assertRaises(AuthenticationError):
                provider.authenticate({"token": _make_token(exp_delta=-3600)}, client_identifier="client-1")
        self.assertEqual(len(repo.list_security_events()), 1)

    def test_success_resets_failure_count(self):
        repo = AuditRepository()
        logger = AuditLogger(repository=repo)
        detector = SecurityEventDetector(logger, repeated_failure_threshold=3)
        provider = _provider(audit_logger=logger, security_detector=detector)
        for _ in range(2):
            with self.assertRaises(AuthenticationError):
                provider.authenticate({"token": _make_token(exp_delta=-3600)}, client_identifier="client-1")
        provider.authenticate({"token": _make_token()}, client_identifier="client-1")
        with self.assertRaises(AuthenticationError):
            provider.authenticate({"token": _make_token(exp_delta=-3600)}, client_identifier="client-1")
        self.assertEqual(repo.list_security_events(), [])


class TestLoadOidcConfig(unittest.TestCase):
    def _write_config(self, tmp_path, content):
        tmp_path.write_text(content, encoding="utf-8")
        return str(tmp_path)

    def test_missing_config_file_and_no_env_raises(self):
        import tempfile

        with self.assertRaises(AuthConfigurationError):
            load_oidc_config(config_path=str(Path(tempfile.gettempdir()) / "nonexistent_auth_config_xyz.yaml"))

    def test_valid_config_loads(self):
        import tempfile

        path = Path(tempfile.gettempdir()) / "test_auth_config_valid.yaml"
        path.write_text(
            "authentication:\n"
            f"  issuer_url: {ISSUER}\n"
            f"  audience: {AUDIENCE}\n"
            "  jwks_url: https://issuer.example.test/jwks.json\n"
            "  algorithms: [RS256]\n",
            encoding="utf-8",
        )
        config = load_oidc_config(config_path=str(path))
        self.assertEqual(config.issuer, ISSUER)
        self.assertEqual(config.audience, AUDIENCE)
        path.unlink()

    def test_alg_none_in_config_rejected(self):
        import tempfile

        path = Path(tempfile.gettempdir()) / "test_auth_config_alg_none.yaml"
        path.write_text(
            "authentication:\n"
            f"  issuer_url: {ISSUER}\n"
            f"  audience: {AUDIENCE}\n"
            "  jwks_url: https://issuer.example.test/jwks.json\n"
            "  algorithms: [none]\n",
            encoding="utf-8",
        )
        with self.assertRaises(AuthConfigurationError):
            load_oidc_config(config_path=str(path))
        path.unlink()

    def test_env_var_overrides_yaml(self):
        import os
        import tempfile

        path = Path(tempfile.gettempdir()) / "test_auth_config_env_override.yaml"
        path.write_text(
            "authentication:\n"
            "  issuer_url: https://yaml-issuer.example/\n"
            f"  audience: {AUDIENCE}\n"
            "  jwks_url: https://issuer.example.test/jwks.json\n",
            encoding="utf-8",
        )
        os.environ["OIDC_ISSUER_URL"] = "https://env-issuer.example/"
        try:
            config = load_oidc_config(config_path=str(path))
            self.assertEqual(config.issuer, "https://env-issuer.example/")
        finally:
            del os.environ["OIDC_ISSUER_URL"]
            path.unlink()

    def test_unknown_role_in_mapping_rejected(self):
        import tempfile

        path = Path(tempfile.gettempdir()) / "test_auth_config_bad_role.yaml"
        path.write_text(
            "authentication:\n"
            f"  issuer_url: {ISSUER}\n"
            f"  audience: {AUDIENCE}\n"
            "  jwks_url: https://issuer.example.test/jwks.json\n"
            "  role_mapping:\n"
            "    admin: SUPERUSER\n",
            encoding="utf-8",
        )
        with self.assertRaises(AuthConfigurationError):
            load_oidc_config(config_path=str(path))
        path.unlink()


class TestSessionAndMemoryBindingWithOidcIdentity(unittest.TestCase):
    """
    plan.md Steps 9.13/9.14: SessionManager/MemoryManager's existing
    cross-user ownership checks (Phase 5/7, unmodified) must hold
    end-to-end when the trusted AuthContext comes from a REAL OIDC token
    specifically, not only from DevelopmentAuthenticationProvider.
    """

    def test_cross_user_session_access_denied_for_oidc_identities(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "agent"))
        from session_manager import SessionManager

        provider = _provider()
        user_a = provider.authenticate({"token": _make_token(sub="oidc-user-a")})
        user_b = provider.authenticate({"token": _make_token(sub="oidc-user-b")})

        manager = SessionManager()
        manager.create_session(session_id="s1", user_id=user_a.user_id)
        self.assertIsNotNone(manager.get_session("s1", user_id=user_a.user_id))
        self.assertIsNone(manager.get_session("s1", user_id=user_b.user_id))

    def test_cross_user_memory_access_denied_for_oidc_identities(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "agent"))
        from memory_manager import MemoryManager
        from memory_models import MemoryCategory
        from policy_engine import PolicyEngine

        provider = _provider()
        user_a = provider.authenticate({"token": _make_token(sub="oidc-user-a")})
        user_b = provider.authenticate({"token": _make_token(sub="oidc-user-b")})

        manager = MemoryManager(PolicyEngine())
        record = manager.propose_memory(
            user_id=user_a.user_id,
            category=MemoryCategory.PREFERENCE,
            key="likes_texting",
            value="yes",
            source="user",
        )
        saved = manager.persist_memory(record)
        self.assertEqual(manager.remove_memory(saved.id, user_id=user_b.user_id), False)
        self.assertEqual(manager.remove_memory(saved.id, user_id=user_a.user_id), True)


if __name__ == "__main__":
    unittest.main()

# Phase 9 — Production Authentication and Security Hardening Report

## 1. Executive Summary

Phase 9 adds a real, production-grade authentication boundary — `OIDCAuthenticationProvider` (`src/agent/oidc_provider.py`) — as a second implementation of Phase 7's existing `AuthenticationProvider` interface, alongside the unmodified, TEST-ONLY `DevelopmentAuthenticationProvider`. It validates standard OAuth 2.0/OIDC JSON Web Tokens using PyJWT (signature, algorithm allow-listing, issuer, audience, expiration, not-before, JWKS-based key resolution with built-in caching) — no cryptographic verification is hand-rolled anywhere in this codebase. A new `AUTH_MODE` environment variable (`dev` default / `production`/`oidc`) selects which provider `src/api/server.py` constructs at import time; production mode fails closed (uncaught `AuthConfigurationError`, process refuses to start) if required OIDC configuration is missing, and never silently falls back to the development provider. No downstream component — `PolicyEngine`, `SessionManager`, `MemoryManager`, `ToolOrchestrator`, `ConversationManager` — required any change, since all of them already consumed `AuthContext` opaquely.

## 2. Authentication Architecture

```
Client
  │  Authorization: Bearer <JWT>
  ▼
FastAPI (src/api/server.py) — resolve_identity() dependency
  │
  ▼
AUTH_MODE selects provider (constructed once, at import time):
  │
  ├── "dev" (default) ──► DevelopmentAuthenticationProvider (Phase 7, unmodified)
  │
  └── "production"/"oidc" ──► OIDCAuthenticationProvider (Phase 9, new)
                                   │
                                   ▼
                          jwt.PyJWKClient — JWKS fetch + caching
                                   │
                                   ▼
                          PyJWT signature + claims validation
                                   │
                                   ▼
                          Trusted claim → Role mapping (identity.py's Role enum)
                                   │
                                   ▼
                              AuthContext
  │
  ▼
ConversationManager.handle_turn(auth=...) — consumes identity, never authenticates, never assigns roles
```

Both providers produce the exact same `AuthContext` type Phase 7 defined; nothing downstream branches on `authentication_method` ("development_test_provider" vs "oidc").

## 3. Token Validation

All performed by PyJWT (`jwt.decode(token, key, algorithms=[...], audience=..., issuer=..., leeway=..., options={"require": [...]})`), never reimplemented:

- **Signature**: verified against the key `jwt.PyJWKClient` resolves for the token's `kid`. A token signed with any other key (including a manually-forged RS256↔HS256 key-confusion attempt using the RSA public key's PEM bytes as an HMAC secret) is rejected.
- **Algorithms**: only the explicitly configured list (default `["RS256"]`) is ever passed to `jwt.decode()` — the token's own header `alg` is never trusted to select the verification algorithm. `"none"` is rejected both by PyJWT itself and defensively at config-load time (`load_oidc_config()` raises `AuthConfigurationError` if `"none"` appears in the configured algorithm list).
- **Issuer / Audience**: validated against `configs/auth.yaml`'s (or the environment-variable-overridden) `issuer_url`/`audience` — mismatches deny.
- **Expiration / Not-before**: validated with a configurable `leeway` (`clock_skew_seconds`, default 60) — a token expired by less than the tolerance is accepted; beyond it, denied. Never unlimited, never disabled.
- **Required claims**: `sub`, `iss`, `aud`, `exp` (configurable) must all be present (`options={"require": [...]}`) — a token missing `sub`, for example, is denied even if otherwise validly signed.
- **JWKS**: resolved via `jwt.PyJWKClient(jwks_url, cache_jwk_set=True, timeout=...)` — built-in caching (no per-request refetch), bounded HTTP timeout, and any resolution failure (unknown `kid`, unreachable endpoint, malformed JWKS) fails closed (denies authentication) rather than falling back to an unverified token.

## 4. Identity Mapping

`sub` claim → `AuthContext.user_id`, unconditionally (the one claim `OIDCAuthenticationProvider` always trusts, since PyJWT has already cryptographically verified it belongs to this token). Role mapping is explicit and configured: the JWT claim named by `role_claim` (default `roles`) is read, and only claim values present in `role_mapping` (`configs/auth.yaml`, e.g. `admin: ADMIN`) are converted to this application's existing `Role` enum (`identity.py`, unchanged, reused — no second role taxonomy). A claim value with no configured mapping, or a token with no role claim at all, produces the least-privilege `Role.USER`, never an implicit elevated role. Permissions are then derived the same way every other identity's permissions are — `identity.permissions_for_roles()`, unchanged from Phase 7.

## 5. Development vs Production

Selected once, at `src/api/server.py` import time, by the `AUTH_MODE` environment variable:

| `AUTH_MODE` | Provider constructed | Behavior on misconfiguration |
|---|---|---|
| unset / `dev` | `DevelopmentAuthenticationProvider` | Same as Phase 7 — `DEV_AUTH_ENABLED=false` disables it, `resolve_identity()` still handles missing/invalid tokens |
| `production` / `oidc` | `OIDCAuthenticationProvider` | `load_oidc_config()` raises `AuthConfigurationError`, **uncaught** — process fails to start |

There is no code path anywhere that catches `AuthConfigurationError` and substitutes `DevelopmentAuthenticationProvider` — `tests/test_auth_mode_separation.py` proves this by running the actual module import in a subprocess under each mode and asserting the process either succeeds with the expected provider type or exits non-zero with `AuthConfigurationError`, never a silent substitution.

## 6. API Integration

`src/api/server.py`'s `resolve_identity()` dependency (unchanged from Phase 7's shape) calls whichever provider `AUTH_MODE` selected — the route handlers (`/generate`) and the Authorization-header parsing/401 behavior are completely unaware which provider is active. `/health` remains fully public and unchanged. `/ready` (Phase 8) reports only `{"ready": bool}` — it does not check authentication configuration validity at request time, since an invalid production configuration already prevents the process from starting at all (Section 5), so there is no "started but auth is broken" state for `/ready` to report.

## 7. Session Security

`SessionManager.get_session(session_id, user_id=...)`'s existing cross-user denial (Phase 5, unmodified) was re-verified end-to-end specifically against `OIDCAuthenticationProvider`-produced identities (`tests/test_oidc_provider.py::TestSessionAndMemoryBindingWithOidcIdentity::test_cross_user_session_access_denied_for_oidc_identities`): two real, validly-signed JWTs for different subjects produce `AuthContext`s whose `user_id`s SessionManager still correctly distinguishes — session ownership was never based on which provider resolved the identity, only on the resulting `user_id`.

## 8. Memory Security

Same verification for `MemoryManager` (`test_cross_user_memory_access_denied_for_oidc_identities`): an OIDC-authenticated user cannot delete another OIDC-authenticated user's memory record; the record's own owner can.

## 9. Observability

`OIDCAuthenticationProvider` accepts the same optional `audit_logger`/`security_detector` constructor parameters `DevelopmentAuthenticationProvider` does (Phase 8), and emits:
- `AUTH_SUCCESS` — actor is the verified `sub` claim, reason `"OIDC token accepted."`.
- `AUTH_FAILURE` — actor is the caller-supplied `client_identifier` (safe, non-secret — e.g. IP), reason is a specific, safe, human-readable string (`"Token expired."`, `"Invalid signature."`, `"Missing subject claim."`, etc.) — never the raw token or raw JWT claims.
- Repeated failures (same `client_identifier`) trigger Phase 8's existing `SecurityEventDetector.record_auth_failure()` threshold logic, unmodified — no second detector was created (plan.md Step 9.16).
- A subsequent success resets that identifier's failure count, exactly as `DevelopmentAuthenticationProvider` already does.

`test_failure_reason_never_contains_raw_token` asserts the raw JWT string is absent from every recorded `AUTH_FAILURE` event.

## 10. Secret Handling

- No password, client secret, signing key, or API credential is stored, hardcoded, or committed anywhere in this codebase — a JWKS URL points to a *public* key set, not a secret.
- `configs/auth.yaml` ships with `issuer_url`/`audience`/`jwks_url` deliberately **empty**, not filled with a plausible-looking placeholder — this ensures a deployment that forgets to set `OIDC_ISSUER_URL`/`OIDC_AUDIENCE`/`OIDC_JWKS_URL` gets the loud, explicit `AuthConfigurationError` startup failure (Section 5) rather than a provider that silently validates against a fake, never-satisfiable issuer.
- Grepped `src/agent/`, `src/api/` for `print(...token...)`/`print(...secret...)`-shaped logging: none found. `oidc_provider.py` never passes the raw token to `AuditLogger`, `logging`, or any exception message raised to the caller.
- `docker/docker-compose.yml`'s pre-existing `HF_TOKEN` passthrough is an unrelated, pre-existing Hugging Face token environment-variable pass-through (empty default), not a Phase 9 credential.

## 11. Security Tests

- **JWT tests** (`tests/test_oidc_provider.py::TestTokenValidationFailures`, `TestClockSkew`): valid token, wrong issuer, wrong audience, expired (with/without clock-skew tolerance), not-yet-valid (`nbf`), missing subject, invalid signature, unknown `kid`, JWKS/key-resolution failure, malformed token, missing/empty/non-dict credentials.
- **Algorithm attacks** (`TestAlgorithmAttacks`): `alg=none` rejected; a manually-constructed RS256→HS256 key-confusion token (using the RSA public key's PEM bytes as the HMAC secret, bypassing PyJWT's own `encode()`-time guard against this to actually simulate the attack payload) rejected when only `RS256` is configured.
- **Cross-user tests** (`TestSessionAndMemoryBindingWithOidcIdentity`): session and memory ownership enforced between two distinct real OIDC identities.
- **LLM/claim trust-boundary tests** (`TestLLMTrustBoundaryTokenTampering`): a token payload tampered after signing (forged `sub`/`roles`) without re-signing is rejected outright — the signature, not the payload text, is authoritative; extraneous forged keys in the `credentials` dict (`{"role": "admin", "authenticated": True, "user_id": "admin-1"}`) have zero effect since only the `token` key is ever read.
- **Configuration tests** (`TestLoadOidcConfig`): missing required fields raise `AuthConfigurationError`; `algorithms: [none]` raises; an unknown role name in `role_mapping` raises; environment variables correctly override YAML values.
- **Production/development separation** (`tests/test_auth_mode_separation.py`, run as real subprocesses to exercise the actual import-time code path): default/`dev` mode uses the development provider; `production`/`oidc` mode without configuration fails closed with `AuthConfigurationError` and never falls back; `production` mode with valid environment-variable configuration successfully constructs `OIDCAuthenticationProvider`; partial configuration (only one of three required fields) still fails closed.

## 12. Regression Tests

- **Tests added**: 34 (`tests/test_oidc_provider.py`) + 6 (`tests/test_auth_mode_separation.py`) = 40.
- **Tests executed**: full repository suite, 457 tests.
- **Passing**: 456.
- **Failing**: 0 caused by Phase 9. 1 pre-existing environment error (`tests/test_retriever.py` — `faiss` not installed; present before this and every prior phase's work, unrelated).
- Every Phase 3–8 test file (`test_identity.py`, `test_authorization.py`, `test_server_api.py`, `test_session_manager.py`, `test_memory_manager.py`, `test_conversation_manager.py`, `test_tool_orchestrator.py`, `test_observability.py`, `test_metrics.py`, `test_policy_engine.py`, `test_privacy_service.py`, `test_pii_detector.py`, `test_handoff_detector.py`, `test_clinical_guard.py`, `test_intent_engine.py`, `test_predict_facade.py`) still passes unmodified — Phase 9 introduced zero changes to any of those modules' own logic.

## 13. Compatibility

- **IdentityContext (`AuthContext`)**: unchanged type; `OIDCAuthenticationProvider` populates the exact same fields `DevelopmentAuthenticationProvider` does, with `authentication_method="oidc"` distinguishing it only for observability purposes, never for authorization logic.
- **PolicyEngine**: unmodified — `evaluate_authorization()` already treats `AuthContext` opaquely.
- **PrivacyService**: unmodified — no interaction with authentication.
- **SessionManager / MemoryManager**: unmodified — ownership checks already operate on `AuthContext.user_id` regardless of provider (Sections 7–8).
- **ToolOrchestrator**: unmodified — authentication/authorization gate sequence (`_invoke()`) is provider-agnostic.
- **ConversationManager**: unmodified — `handle_turn(auth=...)` already accepted any `AuthContext`.
- **FastAPI (`src/api/server.py`)**: `resolve_identity()`'s shape and the `/generate`/`/health`/`/ready` contracts are unchanged; only the module-level provider-construction block changed, gated by the new `AUTH_MODE` variable which defaults to preserving exact Phase 7/8 behavior.
- **Phase 8 observability**: reused directly — `OIDCAuthenticationProvider` shares the same `AuditLogger`/`SecurityEventDetector` instances `server.py` already constructs, emitting into the same audit trail as every other component (no second observability system).

## 14. Limitations

- **No real external Identity Provider is deployed or configured in this repository.** `OIDCAuthenticationProvider` is built and exhaustively tested against a locally-generated RSA keypair and an injectable, offline key resolver — never a real IdP (Auth0, Okta, Cognito, Google, etc.) over the network. Provisioning a real IdP, registering this application as a client, and populating real `OIDC_ISSUER_URL`/`OIDC_AUDIENCE`/`OIDC_JWKS_URL` values is a deployment-time task explicitly out of this phase's scope.
- **No refresh-token lifecycle** is implemented — this application only ever validates an access/ID token presented on each request; token issuance, refresh, and revocation are entirely the external IdP's responsibility.
- **No distributed rate limiting** exists. Abuse protection is limited to Phase 8's existing in-process `SecurityEventDetector` repeated-failure threshold (per-process, per-`client_identifier`, not shared across multiple server instances) — documented as technical debt, not claimed as production-grade abuse protection (plan.md Step 9.16 explicitly permits this).
- **No MFA or account-recovery flow** exists anywhere in this application — again, IdP responsibility, out of scope.
- **No production secret-management platform integration** (e.g. Vault, AWS Secrets Manager) was introduced — `OIDC_ISSUER_URL`/`OIDC_AUDIENCE`/`OIDC_JWKS_URL` are read from plain environment variables, consistent with this repository's existing convention for every other runtime configuration value, and none of them are actually secret (a JWKS URL is public by design).
- **HS256 (shared-secret) OIDC providers are not supported** — only asymmetric algorithms via JWKS (`RS256` by default) were implemented, since that is the standard, more secure pattern real OIDC providers use; adding HS256 support would require introducing actual shared-secret storage, which was not required by any concrete need in this repository.

## 15. Remaining Technical Debt

- Distributed (cross-process) rate limiting / abuse protection for authentication failures — currently in-process only.
- Real IdP integration/provisioning (registering this application, populating production configuration) is deployment work, not implemented here.
- `OIDCAuthenticationProvider`'s `_map_roles()` only reads a single, flat claim (`role_claim`); nested/namespaced claim structures some IdPs use (e.g. Auth0's namespaced custom claims) would need a small extension to the claim-extraction logic if a specific IdP requires it.
- No automated JWKS-rotation drill exists beyond unit-level "unknown kid" tests — `jwt.PyJWKClient`'s caching behavior under an actual mid-session key rotation was not exercised against a real, changing JWKS endpoint (only simulated via the static test resolver).

## 16. Recommended Next Phase

Proceed to Phase 10 (Reliability, Resilience and Failure-Safety Engineering) as `plan.md` specifies next — timeouts, retries, circuit breakers, and failure-mode hardening around the dependencies this phase (and Phases 1–8) already established, including explicitly verifying that a JWKS-unavailable or authentication-evaluation-unavailable condition fails closed (deny), consistent with this phase's own fail-closed posture.

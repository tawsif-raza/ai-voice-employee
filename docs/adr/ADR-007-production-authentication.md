# ADR-007: Production Authentication via OIDC/JWT, Isolated Behind the Existing AuthenticationProvider Interface

**Date:** 2026-08-18
**Related Documents:**
- `identity.py` module docstring (Phase 7's `AuthenticationProvider` interface)
- `oidc_provider.py` module docstring (Phase 9)
- `PHASE_9_PRODUCTION_AUTH_REPORT.md`

# Status

Accepted

# Context

Phase 7 introduced `AuthenticationProvider` as an abstract interface with exactly one implementation, `DevelopmentAuthenticationProvider` — a deterministic, non-cryptographic, TEST-ONLY provider recognizing a small fixed table of bearer tokens. It was explicitly documented as not production-grade and was never intended to be the system's permanent authentication mechanism. Phase 9's objective is to add a real production identity boundary without inventing a custom protocol, without hand-rolling cryptography, and without weakening any of the trust-boundary guarantees Phases 3–8 already built on top of `AuthContext`.

# Decision

Add a second `AuthenticationProvider` implementation, `OIDCAuthenticationProvider` (`src/agent/oidc_provider.py`), that validates standard OAuth 2.0/OIDC JSON Web Tokens using PyJWT (a maintained security library) — signature verification, issuer/audience/expiration/not-before validation, and JWKS-based asymmetric key resolution (via `jwt.PyJWKClient`, which also provides built-in JWKS caching) are all delegated to that library; this module never implements cryptographic verification itself. Only an explicitly configured JWT claim (default `roles`) is mapped, through a configured mapping table, onto this application's existing `Role` enum — an unmapped or absent claim value never grants elevated privilege, only the least-privilege `USER` role.

Which provider is active is controlled by a new `AUTH_MODE` environment variable read once at `src/api/server.py` import time: `dev` (default) constructs `DevelopmentAuthenticationProvider` exactly as Phase 7 did; `production`/`oidc` constructs `OIDCAuthenticationProvider` from `configs/auth.yaml` plus `OIDC_ISSUER_URL`/`OIDC_AUDIENCE`/`OIDC_JWKS_URL` environment variables. If required OIDC configuration is missing in production mode, `load_oidc_config()` raises `AuthConfigurationError`, uncaught — the server process fails to start rather than silently falling back to the development provider or serving traffic with broken authentication.

Neither `PolicyEngine`, `SessionManager`, `MemoryManager`, `ToolOrchestrator`, nor `ConversationManager` was modified to accommodate this: all of them already consume `AuthContext` opaquely (Phase 7's design), so a second provider producing the same typed object required zero changes downstream.

# Alternatives Considered

- **Hand-rolled JWT parsing/signature verification.** Not selected: plan.md's Non-Negotiable Principles explicitly forbid writing cryptographic verification manually — a maintained, widely-audited library (PyJWT + `cryptography`) is safer and is the standard tool for this exact problem.
- **A custom authentication protocol** (e.g. a proprietary signed-cookie scheme). Not selected: plan.md explicitly requires standard OAuth 2.0/OIDC; a custom protocol would also mean every client integration has to implement something non-standard.
- **Replacing `DevelopmentAuthenticationProvider` entirely** rather than adding a second implementation. Not selected: the development provider remains valuable for local development and the existing test suite that exercises deterministic, non-network-dependent authentication; Phase 7's `AuthenticationProvider` interface was built specifically to support multiple implementations side by side.
- **Silent fallback from production to development authentication on missing OIDC config.** Not selected: this is explicitly forbidden by plan.md's Non-Negotiable Principles (33) — a misconfigured production deployment must fail loudly, not silently degrade to a test-only mechanism.
- **A single provider that branches internally on a "mode" flag.** Not selected: two separate classes implementing the same interface is simpler to reason about and test in isolation than one class with two internal code paths, and matches the Strategy-pattern shape `AuthenticationProvider` was already designed around.

# Consequences

**Positive**
- Real cryptographic authentication is available without any change to the authorization, session, memory, tool, privacy, or observability layers built in Phases 3–8 — they were already provider-agnostic by construction.
- A misconfigured production deployment fails at startup, immediately and loudly, rather than accepting traffic under broken or absent authentication.
- JWKS key rotation is handled by `jwt.PyJWKClient`'s existing caching/refetch behavior, not reimplemented.

**Negative**
- Two `AuthenticationProvider` implementations now exist in the codebase; a future maintainer must understand `AUTH_MODE`'s role to know which one is active in a given deployment.
- `OIDCAuthenticationProvider` introduces two new third-party dependencies (`PyJWT`, `cryptography`) that did not previously exist in `requirements.txt`.

**Known Limitations**
- No external Identity Provider is actually deployed or configured in this repository — `OIDCAuthenticationProvider` is built and exhaustively tested against a locally-generated RSA keypair and a static, injectable key resolver, never a real IdP over the network.
- No refresh-token lifecycle, MFA, or account-recovery flow is implemented — those belong to the external IdP, not this application.
- No distributed rate limiter exists; abuse protection is limited to Phase 8's existing in-process `SecurityEventDetector` repeated-failure threshold, reused unmodified for OIDC failures.

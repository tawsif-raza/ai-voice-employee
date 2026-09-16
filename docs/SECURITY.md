# Security Overview

**Status:** Living document, first published Phase 11 (2026-08-18), updated Phase 18 (2026-09-16).
**Scope:** The AI Voice Employee conversational assistant — FastAPI (`src/api/server.py`) through `ConversationManager`, `PolicyEngine`, `ClinicalSafetyGuard`, `AuthenticationProvider`, `PrivacyService`, `SessionManager`, `MemoryManager`, `ToolOrchestrator`, and the observability/reliability layers built in Phases 3–16, plus the PostgreSQL persistence layer (Phase 12), OpenTelemetry tracing (Phase 14), request-isolated job execution (Phase 15), Twilio webhook/WebSocket signature validation, and the Gemini/Groq free-tier provider path (both added after Phase 16).

This document does not claim the system is "100% secure," nor does it claim regulatory compliance of any kind — no formal compliance assessment has been performed. It documents the threat model, trust boundaries, security invariants this codebase enforces, the classes of attack tested against, and known residual risks, so that claims about this system's security posture are traceable to actual tests rather than assertions.

## 1. Threat Model

**Assets:** user identity, session state, conversation history, durable memory, PII, clinical information, business actions (appointments/orders), authentication credentials, authorization decisions, audit events, system configuration, model integrity, RAG knowledge base.

**Threat actors assumed:** unauthenticated attacker, authenticated malicious user, cross-user attacker, prompt-injection attacker (via user input), malicious document author (via RAG knowledge base content), compromised/malicious API client, tool-abuse attacker.

**Assumptions treated as adversarial by default** (plan.md's Phase 11 mandate): users may be malicious; LLM output may be manipulated (via prompt injection); retrieved documents may contain malicious instructions; tool requests may be crafted to bypass policy; clients may forge identity fields; users may attempt cross-user access; API requests may be malformed or replayed; race conditions may be exploited; authorization bypass may be attempted; privacy leakage may be attempted; tools may be abused; resources may be exhausted.

## 2. Trust Boundaries

```
TRUSTED (this application's own deterministic decision-makers)
  - AuthenticationProvider (DevelopmentAuthenticationProvider / OIDCAuthenticationProvider)
  - PolicyEngine
  - PrivacyService
  - SessionManager
  - MemoryManager
  - ToolOrchestrator
  - ClinicalSafetyGuard (HandoffDetector configured as the clinical guard)
  - AuditLogger / SecurityEventDetector / MetricsRegistry

UNTRUSTED (never treated as authoritative for any decision)
  - Raw user text (user_input)
  - LLM output (model-generated response text)
  - Retrieved RAG documents
  - Client-supplied request metadata (session_id, tool arguments, JSON body fields)
  - Tool arguments as proposed by an LLM-driven or user-driven ActionProposal
```

The LLM operates **inside** this architecture but is never trusted for any security decision — see Section 3.

## 3. Security Invariants

These ten invariants are proven by dedicated tests (`tests/test_security_red_team.py::TestSecurityInvariants`, cross-referenced against each owning component's own test suite):

| # | Invariant | Enforced by | Primary test coverage |
|---|---|---|---|
| 1 | Trusted identity comes only from `AuthenticationProvider`. | `identity.py`, `oidc_provider.py` | `test_server_api.py::TestAuthenticationBoundary`, `test_oidc_provider.py`, `test_security_red_team.py::test_invariant_1` |
| 2 | Authorization comes only from `PolicyEngine`. | `policy_engine.py::evaluate_authorization()` | `test_authorization.py`, `test_security_red_team.py::test_invariant_2` |
| 3 | Clinical safety comes only from `ClinicalSafetyGuard`. | `conversation_manager.py`'s clinical-guard step | `test_conversation_manager.py::TestClinicalSafetyTrigger`, `test_security_red_team.py::test_invariant_3` |
| 4 | Privacy decisions come only from `PrivacyService`. | `privacy_service.py`, `policy_engine.py::evaluate_pii()` | `test_privacy_service.py`, `test_security_red_team.py::test_invariant_4` |
| 5 | `SessionManager` decides session ownership. | `session_manager.py::get_session()` | `test_session_manager.py::TestUnauthorizedAccess`, `test_security_red_team.py::test_invariant_5` |
| 6 | `MemoryManager` decides memory ownership. | `memory_manager.py` | `test_memory_manager.py::TestCrossUserIsolation`, `test_security_red_team.py::test_invariant_6` |
| 7 | `ToolOrchestrator` decides whether a tool actually executes. | `tool_orchestrator.py::invoke()` | `test_tool_orchestrator.py`, `test_security_red_team.py::test_invariant_7` |
| 8 | Trusted confirmation state decides whether confirmation exists. | `ToolRequest.confirmed` (trusted field only), `SessionManager.try_consume_pending_confirmation()` | `test_tool_orchestrator.py::TestConfirmation`, `test_security_red_team.py::test_invariant_8`, `TestConfirmationReplayRace` |
| 9 | `AuditLogger` records only real system decisions. | `audit.py` — events derived from already-computed results, never a separate judgment | `test_observability.py::TestLLMTrustBoundary`, `test_security_red_team.py::test_invariant_9` |
| 10 | LLM output is never authoritative for any security boundary. | Structural: no code path in `policy_engine.py`/`tool_orchestrator.py`/`identity.py`/`session_manager.py`/`memory_manager.py` reads model-generated text as a decision input | `test_security_red_team.py::TestLLMTrustBoundaryMatrix` (10-row matrix), plus per-phase LLM trust-boundary suites in every earlier phase's tests |

## 4. Attack Surface

| Surface | Entry point | Primary defenses |
|---|---|---|
| HTTP API | `src/api/server.py` (`/generate`, `/health`, `/ready`) | `AuthenticationProvider`, Pydantic request validation + resource limits (Phase 10), safe global exception handler, correlation-ID middleware |
| Authentication | `AUTH_MODE`-selected provider | JWT signature/issuer/audience/expiry/nbf validation (PyJWT), JWKS key resolution, fail-closed on any validation error |
| Policy/Authorization | `PolicyEngine` | Deterministic, config-driven; every evaluation call site fails closed on internal error (Phase 10) |
| LLM | `LLMService.generate_stream()` | Pure text-in/text-out; output never parsed as a command; post-generation `HandoffDetector` is observational only |
| RAG | `Retriever.retrieve()` | Retrieved content is always treated as reference DATA appended to the prompt, never as an instruction; `PolicyEngine`/`ToolOrchestrator` boundaries are structurally unreachable from prompt content |
| Tools | `ToolOrchestrator` | Full gate sequence (tool policy → auth → authorization → confirmation → idempotency → execution), fail-closed on internal `PolicyEngine` error (Phase 10), circuit-broken/retried only for the execution step itself, never the gates |
| Sessions | `SessionManager` | User-scoped `get_session()`, atomic `try_consume_pending_confirmation()` (Phase 11 fix), thread-safe |
| Memory | `MemoryManager` | User-scoped read/write/delete, `PrivacyService`-filtered context exposure |
| Privacy | `PrivacyService` / `privacy_logging.py` | Context-aware PII detection/redaction; log-injection-hardened (Phase 11 fix: CR/LF and ANSI escapes neutralized, not just PII) |
| Observability | `AuditLogger` / `MetricsRegistry` | Best-effort, never blocks the underlying action; events derived only from already-computed real decisions |

## 5. Mitigations by Attack Class (plan.md's 27 Required Attack Scenarios)

All 27 required scenarios have dedicated regression coverage. See `PHASE_11_SECURITY_RED_TEAM_REPORT.md` Section 5 for the full findings table and exact test cross-references; summarized here:

1–5 (JWT tampering, `alg=none`, wrong issuer/audience, expired token) — `test_oidc_provider.py`.
6–9 (identity/role/permission spoofing, policy override injection) — `test_authorization.py`, `test_oidc_provider.py::TestLLMTrustBoundaryTokenTampering`, `test_security_red_team.py`.
10–11 (direct/indirect prompt injection) — `test_conversation_manager.py`, `test_privacy_service.py::TestPrivacyAttackRegressions`, `test_security_red_team.py`.
12–15 (tool injection, unknown tool, tool argument spoofing) — `test_tool_orchestrator.py`, `test_security_red_team.py::TestLLMTrustBoundaryMatrix`.
16–17 (confirmation spoofing/replay) — `test_tool_orchestrator.py::TestConfirmation`, `test_security_red_team.py::TestConfirmationReplayRace` (Phase 11 fix).
18–19 (cross-user session/memory access) — `test_session_manager.py`, `test_memory_manager.py`, `test_concurrency.py`.
20 (privacy leakage) — `test_privacy_service.py`.
21 (log injection) — `test_security_red_team.py::TestLogInjectionHardening` (Phase 11 fix).
22 (audit spoofing) — `test_observability.py::TestLLMTrustBoundary`.
23 (resource exhaustion) — `test_server_api.py::TestResourceLimits`.
24 (retry abuse) — `test_tool_reliability.py`, `test_conversation_reliability.py`.
25 (duplicate tool execution) — `test_tool_reliability.py::TestConcurrentDuplicateRequests`, `test_security_red_team.py::TestIdempotencyKeyReuseIsSafeByDefault`.
26 (concurrency race) — `test_concurrency.py`, `test_security_red_team.py::TestConfirmationReplayRace`.
27 (malformed configuration) — `test_security_red_team.py::TestConfigurationTamperingFailsSafe`, `test_oidc_provider.py::TestLoadOidcConfig`, `test_auth_mode_separation.py`.

## 6. Phase 18 — Production Security Gate Findings

Full write-up: `PHASE_18_SECURITY_GATE_REPORT.md`. One genuine finding from this pass:

| ID | Severity | Component | Attack | Impact | Fix | Regression Test | Status |
|---|---|---|---|---|---|---|---|
| F-04 | HIGH | `conversation_manager.py`'s `AWAITING_AUTHENTICATION` step (telephony caller-PIN verification) | An unauthenticated telephony caller reaching a permission-gated tool action was asked for a "4-digit PIN" that was compared against a hardcoded literal (`"1234"`) — any caller who spoke it was granted a real `AuthContext`. A second, compounding defect meant that context was built with `roles=["caller"]` and no `permissions`, so even a "successful" PIN entry could never actually pass any subsequent permission check. | Any caller could impersonate an authenticated telephony identity by speaking a publicly-known 4-digit value; separately, the feature could never functionally succeed even for a legitimate caller. | `ConversationManager` gained an explicit `caller_pin` parameter (`TELEPHONY_MOCK_PIN` env var), defaulting to `None`. `None` now fails closed — every `AWAITING_AUTHENTICATION` attempt hands off to a human instead of accepting any spoken value. When explicitly configured (for a controlled canary only — this remains a single shared secret, not real per-caller identity verification), the re-authenticated context is granted `permissions_for_roles((Role.USER,))` instead of an empty, unusable permission set. | `tests/test_security_red_team.py::TestCallerPinFailsClosedByDefault` (4 tests) | Fixed |

No CRITICAL findings. No unresolved P0/P1.

## 7. Residual Risks

Explicitly not mitigated by this codebase (out of scope or requires infrastructure this repository does not own):

- **Telephony caller identity is not real per-caller verification.** Even with `TELEPHONY_MOCK_PIN` configured (Section 6, F-04), authentication is a single shared secret common to every caller, not a per-patient/per-caller credential. A real deployment needs an actual caller-identity mechanism (e.g. a real per-patient PIN store, SMS OTP, or binding to the calling phone number) before this path is used for anything beyond a controlled canary with known callers — tracked as a Phase 20 (Voice Workflow Completeness) / Phase 26 (Production Deployment) prerequisite, not invented here per plan.md Rule 5 (no invented business requirements).
- **External Identity Provider compromise.** `OIDCAuthenticationProvider` trusts whatever the configured IdP signs; a compromised IdP is outside this application's control.
- **External business API compromise.** Mock tools stand in for real business systems; a real integration's own security is not this codebase's responsibility.
- **Host/infrastructure compromise.** No amount of application-layer hardening protects against a compromised host, container escape, or supply-chain attack on a dependency.
- **Dependency vulnerabilities.** `PyJWT`, `cryptography`, `fastapi`, `transformers`, etc. are trusted as maintained upstream libraries; no independent vulnerability audit of them was performed this phase.
- **Distributed deployment limitations.** Circuit-breaker state, rate-limiting (repeated-auth-failure detection), and idempotency dedup are all per-process, in-memory (Phase 10 Limitations) — a multi-instance deployment has independent state per instance, not a shared view.
- **Secrets-management limitations.** No production secret-management platform (Vault, AWS Secrets Manager, etc.) is integrated; secrets are read from environment variables, consistent with this repository's existing convention.
- **No distributed rate limiting.** Abuse protection is limited to Phase 8's in-process `SecurityEventDetector` repeated-failure threshold.
- **No MFA, account recovery, or refresh-token lifecycle** — entirely the external IdP's responsibility (Phase 9 scope).

## 8. Security Testing Methodology

Per plan.md's explicit quality requirements: every security test in this codebase verifies **actual system state** (a returned object, a recorded audit event, an actual side effect, an actual exception) — never generated response prose as proof. Tests are deterministic and reproducible; concurrency tests use real threads with deterministic final-state assertions (never timing as the pass/fail condition). See `PHASE_11_SECURITY_RED_TEAM_REPORT.md` for the full red-team methodology (five-stage: attack-surface mapping → automated adversarial tests → manual logic review → fix vulnerabilities → regression + report) and findings.

## 9. Known Limitations

- No formal penetration test or third-party security audit has been performed — this is internal, automated adversarial testing only.
- No fuzzing was used as a primary testing method (plan.md explicitly discourages relying on fuzzing alone); all tests here are deterministic, hand-crafted attack simulations.
- Reliability (Phase 10) and security (Phase 11) testing both run against in-memory test doubles for storage — no durable-storage failure mode has been exercised (see Phase 10 report's Limitations).
- This document will drift from the codebase over time if not updated alongside future security-relevant changes; treat it as a snapshot as of Phase 18, not a live scan.
- Twilio `X-Twilio-Signature` enforcement on the `/ws/call` WebSocket upgrade (Section 4) is implemented and unit/integration-tested offline, but explicitly disclosed as **best-effort** — Twilio's exact signing behavior for a Media Streams WebSocket upgrade has not been confirmed against real Twilio traffic (no live Twilio credentials/staging endpoint exist yet; see `LIVE_VERIFICATION_RUNBOOK.md` and Phase 17).

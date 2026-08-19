# Phase 12.13 — Persistent Security Regression Report

## Objective

Prove that adding PostgreSQL did not weaken any security boundary implemented during Phases 3–11.

## Critical Rule

**Persistence stores state. It does not replace authorization.** Every test in this step either tampers
with stored state directly (simulating a compromised database) or breaks the database at a specific moment,
and observes whether the real authorization/policy/confirmation gates are still consulted — never whether
stored state alone was trusted as a substitute for them.

## Existing Security Test Suite — Re-Run in Full

```
python -m unittest tests.test_auth_mode_separation tests.test_authorization tests.test_identity
    tests.test_pii_detector tests.test_policy_engine tests.test_privacy_service
    tests.test_security_red_team tests.test_oidc_provider -v
Ran 204 tests — OK
```

Every category this step names is covered by these files and passes unchanged:

| Category | Covered by |
|---|---|
| Policy bypass | `test_policy_engine.py`, `test_security_red_team.py` |
| Prompt injection | `test_security_red_team.py` (direct/indirect injection tests) |
| LLM authorization spoofing | `test_security_red_team.py`'s `TestSecurityInvariants` (invariant 2, 10), `test_tool_orchestrator.py`'s `TestLLMTrustBoundary` |
| Tool injection | `test_security_red_team.py`, `test_tool_orchestrator.py`'s `TestToolRegistry` |
| Authentication bypass | `test_auth_mode_separation.py`, `test_identity.py`, `test_oidc_provider.py` |
| Identity spoofing | `test_identity.py`, `test_oidc_provider.py` |
| Cross-user sessions | `test_security_red_team.py` invariant 5, `test_session_manager.py` |
| Cross-user memory | `test_security_red_team.py` invariant 6, `test_memory_manager.py` |
| Confirmation replay | `test_security_red_team.py`, Phase 11's original replay-race regression tests |
| Confirmation race | `test_concurrency.py`'s `TestSessionManagerConcurrency` |
| Idempotency abuse | `test_tool_reliability.py`'s `TestConcurrentDuplicateRequests` |
| Audit spoofing | `test_security_red_team.py` invariant 9 (`test_invariant_9_audit_logger_records_only_real_decisions`) |
| Privacy leakage | `test_privacy_service.py`, `test_pii_detector.py` |
| Log injection | `test_security_red_team.py` |

No category regressed. All 9 `TestSecurityInvariants` cases in `test_security_red_team.py` pass unchanged.

## Persistence-Specific Attacks — All 10 Required Scenarios

New file: **`tests/test_persistence_security_regression.py`** — 11 tests (one scenario produced two tests).

### 1. Tampered database ownership field

A session's `user_id` is rewritten directly in the database, bypassing `SessionManager` entirely (simulating
a compromised DB or malicious admin). **Finding**: the ownership check has exactly one source of truth — the
stored field, freshly queried every time. The original owner is correctly denied after tampering (proving no
separate cached "true owner" exists to fall back on), and the new (attacker-set) value is honored by the
check (the honest, expected consequence of a genuinely compromised database — not a gap this application
layer can paper over, and not a security *regression* since Phase 12 introduces no new such surface: the
in-memory equivalent was exactly as tamperable by anything with direct Python object access before).

### 2. Tampered pending action

A session's `pending_action` is rewritten to `"NOT_A_REGISTERED_TOOL"` while `AWAITING_CONFIRMATION`.
**Finding**: the tampered action is consumed exactly as stored (SessionManager has no opinion on whether an
action name is valid — that was never its job), but `ToolOrchestrator.invoke()` — the actual authority —
rejects it with `UNKNOWN_TOOL` before anything executes. Confirms the "pending state" origin grants zero
elevated trust; the full gate sequence runs regardless of where a `ToolRequest` came from.

### 3–7. Reused confirmation, reused idempotency key, cross-user idempotency key, stale confirmation, stale
session

Each denied/isolated exactly as designed in Steps 12.5/12.6/12.9 — re-verified here as explicit named
attacks rather than only positive-path tests. Scenario 5 (cross-user idempotency key) specifically confirms
User B reusing User A's exact key string gets an independent slot and **cannot read or piggyback on User
A's recorded result** (`get_recorded_action()` for each user's own key returns only their own data).

### 8. Database failure during authorization

Two angles: (a) `PolicyEngine.evaluate_authorization()` itself is stateless/YAML-driven and produces the
correct deny decision with no database involved at all; (b) with the idempotency database specifically
unreachable, an unauthorized caller (`USER_B` attempting `USER_A`'s resource) still cannot reach execution —
either the call raises `DatabaseUnavailableError` before reaching a decision, or (if authorization is
reached first) it correctly denies. No path lets a database outage skip past authorization into execution.

### 9. Database failure during confirmation

`SessionManager.try_consume_pending_confirmation()` against an unreachable repository raises — re-confirms
Step 12.11's finding in this security-focused context.

### 10. Database failure during tool execution bookkeeping

The most interesting finding in this step. Simulated: the tool executes **successfully** (the appointment is
genuinely cancelled), then the post-execution idempotency bookkeeping write (`update_result()`) fails
because the database drops at that exact moment.

**Finding, disclosed, not silently patched**: `ToolOrchestrator.invoke()` raises `DatabaseUnavailableError`
in this window — the bookkeeping write is unguarded, so a caller cannot distinguish "definitely failed" from
"succeeded but bookkeeping failed" from the exception alone. **This is not a security bypass** — failing
loudly is the safe direction, not a silent double-execution — and it was verified concretely, not just
argued: a legitimate retry with a *new* `request_id` (since the caller, having seen an exception, reasonably
believes the operation failed) is safely rejected by the underlying business logic's own state guard
(`MockAppointmentStore.cancel()` raises `"Appointment ... is already cancelled"` on a second attempt),
preventing a real duplicate side effect regardless of the orchestrator-level bookkeeping failure. This
finding is carried into "Remaining Technical Debt" for a future phase to decide whether bookkeeping-failure
should be caught and logged rather than raised (trading "caller sees a clean success despite a bookkeeping
gap" against "caller sees an honest but ambiguous failure") — a genuine design decision, not an oversight to
quietly fix here.

## Results

```
python -m unittest tests.test_persistence_security_regression -v
Ran 11 tests in 2.246s — OK (11/11 passed)
```

### Full Test Suite

```
python -m unittest discover -s tests -p "test_*.py"
Ran 708 tests in 67.649s
FAILED (errors=1)
```

- **708 = 697 (Phase 12.12 baseline) + 11 new.** No existing test's outcome changed.
- **1 pre-existing error**, unchanged: `test_retriever.py`'s `sentence_transformers` gap (documented in
  every prior Phase 12 report).

## Conclusion

No security boundary from Phases 3–11 was weakened by introducing PostgreSQL persistence. Every
persistence-specific attack surface this step names was tested directly against real (tampered, expired, or
unreachable) database state, not merely reasoned about — the critical rule held in every case: stored state
informs decisions, but every decision is still made by the same authoritative component (`PolicyEngine`,
`ToolOrchestrator`, `SessionManager`) that made it before Phase 12 existed.

`PHASE 12.13 COMPLETE`

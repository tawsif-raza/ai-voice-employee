# Phase 6 — Privacy and PII Protection Layer Report

## 1. Executive Summary

Phase 6 adds a deterministic Privacy and PII Protection layer: a regex-based `PIIDetector` (`src/agent/pii_detector.py`), a reusable `PrivacyService` (`src/agent/privacy_service.py`) that turns detected findings into policy decisions via a new `PolicyEngine.evaluate_pii()` method (Phase 3's `PolicyEngine` extended, not replaced or duplicated), and a centralized privacy-aware logging boundary (`src/agent/privacy_logging.py`). All three are wired — as optional, additive collaborators — into `MemoryManager` (Phase 5), `ToolOrchestrator` (Phase 4), and `ConversationManager`, plus a targeted fix to `src/api/server.py`'s streaming response so internal policy/intent reasoning never reaches API clients. A pre-implementation audit found this application's live request path had **no logging calls at all** referencing user content — the boundary built here is deliberately proactive, proven end to end by one real, wired-in log call, not a retrofit of pre-existing risky logging.

## 2. Data Flow

```
User Data (message, memory value, tool params/result)
      ↓
PIIDetector.detect()          — deterministic pattern matching only
      ↓
PIIFinding[]  {type, start, end, confidence, value}
      ↓
PolicyEngine.evaluate_pii(types, context)   — Phase 3's PolicyEngine, authoritative
      ↓
PolicyDecision {allowed, action, reason, policy}
      ↓
PrivacyService wraps it as PrivacyDecision {..., findings}
      ↓
   ALLOW ──────────────► pass through unchanged
   REDACT ─────────────► PrivacyService.redact() — spans replaced with [REDACTED_<TYPE>]
   RESTRICT ───────────► treated as REDACT by sanitize() (see Limitations)
   BLOCK ───────────────► value withheld entirely / write denied
      ↓
Destination: logs / MemoryManager storage / LLM prompt context /
             ToolOrchestrator params & results / API response
```

**Data-flow audit (Step 6.1) finding:** grepping `src/` for `logger.`/`logging.`/`print(` found no structured logging framework anywhere, and — critically — **no call in the live request path (`ConversationManager`, `PolicyEngine`, `ToolOrchestrator`, `SessionManager`, `MemoryManager`, `src/api/server.py`) prints or logs raw user content**. Every `print()` found is either offline tooling (`src/eval/evaluate.py`, `src/rag/build_index.py`) or an interactive CLI echoing its own conversation to its own operator (`src/inference/predict.py`'s REPL, `src/voice/client_tts.py`) — not a persistent sink. This is documented, not assumed.

## 3. PII Taxonomy

`PIIType`: `EMAIL`, `PHONE`, `ADDRESS`, `DATE_OF_BIRTH`, `IDENTIFIER`, `PAYMENT_INFORMATION`, `NAME`, `OTHER`. **Reliably detected:** `EMAIL`, `PHONE`, `PAYMENT_INFORMATION` (13–16 digit sequences), `IDENTIFIER` (this application's own `appt_<n>`/`order_<n>` record references). **Deliberately not detected:** `ADDRESS`, `DATE_OF_BIRTH`, `NAME` — free-text addresses and names have no reliable regex signature, and a bare date pattern cannot be distinguished from a routine appointment date without semantic understanding (explicitly out of scope: "avoid unnecessary ML-based PII classification"). This is a documented gap, not a silent omission — see Limitations.

This taxonomy is a second, complementary dimension to Phase 3/5's existing `restricted_fields` (named-**key** policy, e.g. a memory record whose `key` is literally `"medical_condition"`). `PIIType` governs detected **content patterns** in free text, regardless of which field they appear in. Both feed the same `PolicyEngine`.

## 4. Privacy Policy

`configs/policies/privacy.yaml` extended with a new `pii_policies` section — one file, not a competing config structure (plan.md's explicit instruction). `default_action: REDACT` (deliberately more conservative than `restricted_fields`' `ALLOW` default, since content-pattern detection is inherently higher-stakes than an explicitly-enumerated field list). Explicit per-(context, type) entries for all 7 contexts × 4 detectable types:

| Context | EMAIL | PHONE | PAYMENT_INFORMATION | IDENTIFIER |
|---|---|---|---|---|
| LOGGING | REDACT | REDACT | REDACT | ALLOW |
| MEMORY | RESTRICT | RESTRICT | BLOCK | ALLOW |
| SESSION | RESTRICT | RESTRICT | BLOCK | ALLOW |
| LLM_CONTEXT | REDACT | REDACT | REDACT | ALLOW |
| TOOL_INPUT | ALLOW | ALLOW | BLOCK | ALLOW |
| API_RESPONSE | REDACT | REDACT | REDACT | ALLOW |
| TELEMETRY | REDACT | REDACT | BLOCK | ALLOW |

`IDENTIFIER` is allowed everywhere by design — this application's own record references (`appt_1005`, `order_1001`) are routine, non-sensitive, and already used pervasively in normal conversation; treating them as PII would make ordinary debugging and conversation nonsensical. `LLM_CONTEXT`'s conservative EMAIL/PHONE REDACT (found and corrected during test-writing — see Section 9) matches Step 6.11's own worked example: a tool result's email field must not reach the LLM unredacted.

## 5. Redaction

`PrivacyService.redact(text, findings=None)`: replaces each finding's span with `[REDACTED_<TYPE>]`, processing spans in reverse position order so earlier indices remain valid while later ones are rewritten — proven correct for multiple findings in one string. `PrivacyService.sanitize(value, context)` recursively handles `str`/`dict`/`list`/`tuple` (plan.md Step 6.7's nested-structure requirement) — every string leaf is independently inspected and redacted/blocked/passed-through per `decide()`; non-string leaves (`int`/`float`/`bool`/`None`) pass through unchanged.

## 6. LLM Trust Boundary

> **The LLM cannot determine whether information is private, safe to store, safe to log, or safe to transmit.**

No method on `PrivacyService`/`PolicyEngine.evaluate_pii()` accepts a boolean toggle or free-text claim as an authorization signal — confirmed structurally (`test_attack_1_llm_disabling_redaction_is_ignored` inspects every public `PrivacyService` method's signature for a `redact` parameter; none exists). `decide()`/`redact()`/`sanitize()` only ever accept plain text to *inspect* and a context string the *caller* supplies — never a pre-computed "is this safe" claim. All five Step 6.14 attack scenarios pass: disabled-redaction claim, storage-authorization claim, consent claim, a tool literally returning raw PII (sanitized before the caller sees it), and PII embedded in an exception message (sanitized in the log payload).

## 7. Security

- **Cross-user isolation:** unchanged from Phase 5 — `SessionManager`/`MemoryManager`'s existing `user_id` scoping is still the enforcement point; `PrivacyService` itself is stateless per call (`TestCrossUserPrivacyAtServiceLevel` confirms one `decide()` call cannot influence another). PII detected in User A's memory value is redacted/blocked at write time (`MemoryManager.persist_memory()`) independent of which user it belongs to — the *content* rule is the same for everyone; *which* records a user can read is still `MemoryManager`'s existing per-`user_id` filter.
- **Unauthorized memory access:** unchanged from Phase 5's `remove_memory()`/`list_allowed_memory()` scoping; Phase 6 adds a second, independent gate (`evaluate_pii` on the *value*) on top of Phase 3's existing gate (`evaluate_privacy` on the *key*).
- **Tool privacy:** `ToolOrchestrator.invoke()` now (when `privacy_service` is configured) checks each string parameter against `TOOL_INPUT` policy *before* execution, and sanitizes the tool's raw result against `LLM_CONTEXT` policy *after* execution, before returning — proven directly by `test_attack_4_tool_returning_raw_pii_is_sanitized_before_reaching_caller`.
- **API exposure:** `src/api/server.py`'s streaming `/generate` response was leaking `ConversationManager`'s full internal result dict (`{"done": True, **item}`) — including `policy`/`intent` routing internals and `degraded`/`error` metadata — into the wire format. Fixed to build the "done" event from named, client-relevant fields only (`response`, `is_handoff`, `latency_ms`, and a narrow `tool_status` string), proven by a new test (`test_streaming_done_event_excludes_internal_metadata`). The non-streaming response was already field-scoped since Phase 1.
- **Logging protection:** `PrivacySanitizingFilter` sanitizes both `record.msg` and a structured `record.privacy_payload` extra before any handler sees them — tested against actual captured `logging.LogRecord`s (not the `redact()` function in isolation), per plan.md's explicit instruction.

## 8. Tests

- **Tests added:** 17 (`tests/test_pii_detector.py`) + 28 (`tests/test_privacy_service.py`, including all 5 Step 6.14 attacks and the logging-boundary tests) + 1 (`tests/test_server_api.py`'s new API-boundary test) = 46.
- **Tests executed:** full repository suite, 314 tests.
- **Passing:** 313.
- **Failing:** 0 caused by Phase 6. 1 pre-existing environment error (`test_retriever.py` — `faiss` not installed, unrelated).
- **Privacy attack tests:** all 5 mandatory scenarios (Step 6.14) pass.
- **Logging tests:** capture real `logging.LogRecord` output via a handler and assert on it directly, not the redaction function.
- **Cross-user tests:** `PrivacyService` statelessness confirmed at the service level; cross-user data isolation itself remains `SessionManager`/`MemoryManager`'s Phase-5-tested responsibility (Phase 6 adds a second, orthogonal content gate on top, not a replacement).

## 9. Compatibility

- **PolicyEngine:** extended (`evaluate_pii()`, two new `Action` values `REDACT`/`RESTRICT`), not replaced. All 45 pre-existing `test_policy_engine.py` tests pass unmodified.
- **ConversationManager / SessionManager / ToolOrchestrator / MemoryManager:** `privacy_service` is an additive, optional constructor parameter everywhere it was added; `None` (the default for every raw class constructor) preserves exact pre-Phase-6 behavior. `build_conversation_manager()` enables it by default (`privacy_enabled=True`) for parity with every other Phase 3–5 capability.
- **Clinical Safety Guard / RAG:** untouched.
- **API:** the non-streaming `/generate` contract is unchanged; the streaming "done" event is now narrower (see Section 7) — a compatible tightening, not a breaking field removal from anything a client was documented to rely on (the removed fields were never part of `ChatResponse`'s documented contract).
- **A real, honest bug was found and fixed during this phase's own test-writing**, not left in: the initial `LLM_CONTEXT` policy allowed raw emails/phones through by mistake, contradicting the plan's own worked tool-result example — caught by `test_attack_4`, corrected in `configs/policies/privacy.yaml`, with the earlier (now-wrong) test updated to match the corrected, intended policy.

## 10. Limitations

Stated plainly, per plan.md's explicit instruction:

- **Regex-only detection has real false-negative gaps.** Names, addresses, and dates of birth are not detected at all (see Section 3) — a user typing "My name is Jane Doe" or "I live at 12 Main St" will not be flagged or redacted anywhere in this system today.
- **Phone/payment patterns can have false positives or false negatives** on non-US formats, international numbers, or digit sequences that coincidentally match the length heuristic (e.g. a 16-digit non-payment reference number would be flagged as `PAYMENT_INFORMATION`). The detector is tuned for common cases, not exhaustive correctness — explicitly acceptable per plan.md ("do not pretend regex can perfectly detect all PII").
- **`RESTRICT` has no behavior distinct from `REDACT`** in `PrivacyService.sanitize()` today — both redact the matched span. A genuinely different "usable but limited" semantic (e.g. showing only the last 4 digits of a phone number) was not built this phase; `RESTRICT` exists in the taxonomy and is policy-configurable, but its runtime treatment is currently identical to `REDACT`.
- **No enterprise compliance claim is made or implied.** This is a technical privacy enforcement architecture — deterministic detection, policy-driven redaction/blocking, a centralized logging boundary. It is not HIPAA, GDPR, or SOC 2 compliant, and no organizational/legal review of this system has occurred. Nothing in this report or the code should be read as such a claim.
- **Free-text `value` fields in `MemoryRecord`/tool params are the only content actually scanned.** Metadata dicts, session `pending_parameters`, and conversation `history` entries are not currently routed through `PrivacyService` — only the specific integration points built this phase (memory persistence/retrieval, tool input/output, one demonstrative log call) are covered.

## 11. Remaining Technical Debt

- Conversation `history` (prior turns passed into `ConversationManager.handle_turn()`) is never sanitized before reaching the LLM prompt or being echoed back — only newly-injected memory context and tool results are covered.
- `RESTRICT` needs a real, distinct implementation (partial masking) if a future consumer actually needs that semantic instead of full redaction.
- No telemetry/observability system exists yet for the `TELEMETRY` context's policy rules to actually apply to (Phase 8's job) — the config entries exist and are tested at the `PolicyEngine` level, but nothing in the live request path currently emits telemetry events.
- Session `pending_parameters`/`confirmation_state` (Phase 5) are not scanned for embedded PII before persistence, only `MemoryRecord.value` is.
- The PII detector's phone-number regex is US-format-biased; broader international coverage is unimplemented.

## 12. Recommended Next Phase

Proceed to Phase 7 (Authentication, Authorization and Identity Boundary) as `plan.md` specifies next. It is the natural continuation of the `AuthContext`/`trusted state` plumbing Phases 4–6 all built against but never had a real identity provider behind (every test in this codebase still constructs `AuthContext` by hand) — and real authentication is a prerequisite for `user_id`-scoped session/memory/privacy behavior to mean anything over the live API, since `src/api/server.py` does not yet accept or forward any identity at all.

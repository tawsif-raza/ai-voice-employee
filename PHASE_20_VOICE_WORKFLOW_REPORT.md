# Phase 20 — Voice Workflow Completeness Report

## 1. Objective

Per plan.md's Phase 20: ensure every critical business workflow can be completed through the voice channel, evaluating each `ToolOrchestrator` tool for voice support, authentication, confirmation, safety review, error recovery, retry behavior, cancellation, interruption, and audit events — and implement any missing voice adapters.

## 2. Scope

`ToolOrchestrator`'s complete tool registry (`src/agent/mock_tools.py::build_default_tool_registry()`) has exactly four registered actions: `BOOK_APPOINTMENT`, `CANCEL_APPOINTMENT`, `RESCHEDULE_APPOINTMENT`, `ORDER_LOOKUP`. There is no separate "voice adapter" layer by design: `src/voice/voice_pipeline.py`'s `VoiceCallHandler._execute_turn()` calls the exact same `ConversationManager.handle_turn()` the HTTP API uses (confirmed by reading both call sites), so every tool already available to `ConversationManager` is already voice-reachable generically — there is structurally no way to register a tool that is HTTP-only or voice-only.

## 3. Per-Tool Checklist

| | `BOOK_APPOINTMENT` | `CANCEL_APPOINTMENT` | `RESCHEDULE_APPOINTMENT` | `ORDER_LOOKUP` |
|---|---|---|---|---|
| Voice-reachable | Yes (shared `handle_turn()` path) | Yes | Yes | Yes |
| Authentication required | Yes (`Permission.BOOK_APPOINTMENT`) | Yes (`Permission.CANCEL_APPOINTMENT`) | Yes (`Permission.CANCEL_APPOINTMENT`, reused — no separate reschedule permission exists) | Yes (`Permission.READ_ORDER`) |
| Confirmation required | No (non-destructive create) | Yes (`destructive=True`) | Yes (`destructive=True`) | No (read-only) |
| Safety review | Structural — `ClinicalSafetyGuard` runs before intent routing/tool dispatch for every turn regardless of which tool would be reached | same | same | same |
| Error recovery | `ToolOrchestrator`'s Phase 10 fail-closed/retry-safe gate sequence (unchanged) | same | same | same |
| Retry behavior | `NON_IDEMPOTENT_WRITE` — never auto-retried (would create a duplicate appointment) | `IDEMPOTENT_WRITE` — safe to retry | `IDEMPOTENT_WRITE` — safe to retry | `READ_ONLY` — always safe to retry |
| Cancellation / interruption | Barge-in aborts the active turn (`VoiceCallHandler.handle_interruption()`); see Section 4 for a gap found and fixed here | same | same | same |
| Audit event | `ToolOrchestrator.invoke()` emits `TOOL_REQUESTED`/`ALLOWED`/`DENIED`/`STARTED`/`SUCCEEDED`/`FAILED` generically for every action (Phase 8, unchanged) | same | same | same |

Every column was already complete except "Cancellation / interruption," and — discovered during this review, not by the checklist itself — the authentication row for all four tools was silently broken end-to-end in the real voice pipeline. No missing voice adapter needed to be implemented; the gaps were in the shared authentication plumbing every tool depends on, described next.

## 4. Findings

All four permission-gated tools depend on the same `AWAITING_AUTHENTICATION` telephony-PIN mechanism reviewed in Phase 18 (`PHASE_18_PRODUCTION_SECURITY_GATE_REPORT.md`, F-04). Reviewing its voice-pipeline half for this phase's "authentication"/"interruption" checklist columns surfaced two further real defects in the same feature, both fixed here:

### F-05 (HIGH) — Voice pipeline read caller authentication state from the wrong object

**Component:** `src/voice/voice_pipeline.py`, `VoiceCallHandler._execute_turn()`.

**Defect:** Every turn's `AuthContext` was built from `self.session.metadata.get("authenticated_caller")` — `self.session` is the local, in-process `CallSession` object, whose `.metadata` is populated exactly once, at call start, from Twilio's start-frame `custom_parameters` (`voice_pipeline.py`'s `on_start()`), and never updated again. The real flag is written by `ConversationManager._execute_pending_authentication()` (Phase 18's fix) onto the `SessionManager`-persisted `Session` object — a different object entirely. Reading the wrong one meant `is_authenticated` was **always `False`** for a real caller, regardless of whether they spoke the correct PIN: the entire telephony authentication feature was silently non-functional end-to-end, for every one of the four tools above, independent of Phase 18's F-04 fix.

**Fix:** `is_authenticated` is now resolved via `self.conversation_manager.session_manager.get_session(self.session.session_id)` — the same persisted session `ConversationManager` itself writes to and reads from — matching the pattern this file already uses elsewhere (`handle_interruption()`'s existing `sm.get_session(...)` call).

### F-06 (MEDIUM, same code, found alongside F-05) — Non-functional permissions on the reconstructed identity

Same root cause as Phase 18's F-04: the `AuthContext` built here also set `roles=["caller"]` with no `permissions`, which can never pass `PolicyEngine.evaluate_authorization()`'s permission check regardless of the `authenticated` flag. Fixed identically to F-04: `roles=(Role.USER.value,)` and `permissions=permissions_for_roles((Role.USER,))` once authenticated.

### F-07 (LOW) — `AWAITING_AUTHENTICATION` not cleared on barge-in

`VoiceCallHandler.handle_interruption()` already clears a pending `AWAITING_CONFIRMATION` workflow state on barge-in (so an interrupted "yes/no" doesn't get accidentally resolved by unrelated follow-up speech) but did not do the same for `AWAITING_AUTHENTICATION` — a caller who barged in while being asked for a PIN would leave the session stuck in that state, and their next (unrelated) utterance would be regex-scanned for a 4-digit number as if it were a PIN attempt. Given Phase 18's fail-closed default, the worst case is an unwarranted handoff to a human rather than a security issue, but it is a real conversational-completeness defect ("interruption" is an explicit Phase 20 checklist column). Fixed by clearing both `AWAITING_CONFIRMATION` and `AWAITING_AUTHENTICATION` on barge-in, in the same existing code path.

## 5. Files Changed

- `src/voice/voice_pipeline.py` — F-05/F-06 (AuthContext construction), F-07 (barge-in clears `AWAITING_AUTHENTICATION`).
- `tests/test_voice_canary_auth.py` — new `test_authenticated_caller_auth_context_has_usable_permissions`, asserting the *actual* `AuthContext` object passed to `ConversationManager` on a post-authentication turn (not just the session metadata flag the pre-existing tests here checked) has `authenticated=True` and a working `Role.USER` permission set.
- `tests/test_voice_canary_barge_in_improved.py` — new `test_pending_authentication_invalidated_on_barge_in`, mirroring the existing `test_pending_confirmation_invalidated_on_barge_in`.

## 6. Tests

- **New tests this phase:** 2.
- **Targeted re-run:** `pytest tests/test_voice_canary_auth.py tests/test_voice_canary_barge_in_improved.py tests/test_voice_pipeline.py -q` — all pass.
- **All voice tests:** `pytest tests/ -k voice -q` — 79 passed.
- **Full project suite:** `pytest -q` — **914 passed, 0 failed** (912 after Phase 18 + 2 new), 52 subtests passed, same 2 pre-existing unrelated SQLAlchemy warnings as Phase 18's run.

## 7. Security Impact

F-05/F-06 mean Phase 18's F-04 fix, while correct in isolation, did not by itself make telephony authentication functional in the live voice pipeline — this phase's fix is what actually closes the loop end-to-end. Net effect across Phase 18 + Phase 20: the mock telephony-PIN mechanism now either (a) fails closed to human handoff for every attempt when unconfigured (the safe default, Phase 18), or (b) genuinely authenticates and grants a real, usable, least-privilege identity when explicitly configured for a controlled canary (Phase 18 + Phase 20 together) — previously it could do neither correctly.

## 8. Known Limitations

- Real per-caller telephony identity verification remains a documented, un-invented product gap (Phase 18 Section 8 / `docs/SECURITY.md` Section 7) — out of scope for both phases per plan.md Rule 5.
- No new voice-specific tool adapter was required because none was missing; this phase's substantive work was fixing the shared authentication plumbing every voice-reachable tool depends on, not adding new tool integrations.
- STT reconnect/retry-on-failure and duplicate-START-frame handling (both carried-over Phase 16 residual risks) were not re-examined in this phase — out of scope for "workflow completeness," which concerns the tool-invocation path, not STT/Twilio transport reliability.

## 9. Acceptance Criteria

Per plan.md Phase 20: "Every approved voice workflow has a complete Caller → STT → policy → tool → result → TTS path." **Met** for all four registered tools — confirmed structurally (single shared `handle_turn()` path) and, for the three tools requiring authentication for an anonymous caller, now genuinely functional end-to-end after F-05/F-06/F-07, not merely structurally present.

## 10. Final Status

`PHASE 20 COMPLETE`

## 11. Next Phase

Phase 19 (Real-World Voice Quality) requires real PSTN/Twilio access and is blocked the same way Phase 17 is (no live environment). Phase 21 (Performance & Load) is independent of live external credentials — it can be exercised against the real FastAPI application with simulated/local providers, the same approach Phase 16 already used. Proceeding to Phase 21 next per plan.md Rule 17.

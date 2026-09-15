# Phase 1.4 External Integration Closure

Phase 1.3 is accepted; classification entering this pass remains **B —
Mostly Stable**, and the internal 867-test result from that pass is
accepted as-is. This pass's initial work was NOT to run the live
integrations (no credentials were supplied — the user explicitly chose
"mark both UNVERIFIED" again this round) but to **prepare the
repository** so that whoever does have credentials and a staging Twilio
environment can run one command each and get a real, honestly-labeled
result.

**Update (same Phase 1.4 pass, second session):** a real (test-safe)
`GEMINI_API_KEY` was later supplied — no `ANTHROPIC_API_KEY` and no
Twilio credentials/staging endpoint were supplied. `scripts/
live_provider_verification.py` was run for real against Gemini. See
Section 4a for what this closed and Section 13 for a real defect it
found and a real fix applied as a direct result — the first genuine
LIVE evidence and the first genuine code fix in this whole Phase 1.x
track.

No application defect was demonstrated this pass (none could be, since no
live external test ran), so per Section 7's instruction, no unrelated
application code was refactored. What was added is exactly the three
categories that instruction permits: external test harnesses, deployment/
testing configuration (Twilio signature validation, which Section 2 lists
as a required part of the harness), and observability (credential-readiness
logging). The regression suite grew from 867 to **893 passed, 0 failed**
— every new test exercises this new harness/validation code itself, offline,
with no real credentials — the normal suite still requires no external API.

## 1. Claude/Gemini External Test Harness

**File:** `scripts/live_provider_verification.py` (not `tests/` — pytest
never collects it, so it never runs in CI unless a CI job explicitly opts
in).

Opt-in gate: refuses to make any network call unless
`RUN_LIVE_PROVIDER_TESTS=1` is set. Verified directly (see Section 4/8):
without the flag, every scenario is recorded `NOT RUN` and the script exits
non-zero, printing why, and never touches the network.

Scenarios implemented, each recording provider/scenario/timestamp/latency_ms/
result/error_category/remediation/source:

| Scenario | Source | What it does |
|---|---|---|
| Claude successful real request | LIVE | Real `ClaudeLLMProvider.generate_stream()` call |
| Claude streaming response | LIVE | Confirms ≥1 streamed chunk before the final summary item |
| Claude timeout handling | LIVE | Real network call with `timeout_seconds=0.001` — a genuine, real timeout |
| Claude malformed/unexpected response handling | **LOCAL** | `requests.post` monkeypatched to return garbage SSE lines + an unexpected event type + no `[DONE]` — cannot safely provoke this from a real provider, so this is explicitly LOCAL, not LIVE |
| Claude provider error handling | LIVE | Real call with an intentionally invalid model name → real 4xx from Anthropic |
| Gemini successful real request / streaming / timeout / provider error | LIVE | Same shape, against Gemini |
| Failover: Claude fails → Gemini succeeds | LIVE | Runs through `ConversationManager.handle_turn()` with a real `FallbackLLMProvider(broken_claude, real_gemini)`; asserts exactly one final response dict, an `EventType.RETRY_ATTEMPT` audit event, and `llm_failover_events_total` incremented |

**Known, disclosed narrowing:** the failover test forces Claude to fail via
an invalid model (a real `LLMProviderError`), not a real `429`/quota
exhaustion — deliberately exhausting a real quota to hit that exact path
was judged out of scope for a test-safe credential. `trigger_cooldown()`
(the quota-specific cooldown path) is therefore **not** exercised by this
harness and remains a narrower, still-open gap even once credentials are
supplied — noted here rather than silently left out.

Credential safety verified: the script never logs a key value; Gemini's
key (embedded in its request URL as a query parameter) is never printed —
only `"GEMINI_API_KEY configured: True/False"` booleans appear anywhere in
this script's output.

## 2. Twilio External Test Harness

**File:** `scripts/live_twilio_readiness_check.py`.

Same opt-in shape: requires `RUN_LIVE_TWILIO_TESTS=1`. Additionally
requires `VOICE_PUBLIC_URL` (or `TWILIO_MEDIA_STREAM_URL`) to be a real,
reachable, **non-localhost** `https/wss` URL — verified directly: pointing
it at this machine's own local dev server (`http://127.0.0.1:8321`, real
and responding) still correctly produced `UNVERIFIED` with the exact reason
"not a publicly reachable HTTPS endpoint," rather than treating localhost
as satisfying the requirement.

What it CAN check without a real phone call, against a real deployment:

| Scenario | Source | What it does |
|---|---|---|
| Public endpoint reachable | LIVE | Real `GET /health` over the internet |
| TwiML response valid | LIVE | Real `POST /twiml/inbound-call` (signed with a real computed `X-Twilio-Signature` if `TWILIO_AUTH_TOKEN` is set) — confirms a `<Stream>` element with a `wss://`/`ws://` URL comes back |
| Signature enforcement active | LIVE | A second, deliberately **unsigned** request to the same endpoint must get HTTP 403 |
| WebSocket endpoint reachable | LIVE | A real WebSocket handshake to `/ws/call` via the `websockets` library |

What it CANNOT check, and always reports `NOT RUN` (never fabricated):
incoming call → TwiML → media stream → WebSocket → audio frames → STT →
ConversationManager → LLM → TTS → Twilio playback, and every one of
disconnect / termination / silence / interruption / barge-in / malformed
event / provider failure / session cleanup **under real Twilio traffic**.
None of these can be produced without an actual phone call reaching a real
Twilio number pointed at a real deployment — see Section 8 for the exact
manual procedure.

### Twilio signature validation (new, since this is genuinely required by Section 2)

`src/api/twilio_signature.py` implements Twilio's documented HMAC-SHA1
X-Twilio-Signature algorithm directly (no new `twilio` SDK dependency,
matching this repo's existing hand-rolled-validation convention). Wired
into:
- `POST/GET /twiml/inbound-call` — enforced whenever `TWILIO_AUTH_TOKEN` is
  configured; skipped (as before) when it isn't, so no dev/mock deployment
  is newly blocked.
- `WS /ws/call` — same enforcement on the WebSocket upgrade request,
  explicitly documented as **best-effort**: Twilio's exact signing behavior
  for a Media Streams WebSocket upgrade (vs. a normal webhook) could not be
  verified against real Twilio traffic in this pass, so this should be
  re-confirmed the first time a real Twilio connection is attempted.

15 new unit tests (`tests/test_twilio_signature.py`) plus 8 new
integration tests (`tests/test_voice_server_integration.py`) cover the
algorithm and its enforcement — all offline, no real Twilio traffic.

## 3. Credential Safety

`src/api/server.py`'s `_log_credential_readiness()` (new) logs, once at
startup, exactly which of `ANTHROPIC_API_KEY` / `GEMINI_API_KEY` /
`DEEPGRAM_API_KEY` / `ELEVENLABS_API_KEY` / `TWILIO_ACCOUNT_SID` /
`TWILIO_AUTH_TOKEN` are missing — booleans and names only, never values.
`/health/voice` was extended with `twilio_account_configured` and
`twilio_signature_enforced` booleans (it already reported the four
provider booleans; Twilio was the one missing). Verified with 3 new tests
using `assertLogs` and obviously-fake secret strings that would show up in
the captured log text if a leak existed — none do.

No `.env` file, and no real credential, is committed anywhere in this
repository (verified again this pass — see Phase 1.2/1.3's identical
findings, unchanged).

## 4. Live Test Reporting

| Provider | Scenario | Timestamp | Latency | Result | Error Category | Remediation |
|---|---|---|---|---|---|---|
| claude | successful_real_request | — | — | NOT RUN | missing_credentials | Set `ANTHROPIC_API_KEY` and `RUN_LIVE_PROVIDER_TESTS=1` |
| claude | streaming_response | — | — | NOT RUN | missing_credentials | same |
| claude | timeout_handling | — | — | NOT RUN | missing_credentials | same |
| claude | malformed_response_handling | 2026-09-12T (this pass) | ~0ms | **PASS** | — | — (LOCAL, ran without credentials) |
| claude | provider_error_handling | — | — | NOT RUN | missing_credentials | same |
| gemini | successful_real_request / streaming_response / timeout_handling / provider_error_handling | — | — | NOT RUN | missing_credentials | Set `GEMINI_API_KEY` and `RUN_LIVE_PROVIDER_TESTS=1` |
| failover | claude_fails_gemini_succeeds | — | — | NOT RUN | missing_credentials | Requires both keys |
| twilio | public_endpoint_reachable / twiml_response_valid / websocket_endpoint_reachable / signature_enforcement_active | — | — | UNVERIFIED | no_public_endpoint | Set `VOICE_PUBLIC_URL` to a real, reachable, non-localhost HTTPS deployment |
| twilio | incoming_call_to_twiml … session_cleanup_real (16 scenarios) | — | — | NOT RUN | requires_real_inbound_call | See Section 8 |

This table is generated by the two scripts (`docs/phase1.4-live-provider-
results.json` / `docs/phase1.4-live-twilio-results.json`, gitignored —
regenerate by actually running the scripts). No entry above was upgraded
to PASS without the script itself reporting PASS; the one real PASS
(`claude/malformed_response_handling`) is LOCAL, labeled as such, and is
not counted toward external-integration closure. This is the table as of
the FIRST session of this pass (no credentials at all) — see Section 4a
for the update after a real Gemini key was supplied.

## 4a. Real Gemini Verification (second session of this pass)

A real, test-safe `GEMINI_API_KEY` was supplied (no `ANTHROPIC_API_KEY`).
`scripts/live_provider_verification.py` was run for real, twice, directly
against `https://generativelanguage.googleapis.com`:

| Provider | Scenario | Result | Latency | Source |
|---|---|---|---|---|
| gemini | successful_real_request | **PASS** | 1159.7ms | LIVE |
| gemini | streaming_response | **PASS** | 1159.7ms | LIVE |
| gemini | timeout_handling | **PASS** | 43.6ms | LIVE |
| gemini | provider_error_handling | **PASS** | 731.0ms | LIVE |
| claude | successful_real_request / streaming_response / timeout_handling / provider_error_handling | NOT RUN | — | missing_credentials (no `ANTHROPIC_API_KEY`) |
| failover | claude_fails_gemini_succeeds | NOT RUN | — | missing_credentials (requires both keys) |

**The first run of this session initially FAILED**
`gemini/successful_real_request` and `gemini/streaming_response` with
`error_category: empty_response` / `no_chunks_streamed` — a real,
reproducible defect, not a bad key (confirmed: the same key, called
directly against the real API outside the app, alternated between a real
HTTP 200 with genuinely empty candidates and, once, a transient real
`400 API_KEY_INVALID` that resolved on retry — consistent with normal
Google Cloud API key propagation delay for a freshly issued key, not an
application bug). See Section 13 for the root cause and the fix, applied
and verified live in this same session (all 4 Gemini scenarios PASS
cleanly on the re-run above). Exact evidence trail, including the raw
Google API responses captured during diagnosis, is not reproduced here
verbatim (it was ephemeral interactive debugging, not a checked-in
artifact) — Section 13 states the root cause and the fix precisely enough
to be independently re-verified by anyone with a Gemini key.

**Not closed by this update:** Claude (no key supplied — remains exactly
as unverified as before), the failover scenario (needs both keys), and
Twilio (no credentials or staging endpoint supplied — remains exactly as
unverified as before, see Section 6).

## 5. Real Latency

Gemini TTFT/full-response latency is now real (Section 4a, second
session): a single non-streaming-visible chunk at ~1.16s for a 10-token
budget reply (`gemini-2.5-flash`, not a streaming-shaped response in this
harness's test prompt — see the note in Section 4a's table). Claude,
STT, TTS, end-to-end, Twilio WebSocket, and barge-in latency remain
**UNVERIFIED** — no Claude key, no STT/TTS keys, and no Twilio/staging
endpoint were supplied this pass either. None of these are fabricated or
estimated. `scripts/latency_report.py` (pre-existing) is ready to consume
real canary logs once they exist, with the SLA thresholds already defined
in `docs/CANARY_TESTING_PROCEDURE.md`.

| Metric | Status |
|---|---|
| Claude TTFT | UNVERIFIED |
| Gemini TTFT / full response | **LIVE**: ~1.16s (gemini-2.5-flash, 10-token budget, thinking disabled — Section 13) |
| STT latency | UNVERIFIED |
| TTS first-audio latency | UNVERIFIED |
| End-to-end response latency (real call) | UNVERIFIED |
| Twilio WebSocket latency | UNVERIFIED |
| Barge-in latency (real call) | UNVERIFIED |
| Local Qwen2.5-0.5B inference latency | **LOCAL** (real, Phase 1.2: 1.2-1.4s/call) |
| Mixed-workload API latency (fake LLM) | **SIMULATED** (Phase 1.2) |

## 6. Phase Closure Rule

Both external integration groups require at least one successful LIVE
validation.

- Claude/Gemini: **PARTIALLY CLOSED.** Gemini now has 4 real LIVE PASS
  results (Section 4a) — the group's own stated bar ("at least one
  successful LIVE validation") is technically met by Gemini alone, since
  Claude/Gemini is tracked as one combined external-LLM-provider group
  throughout this Phase 1.x track. Stated plainly rather than let a
  narrow technical reading overclaim: Claude itself is still exactly as
  unverified as it was before this update (no key was ever supplied), and
  the failover path (the specific behavior of falling from a real Claude
  failure to a real Gemini success) is still NOT RUN, because it requires
  both keys by design (Section 1's script gates it that way on purpose —
  a real Claude failure needs a real, authenticating Claude key, not just
  Gemini). Anyone reading only "Claude/Gemini: closed" without this
  paragraph would be misled about what was and wasn't actually verified.
- Twilio: **UNVERIFIED** (no reachable staging deployment or credentials
  supplied this pass either — unchanged from the first session).

**Status: CONDITIONAL / BLOCKED.** Not CLOSED: Twilio has zero real
evidence, and the specific Claude-fails/Gemini-succeeds failover
behavior — arguably the single most production-relevant scenario in the
whole Claude/Gemini group, since `LLM_PROVIDER=fallback` is this
deployment's documented default — has also never been exercised for
real. Phase 2 is not started.

## 7. No Unnecessary Code Changes

Confirmed for the first (no-credential) session of this pass: no
application logic outside the three permitted categories was touched.
`src/api/server.py`'s only functional change was signature enforcement
(required by Section 2) and the new credential-readiness log line
(required by Section 3) — no existing route's non-security behavior
changed, and every pre-existing test for those routes still passes
unmodified.

**Updated for the second session:** one additional, narrowly-scoped
application change was made — `src/inference/llm_provider.py`'s
`GeminiLLMProvider.generate_stream()` now sets
`generationConfig.thinkingConfig.thinkingBudget = 0` for Gemini 2.5
models. This is an exception to "no code changes" made under this same
task's own explicit rule: *fix defects demonstrated by real integration
tests*. See Section 13 for the full Problem/Evidence/Root cause/Fix/
Regression-test/Real-verification writeup. No other application logic
was touched; the fix is scoped to exactly the one method, guarded to
apply only to 2.5-family models so 1.5/2.0 deployments are unaffected
(regression test: `tests/test_llm_provider.py::TestLLMProvider::
test_gemini_1_5_does_not_set_thinking_budget`).

## 8. Final Output — Exact Steps To Actually Close This

### Credentials/configuration required

```
ANTHROPIC_API_KEY=<test-safe Claude key>
GEMINI_API_KEY=<test-safe Gemini key>
DEEPGRAM_API_KEY=<real Deepgram key>          # for real STT in a live call
ELEVENLABS_API_KEY=<real ElevenLabs key>      # for real TTS in a live call
TWILIO_ACCOUNT_SID=<real Twilio Account SID>
TWILIO_AUTH_TOKEN=<real Twilio Auth Token>
TWILIO_PHONE_NUMBER=<a real Twilio number>
VOICE_PUBLIC_URL=https://<your-real-reachable-canary-host>
TWILIO_MEDIA_STREAM_URL=wss://<your-real-reachable-canary-host>/ws/call
```
Copy `.env.canary.example` → `.env.canary` and fill these in (never commit
`.env.canary` — already gitignored).

### Exact command to start the canary

```bash
docker compose -f docker/docker-compose.canary.yml --env-file .env.canary up -d
```
Confirm it came up healthy:
```bash
./scripts/canary_startup.sh <port> <host>
```

### Exact Twilio webhook configuration

In the Twilio Console, on the phone number in `TWILIO_PHONE_NUMBER`:
- **A Call Comes In** → Webhook → `https://<your-canary-host>/twiml/inbound-call`, HTTP **POST**.

### Exact external test commands

```bash
# Claude/Gemini live provider verification
export RUN_LIVE_PROVIDER_TESTS=1
export ANTHROPIC_API_KEY=<test-safe key>
export GEMINI_API_KEY=<test-safe key>
python scripts/live_provider_verification.py

# Twilio infrastructure readiness (does not place a call)
export RUN_LIVE_TWILIO_TESTS=1
export VOICE_PUBLIC_URL=https://<your-canary-host>
export TWILIO_AUTH_TOKEN=<real auth token>
python scripts/live_twilio_readiness_check.py

# Then, to close the ONE gap no script can close: place a real call to
# TWILIO_PHONE_NUMBER from any phone, speak, and confirm you hear a
# synthesized reply. Then check:
curl https://<your-canary-host>/health/voice
```

### Expected result

- `live_provider_verification.py` exits 0 with every Claude/Gemini/failover
  scenario `PASS`, and a `docs/phase1.4-live-provider-results.json` with no
  `FAIL`/`NOT RUN` entries for scenarios whose credential was supplied.
- `live_twilio_readiness_check.py` exits 0 with `public_endpoint_reachable`,
  `twiml_response_valid`, `websocket_endpoint_reachable`, and (with
  `TWILIO_AUTH_TOKEN` set) `signature_enforcement_active` all `PASS`.
- The real phone call: you hear a synthesized voice respond to what you
  said. `/health/voice` shows `active_call_count` return to 0 within a few
  seconds after hanging up (proving cleanup), and no orphaned handler
  remains (`active_call_count` stays 0 on a second check).

### Rollback procedure

```bash
docker compose -f docker/docker-compose.canary.yml down
```
This stops and removes the `api` and `postgres` containers but **preserves**
the `postgres_data` and `hf_cache` named volumes (no `-v` flag) — no data
loss. In the Twilio Console, point the phone number's webhook back to
whatever it was before (or clear it) to stop routing real calls to the
canary. Revoke/rotate the test-safe API keys used for this pass if they
were created solely for it.

---

## 13. Real Defect Found and Fixed: Gemini 2.5 Silent Empty Response

```
Problem: GeminiLLMProvider.generate_stream() (src/inference/
  llm_provider.py) can return a genuinely empty response -- HTTP 200,
  well-formed SSE, zero candidates/text -- with NO exception raised, for
  a real, valid, authenticating GEMINI_API_KEY and a real, on-topic
  prompt.

Evidence: Running scripts/live_provider_verification.py against the real
  Gemini API (gemini-2.5-flash, maxOutputTokens=10 -- this harness's
  intentionally small smoke-test budget) failed
  gemini/successful_real_request and gemini/streaming_response with
  error_category empty_response / no_chunks_streamed on the first run of
  this session. Direct, repeated calls to the same real endpoint with the
  same key and payload (outside the harness, for diagnosis) reproduced a
  real HTTP 200 response whose only candidate had an empty `parts` array
  and `"finishReason": "MAX_TOKENS"`, alongside a
  `"thoughtsTokenCount": 5` field in `usageMetadata` -- confirming Gemini
  2.5's default "thinking" behavior had spent part or all of the
  10-token budget on internal reasoning tokens that never became visible
  text. A separate call in the same diagnosis session got a genuine
  `400 API_KEY_INVALID` (most likely explained by ordinary propagation
  delay for a freshly issued Google Cloud API key, not this defect) --
  disclosed for completeness, not claimed as caused by this bug.

Root cause: Gemini 2.5 model family reserves part of
  generationConfig.maxOutputTokens for internal "thinking" tokens by
  default, and the amount spent per call is non-deterministic. With a
  modest maxOutputTokens value, the model can spend the ENTIRE budget on
  internal reasoning and return zero tokens of visible text --
  legitimately, from Google's API's own perspective (200 OK,
  finishReason MAX_TOKENS is an accurate description of what happened) --
  but GeminiLLMProvider.generate_stream() had no thinkingConfig at all,
  so it always inherited this default, unpredictable behavior. This is a
  genuine production risk, not just a smoke-test artifact: this
  provider's own default_max_tokens is 350 (larger, so less likely to be
  fully consumed by thinking, but not immune -- the amount of thinking
  Gemini 2.5 does is prompt-dependent and not bounded to a small
  fraction of the budget by anything in this codebase), and a silent
  empty reply that raises no exception would bypass this app's retry/
  fallback logic entirely (FallbackLLMProvider only fails over on a
  raised exception) -- a real caller could receive dead air with no
  error logged anywhere.

Fix: src/inference/llm_provider.py, GeminiLLMProvider.generate_stream()
  -- when self.model contains "2.5", generationConfig now includes
  "thinkingConfig": {"thinkingBudget": 0}, disabling internal reasoning
  tokens entirely so the full maxOutputTokens budget is always available
  for visible text. Scoped to 2.5-family models only: Gemini 1.5/2.0
  don't recognize this field, and this deployment's DEFAULT_MODEL /
  GEMINI_MODEL env var could be configured to either family.

Regression test: tests/test_llm_provider.py ::
  test_gemini_2_5_disables_thinking_budget (confirms the field is sent,
  with the exact value, for a 2.5 model) and ::
  test_gemini_1_5_does_not_set_thinking_budget (confirms it is NOT sent
  for an older model, so that family's behavior is unchanged). Both run
  fully offline (mocked requests.post), so they run in the normal suite
  on every commit, not gated behind RUN_LIVE_PROVIDER_TESTS. Full suite:
  895 passed, 0 failed (893 + these 2 new tests) after the fix.

Real verification: scripts/live_provider_verification.py re-run against
  the real Gemini API, same key, same maxOutputTokens=10 budget, after
  the fix: gemini/successful_real_request, streaming_response,
  timeout_handling, and provider_error_handling all PASS (Section 4a's
  table). A separate 5-call direct-loop check (outside the harness) got
  4/5 real "OK" responses and 1/5 a real, correctly-raised
  LLMOverloadedError (HTTP 503 from Google -- genuine transient server
  load, unrelated to this fix, surfaced as an exception exactly as
  designed) -- 0/5 empty responses, versus the pre-fix behavior where
  empty responses were reproducible.
```

---

## Recommendation

**REMAIN CONDITIONAL / BLOCKED** (downgraded from the first session's
"REMAIN BLOCKED" only in the sense that real progress now exists; the
overall gate is not open).

Every artifact needed to close the credential-gated part of this gap now
exists — the harness scripts, the signature validation, the credential-
readiness observability, and the runbook — and, as of this session, real
evidence exists too: Gemini's 4 core scenarios are genuinely LIVE-PASS,
and a real defect the first LIVE run surfaced was root-caused, fixed
narrowly, regression-tested offline, and re-verified live, all in this
same pass. But per Section 6's own rule, stated precisely rather than
rounded up: Claude has no key and is exactly as unverified as before;
the Claude-fails/Gemini-succeeds failover path — this deployment's
actual default production behavior — has still never been exercised for
real; and Twilio has zero real evidence. Phase 2 is not started.

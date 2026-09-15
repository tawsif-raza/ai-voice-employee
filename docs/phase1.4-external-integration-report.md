# Phase 1.4 External Integration Closure

Phase 1.3 is accepted; classification entering this pass remains **B —
Mostly Stable**, and the internal 867-test result from that pass is
accepted as-is. This pass's job was NOT to run the live integrations
(no credentials were supplied — the user explicitly chose "mark both
UNVERIFIED" again this round) but to **prepare the repository** so that
whoever does have credentials and a staging Twilio environment can run
one command each and get a real, honestly-labeled result.

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
not counted toward external-integration closure.

## 5. Real Latency

**No LIVE latency measurements exist from this pass** — no live call was
made. The only real numbers available are from Phase 1.2 (LOCAL: real
Qwen2.5-0.5B CPU inference, 1.2-1.4s per call) and Phase 1.2's SIMULATED
mixed-workload figures (fake LLM services). Claude TTFT, Gemini TTFT, STT
latency, TTS first-audio latency, end-to-end response latency, Twilio
WebSocket latency, and barge-in latency are all **UNVERIFIED** — none are
fabricated or estimated here. `scripts/latency_report.py` (pre-existing)
is ready to consume real canary logs once they exist, with the SLA
thresholds already defined in `docs/CANARY_TESTING_PROCEDURE.md`.

| Metric | Status |
|---|---|
| Claude TTFT | UNVERIFIED |
| Gemini TTFT | UNVERIFIED |
| STT latency | UNVERIFIED |
| TTS first-audio latency | UNVERIFIED |
| End-to-end response latency (real call) | UNVERIFIED |
| Twilio WebSocket latency | UNVERIFIED |
| Barge-in latency (real call) | UNVERIFIED |
| Local Qwen2.5-0.5B inference latency | **LOCAL** (real, Phase 1.2: 1.2-1.4s/call) |
| Mixed-workload API latency (fake LLM) | **SIMULATED** (Phase 1.2) |

## 6. Phase Closure Rule

Both external integration groups require at least one successful LIVE
validation. Neither has one:

- Claude/Gemini: **NOT RUN** (no credentials supplied)
- Twilio: **UNVERIFIED** (no reachable staging deployment supplied)

**Status: CONDITIONAL / BLOCKED.**

## 7. No Unnecessary Code Changes

Confirmed: no application logic outside the three permitted categories was
touched. `src/api/server.py`'s only functional change is signature
enforcement (required by Section 2) and the new credential-readiness log
line (required by Section 3) — no existing route's non-security behavior
changed, and every pre-existing test for those routes still passes
unmodified.

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

## Recommendation

**REMAIN BLOCKED.**

Every artifact needed to close this gap now exists — the harness scripts,
the signature validation, the credential-readiness observability, and this
exact runbook — and none of it required guessing at credentials or
fabricating a result. But per Section 6's own rule, Phase 1 stays CLOSED
only once both external groups have at least one real PASS, and neither
does yet. Phase 2 is not started.

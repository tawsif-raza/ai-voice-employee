# Real Telephony Readiness

**Date:** 2026-10-08 · **Code:** `hardening/h3-voice-reliability` (H0–H4 + H3, PRs #1/#2, CI green) · **Companion:** [`REAL_TELEPHONY_TEST_REPORT.md`](REAL_TELEPHONY_TEST_REPORT.md)

Phone → Twilio → Deepgram STT → safety/intent → Gemini/Groq → ElevenLabs TTS → Twilio → phone.

## 1. How a real call reaches the application

1. Caller dials the Twilio number. Twilio POSTs the number's **Voice webhook** → `POST https://<public-host>/twiml/inbound-call`.
2. The server checks `X-Twilio-Signature` (HMAC-SHA1 over the reconstructed URL + form params, keyed by `TWILIO_AUTH_TOKEN`). Outside `APP_ENV=dev` an unsigned or mis-signed request is rejected with 403, and a missing `TWILIO_AUTH_TOKEN` rejects everything.
3. Response is TwiML: `<Connect><Stream url="wss://<public-host>/ws/call"/></Connect>` then `<Say>` (`VOICE_FALLBACK_MESSAGE`), which Twilio speaks if the service ends the stream. **Exception:** a call that produced an urgent-risk answer, or a medication answer the caller never heard, is instead redirected by a Twilio call-update request to a safety message (`VOICE_URGENT_FALLBACK_MESSAGE`) before the stream closes (docs/CLINICAL_SAFETY.md, "Voice: when TTS fails").
4. Twilio opens `wss://<public-host>/ws/call` (signed again). Admission check (`MAX_CONCURRENT_CALLS`), then `connected` → `start` (opens Deepgram) → 20 ms μ-law `media` frames → `stop`.
5. Each Deepgram final transcript runs one turn through `ConversationManager.handle_turn()` (clinical guard → session → intent/policy → tools or LLM) in the dedicated voice pool; text streams to ElevenLabs; μ-law audio goes back as `media` frames, `clear` on barge-in, `mark` at turn end.

**There is no greeting**: after connecting, the caller hears silence until they speak (see the report, P1).

## 2. What is ready

| Area | Status | Evidence |
|---|---|---|
| Production image | Ready | `production-image` CI green: lock == installed, non-root, 1022 in-image tests, migrations, smoke 27/27, fail-safe 8/8 |
| Security posture | Ready | Outside dev: OIDC required to start, Twilio signature required, mock voice / mock PIN refused |
| Clinical guard on voice | Ready | Simulated call: dosage question → pharmacist handoff, LLM never called |
| Voice deadlines & graceful failure | Ready (simulated) | 18/18 simulated scenarios, measured within ~0.1 s of configured deadlines |
| Real adapter protocol code | Ready (simulated) | Real Deepgram/Gemini/Groq/ElevenLabs adapters exercised against local fakes; Deepgram handshake verified against the real endpoint earlier (HTTP 401 with a fake key) |
| Logging hygiene | Ready (simulated) | No keys, no unmasked caller number, no transcripts or answers in 388 log lines |

## 3. What is missing (blocks a real call)

| Missing | Why | Owner |
|---|---|---|
| Twilio account + **dedicated test number** | Nothing to call | Owner |
| `TWILIO_AUTH_TOKEN` (test account) | Signature check; endpoints refuse without it outside dev | Owner |
| `DEEPGRAM_API_KEY`, `ELEVENLABS_API_KEY` | Real STT/TTS | Owner |
| `GEMINI_API_KEY`, `GROQ_API_KEY` | LLM (free tier). No key is configured on this machine | Owner |
| Public **HTTPS + WSS** endpoint | Twilio only connects to `https://` webhooks and `wss://` streams | See §5: a local tunnel is enough for testing; the AWS deployment cannot receive calls today (an idle ECS task and RDS instance remain; not internet-reachable; decision pending) |
| OIDC values | Required to start outside dev. For a **telephony-only** test any well-formed issuer/audience/JWKS URL lets the server start (they are only used to validate text-API tokens) | Owner |

### Pre-call risks to check on the very first call (do not "fix" speculatively)

1. **WebSocket signature URL (highest risk).** Twilio's security docs say that for Media Streams WSS handshakes you may need to *append a trailing `/`* to the URL passed to signature validation ([Twilio: Webhooks security](https://www.twilio.com/docs/usage/security)). Our `/ws/call` check validates `https://<host>/ws/call` (no trailing slash). The scheme Twilio signs (`https` vs `wss`) is not explicit in that document. **Symptom if wrong:** every call is rejected at the WebSocket (`Rejected /ws/call connection: missing or invalid X-Twilio-Signature`), and the caller hears the TwiML `<Say>` immediately. **Proposed fix, after confirmation:** accept the documented URL variants (with/without trailing slash, `https`/`wss`), each still requiring a valid HMAC with `TWILIO_AUTH_TOKEN`, with a regression test. This is not a weakening; it is not applied yet because the change policy requires real-call evidence.
2. **Webhook URL reconstruction behind a proxy/tunnel.** `/twiml/inbound-call` signs `X-Forwarded-Proto` + `X-Forwarded-Host`/`Host` + path. If the tunnel rewrites `Host`, the webhook returns 403. Check the first webhook's status in the Twilio debugger.
3. **`TWILIO_MEDIA_STREAM_URL`** must be set to the public `wss://…/ws/call`; otherwise the stream URL is derived from request headers.
4. **Safety fallback (call-update request).** This is unverified against real Twilio. Check that the test account's auth token can update its own calls, that the `start` frame's `accountSid`/`callSid` arrive as expected, and that Twilio speaks the `<Say>` after the stream is redirected. **Symptom if wrong:** the log line `…with the urgent safety fallback: NOT delivered, generic fallback plays`, and `voice_safety_fallback_failures_total` increments.

## 4. Exact configuration for a real test

Set as environment variables (or a secrets store), **never** committed or pasted into chat:

```
APP_ENV=staging                      # strict posture; never dev for a real call (dev skips the Twilio signature)
AUTH_MODE=production
OIDC_ISSUER_URL=…  OIDC_AUDIENCE=…  OIDC_JWKS_URL=…   # real IdP, or well-formed placeholders for a telephony-only test
TWILIO_ACCOUNT_SID=…  TWILIO_AUTH_TOKEN=…             # TEST Twilio account
TWILIO_MEDIA_STREAM_URL=wss://<public-host>/ws/call
DEEPGRAM_API_KEY=…  ELEVENLABS_API_KEY=…  ELEVENLABS_VOICE_ID=… (optional)
GEMINI_API_KEY=…  GROQ_API_KEY=…                      # LLM_PROVIDER auto-selects free_fallback
PERSISTENCE_MODE=dev                                  # or production + DATABASE_URL of a TEST database
VOICE_MOCK_SERVICES / TELEPHONY_MOCK_PIN              # leave UNSET (refused outside dev anyway)
MAX_CONCURRENT_CALLS=2   MAX_CALL_DURATION_SECONDS=300  # conservative for testing
LOG_FORMAT=json  LOG_LEVEL=INFO
```

Run the production image (`docker/Dockerfile.production`) or, for a quick local test, the same code with `requirements-production.lock` on Python 3.12. RAG stays off (production image), which also avoids the RAG cold-start stall found in the simulation.

## 5. Exact test procedure

1. **Local rehearsal (no credentials):** `APP_ENV=dev python scripts/telephony_simulation.py` → expect `RESULT: ALL PASS`.
2. **Start the server** with §4's configuration on port 8000; confirm `GET /health` and `GET /ready` = 200 and `GET /health/voice` reports `twilio_signature_enforced: true`.
3. **Expose it over HTTPS/WSS:** e.g. `cloudflared tunnel --url http://localhost:8000` (installed on this machine) or an existing HTTPS ALB + domain. Set `TWILIO_MEDIA_STREAM_URL` to `wss://<tunnel-host>/ws/call` and restart.
4. **Twilio console (test number):** Voice → "A call comes in" → Webhook → `POST https://<tunnel-host>/twiml/inbound-call`.
5. **First call — connectivity only:** call, stay silent 3 s, then say "What are your opening hours?". Check pre-call risks §3 in this order: webhook 200 (Twilio debugger) → `/ws/call` accepted (no "Rejected /ws/call" log) → `Connected to Deepgram` log → audio heard.
6. **Run the test matrix** in the report (§3 there), one scenario per call, recording timestamps from the server's `TIME_TO_FIRST_AUDIO` / `TURN_*` events and a stopwatch for what the caller hears.
7. **Failure tests:** use a second deployment configuration with an invalid provider key (e.g. ElevenLabs) — never by editing code — to observe the real fallback and whether Twilio plays the `<Say>`. With ElevenLabs disabled this way, say an **urgent** test phrase ("I'm having trouble breathing after taking my medicine") and confirm the caller hears the urgent safety message from Twilio, **not** "call back later" (pre-call risk 4).
8. **After testing:** rotate any key that was used in an environment that logged verbosely; delete test call recordings in Twilio if any were enabled (keep recording **off**).

## 6. Safety precautions

- Use the **test** Twilio number/account and non-production data only; speak only the scripted, fictional scenarios. No real patient names, numbers, or conditions.
- Keep the clinical guard, authentication, and signature checks **on** (`APP_ENV=staging`). If a test seems to need weakening one, stop and report instead.
- Keep Twilio call **recording off**; do not save audio locally.
- Logs already exclude transcripts and answers and mask caller numbers (verified in simulation); still treat logs from real calls as sensitive and do not paste them anywhere public.
- Secrets only in environment variables / a secrets manager; never in the repo, chat, or tickets.
- Do not modify production infrastructure for testing; a local tunnel is sufficient.

## 7. What can be tested locally vs. needs a real phone

| Locally (done — `scripts/telephony_simulation.py`) | Needs a real phone call |
|---|---|
| Media Streams protocol handling, turn flow, history | Real audio quality, codecs, packet loss |
| Real adapter code vs fake Deepgram/ElevenLabs/Gemini/Groq | Real STT accuracy on natural speech, accents, noise |
| Barge-in: `clear` timing, cancellation, no stale audio, history | Echo/AEC on speakerphones, false barge-ins, Twilio playback buffering |
| Gemini error/empty → Groq; both down; Gemini hang | Real provider latency and its variance |
| ElevenLabs temporary/permanent failure → stream close | **Whether Twilio actually plays the `<Say>` after the stream closes** |
| Deepgram unreachable / drop + reconnect / exhaustion | Real Deepgram endpointing timing and reconnect behaviour |
| Dead media (20 s), call duration, consecutive failures | Twilio's own stream timeouts and hang-up semantics |
| 5 concurrent calls, slot release, log hygiene | Signature validation against real Twilio requests |

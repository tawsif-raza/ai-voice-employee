# Real Telephony Test Report

**Date:** 2026-10-08 · **Code:** `hardening/h3-voice-reliability` (H0–H4 + H3; PRs #1 and #2, CI green) · **Readiness/procedure:** [`REAL_TELEPHONY_READINESS.md`](REAL_TELEPHONY_READINESS.md)

## Executive Summary

- **Overall result: real-telephony validation NOT performed.** No Twilio account/number and no Deepgram, ElevenLabs, Gemini or Groq credentials exist in this environment, and no public HTTPS/WSS endpoint is deployed. No real call was placed, and none of the "real call" results below are claimed.
- **What was done instead:** a full local **simulation** (`scripts/telephony_simulation.py`): the real server under uvicorn, the **real** Deepgram/Gemini/Groq/ElevenLabs adapters against local fake provider endpoints, and a fake Twilio client speaking the real Media Streams protocol, with the production deadlines and the production image's configuration (RAG off). **18/18 scenarios pass**, plus resource and log checks.
- **Ready / Not Ready:** **Not ready to claim telephony works.** Ready for a first controlled real test once credentials and a tunnel exist.
- **Biggest discovered issue:** a **RAG cold-start stall** — the first retrieval after process start takes **18.7 s** (lazy embedding-model load), longer than the 15 s first-token deadline, so the first caller gets a holding phrase and then an apology. **Does not affect the production image** (RAG off); affects the full `docker/Dockerfile` image.
- **Biggest remaining risk:** the **WebSocket signature URL** for real Twilio Media Streams (Twilio documents a trailing-slash quirk we don't handle). If wrong, every real call is rejected at `/ws/call`. Must be checked on the first real call.

## Environment

| Component | This test |
|---|---|
| Twilio | **Not available.** Simulated by a local Media Streams client (`connected`/`start`/20 ms μ-law `media`/`stop`). |
| Deepgram | **Not available.** Real `DeepgramSTTService` against a local fake WebSocket (Results / SpeechStarted / drop / refuse). |
| Gemini, Groq | **Not available.** Real `GeminiLLMProvider` / `GroqLLMProvider` over real HTTP+SSE against a local fake (ok / error / empty / hang). |
| ElevenLabs | **Not available.** Real `ElevenLabsTTSService` with an httpx transport fake (ok / temporary failure / permanent failure). |
| Application | `src/api/server.py` under uvicorn, `APP_ENV=dev` (needed: no Twilio token to sign with), production deadlines from `configs/reliability.yaml`, RAG off as in `Dockerfile.production`. Python 3.14 (local venv). |
| Deployment endpoint | None. The AWS deployment cannot receive calls (an idle ECS task and RDS instance remain; not internet-reachable; decision pending). |

## Test Results

"Real" = with a real phone call. "Simulated" = `scripts/telephony_simulation.py`.

| Test | Real | Simulated | Observations (simulated) |
|---|---|---|---|
| Basic call | NOT RUN | **PASS** | 3 turns on one call, each answered with audio and an end-of-turn `mark`; history continuous |
| STT | NOT RUN | **PASS** | Real adapter sends `Authorization: Token …`, streams frames, maps `Results` → final transcript |
| LLM | NOT RUN | **PASS** | Real Gemini adapter, header auth, SSE parsing |
| TTS | NOT RUN | **PASS** | Real ElevenLabs adapter, `xi-api-key`, μ-law frames to Twilio |
| Clinical guard on voice | NOT RUN | **PASS** | Dosage + interaction question → pharmacist handoff; LLM never called |
| Barge-in | NOT RUN | **PASS** (3/3) | `clear` within 3–22 ms of the interruption; 0 old-turn clauses synthesized afterwards; new answer spoken; history ends with the new answer |
| Gemini fallback | NOT RUN | **PASS** | Gemini 500 → Groq answered; Gemini empty stream → treated as failure → Groq answered |
| Both LLMs down | NOT RUN | **PASS** | Fixed apology spoken (not silence) |
| Gemini hang | NOT RUN | **PASS** | Filler at **4.04 s**, apology at **15.02 s** after speech end |
| ElevenLabs failure | NOT RUN | **PASS** | Temporary: apology spoken, call stays open. Permanent: service closes the stream (code 1000) after 0.03 s. **Twilio `<Say>` playback NOT verifiable** |
| STT reconnect | NOT RUN | **PASS** | Unreachable at start → stream closed in 0.01 s; drop → reconnected and answered; drop + refused reconnects → 3 bounded attempts, stream closed **3.06 s** after the drop |
| Dead media | NOT RUN | **PASS** | Stream closed **19.99 s** after media stopped (config 20 s); inactivity metric +1 |
| Call duration | NOT RUN | **PASS** | Shortened limit 5 s → closed at **5.01 s**; duration metric +1, inactivity metric +0 |
| Consecutive failures | NOT RUN | **PASS** | Failures 1 and 2: apology + call continues; failure 3: call ends (code 1000), each at ~15.0 s (first-token deadline) |
| Concurrency | NOT RUN | **PASS** | 5 simultaneous calls all answered; slots released afterwards |
| Security/logging | NOT RUN | **PASS** | 388 log lines: no Gemini/Groq/Deepgram/ElevenLabs sentinel key, caller number never unmasked, no transcript or answer text |
| Real ElevenLabs API (one-off) | **BLOCKED** | — | The only provider credential available was an ElevenLabs value placed in `.env.canary.example`. The real API rejected every request with **HTTP 400** `api_key_id_used_as_api_key`: it is a key *ID*, not a secret key (`sk_…`). Request-format variants all gave the same answer, so the adapter is not at fault. Confirmed: the adapter turns this real 400 into `TTSError` (call apologises/ends instead of going silent); the value never appeared in logs. No audio synthesized. |

## Latency Measurements

**No real latency was measured** — that requires real providers and a real network. The simulation measures only the application's **own overhead** with zero-latency fakes (median of 3 turns):

| Stage | Simulated overhead |
|---|---|
| Caller speech end (STT final) → LLM request | 21.8 ms |
| LLM first text → TTS request | 4.3 ms |
| TTS response → first audio frame to Twilio | 1.1 ms |
| **Speech end → first audio frame (total overhead)** | **32.8 ms** |
| Barge-in → Twilio `clear` | 3–22 ms |

Deadline accuracy (configured → measured): filler 4 s → 4.04 s · first-token 15 s → 15.02 s · media inactivity 20 s → 19.99 s · call duration (shortened) 5 s → 5.01 s · STT reconnect exhaustion (3 × 1 s) → 3.06 s.

**Are the deadlines reasonable?** Not decidable without real calls. The overhead (~33 ms) is negligible, so real latency will be dominated by Deepgram endpointing (configured 300 ms), LLM time-to-first-token and ElevenLabs synthesis. The 4 s filler and 15 s first-token deadline leave wide margins for typical free-tier latency; the 15 s deadline is *shorter* than one provider's 30 s read timeout, so a hung Gemini produces an apology before Groq is tried (measured: apology at 15 s). That is a deliberate trade-off to tune with real data, not a defect.

## Reliability Findings

### Confirmed bugs (with evidence)

| # | Finding | Evidence | Scope |
|---|---|---|---|
| B1 | **RAG cold start blocks the first turn ~18.7 s** (embedding model loaded lazily on the first `retrieve()`); later calls ~0.05 s. On a call, the first turns stall past the 15 s deadline (filler then apology), and new utterances supersede them. | `retrieve()` timed: 18.74 s, 0.08 s, 0.04 s; simulation with `--rag`: every turn's first audio was the filler, no turn completed | Full image / dev only. **Production image unaffected** (RAG off). |

### Evidence-based risks (need a real call to confirm)

| # | Risk | Evidence |
|---|---|---|
| R1 | WebSocket signature URL: Twilio documents a trailing-`/` requirement for Media Streams handshakes; we validate without it, and the signed scheme is unconfirmed | [Twilio: Webhooks security](https://www.twilio.com/docs/usage/security) |
| R2 | Twilio playing the TwiML `<Say>` after the server closes the stream | Simulation proves the server closes the stream with code 1000; Twilio's behaviour is unverified |
| R3 | Webhook signature URL behind a tunnel/proxy (`Host` / `X-Forwarded-*` rewriting) | Code reads; untestable without Twilio |

### Configuration tuning (needs real data)

- Filler 4 s / first-token 15 s / turn 60 s vs real provider latency; provider read timeouts (30 s) vs the 15 s first-token deadline.
- Deepgram `endpointing=300` ms vs natural pauses (cutting callers off vs slow responses).

### Expected limitations

- A worker blocked in a hung provider call stays busy until that provider's own timeout. Observed: after all scenarios (including three hung-LLM turns), 8 idle voice-pool threads remained and the process went from 4 to 16 threads, all bounded by the pool (2 × `MAX_CONCURRENT_CALLS`), with 0 active calls and 0 registered handlers. Acceptable for now; revisit only if real traffic shows pool exhaustion.

### Product / UX findings (decisions, not bugs)

| # | Finding |
|---|---|
| U1 | **No greeting at call start**: the caller hears silence until they speak. Callers usually wait for a greeting. |
| U2 | When both LLMs fail, the fixed text says "Let me connect you with a human agent", but no transfer exists: the caller is told something that does not happen. |

### Cosmetic

- `LLM_FAILURE_RESPONSE` uses an em dash, which shows as mojibake in Windows console output only (audio unaffected).

### Future improvements

- Warm the RAG retriever at startup (if RAG is ever deployed).
- A `scripts/telephony_simulation.py` CI job (currently local-only; ~2.5 min runtime).

## Recommended Changes

| Priority | Change |
|---|---|
| **P0** | Obtain a test Twilio account/number, Deepgram/ElevenLabs keys (an ElevenLabs **secret key** `sk_…`, not a key ID), Gemini/Groq keys and a tunnel; supply them as environment variables, never in tracked files; run the readiness procedure. Nothing else can validate R1–R3. |
| **P0** (on first call) | If R1 is confirmed (`Rejected /ws/call … X-Twilio-Signature`), accept the documented signed-URL variants, each still HMAC-verified, with a regression test; re-run CI. |
| **P1** | U1: decide the greeting (text + whether it can be barged-in). |
| **P1** | U2: replace "connect you with a human agent" with wording that matches what actually happens, or implement a transfer (a product decision). |
| **P1** | Tune deadlines with real latency data (keep the config; change values only on evidence). |
| **P2** | B1: warm RAG at startup in RAG-enabled images (only if RAG is deployed). |
| **P3** | Run the simulation in CI. |

## Final Recommendation

**Option B — fix and retest**, in this sense: **real-telephony validation has not happened**, so Option A is not justified. Nothing so far points to an architecture problem (no Option C). The simulation shows the pipeline behaves correctly end to end with real adapter code, but the decisive questions — signature validation against real Twilio, `<Say>` after stream close, real latency, echo/barge-in on real phones — can only be answered by real calls. Next: provide test credentials and run the readiness procedure; decide U1/U2 before beta.

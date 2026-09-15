# Live Verification Runbook

Purpose: the exact, complete procedure to close Phase 1's two remaining
blockers (real Claude/Gemini, real Twilio) once real credentials and a
staging deployment are available. Nothing in this document requires or
stores a real secret — every value below is a placeholder or a variable
name.

---

## 1. Required Credentials

| Credential | Purpose | Required? |
|---|---|---|
| `ANTHROPIC_API_KEY` | Real Claude request/streaming/timeout/error/failover verification | **Required** |
| `GEMINI_API_KEY` | Real Gemini request/streaming/timeout/error/failover verification | **Required** |
| `TWILIO_ACCOUNT_SID` | Twilio account identity | **Required** |
| `TWILIO_AUTH_TOKEN` | Twilio webhook signature validation | **Required** |
| `TWILIO_PHONE_NUMBER` | The number that will receive the one controlled inbound test call | **Required** |
| `DEEPGRAM_API_KEY` | Real STT during the live call | **Required** for a real (non-mock) voice call |
| `ELEVENLABS_API_KEY` | Real TTS during the live call | **Required** for a real (non-mock) voice call |
| `DATABASE_URL` (Postgres) | Staging persistence, if `PERSISTENCE_MODE=production` | Optional — only if testing production persistence; `PERSISTENCE_MODE=dev` (in-memory) is sufficient to close the two Phase 1 blockers |
| `HF_TOKEN` | Higher Hugging Face rate limits when loading the base model | Optional |
| `OIDC_ISSUER_URL` / `OIDC_AUDIENCE` / `OIDC_JWKS_URL` | Real OIDC production authentication | Optional — not required to close the Claude/Gemini/Twilio gaps; see Section 8's authentication procedure, which uses the existing dev-mode test tokens already in source (`test-user-token`, `test-admin-token` — not secrets, see `src/agent/identity.py`) |

No other credential is referenced anywhere in this repository
(`.env.canary.example` is the complete list).

---

## 2. Required Staging Infrastructure

- A **publicly reachable HTTPS endpoint** (e.g. `https://<canary-host>`) — verified in Phase 1.4 to genuinely require non-localhost; a tunnel (ngrok, Cloudflare Tunnel, or a real cloud VM/container host with a real domain/TLS cert) satisfies this if a permanent host isn't available.
- A **WebSocket endpoint** at `<same host>/ws/call`, reachable through any reverse proxy/load balancer in front of it (the proxy must pass through the WebSocket upgrade, not just plain HTTP).
- **PostgreSQL** — only required if `PERSISTENCE_MODE=production`. `docker/docker-compose.canary.yml` already provisions this as the `postgres` service; no separate staging DB is needed if using that compose file.
- **DNS/tunnel configuration** — the HTTPS hostname above must resolve and present a valid TLS certificate (Twilio requires HTTPS for its webhook and WSS for Media Streams). If using a tunnel tool, its assigned public URL is `VOICE_PUBLIC_URL`.

---

## 3. Environment Variables

**Required:**
```
ANTHROPIC_API_KEY
GEMINI_API_KEY
TWILIO_ACCOUNT_SID
TWILIO_AUTH_TOKEN
TWILIO_PHONE_NUMBER
DEEPGRAM_API_KEY
ELEVENLABS_API_KEY
VOICE_PUBLIC_URL          # e.g. https://<canary-host>
TWILIO_MEDIA_STREAM_URL   # e.g. wss://<canary-host>/ws/call
VOICE_MOCK_SERVICES=false
```

**Optional:**
```
DATABASE_URL              # only if PERSISTENCE_MODE=production
PERSISTENCE_MODE          # defaults to dev (in-memory) if unset
LLM_PROVIDER              # defaults to "fallback" (Claude primary, Gemini fallback)
HF_TOKEN
OIDC_ISSUER_URL / OIDC_AUDIENCE / OIDC_JWKS_URL   # only for real production OIDC auth
PORT / LOG_LEVEL / LOG_FORMAT
CANARY_DEPLOYMENT_ID / OTEL_SERVICE_NAME
```

**Test-only (never set outside a deliberate verification run):**
```
RUN_LIVE_PROVIDER_TESTS=1   # opt-in gate for scripts/live_provider_verification.py
RUN_LIVE_TWILIO_TESTS=1     # opt-in gate for scripts/live_twilio_readiness_check.py
```

All values are supplied via `.env.canary` (copied from `.env.canary.example`,
already gitignored) or the shell environment — never hardcoded, never
committed, never printed by any script in this repository.

---

## 4. Exact Startup Command

```bash
cp .env.canary.example .env.canary
# edit .env.canary: fill in every value from Section 3
docker compose -f docker/docker-compose.canary.yml --env-file .env.canary up -d
```

Verify it came up healthy:
```bash
# Linux/macOS:
./scripts/canary_startup.sh <port> <host>
# Windows:
./scripts/canary_startup.ps1 -Port <port> -HostName <host>
```

---

## 5. Exact External Provider Verification Command

```bash
export RUN_LIVE_PROVIDER_TESTS=1
export ANTHROPIC_API_KEY=<real test-safe key>
export GEMINI_API_KEY=<real test-safe key>
python scripts/live_provider_verification.py
```
Produces `docs/phase1.4-live-provider-results.json` (gitignored).

---

## 6. Exact Twilio Readiness Command

```bash
export RUN_LIVE_TWILIO_TESTS=1
export VOICE_PUBLIC_URL=https://<canary-host>
export TWILIO_AUTH_TOKEN=<real auth token>
python scripts/live_twilio_readiness_check.py
```
Produces `docs/phase1.4-live-twilio-results.json` (gitignored). Only after
this reports `public_endpoint_reachable`, `twiml_response_valid`, and
`websocket_endpoint_reachable` as `PASS` should Section 7 be attempted.

---

## 7. Exact Procedure for One Controlled Inbound Call

1. In the Twilio Console, open the number in `TWILIO_PHONE_NUMBER`.
2. Under **Voice Configuration → A Call Comes In**, set: Webhook, `https://<canary-host>/twiml/inbound-call`, HTTP **POST**. Save.
3. From any phone, place exactly one call to `TWILIO_PHONE_NUMBER`.
4. Speak a simple question (e.g. "What are your hours?") and wait for a spoken reply.
5. Hang up normally.
6. Immediately after, `curl https://<canary-host>/health/voice` and confirm `active_call_count` is `0`.

This is the only step in this runbook that involves a real telephone call.
Do not repeat it more than necessary to gather the evidence Section 9 requires.

---

## 8. Exact Procedure for Each Scenario

**Normal Claude request** — covered automatically by Section 5's script
(`claude/successful_real_request`, `claude/streaming_response`). To also
confirm it through the real voice path: complete Section 7 once with
`ANTHROPIC_API_KEY` valid; the spoken reply came from Claude.

**Gemini fallback** — covered automatically by Section 5's script
(`failover/claude_fails_gemini_succeeds`, which forces a real Claude
provider error and confirms a real Gemini response). To confirm through the
real voice path: temporarily set `ANTHROPIC_MODEL` to an invalid value
(e.g. `not-a-real-model`), redeploy, repeat Section 7 once, confirm you
still get a spoken reply, then revert `ANTHROPIC_MODEL` and redeploy again.

**Barge-in** — during the call in Section 7, start speaking while the
assistant is mid-reply. Confirm the assistant's audio stops within
roughly one turn's audio buffer (not a fixed hard number without a real
measurement) and it starts listening to the new input.

**Safety/handoff** — during a call, ask a clinical/dosage question (e.g.
"how many mg of ibuprofen should I take?"). Confirm the reply is the
handoff response, not a direct dosage answer.

**Authentication** — this canary's `AUTH_MODE` determines what to check:
- If left at `dev` (default; no OIDC configured), the boundary is
  verified via the deterministic dev tokens already in source
  (`src/agent/identity.py` — not secrets):
  `curl -H "Authorization: Bearer test-user-token" https://<canary-host>/generate -d '{"message":"hi"}'`
  should succeed; the same request with `Bearer not-a-real-token` should
  return 401; with no header at all it should succeed anonymously
  (documented existing behavior, unchanged by this pass).
- Separately, Twilio webhook authentication is already verified by
  Section 6's `signature_enforcement_active` check — a request to
  `/twiml/inbound-call` with a missing/invalid `X-Twilio-Signature` must
  return 403.
- Real OIDC production authentication is a separate, pre-existing
  configuration path (Section 1) — out of scope for closing the two
  Phase 1 blockers unless you specifically want it re-verified too.

**Disconnect** — covered by Section 7 step 6 (hang up, confirm
`active_call_count` returns to `0`). To additionally test an *abnormal*
disconnect (not a clean hangup): during a call, kill the network
connectivity of the calling phone instead of hanging up normally, then
check `/health/voice` after ~30-60 seconds for the same result.

---

## 9. Exact Success Criteria

- Section 5's script exits `0` with every `claude/*`, `gemini/*`, and
  `failover/*` scenario `PASS` in `docs/phase1.4-live-provider-results.json`.
- Section 6's script exits `0` with `public_endpoint_reachable`,
  `twiml_response_valid`, `websocket_endpoint_reachable`, and
  `signature_enforcement_active` all `PASS` in
  `docs/phase1.4-live-twilio-results.json`.
- Section 7's call: a spoken reply was heard, and `active_call_count`
  returned to `0` after hangup without a server restart.
- Section 8's five scenario checks (Claude, Gemini fallback, barge-in,
  safety/handoff, authentication, disconnect) each behave as described
  above.
- No `FAIL` in either results JSON. Any `UNVERIFIED`/`NOT RUN` entry means
  that specific scenario is still not closed — it must not be reported as
  `PASS`.

---

## 10. Exact Rollback Procedure

```bash
docker compose -f docker/docker-compose.canary.yml down
```
This stops and removes the `api`/`postgres` containers but preserves the
`postgres_data`/`hf_cache` named volumes (no `-v` flag) — no data loss. In
the Twilio Console, clear or restore the phone number's webhook to
whatever it pointed to before Section 7.

---

## 11. Exact Cleanup Procedure

1. Run the rollback command above.
2. Delete the local `.env.canary` file (never commit it; it is already gitignored).
3. Revoke or rotate every test-safe key used in this run (Anthropic, Gemini, Deepgram, ElevenLabs, Twilio auth token) if they were issued solely for this verification.
4. Tear down any DNS/tunnel configuration created solely for this verification (e.g. stop the ngrok/Cloudflare Tunnel process).
5. Delete `docs/phase1.4-live-provider-results.json` and `docs/phase1.4-live-twilio-results.json` locally once their content has been transcribed into the report in Section 12 (both are gitignored; they are not meant to persist as the permanent record).

---

## 12. Exact Artifact To Produce After Live Testing

Update `docs/phase1.4-external-integration-report.md`:
- Replace Section 4's table with the real entries from both results JSON files (provider, scenario, timestamp, latency, result, error_category, remediation — verbatim, no rewriting a real result to look better).
- Replace Section 5's latency table with the real LIVE numbers now available (Claude TTFT, Gemini TTFT, STT latency, TTS first-audio latency, end-to-end response latency, Twilio WebSocket latency, barge-in latency — each explicitly still UNVERIFIED unless actually measured during Section 7's call).
- Update Section 6's status: `CLOSED` only if both Claude/Gemini and Twilio have at least one real `PASS`; otherwise it remains `CONDITIONAL / BLOCKED` and the report must say exactly which scenario(s) are still not closed.
- Update Section 4 (this document's numbering aside) with the final classification (A/B/C/D) and the explicit `CLOSE PHASE 1` or `REMAIN BLOCKED` decision, per the same rule already established: never A merely because tests pass, never CLOSED merely because most scenarios passed.

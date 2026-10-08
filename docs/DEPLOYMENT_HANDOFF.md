# Deployment Handoff

**Date:** 2026-09-17
**Purpose:** everything the deployment track (Priority 10, plan.md Phases
26-28) needs to pick up from, now that the application itself has passed
`docs/PRODUCT_READINESS_AUDIT.md`. This document does not provision
anything — see Rule "Do NOT automatically provision cloud resources."

## 1. Application architecture

FastAPI app (`src/api/server.py`) fronting:

- `ConversationManager` (`src/agent/conversation_manager.py`) — turn
  orchestration: PolicyEngine/ClinicalGuard (deterministic safety) →
  authorization → LLM reasoning → ToolOrchestrator (schema-validated,
  idempotent tool execution) → response.
- LLM provider abstraction (`src/inference/llm_provider.py`) — Claude,
  Gemini, Groq, or local, composed via `FallbackLLMProvider`. Currently
  configured for `free_fallback` (Gemini primary → Groq fallback) since no
  `ANTHROPIC_API_KEY` exists.
- Voice pipeline (`src/voice/voice_pipeline.py`, `stt_service.py`,
  `tts_service.py`) — Deepgram STT / ElevenLabs TTS adapters, Twilio Media
  Streams over WebSocket, `src/api/twilio_signature.py` for inbound
  webhook signature validation.
- Persistence (`src/agent/db.py` + `*_repository_postgres.py`) — PostgreSQL
  in production, in-memory for dev/test. Alembic migrations in `alembic/`.
- Observability — OpenTelemetry tracing (Phase 14), `GET /metrics` (Phase
  22), structured logs, `docs/INCIDENT_RESPONSE.md`.

## 2. Required infrastructure (not yet real)

| Item | Needed for | Status |
|---|---|---|
| Domain + DNS | HTTPS termination (ACM cert), Twilio webhook URL | Not owned — 0 Route53 zones/domains confirmed in AWS account 801702847930 |
| Real Twilio account + number | Any real inbound/outbound call | Not provisioned |
| Real `DEEPGRAM_API_KEY` | Real STT | Not available |
| Real `ELEVENLABS_API_KEY` | Real TTS | Not available |
| OIDC provider (Google/Auth0/Okta/Cognito/etc.) | `AUTH_MODE=production` (required — server refuses to start without it) | Not selected |
| Managed PostgreSQL | Production persistence | Real RDS instance already exists (`ai-voice-agent-postgres`, us-east-1, `available`) from the prior Phase 26 execution pass — reachable via `/ready`, migrations applied (6 tables) |

## 3. Already provisioned (Phase 26 execution pass, real AWS, account 801702847930, us-east-1)

Per `docs/PHASE_26_PRODUCTION_DEPLOYMENT_REPORT.md` — kept for reference,
not re-verified in this handoff since deployment is not the current focus:

- ECS Fargate cluster + service (`ai-voice-agent`)
- RDS PostgreSQL 16 (`ai-voice-agent-postgres`)
- Secrets Manager: 10 secrets, `database-url` real, the other 9 placeholder
  (`REPLACE_ME_NOT_A_REAL_VALUE`)
- ALB with public HTTP DNS (`ai-voice-agent-alb-1924985934.us-east-1.elb.amazonaws.com`)
- CloudWatch logs + 4 alarms
- S3 + CodeBuild (workaround for local Docker build OOM)
- `docker/Dockerfile.production` — lean image, Gemini+Groq-only routing,
  excludes the ML/RAG stack

**This infrastructure incurs real cost right now regardless of whether the
deployment track resumes.** Whoever picks this up should decide whether to
keep it running, pause it, or tear it down — this handoff does not make
that call.

## 4. Environment variables

Full reference: `.env.example`. Summary of what's REQUIRED vs. already
resolved:

| Variable | Status |
|---|---|
| `LLM_PROVIDER` | Resolved — auto-selects `free_fallback` when `GEMINI_API_KEY`+`GROQ_API_KEY` set and no `ANTHROPIC_API_KEY` |
| `GEMINI_API_KEY`, `GROQ_API_KEY` | Available (user-supplied) |
| `ANTHROPIC_API_KEY` | Standing decision: not obtained. Not required — `free_fallback` is a complete provider path. |
| `APP_ENV` | Leave unset (= `production`) or `staging`. Outside `dev` the server refuses to start with `AUTH_MODE=dev`, `VOICE_MOCK_SERVICES=true` or `TELEPHONY_MOCK_PIN`, and every text-API call must authenticate (H2, 2026-10-08). An existing deployment configured without `AUTH_MODE=production` will therefore **not start** if redeployed — intended. |
| `AUTH_MODE` | **REQUIRED** to change from `dev` to `production` before any public exposure — currently dev default, explicitly unsafe for real traffic |
| `OIDC_ISSUER_URL`, `OIDC_AUDIENCE`, `OIDC_JWKS_URL` | **REQUIRED** once `AUTH_MODE=production` — depends on OIDC provider choice (§2) |
| `PERSISTENCE_MODE`, `DATABASE_URL` | **REQUIRED** for production — real RDS URL already exists in Secrets Manager (`database-url`), not reproduced in plaintext here |
| `DEEPGRAM_API_KEY`, `ELEVENLABS_API_KEY` | **REQUIRED** for any real call — not available |
| `TELEPHONY_MOCK_PIN` | Leave unset for any real/public deployment (fails closed to human handoff by design) — do not set this thinking it's a real auth mechanism |
| `TRACING_ENABLED`, `OTEL_*` | Optional, recommended on for production |

## 5. Database / migrations

`alembic upgrade head` against `DATABASE_URL`. Already applied once against
the real RDS instance in §3 (6 tables confirmed present). Re-verify current
head matches `alembic/versions/` before resuming.

## 6. Deployment steps (once inputs in §2 exist)

1. Register/point the domain; request an ACM certificate.
2. Add an HTTPS listener to the existing ALB; keep the HTTP listener only
   for redirect.
3. Choose an OIDC provider, set `AUTH_MODE=production` +
   `OIDC_ISSUER_URL`/`OIDC_AUDIENCE`/`OIDC_JWKS_URL`, leave `APP_ENV`
   unset (= production) or set `APP_ENV=staging` for the canary, redeploy,
   confirm the app starts (it fails closed if these are wrong — that's
   correct). **Do this before step 4**: real provider keys must never be
   live behind a service that still accepts anonymous or dev-token traffic
   (order corrected 2026-10-08, docs/MASTER_PROJECT_PLAN.md F-03).
4. Populate the 9 placeholder Secrets Manager secrets with real values
   (`TWILIO_AUTH_TOKEN` is required outside `APP_ENV=dev` — without it the
   Twilio endpoints reject every request).
5. Point the real Twilio number's webhook at
   `https://<domain>/twiml/inbound` (or current route in `server.py`);
   confirm `X-Twilio-Signature` validation accepts real Twilio requests.
6. Run `docs/PHASE_27_PRODUCTION_CANARY_REPORT.md`'s scenario (Phase 27
   spec: internal users, limited calls, controlled workflows, monitor
   errors/latency/failovers/safety events before expanding traffic).

## 7. Health checks / monitoring

`GET /health` (liveness, always 200 if the process is up), `GET /ready`
(readiness, 503 during DB outage — verified in Phase 25), `GET /metrics`
(Phase 22), CloudWatch alarms already created (§3), `docs/INCIDENT_RESPONSE.md`
for the diagnosis method and playbooks.

## 8. Rollback

Existing canary/rollback documentation: `docs/CANARY_DEPLOYMENT.md`. ECS
Fargate service supports standard task-definition-revision rollback (no
custom mechanism needed).

## 9. Security requirements before any public exposure

- `AUTH_MODE=production` (never `dev`) — see §4.
- `TELEPHONY_MOCK_PIN` unset — see §4.
- All 9 placeholder secrets replaced with real values, none left as
  `REPLACE_ME_NOT_A_REAL_VALUE`.
- HTTPS-only (no plain HTTP listener reachable externally).
- Deploy only an image whose commit passed CI job `production-image`
  (H4, 2026-10-08): it builds `docker/Dockerfile.production`, checks the
  installed packages equal `requirements-production.lock`, runs the test
  suite and a production smoke test inside the image, and verifies it
  refuses to start on missing/forbidden configuration. The image now runs
  as uid 10001 (`USER app`), not root; task definitions that do not
  override the container user inherit that.
- Re-run `tests/test_voice_server_integration.py`'s signature-validation
  suite against the real deployed endpoint once real Twilio credentials
  exist, not just in CI.

## 10. Estimated operational requirements

Not independently re-estimated in this handoff — see
`docs/PHASE_26_DEPLOYMENT_PLAN.md` §"cost estimate" for the last real
figure produced (ECS Fargate + RDS + ALB + Secrets Manager + CloudWatch,
us-east-1 pricing at the time).

---

**Per the current project strategy: product development stops here.** Do
not resume deployment work (provisioning, DNS, secrets population, canary)
until the user intentionally begins the deployment track and supplies the
inputs in §2.

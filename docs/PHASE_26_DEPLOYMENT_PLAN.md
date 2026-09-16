# Phase 26 — Production Deployment Plan

**Status:** Planning only. No infrastructure created, no deployment performed, no cost incurred by this document.
**Purpose:** Specify exactly what Phase 26 (Production Deployment) requires so it can be executed the moment the inputs in Section 17 are supplied — and to inventory what this repository *already has* (most of it) versus what genuinely doesn't exist yet.

## How to read this document

Every section below labels its content with one of:

- **AVAILABLE NOW** — already built and verified in this repository; reuse as-is.
- **AUTOMATABLE BY THE REPOSITORY** — a script, Docker config, or CI step that already exists or could be added with no new infrastructure decision.
- **REQUIRED FROM YOU** — a decision or credential only you can provide (an account, a domain, a choice between options).
- **MANUAL/CLOUD-PROVISIONING STEP** — an action that happens outside this repository, in a cloud console/registrar/Twilio console, that no script here performs (deliberately — this plan does not invent or guess at cloud resources).

No account ID, domain name, or credential value appears anywhere below except as an explicit placeholder to be filled in by you.

---

## 1. Recommended Hosting Architecture

**AVAILABLE NOW:** the application is already a stateless-at-the-process-level FastAPI service (`src/api/server.py`) with all conversational/session state externalized to PostgreSQL (Phase 12) — `docs/ARCHITECTURE.md` §8 already documents this as the intended scaling model ("Stateless API layer... API instances can be scaled horizontally"). It needs:
1. A container/VM runtime that can hold **long-lived WebSocket connections** (`/ws/call`, Twilio Media Streams) — this rules out pure request/response serverless platforms that terminate connections after a short timeout (e.g. plain AWS Lambda without a WebSocket-aware front end), but not managed-container platforms.
2. A reachable PostgreSQL instance.
3. A public HTTPS endpoint Twilio can reach.

**REQUIRED FROM YOU:** which hosting model you want. Three architecturally-compatible options (this plan does not choose one on your behalf — see Section 17):

| Option | Fits this app because | Typical fit |
|---|---|---|
| **A. Managed container platform** (e.g. a managed container/app service that supports WebSockets and long-running processes) | Deploys the existing `docker/Dockerfile` image directly, no code change; most such platforms include HTTPS, health checks, and horizontal scaling out of the box | Fastest path to a working canary |
| **B. Self-managed VM + Docker Compose** | Runs `docker/docker-compose.canary.yml` (already built, Section 3) almost unmodified; you own the OS/patching | Maximum control, more manual ops burden |
| **C. Container orchestrator** (e.g. a managed Kubernetes-compatible service) | Same container image, more moving parts (ingress, secrets, autoscaling config) — only worth it if you already run other workloads there | Only recommended if you already operate one |

Whichever you choose, the **application container itself does not change** — same image, same env vars (Section 8), same health endpoints (Section 10).

## 2. Required Compute / Runtime

**AVAILABLE NOW:** `docker/Dockerfile` already builds a complete runtime image. Two sizing paths exist depending on `LLM_PROVIDER`:

| Configuration | What it needs | Notes |
|---|---|---|
| `free_fallback` / `gemini` / `groq` / `fallback`/`claude` (any remote-provider mode — the expected production mode, since you've stated you don't want to run/pay for the local model) | CPU only, no GPU. Generation happens via the remote API; this process just orchestrates. | Recommended for the canary — matches your existing `free_fallback` (Gemini+Groq) setup already verified end-to-end in this repo's history. |
| `local` (on-box Qwen2.5-0.5B model) | GPU strongly recommended for acceptable telephony latency (CPU fallback exists but is slow — `docs/ARCHITECTURE.md` §7/§8) | Not recommended for the initial canary given the remote-provider path is already working and paid-free. |

**Identified gap (not fixed by this plan — flagged for the deployment step):** `docker/Dockerfile`'s base image is `pytorch/pytorch:2.2.0-cuda12.1-cudnn8-runtime` (several GB, CUDA-oriented) and `requirements.txt` always installs the full training/RAG stack (`transformers`, `peft`, `trl`, `bitsandbytes`, `faiss-cpu`, `sentence-transformers`) regardless of whether the local model or RAG is actually used at runtime. For a `free_fallback`-only canary with RAG disabled, a leaner CPU-only image is possible but is **not required** to deploy correctly — it is a cost/startup-time optimization, not a blocker. Recommendation: deploy with the existing image for the initial canary (works, already tested), and revisit image size in Phase 23 (Cost Optimization) once real usage data exists to justify the engineering time.

**Estimated minimum instance size** (CPU-only, `free_fallback` mode, no local model loaded): 1 vCPU / 1–2GB RAM is likely sufficient for a low-volume canary (no GPU, no local model in memory) — this is an estimate based on the code path (no torch model load in this mode), not a measured production benchmark; `PHASE_21_PERFORMANCE_REPORT.md`'s load results (LOCAL/SIMULATED) support this as a reasonable starting point, and should be re-measured against real instance sizing once deployed (Phase 21's own disclosed scaling-threshold gap).

## 3. PostgreSQL Deployment

**AVAILABLE NOW:** full schema, migrations, and a working canary Compose service (`docker/docker-compose.canary.yml`'s `postgres` service — self-hosted Postgres 16-alpine, healthchecked, with a named volume).

**REQUIRED FROM YOU:** self-hosted (via the existing Compose service) vs. a managed database. Recommendation, not a decision made for you: **a managed PostgreSQL service** (whatever your chosen hosting provider offers) over self-hosting, specifically because `docs/DATABASE.md` §7 already states automated backups/PITR/read replicas are "infrastructure-level concerns... not orchestrated directly by application code" — a managed database gets you those for free; self-hosting via the Compose `postgres` service means you are responsible for backup automation yourself (Section 12).

Either way, the application only needs one thing from you: a `DATABASE_URL` connection string (Section 17) — `PERSISTENCE_MODE=production` and everything else is already wired (`src/agent/db.py`, Phase 12).

## 4. HTTPS / TLS

**AVAILABLE NOW:** the application does not terminate TLS itself (`docs/ARCHITECTURE.md` §9: "TLS termination is assumed to be handled by infrastructure in front of the API"), consistent with standard practice — this is correct as-is, not a gap.

**MANUAL/CLOUD-PROVISIONING STEP:** whichever hosting option from Section 1 you choose, it (or a reverse proxy/load balancer in front of it) must terminate HTTPS on a real certificate. Every managed-container option in Section 1 provides this automatically once a domain is attached (Section 6); a self-managed VM (Option B) needs a reverse proxy (e.g. Caddy/nginx with Let's Encrypt) added in front of the `api` container — not currently part of `docker-compose.canary.yml` and would need to be added at deployment time if you choose Option B.

## 5. WebSocket Support

**AVAILABLE NOW:** `/ws/call` (`src/api/server.py:809`) is a native FastAPI/Starlette WebSocket route; `uvicorn[standard]` (already in `requirements.txt`) includes WebSocket support. No code change needed.

**REQUIRED FROM YOU (via Section 1's hosting choice):** confirm your chosen platform supports long-lived WebSocket connections and doesn't impose a short idle/connection-duration timeout that would cut off an active phone call. This is a property of the hosting platform, not something this repository can configure around.

## 6. Domain / DNS

**REQUIRED FROM YOU:** a domain (or subdomain) you own, pointed at wherever Section 1's hosting lands (an A/CNAME record to the host's public IP/hostname). This repository has no domain today and does not choose or purchase one for you. Existing docs already use a placeholder pattern for this (`docs/CANARY_DEPLOYMENT.md`: `https://canary.domain.com`, `.env.canary.example`: `https://canary-voice.example.com`) — those are templates, not real values, confirmed by reading both files directly.

Once you have a domain, it becomes `VOICE_PUBLIC_URL` / `TWILIO_MEDIA_STREAM_URL` (Section 8).

## 7. Twilio Configuration

**AVAILABLE NOW:** the full webhook contract already exists and is tested:
- `POST/GET /twiml/inbound-call` (`src/api/server.py:745`) — returns the `<Connect><Stream>` TwiML, with real HMAC-SHA1 `X-Twilio-Signature` enforcement (`src/api/twilio_signature.py`, Phase 1.4/Phase 18) whenever `TWILIO_AUTH_TOKEN` is set.
- `WS /ws/call` — Media Streams ingestion, same signature enforcement applied to the upgrade request (best-effort, disclosed as not yet confirmed against real Twilio traffic — `docs/SECURITY.md` §9).
- `docs/CANARY_DEPLOYMENT.md` §3 already documents the exact Twilio Console webhook configuration steps.

**REQUIRED FROM YOU:** a Twilio account, account SID + auth token, and a purchased phone number.

**MANUAL/CLOUD-PROVISIONING STEP:** in the Twilio Console, point that phone number's "A Call Comes In" webhook at `https://<your-domain>/twiml/inbound-call` (POST) — exactly as `docs/CANARY_DEPLOYMENT.md` §3 already specifies.

## 8. Secret Management

**AVAILABLE NOW:** every secret is environment-variable-driven, never hardcoded, never committed (`docs/ARCHITECTURE.md` §9, re-verified in Phase 18/22). `.env.canary.example` is the template; `.env.canary` is gitignored.

**Identified gaps in the existing template (small, low-risk, should be closed before deployment — not fixed by this planning pass, per your instruction not to touch infrastructure yet):**
1. `.env.canary.example` and `docker-compose.canary.yml` predate two additions: `GROQ_API_KEY`/`GROQ_MODEL` (the free-tier fallback provider) and `TELEPHONY_MOCK_PIN` (Phase 18's fail-closed-by-default telephony auth — leaving it unset, the safe default, requires no template change, but it should be documented in the template so a deployer knows it exists and is intentionally left unset).
2. `docker-compose.canary.yml`'s `POSTGRES_PASSWORD` has no default in the compose file itself (good — it's required), but `.env.canary.example` shows `POSTGRES_PASSWORD=voice_secret` as an example value. **This must be changed to a real, unique, strong secret in the actual `.env.canary` you create — never reuse the dev/test default value for anything internet-reachable.**

**REQUIRED FROM YOU / MANUAL STEP:** decide where `.env.canary`'s real values live at runtime — options, not a decision made for you:
- Your hosting platform's own secret store (most managed-container platforms have one) — recommended, keeps secrets out of the VM/container filesystem entirely.
- A dedicated secrets manager (e.g. your cloud provider's, or Vault) if you already use one elsewhere.
- The `--env-file .env.canary` mechanism `docker-compose.canary.yml` already supports, if self-hosting (Option B) — acceptable for a canary, less ideal long-term (the file exists on disk on the host).

### Full required-secret inventory (Section 17 has the exact list)

## 9. Logging / Metrics / Tracing

**AVAILABLE NOW, no changes needed:**
- Structured JSON logging: `LOG_FORMAT=json` (already wired, already the canary default).
- Metrics: `GET /metrics` (Phase 22) exposes `MetricsRegistry.snapshot()` — point any scraper (your platform's own log-based metrics, or a Prometheus-compatible `json_exporter`) at it. No new infrastructure required to *collect* it; visualizing it in a dashboard is a platform choice (Section 17).
- Tracing: OpenTelemetry (Phase 14, `docs/TRACING.md`) — disabled by default (zero overhead), enable with `TRACING_ENABLED=true` + `OTEL_EXPORTER_OTLP_ENDPOINT` pointed at any OTLP collector (a self-hosted Jaeger via the existing `docker/docker-compose.yml`'s `observability` profile, or a managed tracing backend if you have one).
- Alert definitions and the 4-signal (metrics → audit → trace → log) incident-diagnosis method: `docs/INCIDENT_RESPONSE.md` (Phase 22) — already written, references the real metric/event names this deployment will produce.

**REQUIRED FROM YOU:** whether you want a real dashboard/alerting backend wired up (e.g. your platform's built-in monitoring, or a Grafana/Prometheus stack) — `docs/INCIDENT_RESPONSE.md` §9 already states this repository deliberately does not pick one for you.

## 10. Health / Readiness

**AVAILABLE NOW, already correct, reuse as-is:**
- `GET /health` — liveness only, no dependency checks (fast, always answers if the process is up).
- `GET /health/voice` — telephony/provider configuration readiness (booleans only, never leaks secret values).
- `GET /ready` — real dependency check (`Database.health_check()` when `PERSISTENCE_MODE=production`) — returns 503 correctly during a DB outage (verified for real in Phase 25).
- `docker-compose.canary.yml` already wires `/ready` as the container healthcheck.
- `scripts/canary_startup.sh` already performs a post-deploy verification pass against these exact endpoints.

Whatever hosting platform you choose (Section 1), point its own health-check mechanism (load balancer target group health check, platform-native healthcheck, etc.) at `GET /ready` for traffic-routing decisions and `GET /health` for basic liveness — no new code needed.

## 11. Database Migration Strategy

**AVAILABLE NOW:** Alembic is already configured (`alembic.ini`, `alembic/`), with 3 real migrations already written and tested against both SQLite (CI) and real local Postgres (Phase 12, `scripts/validate_real_postgres.py`).

**AUTOMATABLE BY THE REPOSITORY:** the deploy step is exactly:
```bash
alembic upgrade head
```
run once against the target `DATABASE_URL` before (or as part of) the first deploy, and again after any future migration is added. `docs/ROLLBACK_PROCEDURE.md` §2 Step 3 already documents the reverse (`alembic downgrade -1`) for rollback.

**REQUIRED FROM YOU:** decide whether this runs as a manual pre-deploy step (simplest for a first canary) or as an automated init-container/deploy-hook step (better once this is a repeated process) — a CI/CD pipeline choice, not something this plan invents on your behalf.

## 12. Backup / Recovery

**AVAILABLE NOW:** `docs/DATABASE.md` §7 already documents recovery guarantees (crash-consistent transactions, verified restart recovery — Phase 12.12) and explicitly states automated backups/PITR are "infrastructure-level concerns... managed by cloud providers," not application code. `PHASE_25_DISASTER_RECOVERY_REPORT.md` independently re-verified real outage/restart recovery against real Postgres this session.

**REQUIRED FROM YOU:** if you choose a managed Postgres (Section 3's recommendation), automated backups/PITR typically come with it — confirm what your chosen provider includes. If self-hosting Postgres via `docker-compose.canary.yml`, you are responsible for your own backup automation (e.g. periodic `pg_dump` to object storage) — **not currently automated by this repository**, and out of this plan's scope to invent without knowing your hosting choice.

## 13. Deployment Strategy

**AVAILABLE NOW:** `docker-compose.canary.yml` is a complete, working canary deployment definition — build once, `docker compose -f docker/docker-compose.canary.yml --env-file .env.canary up -d`. This is the deployment unit regardless of which hosting option (Section 1) you choose (a managed container platform typically takes the same image directly; a VM runs the compose file as-is; an orchestrator adapts the same image into its own manifest format).

**REQUIRED FROM YOU:** whether the very first deployment is this same `docker-compose.canary.yml` on a VM (Option B, fastest to reuse verbatim) or the container image pushed to your chosen managed platform (Option A, needs an image registry — Section 17).

## 14. Canary Strategy

**AVAILABLE NOW, fully specified, do not rewrite:**
- `docs/CANARY_PROCEDURE.md` — pre-flight checklist + 6 real-call test scenarios (standard query, barge-in, clinical safety, prompt injection, Claude→Gemini failover, silence/disconnect) with explicit pass criteria, plus escalation/abort criteria (clinical safety bypass, audio feedback loop, >2% call drops, data leakage).
- `docs/CANARY_TESTING_PROCEDURE.md` — automated regression suite instructions + the exact SLA latency thresholds (`total_turn_latency` p95 ≤850ms, `barge_in_detection_latency` p95 ≤150ms, etc.) and `scripts/latency_report.py` to generate a report against them.
- `docs/CANARY_DEPLOYMENT.md` — full architecture/protocol reference and environment variable table for the canary.

Plan for Phase 26's own Definition of Done (Section 20) is to have executed `docs/CANARY_PROCEDURE.md`'s checklist for real once a deployment exists — that execution is Phase 27's job (Production Canary), not this planning phase's or Phase 26's implementation step's.

**REQUIRED FROM YOU:** how much real call volume you're comfortable routing to the canary phone number initially (internal test calls only, vs. a small % of real traffic) — a business/risk decision `docs/CANARY_PROCEDURE.md` doesn't make for you.

## 15. Rollback Procedure

**AVAILABLE NOW, fully specified, do not rewrite:** `docs/ROLLBACK_PROCEDURE.md` — rollback criteria (clinical safety failure, >1% 5xx rate over 5 min, P95 latency >2000ms, audio corruption), a <30s Twilio re-routing step, a <60s container-stack rollback step, and the `alembic downgrade -1` database step (only needed if a migration itself must be reverted — Phase 13's own schema changes were additive-only, so this has rarely been necessary historically).

Nothing to add here beyond confirming it's real and current (it is — cross-checked against the actual `docker-compose.canary.yml`/`docker-compose.yml` commands, which match).

## 16. Estimated Recurring Infrastructure Requirements

Order-of-magnitude estimate only — not a quote, not a commitment, and deliberately not tied to a specific vendor's current pricing (which this document cannot know and should not guess precisely):

| Item | Est. monthly range (USD) | Driver |
|---|---|---|
| Compute (1 small CPU instance, `free_fallback` mode) | ~$5–$40 | Depends entirely on hosting choice (Section 1); a small managed-container instance is typically at the low end |
| Managed PostgreSQL (smallest tier) | ~$10–$25 | Only if managed; self-hosted via Compose is "free" but shifts backup/ops burden to you |
| Domain registration | ~$1–$2/month (billed annually, ~$10–$15/yr) | One-time-ish, provider-dependent |
| Twilio phone number | ~$1–$2/month | Twilio's own published pricing for a standard number |
| Twilio usage (per-minute, inbound calls) | Usage-based, ~$0.0085–$0.014/min typical for US inbound (Twilio's own list pricing) | Scales with real call volume |
| Deepgram STT | Usage-based (per-minute) | Only if you supply `DEEPGRAM_API_KEY` — required for real voice (not currently supplied) |
| ElevenLabs TTS | Usage-based (per-character or subscription tier) | Only if you supply `ELEVENLABS_API_KEY` — required for real voice (not currently supplied) |
| Gemini + Groq (LLM, `free_fallback` mode) | $0 at free-tier usage levels | Your existing, already-verified, no-spend setup — no change |
| Claude (only if you later enable `fallback`/`claude` mode) | Usage-based, pay-per-token | Not needed — you've stated you don't want to pay for this |

**Total rough floor for a minimal, low-volume canary (compute + managed DB + domain + Twilio number, excluding usage-based STT/TTS/telephony minutes and excluding LLM since `free_fallback` is $0):** roughly **$20–$70/month** before any real call volume, plus Deepgram/ElevenLabs/Twilio-minutes usage once real calls start (magnitude depends entirely on call volume — cannot be estimated without knowing expected usage).

## 17. Required Credentials / Inputs (the exact list to unblock Phase 26)

### REQUIRED FROM YOU — decisions
1. Hosting platform choice (Section 1: A/B/C, or a specific provider you already use).
2. Managed vs. self-hosted PostgreSQL (Section 3).
3. Domain/subdomain you own (Section 6).
4. Where secrets live at runtime (Section 8).
5. Real-call volume comfort level for the initial canary (Section 14).

### REQUIRED FROM YOU — accounts/credentials (none currently exist in this environment; verified via `.env`/`.env.canary` absence throughout Phases 17/24/25)
| Credential | Purpose | Currently supplied? |
|---|---|---|
| `GEMINI_API_KEY` + `GROQ_API_KEY` | LLM (`free_fallback` mode — your existing, already-verified, no-spend choice) | Previously supplied transiently in an earlier session (per `eac3a70`'s commit message) but not persisted anywhere in this repo or environment — will need to be supplied again |
| `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` / a purchased Twilio phone number | Telephony | Not supplied |
| `DEEPGRAM_API_KEY` | Real-time STT | Not supplied |
| `ELEVENLABS_API_KEY` | Real-time TTS | Not supplied |
| `DATABASE_URL` (or managed-Postgres credentials to build one) | Persistence | Not supplied — canary default `voice_secret` password must NOT be reused |
| OIDC provider details (`OIDC_ISSUER_URL` / `OIDC_AUDIENCE` / `OIDC_JWKS_URL`) | Production API authentication (`AUTH_MODE=production` requires a real identity provider — Google/Auth0/Okta/Cognito/etc.; this repository does not run one itself) | Not supplied — **which IdP to use is your decision**, not inferred here |
| A domain you control | Public HTTPS/WSS endpoint | Not supplied |
| (Optional) `TELEPHONY_MOCK_PIN` | Only if you want the Phase 18 mock-PIN canary behavior enabled instead of its safe fail-closed default | Recommend leaving unset for any deployment beyond a fully-trusted internal test |
| (Optional) Container image registry credentials | Only needed if your Section 1 hosting choice requires pushing the built image somewhere (Option A typically does; Option B does not) | Depends on Section 1's answer |

### MANUAL/CLOUD-PROVISIONING STEPS (cannot be automated from inside this repository)
- Creating the hosting account/instance itself.
- Registering/configuring the domain and DNS records.
- Creating the Twilio account, buying the number, and pointing its webhook (exact steps already in `docs/CANARY_DEPLOYMENT.md` §3).
- Provisioning managed PostgreSQL, if chosen.
- Registering an OIDC application with your chosen identity provider.

## 18. Required Manual Steps (execution order, once inputs exist)

1. Provision hosting target + domain + DNS (Sections 1, 6).
2. Provision PostgreSQL, obtain `DATABASE_URL` (Section 3).
3. Register an OIDC application, obtain issuer/audience/JWKS URL (Section 17).
4. Create a Twilio account, buy a number (webhook configured *after* step 6, once the domain resolves).
5. Obtain Deepgram + ElevenLabs + Gemini + Groq API keys.
6. `cp .env.canary.example .env.canary`, fill in every value from Section 17, generate a strong unique `POSTGRES_PASSWORD` (never `voice_secret`), place it wherever Section 17's secret-storage decision points.
7. `alembic upgrade head` against the real `DATABASE_URL` (Section 11).
8. Build and deploy the image per Section 1/13's chosen method.
9. Point the Twilio number's webhook at the now-live `https://<domain>/twiml/inbound-call` (Section 7).
10. Run `scripts/canary_startup.sh <port> <host>` against the live deployment (already built, Section 10).
11. Run `scripts/live_provider_verification.py` and `scripts/live_twilio_readiness_check.py` (already built — Phase 1.4/17) to close the external-integration verification gap Phase 17 is currently blocked on.
12. Execute `docs/CANARY_PROCEDURE.md`'s 6 real-call scenarios (this is Phase 27's job, not this plan's).

## 19. Security Considerations

Reused from existing, already-audited material — not re-derived here:
- `docs/SECURITY.md` — full threat model, trust boundaries, 10 security invariants, Phase 18's telephony-auth findings and fixes.
- `docs/INCIDENT_RESPONSE.md` — alert definitions, diagnosis method, playbooks.
- Twilio signature enforcement (Section 7) is already real and tested; its WebSocket-upgrade enforcement is explicitly disclosed as best-effort pending confirmation against real Twilio traffic (`docs/SECURITY.md` §9) — step 11 above is exactly what closes that.
- `AUTH_MODE=production` + `DEV_AUTH_ENABLED=false` are **mandatory** for any real deployment (`docker-compose.canary.yml` already defaults to this) — the development authentication provider must never be reachable outside a trusted local network.
- Secrets: never commit `.env.canary`; rotate any credential that was ever pasted into a chat/terminal session (per this session's own standing practice — see this repo's memory notes on live secrets in chat).
- `TELEPHONY_MOCK_PIN`: leave unset for any deployment beyond a fully-trusted internal test (Section 17) — its own documentation (`docs/SECURITY.md` §7, `.env.example`) already states this plainly.
- New, not previously flagged: **`.env.canary.example`'s example `POSTGRES_PASSWORD=voice_secret` must never be reused verbatim** — flagged explicitly in Section 8/17 rather than left implicit.

## 20. Definition of Done for Phase 26

Phase 26 (actual production deployment, not this planning document) is complete when **all** of the following are independently verified, matching this repository's evidence discipline (no PASS inferred from the absence of an error):

1. The application container is running on real infrastructure (Section 1/13), reachable over HTTPS at the real domain (Section 6).
2. `GET /health`, `GET /health/voice`, and `GET /ready` all return healthy against the live deployment.
3. `alembic upgrade head` has been applied to the real production `DATABASE_URL`, and `docs/DATABASE.md`'s schema matches what's actually in the database (verified, not assumed).
4. Twilio's webhook is configured and `POST /twiml/inbound-call` returns valid TwiML with real Twilio-signature verification passing (`scripts/live_twilio_readiness_check.py`, step 11 above).
5. `scripts/live_provider_verification.py` passes for every configured LLM provider (closing Phase 17's remaining gap for whichever providers you actually configure — Claude remains out of scope per your standing no-spend decision).
6. A real, controlled inbound phone call completes successfully end-to-end (STT → policy → LLM/tool → TTS → audible response), with clean session teardown afterward (`active_call_count` returns to 0 — the same check Phase 25 already verified generically).
7. The rollback procedure (`docs/ROLLBACK_PROCEDURE.md`) has been **rehearsed at least once** against the real deployment, not just read.
8. Logging is flowing in structured JSON; metrics are reachable at `/metrics`; tracing is enabled if you chose to (Section 9).
9. No secret value is present in this repository, in logs, or in any committed file (re-verified at deploy time, not just assumed from the code-level audit already done in Phase 18/24).

Only once all nine are true does plan.md's own Phase 26 acceptance criterion ("Production deployment succeeds and rollback is verified") hold — and only then is Phase 27 (Production Canary) eligible to start, per your explicit instruction not to begin it yet.

---

## Summary: What's Actually Missing

Everything on the *engineering* side already exists and was reused, not rewritten, in this plan: Docker image, canary Compose file, database schema + migrations, health/readiness endpoints, metrics endpoint, tracing, canary test procedure, rollback procedure, Twilio signature verification, OIDC auth wiring. Two small documentation gaps were identified (Section 8) but not fixed, per your instruction to plan only.

What's genuinely missing is **entirely external to this repository**: a hosting decision, a domain, a Twilio account/number, an OIDC identity provider decision, and the STT/TTS/telephony API keys. None of those can be invented or automated from inside this codebase.

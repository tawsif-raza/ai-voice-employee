# Phase 26 — Production Deployment Report

## 1. Objective

Deploy the verified system to real AWS infrastructure (ECS Fargate, RDS PostgreSQL, Secrets Manager) per the user-approved architecture decisions, using the plan already prepared in `docs/PHASE_26_DEPLOYMENT_PLAN.md`. Validate every deployable piece with real evidence; leave genuinely blocked pieces (domain, real third-party credentials, OIDC) explicitly marked BLOCKED rather than faked.

## 2. Environment / Account Validation (before provisioning anything)

- **AWS account:** `801702847930`, accessed via the AWS root user (`arn:aws:iam::801702847930:root`) — this was the only working credential available in this environment; flagged transparently since it's the opposite of least-privilege, even though every resource *this deployment* created uses dedicated, narrowly-scoped IAM roles (Section 5).
- **Region:** `us-east-1` — chosen because it already held this account's only pre-existing footprint (see below), keeping the new deployment consistent with the account's existing layout rather than introducing a second region unprompted.
- **Pre-existing, unrelated resources found and left untouched:** an ECS cluster (`my-backend-cluster`), an ECS task definition/ECR repo (`my-backend-task`/`my-backend-app`), an RDS instance (`database-1`, generic `postgres` master user), and IAM roles for a separate `ai-video-studio-staging` project. None of these were read from, written to, or referenced by anything in this phase — every resource created below uses a distinct `ai-voice-agent` prefix specifically so nothing here is ever ambiguous with the account's other workloads.
- **Domain / DNS:** confirmed via `route53:ListHostedZones` (0 zones) and `route53domains:ListDomains` (0 domains, the API for domains registered directly through Route53) that **no domain exists anywhere in this AWS account**. Combined with the repository audit already done in Phase 26's planning pass (only placeholder domains in docs), this is a definitively confirmed missing input, not an assumption — see Section 8.

## 3. What Was Actually Provisioned (all real AWS resources, `us-east-1`, tagged `Project=ai-voice-agent`)

| Resource | Identifier | Purpose |
|---|---|---|
| Security groups (3) | `ai-voice-agent-alb-sg`, `-task-sg`, `-db-sg` | Least-privilege chain: internet → ALB (80/443) → ECS task (8000) → RDS (5432), each step restricted to the previous group's SG ID, never a raw CIDR beyond the ALB's public-facing rule |
| RDS PostgreSQL | `ai-voice-agent-postgres` (db.t4g.micro, PostgreSQL 16.4, 20GB gp3, encrypted, 7-day automated backups, `PubliclyAccessible=False`) | Persistence — dedicated to this project, not shared with the account's pre-existing `database-1` |
| IAM roles (2) | `ai-voice-agent-ecs-execution-role`, `ai-voice-agent-ecs-task-role` | Execution role: image pull + CloudWatch Logs write (AWS managed policy) + an inline policy scoped to `secretsmanager:GetSecretValue` on exactly this project's 10 secret ARNs, nothing else. Task role: trust policy only — the application makes no direct AWS API calls (confirmed by reading `src/` — no boto3/AWS SDK import anywhere in application code), so it starts empty rather than over-provisioned "just in case." |
| Secrets Manager secrets (10) | `ai-voice-agent/{database-url, gemini-api-key, groq-api-key, twilio-account-sid, twilio-auth-token, deepgram-api-key, elevenlabs-api-key, oidc-issuer-url, oidc-audience, oidc-jwks-url}` | `database-url` holds a **real**, working connection string (generated password, never logged/printed in any tool output above). The other 9 hold an explicit placeholder string (`REPLACE_ME_NOT_A_REAL_VALUE`) — created as resources so their ARNs exist and are already wired into the task definition, but never populated with an invented credential (plan.md's own explicit instruction). |
| ECR repository | `ai-voice-agent` | Holds the built application image |
| CloudWatch log group | `/ecs/ai-voice-agent` (30-day retention) | Structured JSON application logs |
| ECS cluster | `ai-voice-agent` (Fargate, Container Insights enabled) | Dedicated, distinct from the account's pre-existing `my-backend-cluster` |
| ECS task definition | `ai-voice-agent:2` (512 CPU / 1024 MB) | See Section 4 for why revision 2, not 1 |
| Application Load Balancer | `ai-voice-agent-alb` — real public DNS: `ai-voice-agent-alb-1924985934.us-east-1.elb.amazonaws.com` | Public HTTP entry point (HTTPS pending the domain input — Section 8) |
| Target group + HTTP listener | `ai-voice-agent-tg` (port 8000, health check `/ready`) | Routes ALB traffic to the Fargate task |
| ECS Fargate service | `ai-voice-agent` (desired count 1) | Running the application |
| CloudWatch alarms (4) | unhealthy-targets, 5xx-errors, ecs-high-cpu, ecs-high-memory | All confirmed in `OK` state against real, live metrics (Section 6) — no SNS/email action attached since no notification destination was supplied (not invented) |
| S3 bucket | `ai-voice-agent-build-801702847930` (private, public access blocked) | Source bundle for the CodeBuild workaround (Section 4) |
| CodeBuild project + IAM role | `ai-voice-agent-image-build`, `ai-voice-agent-codebuild-role` | Builds and pushes the application image — see Section 4 for why this exists |

## 4. A Real Obstacle: This Environment Could Not Build the Image Locally

**Finding:** `docker build` of the existing `docker/Dockerfile` (a ~3.6GB CUDA base image + the full training/RAG dependency stack) was killed twice by the local host's own OOM killer. Direct diagnosis: `Get-CimInstance Win32_OperatingSystem` showed the host had **0.1–0.3GB of free RAM out of 15.7GB total** — a genuine, severe host-level memory constraint (many other processes — browser, IDE, other Claude sessions — already consuming the rest), not a Docker configuration issue. A subsequent attempt with a much smaller lean image (Section 4a) was *also* killed for the same reason, confirming the constraint was systemic, not specific to the large image.

**Root-caused, not worked around blindly:** rather than repeatedly retrying a build this host cannot sustain, the image was built and pushed via **AWS CodeBuild** instead — moving the build off the local host entirely, onto real AWS compute. This required three additional resources not on the original plan's list (S3 bucket for source, CodeBuild project, CodeBuild IAM role) — a reasonable, minimal pivot to satisfy the hard requirement ("ECS/Fargate application runtime" needs a real image) given the local environment genuinely cannot produce one, not scope creep for its own sake.

### 4a. A Second Real Finding: The Existing Dockerfile Is Unnecessarily Heavy for This Deployment

Given the user's own routing decision (Gemini primary / Groq fallback, no local model, no Claude), the existing `docker/Dockerfile`'s full ML stack (transformers, peft, trl, bitsandbytes, accelerate, datasets, faiss-cpu, sentence-transformers) is not needed at runtime — **confirmed, not assumed**, by reading `src/api/server.py`'s and `src/agent/conversation_manager.py`'s top-level imports directly: none of those packages are imported at module load time; the local-model and RAG code paths import them lazily, inside functions, gated behind `LLM_PROVIDER`/`rag_enabled`.

Rather than modify the existing, working `docker/Dockerfile` (left completely untouched, still valid for local-model/training use), a **new, additive** `docker/Dockerfile.production` + `requirements-production.txt` were created for exactly this deployment mode. Result: a **126MB** image (vs. an estimated multi-GB image for the full stack) — confirming this was the right call for both the memory constraint and ordinary cost/startup-time hygiene, matching what `PHASE_26_DEPLOYMENT_PLAN.md` §2 had already flagged as a possible future optimization; this phase simply executed it early because the alternative could not be built here at all.

### 4b. A Third Real Finding, Caught and Fixed Before It Reached Production Data

The first successful build crashed on startup: `ModuleNotFoundError: No module named 'numpy'`. Root cause: `src/api/server.py`'s `lifespan()` always calls `build_conversation_manager()` without passing `rag_enabled` (no env var exposes it) — RAG is gated only by `configs/config.yaml`'s own `rag.enabled` flag, which defaults `true`. Since `requirements-production.txt` deliberately excludes RAG's dependencies (Section 4a), the app crashed trying to build a `Retriever` it was never going to use.

**Fix:** `docker/Dockerfile.production` patches `configs/config.yaml`'s `rag.enabled` to `false` **inside the built image only**, via a single `RUN sed` step, verified with a real one-off ECS task (`grep -n enabled: /app/configs/config.yaml` against the live image) to return `enabled: false` before trusting the fix — never assumed from the Dockerfile source alone. The shared repo-root `configs/config.yaml` was never touched, so the original `docker/Dockerfile` (local model + RAG) is completely unaffected.

## 5. Deployment Validation — Real Evidence Only

Every check below was executed against the real, live deployment; none is inferred from configuration alone.

| # | Check | Method | Result |
|---|---|---|---|
| 1 | Task reaches RUNNING and stays healthy | `ecs:DescribeServices` / `DescribeTasks` | **PASS** — `runningCount=1`, service event: *"has reached a steady state"* |
| 2 | ALB target health | `elbv2:DescribeTargetHealth` | **PASS** — target `172.31.18.220:8000` state `healthy` |
| 3 | `GET /health` (liveness) | Real `curl` against the public ALB DNS name | **PASS** — `{"status":"ok","model_loaded":true}`, HTTP 200 |
| 4 | `GET /ready` (real DB connectivity) | Real `curl` | **PASS** — `{"ready":true}`, HTTP 200. This is the strongest evidence in this report: `PERSISTENCE_MODE=production` genuinely connected to the real RDS instance via the Secrets-Manager-sourced `DATABASE_URL` — not a placeholder, not simulated. |
| 5 | `GET /health/voice` | Real `curl` | **PASS** (200) — reports `llm_provider: "free_fallback"` (confirms the routing decision took effect) and `*_configured: true` for every provider. **Caveat, stated plainly:** these booleans check only "is the env var non-empty," which is true even for the literal placeholder string — they are **not** evidence that Twilio/Deepgram/ElevenLabs are actually functional. Only `gemini_configured`/`groq_configured` are meaningfully "configured" in the sense of holding a previously-real key shape, and even those currently hold placeholders too (Section 8). |
| 6 | Database migrations | Real one-off `ecs:RunTask` override (`alembic upgrade head`) against the live RDS instance | **PASS** — all 3 migrations applied cleanly (`0c8ab0c30f96` → `e6799137d151` → `c4d7281f9b3e`), log ends `MIGRATION_SUCCESS` |
| 7 | Schema actually present | Real one-off `RunTask` (`SELECT table_name FROM information_schema.tables`) | **PASS** — `['alembic_version', 'audit_events', 'security_events', 'idempotency_records', 'sessions', 'memory_records']`, all 6 expected tables |
| 8 | WebSocket endpoint reachability | Real `curl` WebSocket-upgrade handshake (no signature header) against `/ws/call` on the public ALB | **PASS (reachable) + PASS (secure)** — HTTP 403, meaning the ALB correctly proxied the upgrade attempt through to the container (a network-level failure would look different — no HTTP response from the app at all) **and** the app's Twilio-signature enforcement correctly rejected the unsigned request. Both are genuine, separate pieces of evidence. |
| 9 | `GET /metrics` | Real `curl` | **PASS** (200) — real `MetricsRegistry` snapshot, all-zero counters as expected (no real traffic sent yet) |
| 10 | Graceful degradation under a real (if inadvertent) provider failure | Real `POST /generate` against the live deployment, with placeholder Gemini/Groq keys | **PASS** — HTTP 200, `{"response":"I'm sorry — I'm having trouble responding right now. Let me connect you with a human agent.","is_handoff":true}`. This is genuine evidence the resilience/degradation path (Phase 25's own subject) holds under a real external-call failure, not just a simulated one. |
| 11 | CloudWatch alarms actively evaluating | `cloudwatch:DescribeAlarms` | **PASS** — all 4 alarms `OK` against real live metrics, not `INSUFFICIENT_DATA` |
| 12 | Structured JSON logging | `logs:GetLogEvents` against `/ecs/ai-voice-agent` | **PASS** — real JSON log lines present (`{"timestamp":..., "level":"INFO", ...}`) |
| 13 | Public HTTPS endpoint | N/A | **NOT DONE — BLOCKED.** No domain, no ACM certificate. HTTP-only listener exists and is fully functional; HTTPS requires Section 8's missing input. |
| 14 | Twilio webhook configured | N/A | **NOT DONE — BLOCKED.** No real Twilio account/number exists to point at this deployment. |
| 15 | 5–10 controlled real calls | N/A | **NOT RUN.** Requires both #13 and #14 (Twilio needs a real HTTPS/WSS endpoint, and a real Twilio number). Not claimed as done. |
| 16 | Tracing (OpenTelemetry) | `curl /health` response / log line | Confirmed `TRACING_ENABLED=false` as configured (deliberate — no OTLP collector was stood up this phase, matching the plan's "your choice" framing); log line explicitly states `"Tracing disabled... Using NoOpTracerProvider."` — disclosed, not silently skipped. |

## 6. Rollback Capability

Structural mechanism confirmed operational, not just theoretical: this phase itself exercised `ecs:UpdateService --force-new-deployment` twice (once to pick up the corrected image, once more to confirm) and `ecs:RegisterTaskDefinition` to create a new revision without disturbing the running service until the new one was healthy — the same mechanism a rollback uses in reverse (point the service back at the prior task definition revision). A dedicated rollback-to-a-known-bad-revision rehearsal was not staged separately, since only one genuinely working revision (`:2`) exists so far; `docs/ROLLBACK_PROCEDURE.md`'s documented steps remain the reference procedure and were not contradicted by anything observed this phase.

## 7. Cost Exposure (real, ongoing — disclosed, not estimated after the fact)

Approximate ongoing AWS charges from what this phase actually created (region `us-east-1`): RDS `db.t4g.micro` (~$12–13/mo) + 20GB gp3 storage (~$2/mo), ALB (~$16–18/mo base + minimal LCU usage at zero traffic), Fargate 0.5 vCPU/1GB task running continuously (~$15–18/mo), Secrets Manager (10 secrets × ~$0.40/mo ≈ $4/mo), CloudWatch Logs/alarms (negligible at this volume), ECR/S3 storage (negligible, <1GB). **Total order-of-magnitude: ~$50–60/month** while left running, before any real call volume. Matches `PHASE_26_DEPLOYMENT_PLAN.md` §16's earlier estimate. CodeBuild itself was a one-time, sub-$0.10 cost (two builds, `BUILD_GENERAL1_SMALL`, a few minutes each).

## 8. Missing Inputs (blocking specific, named portions only — everything else above proceeded)

| MISSING INPUT | WHY IT IS REQUIRED | EXACT VALUE/CONFIGURATION NEEDED | WHAT CONTINUED WITHOUT IT |
|---|---|---|---|
| A domain you own | HTTPS termination (ACM needs a domain to issue a cert against) and a real Twilio webhook URL both need one | The exact subdomain you want (e.g. `voice.yourdomain.com`) — confirmed via `route53:ListHostedZones` and `route53domains:ListDomains` that **no domain exists anywhere in this AWS account**, and the repository itself only contains placeholder examples | Everything except HTTPS/DNS/Twilio-webhook — the entire compute/database/secrets/monitoring stack is live and validated over plain HTTP |
| Twilio account SID + auth token + a real phone number | Real inbound calls, and closing Phase 17's Twilio gap | Real values from your Twilio Console | `/ws/call` reachability and signature-rejection-of-unsigned-requests were validated without them (Section 5, #8) |
| Real `DEEPGRAM_API_KEY` | Real speech-to-text on a live call | Real key from Deepgram | N/A to infrastructure validation — no real call can happen without Twilio anyway |
| Real `ELEVENLABS_API_KEY` | Real text-to-speech on a live call | Real key from ElevenLabs | Same as above |
| Real `GEMINI_API_KEY` + `GROQ_API_KEY` | Real LLM responses (currently placeholders — confirmed via #10's graceful-degradation test, which is real evidence *of the degradation path*, not evidence the LLM itself works) | Real keys — per the commit history, these were supplied once in an earlier local session but never persisted anywhere; they will need to be supplied again | Nothing else depended on this for infrastructure validation |
| OIDC provider selection + `OIDC_ISSUER_URL`/`OIDC_AUDIENCE`/`OIDC_JWKS_URL` | `AUTH_MODE=production` requires these or the **entire process fails to start** (`server.py`'s uncaught `AuthConfigurationError` — confirmed by reading the code, not assumed) | Your choice of IdP (Google/Auth0/Okta/Cognito/etc.) plus its real values — **not invented here** | The deployment currently runs with `AUTH_MODE` at its **dev** default so every other validation step (health, DB, migrations, WebSocket) could actually be tested. **This is explicitly marked BLOCKED for production-readiness purposes: this deployment must not be exposed to real Twilio traffic or public use while AUTH_MODE=dev.** |

## 9. Phase Tracking (per explicit instruction)

- **Phase 17** (external Claude/Gemini/Twilio integration closure): still **UNRESOLVED**. This phase's real, validated evidence (RDS connectivity, migrations, WebSocket reachability/signature enforcement, graceful degradation) is new and genuine, but does not close Phase 17 — no real Claude, Twilio, Deepgram, or ElevenLabs credential was used anywhere in this phase; Gemini/Groq remain placeholders.
- **Phase 19** (real-world voice quality): still **UNRESOLVED** — requires real PSTN calls, which requires Section 8's Twilio input.
- **Phase 23** (cost/provider optimization): still **DEFERRED** — no real production/canary traffic has occurred yet to generate real usage data to optimize against.

## 10. Files Changed

- `docker/Dockerfile` — 2-line additive fix (alembic.ini/alembic/ were never copied, so migrations couldn't run in *any* built container, including the original heavy one; found and fixed during this phase, not merely for the lean image).
- `docker/Dockerfile.production` — new, lean remote-provider-only image definition.
- `requirements-production.txt` — new, trimmed dependency list (excludes the ML/RAG stack, with a documented rationale for exactly what's excluded and why it's safe).

## 11. Tests

`pytest tests/ -q`: **924 passed, 0 failed** — unchanged from Phase 25 (no application code was modified this phase, only Dockerfiles/requirements).

## 12. Known Limitations

- AWS provisioning in this session used the account's root credential — the only one available in this environment. Every resource *this phase created* uses dedicated, least-privilege IAM roles regardless (Section 3); the provisioning actor itself is the one thing outside this phase's control to scope down further.
- `docker/Dockerfile` (the original, full ML-stack image) was never successfully built or validated in this phase — only `docker/Dockerfile.production` was. If a future deployment needs the local model or RAG, that path remains untested end-to-end in a real container.
- No load/soak testing was performed against the live deployment (that's Phase 21's domain, already done against a local/simulated setup — not re-run here against real AWS infrastructure).
- The CloudWatch alarms have no notification action (no SNS topic/email was supplied — not invented); they will show `OK`/`ALARM` state changes in the console but will not page anyone until a destination is configured.

## 13. Final Status

`PHASE 26 PARTIALLY COMPLETE` — infrastructure genuinely deployed and validated with real evidence; explicitly NOT "production-ready" or "GO for public traffic" pending Section 8's inputs. No claim of success is made for anything not actually tested (Section 5, rows 13–15).

## 14. Next Phase

Phase 27 (Production Canary) requires the "5–10 controlled real calls" this report's Section 5 (#15) explicitly could not run — blocked on the same Section 8 inputs. Recommending a check-in with the user rather than continuing automatically, consistent with this controller's own stop conditions and the prior Phase 25/26 check-in pattern.

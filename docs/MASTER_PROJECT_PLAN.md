# Master Project Plan — AI Voice Agent

**Created:** 2026-10-08 · **Owner:** technical lead (Claude, acting on the repo owner's mandate)
**Status of this document:** living. Update it at the end of every hardening phase (§19), not only at milestones.
**Baseline commit audited:** `8399be9` (main, clean tree)

> **How to read this.** Every finding is tagged with an evidence level:
> **CONFIRMED** (reproduced by running code during this audit, or unambiguous from code with no runtime dependency),
> **LIKELY** (strong code evidence, not executed), **POTENTIAL** (risk that depends on configuration or future use),
> **RECOMMENDATION** (no defect, an improvement). Finding IDs (`F-xx`) are stable; refer to them in commits and phase reports.

---

## 1. Executive Summary

The repository is a well-tested, carefully-documented **backend for an AI phone agent** (pharmacy/clinic-style customer support): a FastAPI service that takes Twilio Media Streams audio, transcribes it with Deepgram, runs a deterministic safety/intent/policy pipeline around a remote LLM (Gemini primary → Groq fallback), and speaks the reply back through ElevenLabs.

The component-level engineering is good: 924 tests pass, lint/format/type gates are clean, failure handling is consistently fail-closed, and the architecture keeps the LLM away from authorization and tool execution.

**But the previous "Product Readiness Audit" (2026-09-17) declared Safety, Voice and Security "COMPLETE", and that conclusion does not survive contact with the production configuration.** This audit reproduced six defects that sit *between* components — in factory wiring, the production Docker image, and dependency versions — which is exactly where the unit-level suite does not look:

| # | Finding | Evidence |
|---|---|---|
| F-01 | The **clinical safety guard is disabled in the production image** — dosage/interaction questions go straight to the LLM. | CONFIRMED, reproduced |
| F-02 | **Deepgram STT cannot connect at all** with the installed `websockets` version — every real call would get no transcription. | CONFIRMED, reproduced |
| F-03 | **Insecure auth defaults**: `AUTH_MODE=dev` + `DEV_AUTH_ENABLED=true` by default, and a hardcoded `test-admin-token` (public on GitHub) grants ADMIN. Phase 26 ran exactly this on a public ALB. | CONFIRMED |
| F-04 | **Anonymous access is allowed even in production auth mode**, with no rate limiting and no call caps → unbounded LLM/STT/TTS spend. | CONFIRMED (code) |
| F-05 | **System-prompt injection**: client-supplied `history` can carry `role: "system"` messages that get merged into the provider's system instruction. | CONFIRMED, reproduced |
| F-06 | **Gemini API key leaks** into exception text (key is in the URL query string), which is logged on failover. | CONFIRMED, reproduced |

> **Status update (2026-10-08, after H2):** F-03, F-04 and F-09 (plus F-18's job-ownership part) are **fixed** by H2 — `APP_ENV` posture, authenticated text API outside dev, voice fail-closed + call admission, rate limiting, identity isolation (§26).
>
> **Status update (2026-10-08, after H0 + H1):** F-01, F-02, F-05, F-06, F-07 and F-08 are **fixed** on branch `hardening/h0-h1`, each with permanent regression tests that were shown to fail on the old code (§26). F-03/F-04 remain open for H2. A follow-up AWS review is summarised in §3 (F-13).

**Maturity verdict:** text API = *Development-ready*; voice path = *Prototype* (its core STT dependency is non-functional). Not Beta-ready. None of the blockers needs a redesign; all are small, targeted fixes. The plan below is a **hardening track (H0–H7)** that fixes these first, adds the missing "test the production configuration" layer so they can't recur, and only then resumes the deployment track (plan.md Phases 17/19/26–28).

**Immediate next action:** Phase H0 + the F-01 fix (§25).

---

## 2. Project Purpose

- **Problem:** answer routine inbound customer calls (FAQs, opening hours, order status, appointment booking/cancel/reschedule) for a pharmacy/clinic-type business, while **never** improvising clinical advice and handing off to a human when unsure.
- **Users:** (a) phone callers via Twilio; (b) any HTTP client of `POST /generate` (there is **no frontend** in this repo — `src/voice/client_tts.py` is a CLI demo client); (c) operators via health/metrics endpoints.
- **Domain evidence:** `data/knowledge/{faqs,policies,medicine,appointments}.json`, `configs/clinical_triggers.yaml` (dosage, interactions, overdose), `CLINICAL_HANDOFF_RESPONSE` ("our pharmacist needs to answer…").
- **Current status (per git + docs):** application work stopped 2026-09-17 by strategy decision; deployment track (Phases 26–28) paused awaiting a domain, real Twilio/Deepgram/ElevenLabs keys and an OIDC provider. Phases 17/19/23 remain blocked on real traffic.

---

## 3. Current Architecture

A single Python process (FastAPI + uvicorn, one worker), all state either in-process memory or PostgreSQL.

```
                  ┌───────────── Twilio (PSTN) ─────────────┐      HTTP clients
                  │ POST /twiml/inbound-call   WS /ws/call  │      POST /generate, /jobs/generate
                  └───────────────┬─────────────────────────┘                │
                                  ▼                                          ▼
 ┌──────────────────────────── src/api/server.py (FastAPI) ─────────────────────────────┐
 │ middleware: X-Request-ID · exception→safe 500 · resolve_identity (Bearer → AuthContext)│
 │ VoiceCallManager/VoiceCallHandler (src/voice/voice_pipeline.py)                        │
 │   Deepgram STT (wss) ──transcript──►┐            ┌──tokens──► ElevenLabs TTS (https)   │
 │                                     ▼            │                                     │
 │            ConversationManager.handle_turn()  (src/agent/conversation_manager.py)      │
 │   1 input check → 2 ClinicalGuard+PolicyEngine → 2.4 Session confirm/PIN               │
 │   → 2.5 IntentEngine+PolicyEngine → 2.6 ToolOrchestrator (mock tools)                  │
 │   → 3 RAG (FAISS; OFF in prod image) → 4 prompt assembly (+memory, +client history)    │
 │   → 5 LLM (semaphore, retry, circuit breaker) → 6 HandoffDetector → result             │
 │ Session/Memory/Idempotency/Audit repos: in-memory (dev) | Postgres (PERSISTENCE_MODE)  │
 └───────────────┬──────────────────────────────┬───────────────────────────────────────┘
                 ▼                              ▼
   FallbackLLMProvider: Gemini 2.5 Flash → Groq (qwen)      PostgreSQL 16 (Alembic, 6 tables)
   (Claude→Gemini "fallback" mode exists, unused)            OpenTelemetry (optional), /metrics JSON
```

**Infrastructure (Phase 26, AWS us-east-1):** ECS Fargate (0.5 vCPU / 1 GB, desired 1), RDS Postgres `db.t4g.micro`, ALB (HTTP only), Secrets Manager (1 real + 9 placeholder secrets), CloudWatch logs + 4 alarms (no notification target), CodeBuild/S3 for image builds.

**F-13 — current state (2026-10-08):** An idle ECS task and RDS instance remain; not internet-reachable; decision pending. They still incur cost; keep / pause / tear down is OD-1. The detailed inventory is intentionally not recorded in this public document.

---

## 4. Technology Stack

| Layer | Technology | Notes |
|---|---|---|
| Language | Python 3.12 (CI, prod image) | **Local `.venv` is Python 3.14.7** — version drift (F-10) |
| API | FastAPI 0.139, uvicorn[standard] 0.51, python-multipart | single process |
| LLM | Gemini `gemini-2.5-flash`, Groq `qwen/qwen3.8-27b` via raw `requests`; Claude `claude-3-5-haiku-latest` (unused, model id outdated); local Qwen2.5-0.5B + QLoRA (dev only) | |
| Voice | Twilio Media Streams, Deepgram Nova-3 (`websockets`), ElevenLabs Flash v2.5 (`httpx`) | `websockets`/`httpx` **unpinned** in prod (F-02, F-10) |
| Safety/routing | YAML-driven phrase/semantic detectors, regex intent engine, PolicyEngine | deterministic |
| RAG | FAISS + sentence-transformers MiniLM | disabled in prod image |
| Persistence | SQLAlchemy 2.0, psycopg2, Alembic (3 migrations) | |
| Auth | PyJWT/cryptography OIDC (RS256, JWKS); dev token table | |
| Observability | OpenTelemetry SDK + FastAPI/SQLAlchemy/requests instrumentation; in-process MetricsRegistry | |
| CI | GitHub Actions: ruff format, ruff check, mypy src/, pytest (full ML requirements, CPU torch) | prod image never built/tested in CI |
| Training (offline) | transformers, peft, trl, bitsandbytes, datasets | not on the production path |

---

## 5. System Flow

### 5.1 Phone call (primary product flow)
1. **INPUT** Twilio POSTs `/twiml/inbound-call` → signature checked *only if* `TWILIO_AUTH_TOKEN` is set → TwiML `<Connect><Stream url=wss://…/ws/call>`.
2. Twilio opens `/ws/call` (same conditional signature check) → `START` frame → `VoiceCallManager.register_call()` creates `CallSession(session_id="call_<CallSid>", user_id="telephony_caller")` with Deepgram STT + ElevenLabs TTS.
3. **PROCESSING** `MEDIA` frames → `stt.send_audio()`; STT events → interim/final transcripts; `SpeechStarted` → barge-in (`clear` to Twilio, cancel turn).
4. Final transcript → `_execute_turn()` → builds `AuthContext` (authenticated only if session metadata says PIN verified) → `ConversationManager.handle_turn()` runs **in the default asyncio thread pool**.
5. **DECISION** clinical guard (if present) → session confirmation/PIN → intent → policy → tool (mock) or LLM.
6. **DATA ACCESS** session/memory/idempotency repos (Postgres in prod).
7. **EXTERNAL** Gemini (30 s timeout) → Groq on failure (30 s) → retried once more by ConversationManager.
8. **OUTPUT** tokens → TTS clause synthesis → μ-law 8 kHz frames → Twilio.

### 5.2 Text API
`POST /generate {message, history[], session_id, stream}` → `resolve_identity` (no header ⇒ `ANONYMOUS_CONTEXT`) → same `handle_turn()` → JSON or NDJSON. `POST /jobs/generate` is the same work dispatched to the default executor; `GET /jobs/{id}` polls (no ownership check).

### 5.3 Failure behaviour (verified by reading + existing DR tests)
- DB down at startup in production persistence → process refuses to start (good). DB down at runtime → `/ready` 503 (good).
- LLM failure before first token → fallback provider → retry → fixed apology + handoff (good). **Empty 200 response** (e.g. Gemini safety block) → no failover, empty reply → dead air (F-11).
- Clinical guard/policy internal error → fail closed (good) — *but only if the guard exists* (F-01).
- STT connect failure → logged, call continues silently (F-02 makes this the default).

---

## 6. Repository Structure

| Path | Role | Prod path? |
|---|---|---|
| `src/api/` | FastAPI server, job store, Twilio signature | yes |
| `src/agent/` (30 files) | orchestration, safety, policy, tools, sessions, memory, privacy, auth, audit, metrics, tracing, DB repos | yes |
| `src/voice/` | Twilio models, STT/TTS adapters, call pipeline, latency tracking | yes |
| `src/inference/` | LLM providers, handoff detector, local LLM service, legacy facade | partly |
| `src/rag/` | FAISS retriever, KB loader | dev only today |
| `src/training/`, `src/data/`, `src/export/`, `src/eval/`, `notebooks/` | QLoRA fine-tune + eval pipeline | no |
| `configs/` | YAML config: safety, intents, policies, reliability, auth | yes |
| `alembic/` | 3 migrations | yes |
| `tests/` (53 files, 924 tests) | unit + integration (in-memory and real-Postgres opt-in) | — |
| `scripts/` | pipeline runner, load/DR/stability/live-verification scripts | some |
| `docker/` | CUDA full image, lean prod image, compose, canary compose | yes |
| repo root | **30+ `PHASE_*_REPORT.md` files**, `plan.md`, runbook | docs debt (F-16) |

Imports rely on `sys.path.insert` (no packages, no `__init__.py`) — deliberate, documented, but fragile (F-16).

---

## 7. Important Components

- **`ConversationManager`** (`src/agent/conversation_manager.py`, 2021 lines): the single orchestration point; constructor-injected collaborators; every downstream failure maps to a fixed, safe response. `build_conversation_manager()` (line ~1763) is the single wiring point — and the location of F-01.
- **`PolicyEngine` / `ToolOrchestrator`**: deterministic authorization, schema validation, confirmation, idempotency, retries; LLM never chooses or authorizes tools.
- **`FallbackLLMProvider`**: provider failover before the first token, quota cooldown.
- **`VoiceCallHandler`**: STT↔turn↔TTS bridge with barge-in and cancellation.
- **`SessionManager` + Postgres repos**: TTL sessions, pending confirmations, optimistic concurrency.
- **`OIDCAuthenticationProvider`**: correct JWT validation (RS256 only, `none` rejected, required claims, JWKS caching, leeway).

---

## 8. Current Strengths (preserve these)

1. **Deterministic safety and authorization outside the LLM** — the right architecture for a regulated-adjacent domain.
2. **Consistent fail-closed error handling** with generic client-facing errors and correlation IDs.
3. **Strong component test suite**: 924 tests, failure injection, concurrency, red-team, real-Postgres DR scenarios (Phase 25).
4. **Honest documentation culture**: evidence levels (LOCAL/MOCKED/LIVE) are tracked; blocked items are not faked.
5. **Persistence layer** is solid: migrations, indexes on hot lookups, pool pre-ping, idempotency scoped per user.
6. **Clean quality gates** (ruff, mypy, CI) — re-verified green in this audit.

---

## 9. Current Weaknesses (summary)

1. **Nothing tests the production configuration as deployed** — the root cause of F-01, F-02 and the earlier `python-multipart` miss.
2. **Security defaults are "open unless configured"** (auth mode, dev tokens, Twilio signature, anonymous access) instead of "closed unless explicitly opened for dev".
3. **Trust boundary leak in prompt assembly** (client history roles).
4. **Voice path never exercised end-to-end with real providers**; latency/timeout budgets are sized for text, not live calls.
5. **Single-process in-memory state** (jobs, breakers, call registry, audit list) — fine for one task, wrong for scale-out and leaks memory.
6. **Tools are all mocks**; the product cannot yet complete a real business action.
7. **Documentation sprawl and drift** (README describes the fine-tuned-Qwen+RAG architecture; prod is Gemini/Groq without RAG).

---

## 10. Security Findings

| ID | Sev | Level | Finding | Evidence | Fix |
|---|---|---|---|---|---|
| F-01 | **CRITICAL** | CONFIRMED (reproduced) | Clinical safety guard disabled in production image. `clinical_guard` is only constructed inside the RAG branch of `build_conversation_manager()`; `docker/Dockerfile.production:51` sets `rag.enabled: false`. A dosage+interaction question reached the LLM and returned its answer; the same text with the guard present → handoff. | `src/agent/conversation_manager.py:1983-1998`, repro F1 | **FIXED (H1).** Guard built on every path; relative trigger path resolved against repo root; missing trigger file fails startup. (Non-dev startup assertion via `APP_ENV` remains H2.) |
| F-03 | **HIGH** (CRITICAL once real keys exist) | CONFIRMED | Insecure defaults: `AUTH_MODE` defaults `dev` (`server.py:113`), `DEV_AUTH_ENABLED` defaults `true` (`server.py:106`), and `identity.py:122-124` maps the public string `test-admin-token` → ADMIN. Phase 26 deployed with `AUTH_MODE=dev` on a public HTTP ALB `DEPLOYMENT_HANDOFF.md` §6 orders "populate real secrets" (step 3) *before* "switch to production auth" (step 4). | code, Phase 26 report §8, public GitHub repo | **FIXED (H2)** — `src/agent/runtime_env.py`: `APP_ENV` (unset = production); outside dev the server refuses to start with `AUTH_MODE=dev`, `VOICE_MOCK_SERVICES=true` or `TELEPHONY_MOCK_PIN`; handoff steps reordered (auth before secrets). |
| F-04 | **HIGH** | CONFIRMED (code) | Anonymous access in every auth mode: no `Authorization` header ⇒ `ANONYMOUS_CONTEXT` (`server.py:176-177`), so OIDC protects nothing for `/generate`/`/jobs`. `/ws/call` and `/twiml` skip signature checks entirely when `TWILIO_AUTH_TOKEN` is unset (fail-open). No rate limiting, no max concurrent calls, no max call duration anywhere in `src/`. | grep: no limiter in `src/api`, `src/voice` | **FIXED (H2)** — outside dev `/generate`, `/jobs/generate`, `/jobs/{id}` return 401 without valid credentials; Twilio endpoints reject when `TWILIO_AUTH_TOKEN` is unset; per-identity token-bucket rate limit (429 + `Retry-After`, default 60/min burst 10 outside dev); `MAX_CONCURRENT_CALLS` (default 10) and `MAX_CALL_DURATION_SECONDS` (default 1800). Rate limit and call cap are per process (single task). |
| F-05 | **HIGH** | CONFIRMED (reproduced) | Prompt injection via `history`: `_normalize_history()` accepts any `role` string (`conversation_manager.py:1618`); Claude/Gemini converters merge every `system` message into the real system instruction. Clinical guard only scores the current message, never history. Clients can also forge `assistant` turns. | repro F2: injected text present in Gemini `systemInstruction` | **FIXED (H1)** — `_normalize_history()` allow-lists `user`/`assistant`. Server-side history remains H2 (OD-4). |
| F-06 | **HIGH** | CONFIRMED (reproduced) | Gemini key in URL query (`llm_provider.py:322`). On a network error the `requests` exception text contains the URL incl. `key=`, wrapped into `LLMProviderError` and logged by `FallbackLLMProvider` (`llm_provider.py:680`) → CloudWatch. `RequestsInstrumentor` may also record the URL as a span attribute. | repro F4 | **FIXED (H1)** — header auth (verified accepted by the real API); `_redact_secret()` applied at all 9 provider error sites (Claude/Gemini/Groq). API responses were already generic (control test kept). Rotation: see §23. |
| F-07 | MEDIUM | CONFIRMED (reproduced) | Session wipe-and-takeover: on a cross-user `get_session()` miss, `handle_turn()` calls `create_session(session_id=<same id>)` (`conversation_manager.py:676-678`), which **deletes** the victim's session first (`session_manager.py:145`). Session IDs are client-chosen. | repro F3: owner became `mallory`, Alice's data gone | **FIXED (H1)** — `SessionManager.get_or_create_session()` never deletes/re-owns a foreign session; turn proceeds sessionless + CROSS_USER_ACCESS_ATTEMPT event. H0 also found a worse variant: a caller with **no** AuthContext skipped the ownership check and could confirm the owner's pending action — fixed by the same change. Ownerless (`user_id=None`) sessions and the shared `anonymous` identity remain F-09/H2. |
| F-08 | MEDIUM | CONFIRMED | Live audit trail is in-memory and unredacted: `server.py:122` builds `AuditLogger()` with no repository and no privacy service; the factory only builds the Postgres-backed one when *none* is passed, so `PostgresAuditRepository` is never used by the server, metadata is not PII-sanitized, and the list grows without bound (500 events after 500 turns, no cap). The jobs module docstring wrongly says AuditRepository is bounded. | repro F5, `audit.py:53-62,150` | **FIXED (H1)** — in-memory `AuditRepository` bounded (deque, 10,000 events each for audit/security); factory calls `AuditLogger.attach_defaults()` on an injected logger so the server's shared logger gets the persisted repo + privacy service. |
| F-09 | MEDIUM | CONFIRMED (code) | Identity collapse: all anonymous users are `user_id="anonymous"`; all phone callers are `"telephony_caller"`. Memory, session ownership and tool ownership checks are therefore shared across unrelated people. CANCEL/RESCHEDULE never set `resource_owner_user_id` (documented Phase 7 debt) → any authenticated user can act on any appointment ID. Impact is bounded today because tools are mocks. | `action_models.py:189`, `voice_pipeline.py:632`, `conversation_manager.py:1388` | **FIXED (H2)** — session ownership must match exactly (no ownerless sharing); memory context only for authenticated identities; telephony identity is per call (`telephony:<CallSid>`); ToolOrchestrator resolves the appointment owner itself (registry `owner_lookup`) before CANCEL/RESCHEDULE, ignoring caller-supplied owner claims. Real per-caller verification (OD-6) remains open. |
| F-17 | LOW | CONFIRMED (code) | TwiML `Stream url` built from `Host`/`X-Forwarded-Host` without XML escaping when `TWILIO_MEDIA_STREAM_URL` is unset. | `server.py:792-805` | Require configured public URL in non-dev; escape. |
| F-18 | LOW | CONFIRMED | `/health/voice` and `/metrics` unauthenticated (configuration + traffic disclosure); `/jobs/{id}` has no ownership check (IDs are 64-bit random). | `server.py:437-502,728` | **Jobs: FIXED (H2)** — owner recorded; non-owner gets 404, unauthenticated 401 outside dev. `/metrics`, `/health/voice`: still unauthenticated by design — restrict at the network layer (H6). |
| F-19 | LOW | CONFIRMED | Production container runs as root (no `USER`). | `Dockerfile.production` | Add non-root user. |
| F-20 | LOW | POTENTIAL | Client-supplied `X-Request-ID` echoed into logs/audit unvalidated (log-forging). | `server.py:317` | Validate format/length. |
| — | INFO | — | Public repo contains AWS account ID and ALB hostname in docs. Not secrets, but aids targeting. | docs | Consider redacting. |
| — | GOOD | — | No secrets committed (`.env` ignored; Secrets Manager values not in repo); OIDC validation is correct; Twilio HMAC uses constant-time compare; idempotency scoped per user. | | |

`pip-audit` was not run (not installed); dependency CVE scanning is part of H4.

---

## 11. Performance Findings

| ID | Current behaviour | Why it hurts | Impact | Recommendation | Risk |
|---|---|---|---|---|---|
| F-11 | LLM timeout 30 s per provider (`config.yaml`), plus ConversationManager retry (`reliability.yaml` llm.max_retries=1) around the whole Fallback chain. | On a live call, a hung Gemini → 30 s silence → Groq → possible retry of both: worst case ~2 min of dead air before the apology. Gemini empty-200 (safety block) yields `""` with no failover. | Callers hang up; no handoff happens. | Voice-specific **time-to-first-token deadline** (~2–3 s) and total turn budget; treat empty text as a provider failure; spoken filler on delay. | Low; config + small code change, testable with fakes. |
| F-12 | Voice turns (`voice_pipeline.py:415`) and `/jobs/generate` (`server.py:723`) both use the **default** asyncio executor (`min(32, cpu+4)` ≈ 5 threads on a 0.5 vCPU task). | A burst of `/jobs` submissions queues ahead of live call turns; ~5 concurrent turns process-wide. | Call latency spikes under any load; easy DoS. | Dedicated bounded executors per workload + admission control (reject jobs when queue full; cap active calls). | Low. |
| F-12b | In-process state: JobStore, circuit breakers, provider cooldown, call registry, audit list. | With desired_count > 1, `/jobs/{id}` polls 404 on the other task; breakers don't share state. | Blocks horizontal scaling. | Keep single task until needed; then move job/session state to Postgres (already present) before adding Redis. | Medium; defer. |
| — | TTS per-clause HTTPS POST (documented in `KNOWN_LIMITATIONS.md` §2). | Extra connection setup per clause. | ~tens of ms per clause. | Reuse one `httpx.AsyncClient` per call first (cheap); WebSocket streaming later. | Low. |
| — | Per-turn `requests.post` with no session reuse for LLM calls. | New TLS handshake per turn. | ~50–150 ms per turn. | `requests.Session` per provider. | Low. |
| — | RAG/FAISS, local model | Not on prod path. | — | No action. | — |

No real-traffic latency data exists yet (correctly noted by prior reports). The documented "550–850 ms" voice latency in `KNOWN_LIMITATIONS.md` is an estimate based on Claude 3.5 Haiku, which is not the deployed provider. Measure in H3 before optimizing further.

---

## 12. Code Quality Findings

| ID | Finding | Priority |
|---|---|---|
| F-02 | **FIXED (H1)** — now `additional_headers=`; `websockets==17.0.1`/`httpx==0.28.1` pinned; contract test against a local server; real-endpoint handshake returns HTTP 401 for a fake key (reaches Deepgram). Original finding — **Deepgram STT never connects**: `websockets.connect(url, extra_headers=…)` (`stt_service.py:116`) raises `TypeError` on `websockets` ≥14 (installed: 17.0.1; prod image gets whatever `uvicorn[standard]` pulls). Tests only use `MockSTTService` or the no-key path, so it never ran. CONFIRMED, reproduced. | P0 |
| F-10 | Build/runtime drift: CI installs `requirements.txt` (full ML stack) and never builds `Dockerfile.production` or installs `requirements-production.txt`; transitive deps (`websockets`, `httpx`) unpinned; local Python 3.14 vs CI/prod 3.12; the prod image patches config with `sed`. | P1 |
| F-15 | Model/config drift: `claude-3-5-haiku-latest` is a retired model id (only matters if Claude is enabled); README presents the fine-tuned Qwen+RAG design as the architecture; readiness audit's "Safety: COMPLETE" is contradicted by F-01. | P2 |
| F-16 | Structure: `sys.path` imports; `conversation_manager.py` is 2021 lines, with `_handle_turn_body` ~750 lines and long phase-history comments; voice `_cm_turn_generator` tries three different `handle_turn` signatures via `TypeError` (masks real TypeErrors); 30+ phase reports at the repo root. | P2–P3 |
| — | Tool layer is 100% mock (`mock_tools.py`); `BOOK_APPOINTMENT` can never succeed (no slot filling — documented). Product gap rather than code defect. | P2 (product) |
| — | Duplicated `_execute_pending_*` / final-dict construction blocks; acceptable, refactor only when touching. | P3 |
| — | Swallowed exceptions in shutdown and AuthContext construction (`voice_pipeline.py` `except Exception: pass` around auth → `auth=None`). Auth failure silently degrades to anonymous — acceptable (fails closed for tools) but should log. | P3 |

Do **not** refactor for style. The only structural refactor recommended in the near term is extracting `_handle_turn_body`'s stages into private methods *when* H1/H2 changes touch them.

---

## 13. Testing Findings

**What is tested well:** every agent component in isolation, policy/authorization matrices, failure injection, concurrency, red-team prompts, real-Postgres repositories (opt-in), signature validation, DR scenarios, voice pipeline with mock STT/TTS.

**What is not tested (most dangerous first):**
1. **The production wiring/configuration** — nothing builds the app the way `Dockerfile.production` does and asserts its invariants (guard present, auth closed, signature enforced). → F-01, F-03, F-04 slipped through.
2. **Real adapter contracts** — Deepgram/ElevenLabs/Gemini adapters are only tested against mocks of themselves, not against the libraries' real call signatures. → F-02.
3. **Trust-boundary tests on the HTTP API** — role injection in history, cross-user session IDs. → F-05, F-07.
4. **Secret hygiene in logs/spans** → F-06.
5. **Voice latency budget** (TTFT deadline, empty response).
6. Live E2E (real Twilio call) — correctly blocked on credentials.

**Minimum high-value additions (H0):**
- `tests/test_production_config.py`: build the server/manager with the prod image's config (RAG off, remote provider stub) and assert: clinical guard non-null and fires on a clinical phrase; dev auth rejected when `APP_ENV=production`; anonymous `/generate` rejected; `/ws/call` rejected without signature config outside mock mode.
- `tests/test_trust_boundaries.py`: system-role history stripped; cross-user session_id cannot delete/take over.
- `tests/test_secret_hygiene.py`: provider network error → no key in exception/log records.
- Adapter contract test: `DeepgramSTTService.connect()` against a local `websockets.serve` echo server (no network).
- CI job: `docker build -f docker/Dockerfile.production` + container smoke test (`/health`, `/ready`, one stubbed turn).

The scratchpad reproduction used in this audit (six scenarios) is the seed for these tests.

---

## 14. Production Readiness

**Level: 2 — Development-ready** (text API). Voice: **1 — Prototype**.

| Dimension | State | Blocks next level |
|---|---|---|
| Reliability | Good component fallbacks; voice budgets unsuitable | F-02, F-11, F-12 |
| Security | Correct primitives, insecure defaults | F-01, F-03–F-07 |
| Observability | Tracing, metrics JSON, runbook; audit not persisted | F-08; alarms have no notification target |
| Monitoring/alerting | 4 CloudWatch alarms, no SNS target | Decide alert destination |
| Logging | Structured, privacy filter on turn logs; key leak | F-06 |
| Backups/recovery | RDS automated backups assumed (not verified this audit); DR tested locally | Verify RDS retention; restore drill |
| Deployment/rollback | ECS task-def revisions; manual CodeBuild; no CI/CD to AWS | Pipeline (H6) |
| Scalability | Single task; in-process state | F-12b (defer) |
| Cost control | No rate limiting; idle infra possibly billing | F-04, F-13 |

**To reach Beta (3):** H1–H4 complete, one real Twilio test number exercised end-to-end, HTTPS, OIDC or another real auth decision, alerts routed somewhere a human sees them.

---

## 15. Technical Debt Register

| Debt | Interest being paid | Action |
|---|---|---|
| No production-config test layer | Defects found only in prod/live | H0 (pay now) |
| Open-by-default security switches | Every new environment is unsafe until configured | H2 |
| Client-owned conversation history | Injection surface; can't trust context | H2 → server-side history |
| In-memory singletons | Memory growth; no scale-out | H2 (audit), defer rest |
| Mock tools, no slot filling | Product can't complete actions | H7 (needs business input) |
| `sys.path` imports / no packages | Import-order fragility, tooling friction | H5, optional |
| Docs sprawl (30+ root reports, 1084-line plan.md) | Hard to find truth; drift | H5: move to `docs/reports/`, this file becomes the index |
| Training/RAG pipeline in the same repo & requirements | Heavy CI, irrelevant to prod | H4: split requirements; keep code |

---

## 16. Critical Issues (P0)

1. **F-01** clinical guard disabled in prod config.
2. **F-02** STT cannot connect.
3. **F-03/F-04** open-by-default access (dev admin token, anonymous LLM access, unsigned voice WS).
4. **F-05** system-prompt injection via history.
5. **F-06** API key leak in logs.

Priority matrix:

| Priority | Problem | Evidence | Impact | Effort | Risk of fix |
|---|---|---|---|---|---|
| P0 | F-01 clinical guard off in prod | repro F1 | Unsafe medical answers | S | Low |
| P0 | F-02 Deepgram connect TypeError | repro, websockets 17 | Voice product non-functional | S | Low |
| P0 | F-03 dev-auth defaults + public admin token | code + Phase 26 config | Full API takeover in any default deployment | S | Low (dev/test env must set `APP_ENV=dev`) |
| P0 | F-04 anonymous access, no limits, unsigned WS | code | Cost abuse, DoS | M | Medium (changes API contract for anonymous clients) |
| P0 | F-05 role injection | repro F2 | Safety bypass via prompt | S | Low |
| P0 | F-06 key in logs | repro F4 | Credential compromise | S | Low |
| P1 | F-07 session takeover | repro F3 | Integrity/DoS of sessions | S | Low |
| P1 | F-08 audit not persisted, unbounded | repro F5 | Compliance gap, memory leak | S | Low |
| P1 | F-10 prod build untested / unpinned deps | CI file | Recurring prod-only failures | M | Low |
| P1 | F-11 voice timeouts/empty response | config + code | Dead air, missed handoffs | M | Medium |
| P1 | F-12 shared default executor | code | Latency/DoS under load | S | Low |
| P1 | F-13 unknown AWS state / cost | DNS gone, no teardown record | ~$50/mo leak, stale RDS | S (decision) | — |
| P2 | F-09 identity collapse, CANCEL IDOR | code | Cross-caller data mixing (once tools are real) | M | Medium |
| P2 | F-15/F-16 docs & structure drift | files | Onboarding cost, wrong decisions | M | Low |
| P2 | Mock-only tools | `mock_tools.py` | No business value from actions | L | — |
| P3 | F-17–F-20 | code | Minor | S | Low |

---

## 17. Target Architecture

Same shape as today — **no new services, no new datastores** in the near term. Changes are about *where trust is decided* and *what is guaranteed by construction*.

| Area | CURRENT → PROBLEM → TARGET → WHY → MIGRATION |
|---|---|
| Safety wiring | Guard built inside RAG branch → disappears when RAG is off → **`SafetyGuard` always constructed by the factory; startup assertion in non-dev env** → safety must not depend on an unrelated feature flag → one-line move + test; no API change. |
| Environment model | Many independent "dev-ish" defaults (`AUTH_MODE`, `DEV_AUTH_ENABLED`, unset `TWILIO_AUTH_TOKEN`) → open by default → **single `APP_ENV` (`dev`/`test`/`staging`/`production`), default `production`-strict in the container; dev conveniences require `APP_ENV=dev`** → one switch is auditable; closed by default → add `settings.py` that derives existing flags; tests/compose set `APP_ENV=dev`; existing env vars keep working. |
| Access control | Anonymous fallback in all modes → **anonymous allowed only when `APP_ENV=dev` or `ALLOW_ANONYMOUS=true`**; rate limit per identity/IP; max concurrent calls + max call duration | Cost and abuse control → middleware + `VoiceCallManager` admission check. Compare: `slowapi` (dependency, decorator-based) vs. ~60-line in-process token bucket. **Recommend in-process token bucket** (single task, no new dependency); revisit with ALB/WAF rate rules when deploying. |
| Conversation context | Client sends full history incl. roles → **server-side history from SessionState for `session_id` turns; client `history` limited to `user`/`assistant` and treated as untrusted** → removes injection surface; enables per-session memory → step 1 role filter (H1), step 2 server-side history (H2), deprecate client history later. |
| Provider adapter | Key in URL; generic exceptions; no TTFT deadline → **header auth, sanitized errors, `requests.Session`, voice deadline, empty-text = failure** → secrets hygiene + voice UX → contained in `llm_provider.py`. |
| Audit/observability | Server-built in-memory audit logger → **one audit logger constructed from persistence resolution, injected into server auth boundary** → persisted, redacted, bounded → reorder construction in `server.py` lifespan. |
| Concurrency | Default executor shared → **two bounded executors (`voice`, `jobs`)** → isolation of live calls from batch work → small change, no API change. |
| Build/release | Prod image untested; `sed` config patch → **CI builds prod image and runs config-invariant tests in it; config overrides via env (`RAG_ENABLED=false`) instead of `sed`; lock file for prod deps** → "what we test is what we ship". |
| Scale-out (later) | In-process job/call state → Postgres-backed job table when >1 task is needed; Redis only if Postgres proves insufficient → avoid premature infra. |

---

## 18. Migration Strategy

- **Additive first, then flip defaults.** Each security default change ships with: (1) the new strict behaviour behind `APP_ENV`, (2) all tests/compose files updated to `APP_ENV=dev`, (3) a startup log line stating the effective security posture.
- **Every fix lands with the failing test first** (the H0 characterization tests should fail on `main` and pass after the fix — that is the proof the fix is real).
- **No behaviour change for dev workflows** beyond setting `APP_ENV=dev`.
- **Deployment track untouched** until H1–H4 are done; then resume plan.md Phase 26 with the reordered handoff (auth before secrets).
- **Rollback:** each phase is one or a few self-contained commits; revert is `git revert <sha>`; no data migrations in H0–H5 (server-side history in H2 reuses existing `sessions` table metadata — verify column capacity before choosing that over a new migration).

---

## 19. Prioritized Roadmap (Hardening Track)

Numbered H-phases to avoid colliding with plan.md's Phase 1–28 numbering. Each phase ends with a short report appended to §26 of this file (not a new root-level report file).

### H0 — Baseline & Safety Net — ✅ COMPLETE (2026-10-08)
- **Objective:** make the defects visible to CI before fixing them; settle infra state.
- **Tasks:** (1) Re-authenticate AWS access and inventory `ai-voice-agent-*` resources; user decides keep/pause/tear down (F-13). (2) Add `tests/test_production_config.py`, `test_trust_boundaries.py`, `test_secret_hygiene.py`, Deepgram contract test — marked `xfail(strict=True)` referencing F-IDs. (3) Pin `websockets`/`httpx` explicitly in both requirements files.
- **Files:** `tests/*` (new), `requirements*.txt`.
- **Dependencies:** none (AWS item needs user).
- **Expected result:** CI green, with xfails documenting each open defect.
- **Validation:** xfails flip to XPASS→fail when a fix lands (strict), forcing the marker removal.
- **Risks:** none to runtime. **Rollback:** revert commit.

### H1 — Critical Fixes (P0, code-only) — ✅ COMPLETE (2026-10-08)
- **Objective:** close F-01, F-02, F-05, F-06 (+ F-07, F-08 pulled forward from H2 at the owner's request).
- **Tasks:** guard construction independent of RAG + non-dev startup assertion; Deepgram `additional_headers` (with a version-compat shim only if a pinned older version must be supported — prefer pinning ≥14 and using the new API); history role allow-list; Gemini header auth + exception scrubbing; rotate Gemini key if logs ever left the box.
- **Files:** `src/agent/conversation_manager.py`, `src/voice/stt_service.py`, `src/inference/llm_provider.py`.
- **Expected result:** H0 xfails for these IDs removed and passing.
- **Validation:** full suite + targeted tests; manual repro script re-run shows all four fixed.
- **Risks:** guard may now fire in deployments that previously answered clinical questions — that is the intended behaviour. **Rollback:** per-finding commits, revertable independently.

### H2 — Secure Defaults & Trust Boundaries — ✅ COMPLETE (2026-10-08)
- **Objective:** close F-03, F-04; begin F-09. (F-07/F-08 were pulled forward into H1 and are done.)
- **Tasks:** `APP_ENV` settings module (incl. a non-dev startup assertion that the clinical guard is present); refuse dev auth outside dev; anonymous only when allowed; Twilio signature required outside mock mode; token-bucket rate limiting; max concurrent calls/duration; ownerless (`user_id=None`) sessions and the shared `anonymous`/`telephony_caller` identities; bound `SecurityEventDetector`'s per-client failure counters; server-side history for session turns; fix `DEPLOYMENT_HANDOFF.md` step order.
- **Files (as built):** new `src/agent/runtime_env.py`, `src/api/rate_limiter.py`, `tests/conftest.py`; `server.py`, `jobs.py`, `metrics.py`, `tool_registry.py`, `tool_orchestrator.py`, `mock_tools.py`, `session_manager.py`/`conversation_manager.py`, `voice_pipeline.py`, compose files, `.env.example`, docs.
- **Dependencies:** H0 tests. **Open decision:** OD-2 (anonymous API policy), OD-4 (client history support).
- **Validation:** production-config tests; manual curl matrix (no header / dev token / bad token) with `APP_ENV=production`.
- **Risks:** breaks any client relying on anonymous access → gated by OD-2. **Rollback:** set `APP_ENV=dev` (behavioural escape hatch) or revert.
- **Deferred (not done in H2):** server-side conversation history (needs OD-4); bounding `SecurityEventDetector`'s per-client failure counters (still an unbounded dict keyed by client address — small, slated for H3 with the other in-process state); F-17 TwiML Host-header handling. OD-2 was resolved by the owner: production requires authentication on the text API.

### H3 — Voice Reliability & Latency
- **Objective:** F-11, F-12; make the call path behave well under provider slowness.
- **Tasks:** voice TTFT deadline + total turn budget; empty-text → failover; spoken filler/handoff on budget exhaustion; dedicated executors; `requests.Session`/shared `httpx.AsyncClient`; latency metrics per stage exported via `/metrics`.
- **Validation:** fake-provider tests with injected delays (deadline hit → handoff within budget); `scripts/performance_load_test.py` before/after; when keys exist, live Gemini/Groq timing.
- **Risks:** too-tight deadline causes premature handoffs → make configurable, start conservative (3 s TTFT).

### H4 — CI & Supply Chain
- **Objective:** "what we test is what we ship" (F-10).
- **Tasks:** CI job building `Dockerfile.production` and running the production-config tests + container smoke test inside it; replace `sed` with `RAG_ENABLED` env override; lock file for prod deps (`pip-compile` → `requirements-production.lock`); `pip-audit` step; align local Python to 3.12 (or move all to 3.13/3.14 deliberately); non-root container user.
- **Validation:** CI run on a branch; intentionally break a pin to confirm the job catches it.

### H5 — Documentation Truth & Code Health
- **Objective:** F-15, F-16.
- **Tasks:** README architecture rewritten to match prod; correct `PRODUCT_READINESS_AUDIT.md` with a dated addendum pointing here; move root `PHASE_*` reports to `docs/reports/`; update `KNOWN_LIMITATIONS.md` latency numbers; update or remove `claude-3-5-haiku-latest` default (→ a current Haiku model id if Claude stays supported); remove the multi-signature `TypeError` fallback in voice turn calls; extract `_handle_turn_body` stages opportunistically.
- **Validation:** link check; tests unchanged and green.

### H6 — Production Hardening (resumes deployment track; gated on user inputs)
- **Objective:** plan.md Phases 26 (resume), 17, 19, 27, 28.
- **Tasks:** domain + ACM + HTTPS-only ALB; OIDC (or explicit API-key) decision; real secrets; alerts → SNS/email; RDS backup retention & restore drill; CI/CD to ECS with task-def rollback; one real Twilio number test; canary per `CANARY_PROCEDURE.md`.
- **Dependencies:** H1–H4; OD-1, OD-3, OD-6.

### H7 — Product Features (only after H6 canary is stable)
- Real tool backends (appointments/orders), slot filling for booking, per-caller identity verification (OD-6), ElevenLabs WebSocket streaming, cost dashboard (plan.md Phase 23).

---

## 20. Execution Order — and why

**H0 → H1 → H2 → H4 → H3 → H5 → H6 → H7**

1. **H0 first** because every later fix needs a test that proves it, and the AWS decision may be costing money today. Zero runtime risk.
2. **H1 before H2** because H1's fixes are small, local and low-risk, yet close the two highest-impact defects (unsafe clinical answers, broken STT) and two credential/safety leaks. Highest value per line changed.
3. **H2 before anything that adds exposure.** It changes defaults and API contracts, so it needs H0's tests and one product decision (OD-2).
4. **H4 before H3** (swap from the template order): the prod-image CI job is what prevents F-01/F-02-class regressions from coming back; H3's tuning is only meaningful if the measured build is the shipped build.
5. **H3** before any live call — a live call is when dead air matters.
6. **H5** is low-risk and can interleave, but is deliberately after the fixes so docs describe the fixed system.
7. **H6/H7** need external inputs and should not start until the foundation is verified.

---

## 21. Future Backlog

**NOW** — H0 (AWS inventory decision, characterization tests, pins), H1 (F-01, F-02, F-05, F-06), H2 (F-03, F-04, F-07, F-08).

**NEXT** — H4 (prod-image CI, lock file, pip-audit, Python alignment), H3 (voice deadlines, executors, connection reuse, per-stage latency metrics), H5 (docs truth, report relocation, model id refresh), F-09 per-caller identity design.

**LATER** — Postgres-backed job state for multi-task scaling; real tool integrations + slot filling; ElevenLabs WebSocket TTS; cost/usage analytics (Phase 23); packaging (`src/` as an installable package); revisit local Qwen pipeline (keep / split to its own repo — OD-7).

Explicitly **not** planned: microservices, a message broker, Kubernetes, a vector DB service, a frontend. None is justified by current evidence.

---

## 22. Engineering Standards (project-specific)

**Code structure**
- All collaborator construction happens in `build_conversation_manager()` / server lifespan. *Safety-critical collaborators are never conditional on unrelated feature flags.*
- New modules follow the existing pattern (constructor injection, in-memory default + Postgres repository) until H5 decides on packaging; do not add new `sys.path` hacks beyond existing directories.

**Naming** — `F-xx` IDs for findings, `H-n` for hardening phases, plan.md `Phase N` for the original track. Env vars: `UPPER_SNAKE`, documented in `.env.example` in the same commit.

**Error handling**
- Fail closed for safety/auth; degrade for routing/RAG — as today.
- Exception text from external libraries is **never** logged verbatim from provider adapters; log `type(exc).__name__` + sanitized status code.
- Never use `except TypeError` to probe call signatures.

**Logging** — through `privacy_logging` for anything containing user text; no secrets, tokens, URLs with query strings, or full transcripts at INFO.

**Testing**
- Every bug fix lands with a test that fails before the fix.
- Every external adapter has a contract test against the real library API (local fake server), not only a mock of the adapter.
- Every security-relevant default has a production-config test.
- Live-provider tests stay opt-in (`RUN_LIVE_*`), never in default CI.

**API design** — client-controlled input is untrusted, including `history`, `session_id`, `X-Request-ID`, Twilio custom parameters. Identity only from the auth header or verified telephony flow. Response bodies built from named fields (existing practice).

**AI/agent design**
- LLM is text-in/text-out; no tool calling by the model (keep).
- Pre-generation safety guard on every LLM-bound turn, regardless of RAG.
- Only the application constructs `system` messages.
- Every model call has a deadline appropriate to its channel (voice ≪ text).
- Empty model output is a failure, not a valid answer.

**Security** — closed by default; dev conveniences require `APP_ENV=dev`; never put API keys in URLs; secrets only via env/Secrets Manager; rotate on any suspected exposure.

**Git workflow** — branch per H-phase (`hardening/h1-critical-fixes`), small commits referencing F-IDs, PR into `main`, CI must be green. Shared files changed by one phase may land in one combined commit (existing project convention); don't hunk-split artificially.

**Pull requests** — template: findings addressed (F-IDs), tests added (and proof they failed before), config/env changes, rollback note.

**Environment management** — `.env.example` is the source of truth for variable names; `APP_ENV` is mandatory in non-dev deployments; local dev uses Python 3.12 to match CI/prod (until a deliberate upgrade).

**Documentation** — this file is the index and the running log. Phase outcomes are appended to §26, not new root files. Claims of "COMPLETE" must name the configuration they were verified against.

**Observability** — every new failure path increments a named counter in `MetricsRegistry` and is mentioned in `docs/INCIDENT_RESPONSE.md` if it should page.

---

## 23. Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Idle AWS resources billing / stale exposed RDS | Unknown (cannot verify — AWS connector needs re-auth) | ~$50/mo, attack surface | H0 inventory + decision |
| Gemini key exposed before the F-06 fix | **AWS: very low** — the deployed environment never held a real Gemini key, so its logs could not contain one. **Local: possible** — the real free-tier key the user supplied on 2026-09-15 was used on this machine (and pasted into a chat session) while the old URL-based code was live; any local network-error log would contain it. | Key misuse / quota theft | **Owner action: rotate that Gemini key** (Google AI Studio → regenerate) as a precaution. Not done by Claude. |
| Tightening auth breaks unknown clients | Low (no known clients besides CLI demo) | API callers fail | OD-2; `APP_ENV=dev` escape hatch |
| Free-tier provider limits (Gemini/Groq) under real call volume | High once live | Fallback churn, dead air | H3 deadlines; Phase 23; OD on paid tier |
| Live voice issues beyond F-02 (barge-in, AEC, Twilio signing of WS) | Medium | Poor call quality | H6 single-number test before canary |
| Over-reliance on component tests | Was High | Missed prod defects | H0/H4 production-config tests |

---

## 24. Open Questions / OPEN DECISIONS

- **OD-1 AWS resources:** keep, pause (scale ECS service to 0, stop RDS — note AWS auto-restarts a stopped RDS after 7 days), or tear down? An idle ECS task and RDS instance remain; not internet-reachable; decision pending. These resources still incur cost. Take a database snapshot before any teardown.
- **OD-2 Anonymous text API:** is unauthenticated `POST /generate` a product requirement (e.g. a public web widget), or should every non-voice client authenticate?
- **OD-3 Auth provider:** OIDC IdP choice, or simpler per-client API keys for server-to-server callers?
- **OD-4 Conversation history:** keep supporting client-supplied history, or move fully to server-side session history?
- **OD-5 Business domain:** confirm the target (pharmacy? clinic? general support?) — it determines clinical-guard strictness and which real tools to integrate first.
- **OD-6 Caller identity:** how should phone callers be verified (caller-ID + OTP, per-customer PIN store, account lookup)? The current shared mock PIN is not a real mechanism.
- **OD-7 Local Qwen fine-tuning pipeline:** keep in this repo, split out, or archive?
- **OD-8 Python version:** standardize on 3.12 (CI/prod) or upgrade everything to 3.14 (local)?
- **OD-9 LLM spend:** remain free-tier only (standing decision), or allow a paid tier for live-call reliability?

---

## 25. Immediate Next Action

H0, H1 and H2 are complete on branch `hardening/h0-h1` (uncommitted — owner to review/commit). Next, **only after owner approval**:
1. **Owner decisions:** OD-1 (AWS keep/pause/teardown), Gemini key rotation (§23), OD-3 (OIDC provider — production now *requires* it to start), OD-4 (client-supplied history), OD-6 (caller verification).
2. **H4 — CI & supply chain** (prod-image build job, lock file, pip-audit, Python alignment, non-root container), then H3.

---

## 26. Change Log

| Date | Phase | Summary |
|---|---|---|
| 2026-10-08 | H2 | **Secure defaults & trust boundaries.** `APP_ENV` posture module (`runtime_env.py`; unset = production; unknown value refused; all violations reported together). Startup refuses `AUTH_MODE=dev`, mock voice, mock PIN outside dev; lifespan asserts the clinical guard outside dev. Text API: 401 without valid credentials outside dev (incl. the public `test-admin-token`); job ownership. Voice: Twilio endpoints fail closed without `TWILIO_AUTH_TOKEN` outside dev; call admission + max duration. Rate limiter (`rate_limiter.py`, bounded keys). F-09: exact session ownership, memory only for authenticated identities, per-call telephony identity, orchestrator-side appointment-owner lookup. Config/docs: `.env.example`, `.env.canary.example` (`staging`), compose (`dev` locally), `Dockerfile.production` comment, `DEPLOYMENT_HANDOFF.md` (auth before secrets), README quick start (`APP_ENV=dev`), `tests/conftest.py` + CI `APP_ENV=dev`, local harness scripts set dev. **Found by tests during H2:** the new metric names weren't registered in `MetricsRegistry`'s fixed set (would have raised in production on the first rejected call). **Validation:** 49 new tests; F-09 tests shown failing on a copy with only those hunks reverted (5/5); full suite **1006 passed**; ruff clean; mypy clean (62 files). Python 3.12 prod-only sim with a local JWKS and real RS256 tokens: 17/17 checks (valid token 200; anonymous/dev-token/wrong-key/expired/wrong-audience 401; job owner-only; burst-then-429; unsigned TwiML 403; no key/token in logs); startup with no `APP_ENV`/`AUTH_MODE` refused; under real uvicorn an idle call is closed (1000) at the 2 s limit. |
| 2026-10-08 | H1 | **Fixes, one per finding, each turning its H0 xfails into passing tests.** F-01: clinical guard built on every factory path; repo-root path resolution; missing trigger file fails startup (`conversation_manager.py`). F-02: `additional_headers=` (`stt_service.py`); verified against real Deepgram (HTTP 401 for a fake key = request reaches the service). F-05: history role allow-list `user`/`assistant`. F-06: Gemini key moved to `x-goog-api-key` header (real API accepts it: "API key not valid" for a fake key); `_redact_secret()` at all 9 Claude/Gemini/Groq error sites. F-07: `SessionManager.get_or_create_session()`; foreign session ids never deleted/re-owned/used, incl. the no-AuthContext variant. F-08: bounded `AuditRepository` (10,000); `AuditLogger.attach_defaults()` gives the server's shared logger the persisted repo + privacy service. Also `alembic/env.py`: `disable_existing_loggers=False` — in-process migrations silenced all app loggers, which made the F-06 log test pass vacuously / fail by order (production runs migrations out-of-process, so live logging was unaffected). **Validation:** full suite 957 passed (924 → +33); ruff format/check clean; mypy clean (60 files). Production simulation (no Docker daemon available): clean Python 3.12 venv with only `requirements-production.txt` + image's `sed` RAG-off config → server lifespan boots, guard active, clinical question handed off with 0 LLM calls, ordinary question fails over through real Gemini/Groq rejections to the safe apology, sentinel key absent from all 78 log lines; the 33 regression tests also pass in that environment. |
| 2026-10-08 | H0 | Regression tests added: `test_production_config.py` (F-01), `test_voice_stt_contract.py` (F-02, local `websockets.serve` Deepgram fake), `test_trust_boundaries.py` (F-05, F-07), `test_secret_hygiene.py` (F-06), `test_audit_logger_wiring.py` (F-08). Run against unmodified code: **23 failures, each for the documented reason**; 7 controls passed. Two corrections from that run: F-06 API responses were already generic (kept as a control test); F-07 is worse than audited — a caller with no AuthContext could *confirm another user's pending action*. Failures were then marked `xfail(strict=True)` so the branch stayed green until each fix. Pinned `websockets==17.0.1`, `httpx==0.28.1` (both imported directly by `src/voice/` but previously only transitive). AWS state reviewed (§3 F-13): an idle ECS task and RDS instance remain; not internet-reachable; decision pending. |
| 2026-10-08 | Audit | Initial discovery & audit. Gates re-run green (924 passed, ruff/mypy clean). Six defects reproduced empirically (F-01, F-02, F-05, F-06, F-07, F-08); F-03/F-04 confirmed from code + Phase 26 config. AWS state not verified at audit time (connector needed re-auth). No code changed. |

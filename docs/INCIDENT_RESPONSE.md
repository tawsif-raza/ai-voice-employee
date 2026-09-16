# Incident Response & Alerting (Phase 22)

**Status:** First published Phase 22 (2026-09-16).
**Scope:** Diagnosing and responding to a production incident using this application's own observability data (metrics, audit events, distributed traces) — without needing to reproduce the failure locally. For *when to roll back* and the exact rollback steps, see `docs/ROLLBACK_PROCEDURE.md` (not duplicated here). For distributed tracing internals (span tree, exporter setup), see `docs/TRACING.md`.

## 1. What This Application Exposes

| Signal | Where | Auth | Contains PII/secrets? |
|---|---|---|---|
| Liveness | `GET /health` | None | No |
| Voice/provider readiness | `GET /health/voice` | None | No (booleans/counts only, never key values) |
| DB/dependency readiness | `GET /ready` | None | No |
| **Metrics snapshot** | `GET /metrics` (new this phase) | None | No — `MetricsRegistry.snapshot()` is aggregate-only by construction; no method on that class accepts a per-request/per-user label |
| Distributed traces | OTLP exporter, when `TRACING_ENABLED=true` (see `docs/TRACING.md`) | N/A (exporter-side) | No — every span attribute is metadata (IDs, names, booleans, latencies), never raw `user_input`/transcript text (verified in `PHASE_18_PRODUCTION_SECURITY_GATE_REPORT.md` §6) |
| Audit trail | `AuditLogger`/`AuditRepository.list_events()` — in-process (dev) or PostgreSQL-backed (`PERSISTENCE_MODE=production`); **not** exposed via any HTTP route | Direct DB/process access only | PII-filtered via `PrivacyService` before storage (Phase 6/11) |
| Structured logs | `privacy_logging.py`-routed loggers, stdout/your log aggregator | N/A | PII-redacted and log-injection-neutralized (Phase 11 F-01) |

`/metrics` did not exist before this phase — `MetricsRegistry.snapshot()` (`src/agent/metrics.py`) was already built "suitable for a metrics endpoint or periodic export" per its own docstring, but nothing consumed it externally. This phase closes exactly that gap: a JSON aggregate view any external scraper (Prometheus's `json_exporter`, a cron+curl, a custom collector) can poll. It deliberately does not pick a monitoring platform (plan.md: do not introduce one speculatively) — dashboards are built by pointing your platform of choice at this endpoint.

## 2. Alert Definitions

Thresholds below are starting points grounded in this codebase's own `docs/ROLLBACK_PROCEDURE.md` (5xx rate, P95 latency) and the actual metric names registered in `src/agent/metrics.py`, tuned for a low-volume canary rather than measured production traffic (no production traffic history exists yet — see `PHASE_21_PERFORMANCE_REPORT.md`). Revisit once real traffic volume exists.

| Alert | Condition (poll `/metrics`, compute the delta over the window) | Severity | First diagnostic step |
|---|---|---|---|
| High request failure rate | `requests_failed` / `requests_total` > 1% over 5 min | CRITICAL | Check `/health`, `/ready`; if `/ready` is 503, it's a dependency (DB/model) — see §4.2. If 200, inspect recent `TOOL_FAILED`/`POLICY_DENY` audit events (§3). |
| Elevated generation latency | `generation_latency_ms` p95 (`get_histogram()`'s `max`/`count`/`total` — compute p95 from repeated snapshots, or add a percentile histogram if this bucket-free average/max view proves insufficient) exceeds 2000ms, matching `docs/ROLLBACK_PROCEDURE.md`'s existing P95 criterion | CRITICAL | Check `llm_failover_events_total` — a rising count means the primary provider is degrading and Gemini/Groq fallback is absorbing load; check the configured provider's own status page. |
| LLM failover storm | `llm_failover_events_total` increasing faster than ~1/min sustained | WARNING | Primary provider (Claude/Gemini per `LLM_PROVIDER`) is failing open to secondary. Not yet an outage (failover is working as designed), but escalate if `llm_fallback_cooldown_triggered_total` also rises (quota exhaustion, not transient error). |
| Circuit breaker open | `circuit_breaker_open_total` > 0 in the window | WARNING | A dependency (RAG retriever or LLM) tripped its breaker after repeated failures (Phase 10). Check `dependency_failures_total` and `timeouts_total` for which one; breaker auto-recovers per `configs/reliability.yaml`'s `circuit_recovery_timeout_seconds`. |
| Repeated authentication failures | Any `REPEATED_AUTH_FAILURE` security event (`SecurityEventDetector`, threshold 3 per identifier — see `src/agent/audit.py`) | WARNING (escalate to CRITICAL if the rate is high across many identifiers, suggesting credential stuffing) | Query audit events by `actor` (§3) to see which identifier and how many attempts; this is the intended brute-force signal — see `PHASE_18_PRODUCTION_SECURITY_GATE_REPORT.md` §8 for its current coverage gap (telephony PIN path not yet wired to this detector). |
| Cross-user access attempt | Any `CROSS_USER_ACCESS_ATTEMPT` security event | CRITICAL (possible active attack, not a bug — `SessionManager`/`MemoryManager` already denied it; this alert is about *detecting the attempt*, not a failure to enforce) | Query by `actor`; correlate with recent deploys/API key rotations. |
| Voice call failure rate | `voice_calls_failed` / `voice_calls_total` > 5% over 15 min | CRITICAL | Check `voice_stt_reconnect_exhausted_total` (STT connection loss — Phase 16 §5 risk #2, still unresolved) and `voice_tts_synthesis_errors_total`. Cross-reference `docs/ROLLBACK_PROCEDURE.md`'s telephony-audio-corruption criterion. |
| STT reconnect exhaustion | `voice_stt_reconnect_exhausted_total` > 0 | CRITICAL | A live call's STT connection dropped and could not recover — the call is effectively deaf for its remaining duration (known, disclosed Phase 16 risk, not newly found). Check Deepgram's own status; consider rollback if sustained. |
| Idempotency duplicate spike | `idempotency_duplicates_total` rising unexpectedly | INFO/WARNING | Usually benign (a client retried a request that already succeeded — the safe, intended behavior). A sudden spike can indicate a client-side retry-storm bug upstream of this API. |

## 3. Diagnosing a Failed Call Without Reproducing It Locally

Given a `request_id` (returned in every API response and voice-turn log line) or a `session_id`:

1. **Metrics context** — `GET /metrics`, compare the snapshot against the alert table above to characterize the failure class (provider failover? circuit breaker? auth?).
2. **Audit trail** — `AuditLogger.list_events(request_id=..., session_id=...)` (direct process/DB access — this is intentionally not an HTTP route, since the audit trail can contain business-sensitive decision detail even after PII redaction). Returns the ordered sequence of real decisions for that turn: `TOOL_REQUESTED` → `TOOL_ALLOWED`/`TOOL_DENIED` → `TOOL_STARTED` → `TOOL_SUCCEEDED`/`TOOL_FAILED`, or `POLICY_DENY`/`SAFETY_HANDOFF` if the turn never reached tool execution. Each event's `reason` field states *why*, in the deterministic language `PolicyEngine`/`ToolOrchestrator` themselves produced — never inferred after the fact.
3. **Distributed trace** — if `TRACING_ENABLED=true` was set at the time (see `docs/TRACING.md` §1), the same `request_id`/`session_id` are span attributes (`app.request_id`, `app.session_id`) on `conversation.handle_turn` and every child span, viewable in Jaeger/your OTLP backend — shows exact per-stage latency (clinical check, intent classify, RAG retrieve, LLM generate, tool invoke) for that specific turn.
4. **Structured logs** — grep by `request_id`/`session_id` (both are included in every log line this application emits that has them available); safe to search directly since Phase 11's log-injection hardening guarantees a forged `user_input` cannot forge a fake log line around the real one.

This four-signal combination (metrics for *what class of problem*, audit for *what decision was made and why*, traces for *where the time went*, logs for *raw detail*) is what makes a failed call diagnosable without reproduction — each signal alone is insufficient (metrics have no per-call detail; traces don't exist unless enabled; audit has no latency breakdown; logs alone lack structured correlation across components).

## 4. Common Incident Playbooks

### 4.1 Elevated 5xx / failed requests

1. `GET /health` and `/ready` — distinguishes "app is up but a dependency isn't" (503 from `/ready`) from "app itself is failing" (500s from `/health` or `/generate`, `/ready` still 200).
2. If `/ready` is 503: check `docs/DATABASE.md`'s troubleshooting section (only relevant when `PERSISTENCE_MODE=production`; `dev` mode has no external DB dependency, matching Phase 16 §5's disclosed scope).
3. If `/ready` is 200 but requests are failing: pull 5-10 recent failing `request_id`s from logs, run §3's diagnosis on each — look for a shared audit `reason` (systemic) vs. scattered causes (noisy but not systemic).
4. Rollback criteria and steps: `docs/ROLLBACK_PROCEDURE.md`.

### 4.2 Provider degradation (Claude/Gemini/Groq)

1. `llm_failover_events_total` and `llm_fallback_cooldown_triggered_total` from `/metrics` establish whether this is transient-error failover (working as designed) or quota exhaustion (`trigger_cooldown()` path — see `docs/phase1.4-external-integration-report.md` §1's disclosed narrowing: this specific path has real-provider test coverage still open).
2. Check the configured provider's own status page.
3. `LLM_PROVIDER=free_fallback`/`fallback` already fails over automatically (Rule 12/`docs/FREE_TIER_SETUP.md`) — no manual action needed unless every configured provider is degraded simultaneously, at which point `local` (fully offline Qwen model) is the last-resort fallback if configured.

### 4.3 Security event triggered

1. Query audit events by the reported `actor`/`resource` (§3).
2. `REPEATED_AUTH_FAILURE`/`CROSS_USER_ACCESS_ATTEMPT`/`POLICY_BYPASS_ATTEMPT`/`UNKNOWN_TOOL_REQUEST`/`MALFORMED_ACTION_PROPOSAL`/`REPEATED_AUTHORIZATION_DENIAL` are the exact six types `SecurityEventDetector` emits (`src/agent/audit.py`) — every one represents an attempt that was **already denied** by the real gate (PolicyEngine/AuthenticationProvider/SessionManager); this playbook is about assessing attack pattern and scope, not confirming whether the system is exploitable (it wasn't, in the specific attempt that triggered the alert — see `docs/SECURITY.md` for the underlying invariants).
3. Escalate per your organization's security incident process if the pattern suggests active, sustained attack (high volume, many identifiers, same source).

## 5. Known Limitations

- No real production traffic history exists yet (Phase 17/19 remain blocked on live infrastructure — see plan.md) — the thresholds in §2 are informed starting points, not empirically tuned values. Revisit once real traffic exists.
- `/metrics` is a JSON snapshot polled on demand, not a push-based alerting pipeline — actually firing the alerts in §2 requires external tooling (a scraper + an alerting rule engine) this repository does not include, consistent with "do not introduce a monitoring platform speculatively."
- `generation_latency_ms`'s histogram (`src/agent/metrics.py::_Histogram`) tracks count/total/min/max/average only — no percentile buckets. The p95 latency alert in §2 needs either repeated snapshot sampling to approximate a percentile, or a follow-up enhancement to `_Histogram` (not implemented this phase — a genuine architecture change beyond "expose what already exists," out of scope per plan.md Rule 5/8).
- `AuditLogger.list_events()` remains intentionally unexposed via HTTP (§3) — incident diagnosis using it requires direct process/DB access (a REPL, a DB query, or a future internal-only admin endpoint — not built here, same reasoning as the histogram limitation above).
- Dashboards themselves (visual, not just the data source) require picking and operating an external platform — genuinely out of this repository's scope, consistent with metrics.py's own stated design decision not to speculatively integrate one.

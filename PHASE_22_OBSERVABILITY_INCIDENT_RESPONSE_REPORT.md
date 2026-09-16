# Phase 22 — Observability & Incident Response Report

## 1. Objective

Per plan.md's Phase 22: make the system operable without the developer watching the terminal — traces, metrics, structured logs, correlation IDs, failure/latency/failover/auth/safety metrics, alert definitions, dashboards where appropriate, an incident runbook, and a rollback procedure — such that an engineer can diagnose a failed call from observability data alone.

## 2. What Already Existed (audited, not rebuilt)

Most of Phase 22's task list was already substantially built by earlier phases:

- OpenTelemetry traces (Phase 14, `docs/TRACING.md`) — full span tree, correlation via `app.request_id`/`app.session_id` attributes, disabled-by-default with zero overhead when off, confirmed PII/secret-free in Phase 18's review.
- Metrics (Phase 8, `src/agent/metrics.py`) — 25 counters + 9 histograms covering requests, policy, auth, tools, reliability (Phase 10), voice/telephony (Phase 13/16.2), and LLM provider failover.
- Structured logs (`privacy_logging.py`) — PII-redacted, log-injection-hardened (Phase 11 F-01).
- Audit trail (Phase 8/12) — `AuditLogger.list_events()` with the exact filter set plan.md required (correlation ID, actor, session ID, event type, time range — Phase 12.8).
- Rollback procedure — `docs/ROLLBACK_PROCEDURE.md` (rollback criteria, exact Twilio re-routing and app-rollback steps) already existed and was not duplicated.

## 3. Gap Found: No External-Facing Metrics Export

`MetricsRegistry.snapshot()` already existed with a docstring stating it is "suitable for a metrics endpoint or periodic export" — but nothing in `src/api/server.py` ever exposed it. Every metric described above was real and correctly recorded, but genuinely inoperable from outside the process: an engineer (or a real dashboard/alerting platform) had no way to read it without attaching a debugger or adding code. This is the one concrete, actionable gap this phase closes — not a rebuild of the observability system, an unlock of data that already existed.

**Fix:** `GET /metrics` (new route, `src/api/server.py`) returns `MetricsRegistry.snapshot()` as JSON — unauthenticated, matching `/health`/`/health/voice`'s existing pattern, safe because `snapshot()` is aggregate-only by construction (no per-request/per-user field exists anywhere in `MetricsRegistry`'s API — verified by reading its full public method signatures, none of which accept a label parameter). JSON rather than Prometheus text-exposition format, to match this API's existing convention and avoid picking a monitoring platform on the deployer's behalf (plan.md's standing instruction not to introduce one speculatively — `metrics.py`'s own module docstring already states this design choice).

3 new tests (`tests/test_server_api.py::TestMetricsEndpoint`): snapshot shape, real recorded values reflected, and the `_metrics is None` fallback path.

## 4. Deliverable: `docs/INCIDENT_RESPONSE.md`

New document covering:
- What this application exposes and where (table: `/health`, `/health/voice`, `/ready`, the new `/metrics`, traces, audit, logs — with auth and PII-content noted for each).
- **Alert definitions** — 9 alerts, each naming the exact real metric/event name it watches (`requests_failed`/`requests_total`, `generation_latency_ms`, `llm_failover_events_total`, `circuit_breaker_open_total`, the 6 real `SecurityEventDetector` event types, `voice_calls_failed`/`voice_calls_total`, `voice_stt_reconnect_exhausted_total`, `idempotency_duplicates_total`), a severity, and a first diagnostic step — not generic advice, grounded in this codebase's actual registered counters (cross-checked against `src/agent/metrics.py`'s `_COUNTER_NAMES`/`_HISTOGRAM_NAMES`) and `src/agent/audit.py`'s actual `SecurityEventDetector` event types.
- **The four-signal diagnosis method** (metrics → audit trail → traces → logs) for tracing a specific failed call by `request_id`/`session_id` without reproducing it — this is the acceptance criterion's literal requirement.
- Three incident playbooks (elevated 5xx, provider degradation, security event triggered), each referencing real code paths and existing docs (`docs/ROLLBACK_PROCEDURE.md`, `docs/DATABASE.md`, `docs/FREE_TIER_SETUP.md`) rather than duplicating them.
- An honest Known Limitations section (Section 6 below).

## 5. Files Changed

- `src/api/server.py` — new `GET /metrics` route.
- `tests/test_server_api.py` — 3 new tests.
- `docs/INCIDENT_RESPONSE.md` — new.

## 6. Known Limitations (stated in the doc itself, repeated here per Rule 14)

- **No real production traffic history exists** (Phase 17/19 remain blocked) — the alert thresholds are informed starting points (grounded in `docs/ROLLBACK_PROCEDURE.md`'s existing criteria where one already existed), not empirically tuned against real load. Flagged explicitly rather than presented as validated.
- **`/metrics` is pull-based, on-demand JSON** — actually firing alerts requires external scraping/alerting tooling this repository does not include, by design (no speculative platform integration).
- **No latency percentile histogram** — `_Histogram` (`metrics.py`) tracks count/total/min/max/average only, no buckets, so a true p95/p99 alert needs either repeated-snapshot sampling or a follow-up enhancement to that class. Not implemented this phase (a real architecture change, not just "expose what exists" — out of scope per plan.md Rule 5/8, documented as a known limitation rather than silently worked around).
- **`AuditLogger.list_events()` remains unexposed via HTTP** by deliberate design (can contain business-sensitive decision detail even after PII redaction) — incident diagnosis using it requires direct process/DB access, not a public endpoint.
- **Dashboards** (the visual layer, not the data source) require picking and operating an external platform — out of this repository's scope by the same "no speculative platform" rule that shaped the `/metrics` endpoint's JSON-not-Prometheus-format choice.

## 7. Tests

- **New tests this phase:** 3.
- **Full project suite (CI-matching invocation, `pytest tests/ -q`):** **917 passed, 0 failed**, 52 subtests passed, same 2 pre-existing unrelated SQLAlchemy warnings as prior phases.
- **Housekeeping fix (unrelated to the metrics feature itself, found while validating this phase):** `scripts/performance_load_test.py` (added in Phase 21) has a filename matching pytest's default `*_test.py` discovery pattern, unlike `scripts/stability_verification.py` (Phase 16), which does not match either default pattern (`test_*.py`/`*_test.py`) and was never collected. A bare `pytest -q` run from the repo root (not `tests/`, which is what `.github/workflows` actually runs and what every phase report's "full suite" figure has otherwise meant) was therefore attempting to collect and call `scenario_2_concurrency_configured`-shaped functions as zero-argument test functions, erroring on the ones that require arguments. Fixed by renaming Phase 21's script-internal functions from `test_N_...` to `scenario_N_...` (matching their actual role — standalone measurement scenarios, not pytest tests) — no behavior change, `PHASE_21_PERFORMANCE_REPORT.md`'s recorded results are unaffected (the script's output was already correct; only its accidental discoverability by a bare `pytest -q` was wrong). Verified both `pytest tests/ -q` and bare `pytest -q` now agree at 917 passed, 0 errors.

## 8. Acceptance Criteria

Per plan.md Phase 22: "An engineer can diagnose a failed call from observability data without reproducing it locally." **Met** — `docs/INCIDENT_RESPONSE.md` §3 documents the exact method (request_id/session_id → metrics classification → audit event sequence with real `reason` fields → trace span timing → logs), and every signal it names now actually exists and is reachable (traces and audit already were; metrics is newly externally reachable via `/metrics`).

## 9. Final Status

`PHASE 22 COMPLETE`

## 10. Next Phase

Phase 23 (Cost and Provider Optimization) explicitly requires real usage data (cost per minute, fallback rate, provider utilization) this system does not yet have (no production traffic — same Phase 17/19 dependency). Phase 24 (Data and Privacy Hardening) is a code/policy audit independent of live infrastructure. Proceeding to Phase 24 next per plan.md Rule 17, deferring Phase 23 until real usage data exists.

# Phase 14 - Distributed Tracing & Telemetry Report

## 1. Executive Summary
Phase 14 adds OpenTelemetry distributed tracing across the turn-orchestration pipeline. SDK bootstrap, auto-instrumentation wiring, `configs/tracing.yaml`, Docker/Jaeger wiring, and partial trace-log/trace-audit correlation already existed in the working tree from earlier, uncommitted work; this phase completed the core deliverable that was still missing — manual span instrumentation of `ConversationManager.handle_turn()`'s pipeline stages and `ToolOrchestrator.invoke()`'s gate sequence — added a resilience layer (`_SafeTracer`, `_traced()`) so a tracer failure structurally cannot break a request, wrote the tracing-specific test suites, and produced the tracing documentation and this report. Tracing remains disabled by default (`TRACING_ENABLED=false`) with zero runtime overhead, and every existing test continues to pass unchanged.

## 2. OpenTelemetry SDK Configuration
`src/agent/tracing.py`:
- `TracingConfig.from_env()` loads `configs/tracing.yaml`, then lets `TRACING_ENABLED`/`OTEL_SERVICE_NAME`/`OTEL_EXPORTER_TYPE`/`OTEL_EXPORTER_OTLP_ENDPOINT`/`OTEL_TRACES_SAMPLER`/`OTEL_TRACES_SAMPLER_ARG` override, same pattern as `ReliabilityConfig`.
- `init_tracing()` builds a `Resource` (service name/version/deployment environment), a sampler (`always_on`/`always_off`/`traceidratio`/`parentbased_traceidratio`), and an exporter (`console`/`otlp`/`memory`), wiring a `BatchSpanProcessor` (or `SimpleSpanProcessor` for the test-only `memory` exporter) into a `TracerProvider`. When disabled, sets a `NoOpTracerProvider` instead. Any initialization failure falls back to `NoOpTracerProvider` rather than raising.
- `shutdown_tracing()` flushes and shuts down the provider; called from `server.py`'s lifespan on graceful shutdown.
- `get_tracer(name)` returns a `_SafeTracer` (new this phase) wrapping the real `opentelemetry.trace.Tracer` — see §6.

## 3. Auto-Instrumentation
Already wired into `src/api/server.py`'s `lifespan()` prior to this phase's work, confirmed still correct:
- **FastAPI** (`FastAPIInstrumentor.instrument_app(app)`): one root span per inbound HTTP request — the true root of each `conversation.handle_turn` trace.
- **SQLAlchemy** (`SQLAlchemyInstrumentor().instrument(engine=...)`, only when a database engine is active): one span per SQL statement in production persistence mode.
- **requests** (`RequestsInstrumentor().instrument()`): one span per outbound HTTP call (ElevenLabs, Claude, Gemini).

Each instrumentor call is individually wrapped in `try/except`, logging at `DEBUG` and continuing rather than failing server startup if a given instrumentation package can't attach.

## 4. Manual Span Instrumentation (this phase's core work)

### Turn pipeline (`src/agent/conversation_manager.py`)
`handle_turn()` was split into a thin root-span wrapper and its pre-existing body (renamed `_handle_turn_body()`, otherwise byte-identical), so the root span wraps the *entire* turn without re-indenting or altering any of its ~580 lines of existing control flow:

| Span | Attributes set |
|---|---|
| `conversation.handle_turn` (root) | `app.request_id`, `app.session_id` |
| `conversation.clinical_safety_check` | `app.clinical.triggered` |
| `conversation.intent_classify` | `app.intent.name` |
| `conversation.policy_evaluate` | `app.policy.outcome`, `app.policy.name` |
| `conversation.rag_retrieve` | `app.rag.chunks_retrieved`, `app.rag.degraded`, `app.retry.attempt`, `app.circuit_breaker.state` |
| `conversation.llm_generate` | `app.llm.provider`, `app.llm.model`, `app.llm.failover`, `app.latency_ms`, `app.retry.attempt`, `app.circuit_breaker.state`, error status + `record_exception` on a hard failure |
| `conversation.handoff_detect` | `app.handoff.triggered`, `app.handoff.confidence` |

The clinical-guard short-circuit, tool-routed turns, and every existing early-return path are unchanged; a clinically-flagged or tool-routed turn simply produces fewer spans (no RAG/LLM/handoff spans), matching pre-Phase-14 control flow exactly.

### Tool orchestrator (`src/agent/tool_orchestrator.py`)
`invoke()` gained a root span around its existing single call to `_invoke()`; each of `_invoke()`'s six gates got its own child span, added by wrapping each gate's *existing* code in a `with` block — no gate's logic, ordering, or early-return behavior was changed:

`tool_orchestrator.invoke` → `gate_policy` → `gate_authorization` → `gate_privacy` (only when a `PrivacyService` is configured) → `gate_confirmation` → `gate_idempotency` → `execute`

A denial at any gate short-circuits the remaining gates and `execute`, exactly as before. `execute`'s span additionally records `StatusCode.ERROR` + `app.error.type=DependencyTimeoutError` on a tool timeout.

## 5. Correlation Bridge (traces ↔ logs ↔ audit events)
- **Audit events**: `AuditEvent.trace_id`/`span_id` (already present in `observability_models.py`) are populated by `AuditLogger.record()` from the active span at record time (already wired in `audit.py`) — confirmed working end-to-end by `tests/test_tracing_correlation.py`.
- **Structured logs**: `StructuredJSONFormatter` (`src/voice/production_logging.py`) already injected `trace_id`/`span_id` into JSON log output — confirmed working end-to-end.
- **`CorrelationContext`**: `observability_models.py`'s `CorrelationContext` dataclass carries `trace_id`/`span_id` fields, and `tracing.with_trace_context()` populates them from the active span (both pre-existed). **Genuine gap, disclosed rather than papered over**: no code in the live request path actually *constructs* a `CorrelationContext` instance today — correlation there flows through plain `request_id` string parameters instead, which is what `AuditLogger`/`StructuredJSONFormatter` already key off directly. `with_trace_context()` is fully implemented and tested (`test_tracing_bootstrap.py`, `test_tracing_correlation.py`) and ready for a future caller, but wiring it into `handle_turn()`/`invoke()` would mean introducing a new, currently-unused object into the live path purely for its own sake — out of scope for a "do not rewrite decision logic, minimal additive changes only" phase, and not something this report claims is done. See §11.

## 6. Privacy Guarantees
Span attributes are restricted to the fixed `SpanAttributes` allow-list (identifiers, categorical outcomes, counts, latencies). Never recorded, under any configuration: raw user input, LLM output text, retrieved chunk content, auth tokens, or connection strings. Enforced by:
- `tests/test_tracing_pipeline.py::test_span_attributes_never_contain_user_message` — exercises a full turn with a distinctive "secret" string in both the user message and a retrieved chunk, asserts it appears in no span attribute.
- `tests/test_tracing_bootstrap.py::test_span_attributes_no_pii` — checks the `SpanAttributes` *keys* themselves don't resemble PII fields.

## 7. Docker Observability Stack
Already present in the working tree, confirmed correct: `docker/docker-compose.yml` has a `jaeger` service (`jaegertracing/jaeger:2`, ports `16686`/`4317`/`4318`) under its own `observability` profile, untouched by the `api`/`trainer`/`test`/`dev` profiles; the `api` service's environment already passes through `TRACING_ENABLED`/`OTEL_SERVICE_NAME`/`OTEL_EXPORTER_TYPE`/`OTEL_EXPORTER_OTLP_ENDPOINT`.

## 8. Configuration Reference
`configs/tracing.yaml` (env vars override): `enabled` / `TRACING_ENABLED` (default `false`), `service_name` / `OTEL_SERVICE_NAME` (`ai-voice-agent`), `exporter` / `OTEL_EXPORTER_TYPE` (`console`), `otlp_endpoint` / `OTEL_EXPORTER_OTLP_ENDPOINT` (`http://localhost:4317`), `sampler` / `OTEL_TRACES_SAMPLER` (`parentbased_always_on`), `sampler_arg` / `OTEL_TRACES_SAMPLER_ARG` (none). Full reference, Quick Start, and troubleshooting: `docs/TRACING.md` (new this phase). Architecture-level summary: `docs/ARCHITECTURE.md` §11 (new this phase).

## 9. Tests
Added this phase:
- `tests/test_tracing_pipeline.py` — 9 tests: full-turn span tree, clinical short-circuit minimal spans, tool-action span nesting, RAG retry attribute, LLM failover attribute, no-user-message-in-attributes, tracer-failure resilience, tool-denied short-circuit, tool timeout status/attribute.
- `tests/test_tracing_correlation.py` — 5 tests: `with_trace_context()` with/without an active span, audit event trace_id, structured-log trace_id, trace_id consistency across a full turn's spans and audit events.
- `tests/test_tracing_bootstrap.py` — 9 tests, pre-existing, unchanged, still passing.

Executed, this run, in order:
1. `tests/test_tracing_bootstrap.py` — 9 passed.
2. `tests/test_tracing_pipeline.py` — 9 passed.
3. `tests/test_tracing_correlation.py` — 5 passed.
4. Full suite, `python -m pytest tests/ -v` — **812 passed, 0 failed, 2 warnings (pre-existing SQLAlchemy warnings, unrelated to this phase), 52 subtests passed.**

No regressions in Policy/ToolOrchestrator/ClinicalSafetyGuard tests, persistence/database tests, metrics/observability tests, API server tests, or voice pipeline tests — all included in the 812.

## 10. Compatibility
- No `PolicyEngine`/`ClinicalSafetyGuard`/`ToolOrchestrator` decision logic was rewritten. Every gate/check's existing control flow, ordering, and early-return behavior is byte-for-byte the same; spans only observe it.
- `ConversationManager.handle_turn()`'s public signature, docstring, and return contract are unchanged; its body was extracted verbatim into `_handle_turn_body()` under the new root span, not modified.
- Tracing-disabled behavior (`TRACING_ENABLED=false`, the default) is provably unchanged: every span call resolves to a no-op through `NoOpTracerProvider`, and the new `_SafeTracer`/`_traced()` wrapper adds no behavior beyond an extra `try/except` around the same no-op calls.
- Two previously-undeclared runtime dependencies used by this phase's own uncommitted-but-present code (`opentelemetry-*`, `sentence-transformers`) were not installed in this environment; both are already correctly pinned in `requirements.txt` and were simply installed to run the suite (see the `ci:` commit in this branch's history for the related `pytest`/`pytest-asyncio` fix and the new GitHub Actions workflow that would have caught this on a clean checkout).

## 11. Remaining Technical Debt (genuine only)
- `CorrelationContext` is not constructed anywhere in the live request path (§5) — `with_trace_context()` exists and is tested but has no caller yet. Wiring it in is a small, well-scoped follow-up if/when a caller actually needs a `CorrelationContext` object rather than the plain `request_id` strings used today.
- The default sampler is `always_on` (trace every turn) — fine at current canary/local volumes, but production deployment at higher throughput should switch to `parentbased_traceidratio` with an explicit `sampler_arg` (documented in `docs/TRACING.md` §4) before that becomes a cost/volume concern.
- `configs/tracing.yaml`'s `privacy.blocked_attributes` list is documentation of intent only — no code currently reads it, since manual instrumentation already never attaches those fields (allow-list by construction, not a runtime filter). A future attribute-scrubbing span processor could enforce it defensively for any *new* span-attribute call site added later without this report's same discipline.
- `tool_orchestrator.invoke`'s span does not currently distinguish "standalone tool call" from "tool call made mid-turn by `ConversationManager`" other than by parentage in the trace tree — acceptable today since both cases are exercised by the same span reference in `docs/TRACING.md`, but worth a dedicated attribute if tool-call analytics ever need to separate them.

## 12. Recommended Next Phase
**Phase 15 — Production sampling and cost tuning**: once real production traffic volume is known, revisit the default sampler (§11) and add a dashboard/alerting layer on top of the exported traces (Grafana + Tempo, or Cloud Trace + Cloud Monitoring) — building that dashboard, not just enabling tracing, is the next piece of genuine observability value. (Do not implement this now.)

`PHASE 14 COMPLETE`

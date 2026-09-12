# Distributed Tracing (Phase 14)

OpenTelemetry distributed tracing across the turn-orchestration pipeline: `src/agent/tracing.py`, integrated into `src/api/server.py` (bootstrap + auto-instrumentation), `src/agent/conversation_manager.py` (turn-pipeline spans), and `src/agent/tool_orchestrator.py` (gate-sequence spans). See `docs/ARCHITECTURE.md` §11 for the span-tree diagram and `PHASE_14_TELEMETRY_TRACING_REPORT.md` for the phase's full report.

Tracing is **disabled by default** and adds zero runtime overhead when off — every `tracer.start_as_current_span()` call becomes a no-op until `TRACING_ENABLED=true` is set.

## 1. Quick Start (local, with Jaeger)

```bash
# 1. Start Jaeger (all-in-one) via the observability profile
docker compose -f docker/docker-compose.yml --profile observability up -d jaeger

# 2. Point the API at it and enable tracing
export TRACING_ENABLED=true
export OTEL_EXPORTER_TYPE=otlp
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317

# 3. Run the API server as usual
python src/api/server.py

# 4. Send a few /generate requests, then open the Jaeger UI
#    http://localhost:16686 -- select service "ai-voice-agent"
```

For a quick local check without Jaeger, set `OTEL_EXPORTER_TYPE=console` instead — spans print to stdout.

## 2. Span Reference

| Span | Parent | Set by | Key attributes |
|---|---|---|---|
| `conversation.handle_turn` | (root) | `conversation_manager.py` | `app.request_id`, `app.session_id` |
| `conversation.clinical_safety_check` | `handle_turn` | `conversation_manager.py` | `app.clinical.triggered` |
| `conversation.intent_classify` | `handle_turn` | `conversation_manager.py` | `app.intent.name` |
| `conversation.policy_evaluate` | `handle_turn` | `conversation_manager.py` | `app.policy.outcome`, `app.policy.name` |
| `conversation.rag_retrieve` | `handle_turn` | `conversation_manager.py` | `app.rag.chunks_retrieved`, `app.rag.degraded`, `app.retry.attempt`, `app.circuit_breaker.state` |
| `conversation.llm_generate` | `handle_turn` | `conversation_manager.py` | `app.llm.provider`, `app.llm.model`, `app.llm.failover`, `app.latency_ms`, `app.retry.attempt`, `app.circuit_breaker.state` |
| `conversation.handoff_detect` | `handle_turn` | `conversation_manager.py` | `app.handoff.triggered`, `app.handoff.confidence` |
| `tool_orchestrator.invoke` | `handle_turn` (on a tool-routed turn) or standalone | `tool_orchestrator.py` | `app.tool.name`, `app.tool.action`, `app.request_id`, `app.tool.outcome` |
| `tool_orchestrator.gate_policy` | `invoke` | `tool_orchestrator.py` | `app.policy.outcome` |
| `tool_orchestrator.gate_authorization` | `invoke` | `tool_orchestrator.py` | `app.policy.outcome` |
| `tool_orchestrator.gate_privacy` | `invoke` | `tool_orchestrator.py` | `app.policy.outcome` (only when a `PrivacyService` is configured) |
| `tool_orchestrator.gate_confirmation` | `invoke` | `tool_orchestrator.py` | `app.policy.outcome` |
| `tool_orchestrator.gate_idempotency` | `invoke` | `tool_orchestrator.py` | `app.policy.outcome` |
| `tool_orchestrator.execute` | `invoke` | `tool_orchestrator.py` | `app.tool.outcome`, `app.latency_ms`, `app.error.type` (on timeout) |

Any gate's denial short-circuits the remaining gates and `execute` — the same control flow the spans observe, unchanged by Phase 14 (see `tests/test_tracing_pipeline.py::TestToolDeniedShortCircuit`).

Auto-instrumentation (enabled automatically alongside the manual spans above, via `opentelemetry-instrumentation-{fastapi,sqlalchemy,requests}`) additionally produces:

* One span per inbound HTTP request (FastAPI), as the true root of `conversation.handle_turn`'s trace.
* One span per SQL statement, when `PERSISTENCE_MODE=production` (SQLAlchemy).
* One span per outbound HTTP call, e.g. to ElevenLabs/Claude/Gemini (`requests`).

## 3. Configuration

`configs/tracing.yaml`, overridable by environment variables (env always wins — same pattern as `configs/reliability.yaml`):

| YAML key | Environment variable | Default | Notes |
|---|---|---|---|
| `tracing.enabled` | `TRACING_ENABLED` | `false` | Master switch. |
| `tracing.service_name` | `OTEL_SERVICE_NAME` | `ai-voice-agent` | Shown in Jaeger/Tempo/Cloud Trace as the service. |
| `tracing.exporter` | `OTEL_EXPORTER_TYPE` | `console` | `console` \| `otlp` \| `memory` (`memory` is test-only — see §5 of `PHASE_14_TELEMETRY_TRACING_REPORT.md`). |
| `tracing.otlp_endpoint` | `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4317` | gRPC OTLP endpoint (Jaeger, Tempo, Cloud Trace gateway, ...). |
| `tracing.sampler` | `OTEL_TRACES_SAMPLER` | `parentbased_always_on` | `always_on` \| `always_off` \| `traceidratio` \| `parentbased_traceidratio`. |
| `tracing.sampler_arg` | `OTEL_TRACES_SAMPLER_ARG` | (none) | Sampling ratio (0.0-1.0) for the two `traceidratio` samplers. |

`docker/docker-compose.yml`'s `api` service already wires `TRACING_ENABLED`/`OTEL_SERVICE_NAME`/`OTEL_EXPORTER_TYPE`/`OTEL_EXPORTER_OTLP_ENDPOINT` through from the host environment; `.env.example` documents the same variables for a non-Docker run.

## 4. Production Deployment

Point `OTEL_EXPORTER_OTLP_ENDPOINT` at any OTLP-gRPC-compatible collector:

* **Grafana Tempo**: `OTEL_EXPORTER_OTLP_ENDPOINT=http://<tempo-host>:4317`
* **Google Cloud Trace**: run the [OpenTelemetry Collector](https://cloud.google.com/trace/docs/setup/opentelemetry) as a sidecar/agent exporting to Cloud Trace, and point this service at that collector's OTLP endpoint — this codebase does not call the Cloud Trace API directly.
* **Self-hosted Jaeger**: `OTEL_EXPORTER_OTLP_ENDPOINT=http://<jaeger-host>:4317` (Jaeger's own OTLP receiver, same as the local Quick Start above).

Start with a `traceidratio`/`parentbased_traceidratio` sampler and a `sampler_arg` well below `1.0` under real production traffic volume — `always_on` (the default) traces every single turn, which is appropriate for the local/canary volumes this system currently runs at (see `docs/CANARY_DEPLOYMENT.md`) but not necessarily at higher scale.

## 5. Privacy

Span attributes are restricted to a fixed allow-list (`tracing.SpanAttributes`): identifiers (`request_id`, `session_id`, `conversation_id`, `turn_id`), categorical outcomes (intent name, policy outcome, tool name/action/outcome, circuit-breaker state), counts (chunks retrieved, retry attempt), and latencies. The following are **never** recorded as span attributes, under any configuration:

* The user's raw message text.
* The LLM's generated response text.
* Retrieved knowledge-chunk content (only chunk *count* is recorded).
* Authentication tokens, API keys, or database connection strings.

`tests/test_tracing_pipeline.py::test_span_attributes_never_contain_user_message` enforces this for a full turn, and `tests/test_tracing_bootstrap.py::test_span_attributes_no_pii` checks the attribute *keys* themselves don't resemble PII fields. `configs/tracing.yaml`'s `privacy.blocked_attributes` list documents the same intent for a future attribute-scrubbing processor, should one be added — no code currently reads that list, since the manual instrumentation simply never attaches those fields in the first place (allow-list, not a runtime filter).

## 6. Troubleshooting

**No spans appearing in Jaeger / console:**
1. Confirm `TRACING_ENABLED=true` is actually set in the process's environment (not just the shell that started `docker compose`) — check `/ready` or process startup logs for `"Tracing enabled: service=... exporter=..."` from `tracing.py`.
2. For the `otlp` exporter, confirm the target endpoint is reachable from the API container/process (`OTEL_EXPORTER_OTLP_ENDPOINT`) — a Jaeger started via a different Docker Compose profile than the API container may not share a network.
3. A tracer failure never raises (see §7 below) but does log at `DEBUG` under the `ai_voice_agent.tracing` logger — raise the log level to confirm whether spans are silently failing to export vs. never being created.

**Spans appear but attributes are missing:**
Every attribute-set call is individually wrapped in a swallowed `try/except` (fire-and-forget, per §7) — a genuinely missing attribute (e.g. `app.llm.provider` on a local, non-fallback LLM run) usually means the underlying data legitimately isn't available for that code path (e.g. `src/inference/llm_service.py`'s local model path doesn't report a `provider`/`model` in its final chunk), not a bug — see the Span Reference table's "only when configured" notes.

**Exporter timeouts / slow shutdown:**
The `otlp` exporter uses a `BatchSpanProcessor` (batched, async export) in every non-test configuration; `shutdown_tracing()` flushes pending spans on process exit. A collector that's unreachable at shutdown can add a few seconds of delay to graceful shutdown — this is bounded by the SDK's own internal export timeout, not configurable separately here.

## 7. Tracing failures never break a request

Every span-creation call site goes through a resilient wrapper (`_traced()` in `conversation_manager.py`/`tool_orchestrator.py`, backed by `tracing.py`'s `_SafeTracer`) that swallows *any* failure from the tracer itself — SDK internals misbehaving, an unreachable exporter, or the tracer object being replaced entirely — and falls back to a real, inert `opentelemetry.trace.INVALID_SPAN` rather than propagating an exception. `tests/test_tracing_pipeline.py::test_tracing_error_does_not_break_turn` proves this by monkeypatching the tracer to always raise and confirming a full turn still completes normally.

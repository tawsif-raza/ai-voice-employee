"""
OpenTelemetry distributed tracing bootstrap (Phase 14; plan.md Steps 14.1, 14.6).

Tracing is strictly observational — it MUST NOT gate any safety, policy, or
business-logic decision.  A tracing failure (SDK error, exporter timeout, span
creation failure) MUST NOT cause a request to fail, degrade, or change behavior.
All tracing calls are fire-and-forget: errors are logged and swallowed.

Privacy discipline: no PII, user messages, LLM outputs, credentials, tokens,
or connection strings may appear in span attributes or resource attributes.
The ``SpanAttributes`` constants class enforces a strict allow-list.

Disabled by default (``TRACING_ENABLED=false``).  When disabled, the global
``TracerProvider`` is set to ``NoOpTracerProvider`` and every
``tracer.start_as_current_span()`` call becomes a zero-cost no-op.
"""

import contextlib
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)
from opentelemetry.trace import NoOpTracerProvider, Tracer

logger = logging.getLogger("ai_voice_agent.tracing")

_CONFIG_PATH = Path(__file__).resolve().parents[2] / "configs" / "tracing.yaml"

# Module-level reference to the InMemorySpanExporter when using the "memory"
# exporter — tests read this to assert on captured spans.
_memory_exporter: Optional[object] = None


# ---------------------------------------------------------------------------
# Span attribute constants — the ONLY keys allowed on manually-created spans.
# Deliberately excludes user_id, user_message, llm_response, auth_token,
# database_url, and all PII-bearing fields.
# ---------------------------------------------------------------------------

class SpanAttributes:
    """Named constants for span attribute keys (Phase 14 allow-list)."""

    REQUEST_ID = "app.request_id"
    SESSION_ID = "app.session_id"
    CONVERSATION_ID = "app.conversation_id"
    TURN_ID = "app.turn_id"
    INTENT_NAME = "app.intent.name"
    POLICY_OUTCOME = "app.policy.outcome"
    POLICY_NAME = "app.policy.name"
    TOOL_NAME = "app.tool.name"
    TOOL_ACTION = "app.tool.action"
    TOOL_OUTCOME = "app.tool.outcome"
    RAG_CHUNKS_RETRIEVED = "app.rag.chunks_retrieved"
    RAG_DEGRADED = "app.rag.degraded"
    LLM_PROVIDER = "app.llm.provider"
    LLM_MODEL = "app.llm.model"
    LLM_FAILOVER = "app.llm.failover"
    LLM_TOKENS_GENERATED = "app.llm.tokens_generated"
    HANDOFF_TRIGGERED = "app.handoff.triggered"
    HANDOFF_CONFIDENCE = "app.handoff.confidence"
    CLINICAL_TRIGGERED = "app.clinical.triggered"
    CIRCUIT_BREAKER_STATE = "app.circuit_breaker.state"
    RETRY_ATTEMPT = "app.retry.attempt"
    ERROR_TYPE = "app.error.type"
    LATENCY_MS = "app.latency_ms"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TracingConfig:
    """
    Tracing configuration loaded from ``configs/tracing.yaml`` with
    environment-variable overrides (env vars always win).
    """

    enabled: bool = False
    service_name: str = "ai-voice-agent"
    exporter_type: str = "console"       # "console" | "otlp" | "memory"
    otlp_endpoint: str = "http://localhost:4317"
    sampler: str = "parentbased_always_on"
    sampler_arg: Optional[float] = None

    @classmethod
    def from_env(cls, config_path: Optional[str] = None) -> "TracingConfig":
        """Load from YAML, then let environment variables override."""
        path = Path(config_path) if config_path else _CONFIG_PATH
        yaml_data: dict = {}
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw = yaml.safe_load(f) or {}
                yaml_data = raw.get("tracing", {}) or {}
            except (OSError, yaml.YAMLError):
                yaml_data = {}

        def _env(key: str, yaml_key: str, default):
            env_val = os.environ.get(key)
            if env_val is not None:
                return env_val
            return yaml_data.get(yaml_key, default)

        enabled_raw = _env("TRACING_ENABLED", "enabled", False)
        if isinstance(enabled_raw, str):
            enabled = enabled_raw.strip().lower() in ("true", "1", "yes")
        else:
            enabled = bool(enabled_raw)

        sampler_arg_raw = _env("OTEL_TRACES_SAMPLER_ARG", "sampler_arg", None)
        sampler_arg = float(sampler_arg_raw) if sampler_arg_raw is not None else None

        return cls(
            enabled=enabled,
            service_name=str(_env("OTEL_SERVICE_NAME", "service_name", "ai-voice-agent")),
            exporter_type=str(_env("OTEL_EXPORTER_TYPE", "exporter", "console")).strip().lower(),
            otlp_endpoint=str(_env("OTEL_EXPORTER_OTLP_ENDPOINT", "otlp_endpoint", "http://localhost:4317")),
            sampler=str(_env("OTEL_TRACES_SAMPLER", "sampler", "parentbased_always_on")).strip().lower(),
            sampler_arg=sampler_arg,
        )


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def _build_sampler(config: TracingConfig):
    """Return an SDK sampler matching the config string."""
    from opentelemetry.sdk.trace.sampling import (
        ALWAYS_ON,
        ALWAYS_OFF,
        ParentBasedTraceIdRatio,
        TraceIdRatioBased,
    )
    name = config.sampler
    if name == "always_on":
        return ALWAYS_ON
    if name == "always_off":
        return ALWAYS_OFF
    if name == "traceidratio":
        return TraceIdRatioBased(config.sampler_arg or 1.0)
    if name == "parentbased_traceidratio":
        return ParentBasedTraceIdRatio(config.sampler_arg or 1.0)
    # Default: parentbased_always_on (OpenTelemetry SDK default)
    return ALWAYS_ON


def _build_exporter(config: TracingConfig):
    """Return a span exporter matching the config string."""
    global _memory_exporter
    etype = config.exporter_type
    if etype == "otlp":
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        return OTLPSpanExporter(endpoint=config.otlp_endpoint, insecure=True)
    if etype == "memory":
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
        exporter = InMemorySpanExporter()
        _memory_exporter = exporter
        return exporter
    # Default: console
    return ConsoleSpanExporter()


def init_tracing(config: Optional[TracingConfig] = None) -> trace.TracerProvider:
    """
    Initialize the global OpenTelemetry TracerProvider.

    Returns the provider (for shutdown in application lifespan).  When
    ``config.enabled`` is False, sets a ``NoOpTracerProvider`` so every
    ``tracer.start_as_current_span()`` is a zero-cost no-op.
    """
    if config is None:
        config = TracingConfig.from_env()

    if not config.enabled:
        provider = NoOpTracerProvider()
        trace.set_tracer_provider(provider)
        logger.info("Tracing disabled (TRACING_ENABLED=false). Using NoOpTracerProvider.")
        return provider

    try:
        resource = Resource.create({
            "service.name": config.service_name,
            "service.version": "14.0.0",
            "deployment.environment": os.environ.get("DEPLOYMENT_ENVIRONMENT", "development"),
        })

        sampler = _build_sampler(config)
        exporter = _build_exporter(config)

        provider = TracerProvider(resource=resource, sampler=sampler)

        # Use SimpleSpanProcessor for memory exporter (testing) to avoid
        # flush-timing issues; BatchSpanProcessor for everything else.
        if config.exporter_type == "memory":
            provider.add_span_processor(SimpleSpanProcessor(exporter))
        else:
            provider.add_span_processor(BatchSpanProcessor(exporter))

        trace.set_tracer_provider(provider)
        logger.info(
            "Tracing enabled: service=%s exporter=%s sampler=%s",
            config.service_name, config.exporter_type, config.sampler,
        )
        return provider
    except Exception:
        logger.exception("Failed to initialize tracing; falling back to NoOpTracerProvider.")
        provider = NoOpTracerProvider()
        trace.set_tracer_provider(provider)
        return provider


def shutdown_tracing(provider: trace.TracerProvider) -> None:
    """Flush pending spans and shut down the provider. Fire-and-forget."""
    try:
        if hasattr(provider, "shutdown"):
            provider.shutdown()
            logger.info("Tracing provider shut down.")
    except Exception:
        logger.exception("Error shutting down tracing provider (swallowed).")


class _SafeTracer:
    """
    Wraps a real OpenTelemetry ``Tracer`` so ``start_as_current_span()``
    can never raise, hang, or otherwise change caller behavior -- this is
    the "Tracing is strictly observational" guarantee from this module's
    docstring, enforced centrally here rather than re-implemented at every
    one of the ~15 call sites across conversation_manager.py and
    tool_orchestrator.py (Steps 14.3.8, 14.4, 14.9's "a tracing SDK
    failure does not cause a request to fail").

    A failure creating or entering the underlying span yields
    ``trace.INVALID_SPAN`` instead -- a real OpenTelemetry ``Span`` object
    whose every method (``set_attribute``, ``set_status``,
    ``record_exception``, ...) is already a documented no-op, so call
    sites need no special-casing. An exception raised by the *wrapped
    code* (the caller's own ``with`` block body) is never swallowed --
    only failures inside the tracer itself are.
    """

    def __init__(self, tracer: Tracer):
        self._tracer = tracer

    @contextlib.contextmanager
    def start_as_current_span(self, name: str, **kwargs):
        try:
            _cm = self._tracer.start_as_current_span(name, **kwargs)
            span = _cm.__enter__()
        except Exception:
            logger.debug("Tracer failed to start span %r; continuing without tracing.", name, exc_info=True)
            yield trace.INVALID_SPAN
            return

        try:
            yield span
        except BaseException:
            try:
                _cm.__exit__(*sys.exc_info())
            except Exception:
                pass
            raise
        else:
            try:
                _cm.__exit__(None, None, None)
            except Exception:
                pass


def get_tracer(name: str) -> Tracer:
    """
    Return a named, failure-safe Tracer from the global provider.

    Each module should call this once at import time with a unique name
    (e.g. ``get_tracer("ai-voice-agent.conversation")``).  When tracing is
    disabled, the returned tracer's spans are zero-cost no-ops. When
    tracing is enabled but the SDK itself misbehaves, spans silently
    become no-ops rather than raising into the caller -- see
    ``_SafeTracer``.
    """
    return _SafeTracer(trace.get_tracer(name, "14.0.0"))


def get_memory_exporter():
    """Return the InMemorySpanExporter if active (for tests), else None."""
    return _memory_exporter


# ---------------------------------------------------------------------------
# Correlation bridge: traces <-> CorrelationContext / AuditEvent / logs
# ---------------------------------------------------------------------------

def get_current_trace_context() -> tuple[Optional[str], Optional[str]]:
    """
    Extract ``(trace_id, span_id)`` from the current OpenTelemetry span
    context, formatted as hex strings.  Returns ``(None, None)`` if no
    valid span is active or if extraction fails.
    """
    try:
        span = trace.get_current_span()
        ctx = span.get_span_context()
        if ctx and ctx.is_valid:
            return (
                format(ctx.trace_id, "032x"),
                format(ctx.span_id, "016x"),
            )
    except Exception:
        pass
    return (None, None)


def with_trace_context(correlation_ctx):
    """
    Return a new CorrelationContext enriched with ``trace_id`` and ``span_id``
    from the current OpenTelemetry span.  If no valid span is active, returns
    the original context unchanged.

    Accepts any object with the CorrelationContext interface (avoiding a
    circular import on ``observability_models``).
    """
    trace_id, span_id = get_current_trace_context()
    if trace_id is None:
        return correlation_ctx

    # CorrelationContext is frozen, so we construct a new instance.
    try:
        from observability_models import CorrelationContext
        return CorrelationContext(
            request_id=correlation_ctx.request_id,
            conversation_id=getattr(correlation_ctx, "conversation_id", None),
            session_id=getattr(correlation_ctx, "session_id", None),
            user_id=getattr(correlation_ctx, "user_id", None),
            turn_id=getattr(correlation_ctx, "turn_id", None),
            trace_id=trace_id,
            span_id=span_id,
        )
    except Exception:
        return correlation_ctx

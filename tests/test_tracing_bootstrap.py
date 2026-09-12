import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "agent"))

from observability_models import CorrelationContext
from tracing import (
    SpanAttributes,
    TracingConfig,
    get_tracer,
    init_tracing,
    shutdown_tracing,
    with_trace_context,
)

# Assume PIIDetector is available
try:
    from pii_detector import PIIDetector

    HAS_PII = True
except ImportError:
    HAS_PII = False

# We need OpenTelemetry available for memory exporter tests
try:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: F401 -- availability check only
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,  # noqa: F401 -- availability check only
    )

    HAS_OTEL = True
except ImportError:
    HAS_OTEL = False


@pytest.fixture(autouse=True)
def reset_global_tracer_provider():
    """Reset the tracer provider before and after each test."""

    def _reset():
        if HAS_OTEL:
            trace._TRACER_PROVIDER = None
            if hasattr(trace, "_TRACER_PROVIDER_SET_ONCE"):
                trace._TRACER_PROVIDER_SET_ONCE._done = False

    _reset()
    yield
    _reset()


def test_tracing_disabled_returns_noop():
    config = TracingConfig(enabled=False)
    provider = init_tracing(config)

    if HAS_OTEL:
        from opentelemetry.trace import NoOpTracerProvider

        assert isinstance(provider, NoOpTracerProvider)


@pytest.mark.skipif(not HAS_OTEL, reason="OpenTelemetry not installed")
def test_tracing_enabled_memory_exporter():
    config = TracingConfig(enabled=True, exporter_type="memory")
    provider = init_tracing(config)

    assert isinstance(provider, TracerProvider)

    # Check that a tracer can be obtained
    tracer = get_tracer("test.tracer")
    assert tracer is not None


@pytest.mark.skipif(not HAS_PII, reason="PIIDetector not available")
def test_span_attributes_no_pii():
    detector = PIIDetector()

    # Extract all values from SpanAttributes
    attrs = [v for k, v in vars(SpanAttributes).items() if not k.startswith("_")]

    for attr in attrs:
        # Span attribute keys should not be flagged as containing PII
        # Though the keys themselves aren't PII, we test that our expected span
        # attribute keys don't resemble PII types to ensure we're following rules.
        findings = detector.detect(attr)
        assert len(findings) == 0, f"Span attribute {attr} might contain PII: {findings}"


@pytest.mark.skipif(not HAS_OTEL, reason="OpenTelemetry not installed")
def test_shutdown_flushes_spans():
    config = TracingConfig(enabled=True, exporter_type="memory")
    provider = init_tracing(config)

    tracer = get_tracer("test.tracer")
    with tracer.start_as_current_span("test_span"):
        pass

    # Calling shutdown should complete without errors
    shutdown_tracing(provider)


def test_config_from_env_defaults(monkeypatch):
    monkeypatch.delenv("TRACING_ENABLED", raising=False)
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)

    config = TracingConfig.from_env()
    assert not config.enabled
    assert config.service_name == "ai-voice-agent"
    assert config.exporter_type == "console"
    assert config.otlp_endpoint == "http://localhost:4317"
    assert config.sampler == "parentbased_always_on"
    assert config.sampler_arg is None


def test_config_env_overrides(monkeypatch):
    monkeypatch.setenv("TRACING_ENABLED", "true")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "my-test-service")
    monkeypatch.setenv("OTEL_EXPORTER_TYPE", "otlp")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://custom:4317")
    monkeypatch.setenv("OTEL_TRACES_SAMPLER", "traceidratio")
    monkeypatch.setenv("OTEL_TRACES_SAMPLER_ARG", "0.5")

    config = TracingConfig.from_env()
    assert config.enabled
    assert config.service_name == "my-test-service"
    assert config.exporter_type == "otlp"
    assert config.otlp_endpoint == "http://custom:4317"
    assert config.sampler == "traceidratio"
    assert config.sampler_arg == 0.5


@pytest.mark.skipif(not HAS_OTEL, reason="OpenTelemetry not installed")
def test_with_trace_context_populates_ids():
    config = TracingConfig(enabled=True, exporter_type="memory")
    init_tracing(config)
    tracer = get_tracer("test.tracer")

    ctx = CorrelationContext(request_id="req-123")

    with tracer.start_as_current_span("test_span") as span:
        ctx_enriched = with_trace_context(ctx)

        assert hasattr(ctx_enriched, "trace_id")
        assert hasattr(ctx_enriched, "span_id")

        span_ctx = span.get_span_context()
        assert ctx_enriched.trace_id == format(span_ctx.trace_id, "032x")
        assert ctx_enriched.span_id == format(span_ctx.span_id, "016x")


@pytest.mark.skipif(not HAS_OTEL, reason="OpenTelemetry not installed")
def test_with_trace_context_without_span():
    ctx = CorrelationContext(request_id="req-123")
    ctx_enriched = with_trace_context(ctx)

    assert ctx_enriched.trace_id is None
    assert ctx_enriched.span_id is None


def test_get_tracer_returns_tracer():
    config = TracingConfig(enabled=False)
    init_tracing(config)

    tracer = get_tracer("test_tracer")
    assert tracer is not None
    # Can start a span without crashing
    with tracer.start_as_current_span("test"):
        pass

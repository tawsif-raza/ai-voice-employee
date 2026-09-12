"""
Tests for Phase 14 (plan.md Step 14.5) correlation bridge: traces <-> logs
<-> audit events. Confirms `with_trace_context()`, `AuditEvent.trace_id`/
`span_id`, `StructuredJSONFormatter`'s JSON output, and a full
ConversationManager turn all share one `trace_id` end to end.

Run with:
    python -m pytest tests/test_tracing_correlation.py -v
"""

import json
import logging
import sys
import unittest
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "agent"))
from audit import AuditLogger, AuditRepository  # noqa: E402
from conversation_manager import ConversationManager  # noqa: E402
from observability_models import CorrelationContext, EventType  # noqa: E402
from tracing import TracingConfig, init_tracing, get_memory_exporter, get_tracer, with_trace_context  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "inference"))
from handoff_detector import HandoffDetector  # noqa: E402
from intent_engine import IntentEngine  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "voice"))
from production_logging import StructuredJSONFormatter  # noqa: E402

from opentelemetry import trace as _otel_trace  # noqa: E402

CLINICAL_CONFIG_PATH = Path(__file__).resolve().parents[1] / "configs" / "clinical_triggers.yaml"
HANDOFF_CONFIG_PATH = Path(__file__).resolve().parents[1] / "configs" / "handoff_phrases.yaml"
INTENT_TAXONOMY_PATH = Path(__file__).resolve().parents[1] / "configs" / "intent_taxonomy.yaml"


class FakeLLMService:
    def __init__(self, response_text: str = "Sure, here is the answer.", latency_ms: float = 42.0):
        self.response_text = response_text
        self.latency_ms = latency_ms
        self.calls: list[list[dict]] = []

    def generate_stream(self, messages, **kwargs):
        self.calls.append(messages)
        for word in self.response_text.split(" "):
            yield word + " "
        yield {"text": self.response_text, "latency_ms": self.latency_ms}


class FakeRetriever:
    def retrieve(self, query, top_k=3, domain=None):
        return []


def _run_turn(cm: ConversationManager, message, **kwargs):
    chunks, final = [], None
    for item in cm.handle_turn(message, **kwargs):
        if isinstance(item, str):
            chunks.append(item)
        else:
            final = item
    return chunks, final


class TracingCorrelationTestCase(unittest.TestCase):
    def setUp(self):
        _otel_trace._TRACER_PROVIDER = None
        if hasattr(_otel_trace, "_TRACER_PROVIDER_SET_ONCE"):
            _otel_trace._TRACER_PROVIDER_SET_ONCE._done = False
        init_tracing(TracingConfig(enabled=True, exporter_type="memory"))
        self.exporter = get_memory_exporter()
        self.exporter.clear()

        # See test_tracing_pipeline.py's identical note: force
        # conversation_manager.py's module-level tracer singleton to
        # re-resolve against the fresh provider (ProxyTracer caches its
        # first resolution forever).
        import conversation_manager as _cm_module
        proxy = getattr(_cm_module._tracer, "_tracer", None)
        if proxy is not None and hasattr(proxy, "_real_tracer"):
            proxy._real_tracer = None

    def tearDown(self):
        _otel_trace._TRACER_PROVIDER = None
        if hasattr(_otel_trace, "_TRACER_PROVIDER_SET_ONCE"):
            _otel_trace._TRACER_PROVIDER_SET_ONCE._done = False


class TestCorrelationContextGetsTraceIds(TracingCorrelationTestCase):
    def test_correlation_context_gets_trace_ids(self):
        tracer = get_tracer("test.correlation")
        ctx = CorrelationContext(request_id="req-123")
        with tracer.start_as_current_span("test_span") as span:
            enriched = with_trace_context(ctx)
            span_ctx = span.get_span_context()
            self.assertEqual(enriched.trace_id, format(span_ctx.trace_id, "032x"))
            self.assertEqual(enriched.span_id, format(span_ctx.span_id, "016x"))


class TestCorrelationContextWithoutSpan(TracingCorrelationTestCase):
    def test_correlation_context_without_span(self):
        ctx = CorrelationContext(request_id="req-123")
        enriched = with_trace_context(ctx)
        self.assertIsNone(enriched.trace_id)
        self.assertIsNone(enriched.span_id)


class TestAuditEventCarriesTraceId(TracingCorrelationTestCase):
    def test_audit_event_carries_trace_id(self):
        repo = AuditRepository()
        logger = AuditLogger(repository=repo)
        tracer = get_tracer("test.correlation")

        with tracer.start_as_current_span("test_span") as span:
            span_ctx = span.get_span_context()
            logger.record(EventType.POLICY_ALLOW, outcome="allowed", request_id="req-1")

        events = repo.list_events(request_id="req-1")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].trace_id, format(span_ctx.trace_id, "032x"))
        self.assertEqual(events[0].span_id, format(span_ctx.span_id, "016x"))


class TestStructuredLogIncludesTraceId(TracingCorrelationTestCase):
    def test_structured_log_includes_trace_id(self):
        tracer = get_tracer("test.correlation")
        stream = StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(StructuredJSONFormatter(service_name="test-service"))
        test_logger = logging.getLogger("test_tracing_correlation.structured")
        test_logger.handlers.clear()
        test_logger.addHandler(handler)
        test_logger.setLevel(logging.INFO)
        test_logger.propagate = False

        with tracer.start_as_current_span("test_span") as span:
            span_ctx = span.get_span_context()
            test_logger.info("a structured log line")

        payload = json.loads(stream.getvalue().strip().splitlines()[-1])
        self.assertEqual(payload["trace_id"], format(span_ctx.trace_id, "032x"))
        self.assertEqual(payload["span_id"], format(span_ctx.span_id, "016x"))


class TestTraceIdConsistentAcrossTurn(TracingCorrelationTestCase):
    def test_trace_id_consistent_across_turn(self):
        repo = AuditRepository()
        audit_logger = AuditLogger(repository=repo)
        llm = FakeLLMService(response_text="We're open nine to five.")
        cm = ConversationManager(
            llm_service=llm, retriever=FakeRetriever(),
            clinical_guard=HandoffDetector(config_path=CLINICAL_CONFIG_PATH),
            handoff_detector=HandoffDetector(config_path=HANDOFF_CONFIG_PATH),
            intent_engine=IntentEngine(config_path=INTENT_TAXONOMY_PATH),
            audit_logger=audit_logger,
        )
        _run_turn(cm, "What are your business hours?", request_id="req-turn-1")

        spans = self.exporter.get_finished_spans()
        self.assertTrue(spans, "the turn should have produced spans")
        span_trace_ids = {s.context.trace_id for s in spans}
        self.assertEqual(len(span_trace_ids), 1, "every span in the turn must share one trace_id")

        events = repo.list_events(request_id="req-turn-1")
        self.assertTrue(events, "the turn should have produced audit events")
        event_trace_ids = {e.trace_id for e in events if e.trace_id is not None}
        # Every recorded trace_id (on the events that have one) must match
        # the single trace_id shared by every span in this turn.
        expected_trace_id = format(next(iter(span_trace_ids)), "032x")
        for trace_id in event_trace_ids:
            self.assertEqual(trace_id, expected_trace_id)


if __name__ == "__main__":
    unittest.main()

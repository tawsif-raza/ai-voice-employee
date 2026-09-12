"""
Tests for Phase 14 (plan.md Steps 14.3, 14.4) manual span instrumentation:
ConversationManager.handle_turn()'s turn-pipeline span tree and
ToolOrchestrator.invoke()'s gate-sequence span tree.

Uses tracing.py's "memory" exporter (InMemorySpanExporter) to capture real
spans without any external collector, and the same lightweight fakes/real-
component pattern as test_conversation_manager.py and
test_tool_orchestrator.py (no torch/faiss/network access needed).

Run with:
    python -m pytest tests/test_tracing_pipeline.py -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "agent"))
from conversation_manager import ConversationManager  # noqa: E402
from tracing import TracingConfig, init_tracing, get_memory_exporter  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "inference"))
from handoff_detector import HandoffDetector  # noqa: E402
from intent_engine import IntentEngine  # noqa: E402

from opentelemetry import trace as _otel_trace  # noqa: E402

CLINICAL_CONFIG_PATH = Path(__file__).resolve().parents[1] / "configs" / "clinical_triggers.yaml"
HANDOFF_CONFIG_PATH = Path(__file__).resolve().parents[1] / "configs" / "handoff_phrases.yaml"
INTENT_TAXONOMY_PATH = Path(__file__).resolve().parents[1] / "configs" / "intent_taxonomy.yaml"


def _real_clinical_guard() -> HandoffDetector:
    return HandoffDetector(config_path=CLINICAL_CONFIG_PATH)


def _real_handoff_detector() -> HandoffDetector:
    return HandoffDetector(config_path=HANDOFF_CONFIG_PATH)


def _real_intent_engine() -> IntentEngine:
    return IntentEngine(config_path=INTENT_TAXONOMY_PATH)


def _authenticated_user(user_id: str = "u1"):
    from action_models import AuthContext
    from identity import Role, permissions_for_roles

    return AuthContext(
        user_id=user_id, authenticated=True, roles=(Role.USER.value,),
        permissions=permissions_for_roles((Role.USER,)), authentication_method="test",
    )


class FakeLLMService:
    """Same generate_stream(messages) contract as test_conversation_manager.py's fake."""

    def __init__(self, response_text: str = "Sure, here is the answer.", fail: bool = False,
                 latency_ms: float = 42.0, extra_final_fields: dict = None):
        self.response_text = response_text
        self.fail = fail
        self.latency_ms = latency_ms
        self.extra_final_fields = extra_final_fields or {}
        self.calls: list[list[dict]] = []

    def generate_stream(self, messages, **kwargs):
        self.calls.append(messages)
        if self.fail:
            raise RuntimeError("simulated model crash")
        for word in self.response_text.split(" "):
            yield word + " "
        final = {"text": self.response_text, "latency_ms": self.latency_ms}
        final.update(self.extra_final_fields)
        yield final


class FailNTimesThenSucceedLLMService:
    """Fails the first N generate_stream() calls before any chunk is streamed, then succeeds."""

    def __init__(self, fail_times: int, response_text: str = "Recovered."):
        self.fail_times = fail_times
        self.response_text = response_text
        self.calls = 0

    def generate_stream(self, messages, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("transient LLM failure")
        for word in self.response_text.split(" "):
            yield word + " "
        yield {"text": self.response_text, "latency_ms": 10.0}


class FakeChunk:
    def __init__(self, id, domain, title, content, score):
        self.id, self.domain, self.title, self.content, self.score = id, domain, title, content, score

    def to_dict(self):
        return {"id": self.id, "domain": self.domain, "title": self.title, "score": round(self.score, 4)}


class FakeRetriever:
    def __init__(self, chunks=None, fail: bool = False):
        self._chunks = chunks or []
        self.fail = fail
        self.calls: list[str] = []

    def retrieve(self, query, top_k=3, domain=None):
        self.calls.append(query)
        if self.fail:
            raise ConnectionError("simulated retrieval backend outage")
        return self._chunks[:top_k]


class FailNTimesThenSucceedRetriever:
    def __init__(self, fail_times: int, chunks=None):
        self.fail_times = fail_times
        self._chunks = chunks or []
        self.calls = 0

    def retrieve(self, query, top_k=3, domain=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ConnectionError("transient retrieval outage")
        return self._chunks[:top_k]


def _run_turn(cm: ConversationManager, message, history=None, auth=None, confirmed=False):
    chunks, final = [], None
    for item in cm.handle_turn(message, history=history, auth=auth, confirmed=confirmed):
        if isinstance(item, str):
            chunks.append(item)
        else:
            final = item
    return chunks, final


class TracingPipelineTestCase(unittest.TestCase):
    """Base class: enables real (in-memory) tracing for every test method."""

    def setUp(self):
        _otel_trace._TRACER_PROVIDER = None
        if hasattr(_otel_trace, "_TRACER_PROVIDER_SET_ONCE"):
            _otel_trace._TRACER_PROVIDER_SET_ONCE._done = False
        init_tracing(TracingConfig(enabled=True, exporter_type="memory"))
        self.exporter = get_memory_exporter()
        self.exporter.clear()

        # conversation_manager.py and tool_orchestrator.py hold their
        # tracer as a MODULE-LEVEL singleton (by design -- see their own
        # "Phase 14: module-level tracer singleton" comments), obtained
        # once at import time. The underlying OpenTelemetry ProxyTracer
        # caches its *first* resolved concrete tracer forever
        # (opentelemetry.trace.ProxyTracer._tracer), so simply resetting
        # the global TracerProvider above does not make already-created
        # singletons pick up the fresh one. Force them to re-resolve here
        # so each test's memory exporter actually captures their spans.
        import conversation_manager as _cm_module
        import tool_orchestrator as _to_module
        for _mod in (_cm_module, _to_module):
            proxy = getattr(_mod._tracer, "_tracer", None)
            if proxy is not None and hasattr(proxy, "_real_tracer"):
                proxy._real_tracer = None

    def tearDown(self):
        _otel_trace._TRACER_PROVIDER = None
        if hasattr(_otel_trace, "_TRACER_PROVIDER_SET_ONCE"):
            _otel_trace._TRACER_PROVIDER_SET_ONCE._done = False

    def _spans(self):
        return self.exporter.get_finished_spans()

    def _span_names(self):
        return [s.name for s in self._spans()]

    def _span_by_name(self, name):
        matches = [s for s in self._spans() if s.name == name]
        self.assertEqual(len(matches), 1, f"expected exactly one {name!r} span, found {len(matches)}")
        return matches[0]


class TestFullTurnSpanTree(TracingPipelineTestCase):
    def test_full_turn_produces_span_tree(self):
        llm = FakeLLMService(response_text="Our business hours are nine to five.")
        retriever = FakeRetriever([FakeChunk("faq_hours", "faqs", "Hours", "9-5", 0.9)])
        cm = ConversationManager(
            llm_service=llm, retriever=retriever,
            clinical_guard=_real_clinical_guard(), handoff_detector=_real_handoff_detector(),
            intent_engine=_real_intent_engine(),
        )
        _run_turn(cm, "What are your business hours?")

        names = self._span_names()
        expected = {
            "conversation.handle_turn", "conversation.clinical_safety_check",
            "conversation.intent_classify", "conversation.policy_evaluate",
            "conversation.rag_retrieve", "conversation.llm_generate", "conversation.handoff_detect",
        }
        self.assertTrue(expected.issubset(set(names)), f"missing spans: {expected - set(names)}")

        trace_ids = {s.context.trace_id for s in self._spans()}
        self.assertEqual(len(trace_ids), 1, "every span in one turn must share the same trace_id")

        root = self._span_by_name("conversation.handle_turn")
        for span in self._spans():
            if span is root:
                continue
            self.assertEqual(
                span.parent.span_id if span.parent else None, root.context.span_id,
                f"{span.name} is not a direct child of the root turn span",
            )


class TestClinicalShortCircuit(TracingPipelineTestCase):
    def test_clinical_short_circuit_minimal_spans(self):
        llm = FakeLLMService()
        cm = ConversationManager(
            llm_service=llm, retriever=FakeRetriever(),
            clinical_guard=_real_clinical_guard(), handoff_detector=_real_handoff_detector(),
        )
        _run_turn(cm, "How many mg of ibuprofen should I take?")

        names = set(self._span_names())
        self.assertIn("conversation.handle_turn", names)
        self.assertIn("conversation.clinical_safety_check", names)
        for absent in ("conversation.rag_retrieve", "conversation.llm_generate", "conversation.handoff_detect"):
            self.assertNotIn(absent, names, f"{absent} must not run when the clinical guard short-circuits")


class TestToolActionSpan(TracingPipelineTestCase):
    def test_tool_action_span(self):
        from mock_tools import build_default_tool_registry
        from policy_engine import PolicyEngine
        from tool_orchestrator import ToolOrchestrator

        policy = PolicyEngine()
        orchestrator = ToolOrchestrator(build_default_tool_registry(), policy)
        llm = FakeLLMService(response_text="irrelevant")
        cm = ConversationManager(
            llm_service=llm, retriever=FakeRetriever(),
            clinical_guard=_real_clinical_guard(), handoff_detector=_real_handoff_detector(),
            intent_engine=_real_intent_engine(), policy_engine=policy, tool_orchestrator=orchestrator,
        )
        auth = _authenticated_user("u1")
        _run_turn(cm, "Can you check my order status for order_1001?", auth=auth)

        names = set(self._span_names())
        self.assertIn("tool_orchestrator.invoke", names)
        self.assertIn("tool_orchestrator.gate_policy", names)
        self.assertIn("tool_orchestrator.execute", names)
        invoke_span = self._span_by_name("tool_orchestrator.invoke")
        root = self._span_by_name("conversation.handle_turn")
        self.assertEqual(invoke_span.parent.span_id, root.context.span_id)


class TestRagRetrySpan(TracingPipelineTestCase):
    def test_rag_retry_recorded_in_span(self):
        from reliability import RetryPolicy

        llm = FakeLLMService(response_text="Here you go.")
        retriever = FailNTimesThenSucceedRetriever(fail_times=1, chunks=[FakeChunk("c1", "faqs", "T", "C", 0.9)])
        cm = ConversationManager(
            llm_service=llm, retriever=retriever,
            clinical_guard=_real_clinical_guard(), handoff_detector=_real_handoff_detector(),
            intent_engine=_real_intent_engine(), sleep_fn=lambda _seconds: None,
            rag_retry_policy=RetryPolicy(max_attempts=2),
        )
        _run_turn(cm, "What is your return policy?")

        rag_span = self._span_by_name("conversation.rag_retrieve")
        attrs = dict(rag_span.attributes)
        self.assertEqual(attrs.get("app.retry.attempt"), 1)


class TestLLMFailoverSpan(TracingPipelineTestCase):
    def test_llm_failover_recorded_in_span(self):
        llm = FakeLLMService(
            response_text="Answered via fallback.",
            extra_final_fields={"provider": "gemini", "model": "gemini-2.5-flash", "fallback_used": True},
        )
        cm = ConversationManager(
            llm_service=llm, retriever=FakeRetriever(),
            clinical_guard=_real_clinical_guard(), handoff_detector=_real_handoff_detector(),
            intent_engine=_real_intent_engine(),
        )
        _run_turn(cm, "What is your return policy?")

        llm_span = self._span_by_name("conversation.llm_generate")
        attrs = dict(llm_span.attributes)
        self.assertTrue(attrs.get("app.llm.failover"))
        self.assertEqual(attrs.get("app.llm.provider"), "gemini")


class TestSpanAttributesNeverContainUserMessage(TracingPipelineTestCase):
    def test_span_attributes_never_contain_user_message(self):
        secret_message = "MySecretMedicalCondition12345"
        llm = FakeLLMService(response_text="A response mentioning " + secret_message + " would be a bug.")
        retriever = FakeRetriever([FakeChunk("c1", "faqs", secret_message, secret_message, 0.9)])
        cm = ConversationManager(
            llm_service=llm, retriever=retriever,
            clinical_guard=_real_clinical_guard(), handoff_detector=_real_handoff_detector(),
            intent_engine=_real_intent_engine(),
        )
        _run_turn(cm, f"Tell me about {secret_message}")

        for span in self._spans():
            for key, value in span.attributes.items():
                self.assertNotIn(secret_message, str(value), f"span {span.name} attribute {key} leaked the user message")


class TestTracingErrorDoesNotBreakTurn(TracingPipelineTestCase):
    def test_tracing_error_does_not_break_turn(self):
        import conversation_manager as cm_module

        original = cm_module._tracer.start_as_current_span

        def _raising_start_as_current_span(*args, **kwargs):
            raise RuntimeError("simulated tracer SDK failure")

        cm_module._tracer.start_as_current_span = _raising_start_as_current_span
        try:
            llm = FakeLLMService(response_text="Still works.")
            cm = ConversationManager(
                llm_service=llm, retriever=FakeRetriever(),
                clinical_guard=_real_clinical_guard(), handoff_detector=_real_handoff_detector(),
                intent_engine=_real_intent_engine(),
            )
            chunks, final = _run_turn(cm, "What are your business hours?")
            self.assertTrue(chunks)
            self.assertEqual(final["response"], "Still works.")
            self.assertIsNone(final["error"])
        finally:
            cm_module._tracer.start_as_current_span = original


class TestToolDeniedShortCircuit(TracingPipelineTestCase):
    def test_tool_denied_short_circuit(self):
        from action_models import ActionSpec, ActionProposal, ToolRequest
        from policy_engine import PolicyEngine
        from tool_orchestrator import ToolOrchestrator
        from tool_registry import ToolRegistry

        registry = ToolRegistry()
        registry.register(ActionSpec(name="UNLISTED_ACTION", description="d", params_schema={}), lambda params: {"ok": True})
        orchestrator = ToolOrchestrator(registry, PolicyEngine())
        result = orchestrator.invoke(ToolRequest(action="UNLISTED_ACTION", params={}, confirmed=True), auth=_authenticated_user())

        self.assertFalse(result.success)
        self.assertEqual(result.status, "policy_denied")

        names = set(self._span_names())
        self.assertIn("tool_orchestrator.invoke", names)
        self.assertIn("tool_orchestrator.gate_policy", names)
        self.assertNotIn("tool_orchestrator.execute", names)
        self.assertNotIn("tool_orchestrator.gate_authorization", names)


class TestToolSpanRecordsTimeout(TracingPipelineTestCase):
    def test_tool_span_records_timeout(self):
        from action_models import ActionSpec, ToolRequest
        from policy_engine import PolicyEngine
        from tool_orchestrator import ToolOrchestrator
        from tool_registry import ToolRegistry

        registry = ToolRegistry()
        registry.register(
            ActionSpec(name="SLOW_ACTION", description="d", params_schema={}, timeout_seconds=0.05),
            lambda params: __import__("time").sleep(0.5) or {"ok": True},
        )
        policy = PolicyEngine()
        policy._tools = {**policy._tools, "rules": [*policy._tools.get("rules", []),
                                                     {"action": "SLOW_ACTION", "rule": "TEST_ALLOWED", "allowed": True, "reason": "test"}]}
        orchestrator = ToolOrchestrator(registry, policy)
        result = orchestrator.invoke(ToolRequest(action="SLOW_ACTION", params={}, confirmed=True), auth=_authenticated_user())

        self.assertEqual(result.status, "timeout")
        exec_span = self._span_by_name("tool_orchestrator.execute")
        self.assertEqual(exec_span.status.status_code, _otel_trace.StatusCode.ERROR)
        self.assertEqual(dict(exec_span.attributes).get("app.error.type"), "DependencyTimeoutError")


if __name__ == "__main__":
    unittest.main()

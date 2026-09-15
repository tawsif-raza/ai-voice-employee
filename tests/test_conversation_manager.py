"""
Unit tests for ConversationManager (src/agent/conversation_manager.py) —
the orchestration boundary extracted from VoiceAssistantInference
(docs/IMPLEMENTATION_ROADMAP.md milestone M2; docs/adr/ADR-001).

These exercise ConversationManager.handle_turn() with the real
HandoffDetector (clinical guard + handoff detector — lightweight, YAML
config only) but a fake LLM service and a fake retriever, so no model
weights, torch, faiss, or network access are required. This is exactly the
"Unit Test Boundary" docs/MODULES.md §2 specifies: orchestration
verifiable with downstream dependencies mocked, no full model load needed
— the boundary that did not exist before this extraction
(docs/ARCHITECTURE_REVIEW.md finding 3.1).

Run with:
    python -m unittest tests.test_conversation_manager -v
"""

import os
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "agent"))
from conversation_manager import (  # noqa: E402
    _REMOTE_PROVIDER_DEFAULT_MAX_CONCURRENT_GENERATIONS,
    ConversationManager,
    _llm_service_is_safe_for_concurrent_generation,
    _resolve_max_concurrent_generations,
    build_conversation_manager,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "inference"))
from handoff_detector import HandoffDetector  # noqa: E402
from intent_engine import IntentEngine, IntentResult, Route, RoutingDecision  # noqa: E402
from llm_provider import (  # noqa: E402
    ClaudeLLMProvider,
    FallbackLLMProvider,
    GeminiLLMProvider,
    GroqLLMProvider,
    LocalLLMProvider,
)

CLINICAL_CONFIG_PATH = Path(__file__).resolve().parents[1] / "configs" / "clinical_triggers.yaml"
HANDOFF_CONFIG_PATH = Path(__file__).resolve().parents[1] / "configs" / "handoff_phrases.yaml"
INTENT_TAXONOMY_PATH = Path(__file__).resolve().parents[1] / "configs" / "intent_taxonomy.yaml"


# ── Test doubles ─────────────────────────────────────────────────────────────


class FakeLLMService:
    """
    Stands in for src/inference/llm_service.py's LLMService: same
    generate_stream(messages) -> Iterator[str | {"text", "latency_ms"}]
    contract, no torch/transformers dependency. Records every `messages`
    argument it's called with so tests can assert whether/how the LLM was
    invoked (e.g. "never called when the clinical guard fires").
    """

    def __init__(self, response_text: str = "Sure, here is the answer.", fail: bool = False, latency_ms: float = 42.0):
        self.response_text = response_text
        self.fail = fail
        self.latency_ms = latency_ms
        self.calls: list[list[dict]] = []

    def generate_stream(self, messages, **kwargs):
        self.calls.append(messages)
        if self.fail:
            raise RuntimeError(
                "simulated model crash (e.g. CUDA OOM) with internal detail that must never reach a client"
            )
        for word in self.response_text.split(" "):
            yield word + " "
        yield {"text": self.response_text, "latency_ms": self.latency_ms}


class FakeChunk:
    """Stands in for src/rag/retriever.py's RetrievedChunk."""

    def __init__(self, id, domain, title, content, score):
        self.id, self.domain, self.title, self.content, self.score = id, domain, title, content, score

    def to_dict(self):
        return {"id": self.id, "domain": self.domain, "title": self.title, "score": round(self.score, 4)}


class FakeRetriever:
    """Stands in for src/rag/retriever.py's Retriever."""

    def __init__(self, chunks=None, fail: bool = False):
        self._chunks = chunks or []
        self.fail = fail
        self.calls: list[str] = []

    def retrieve(self, query, top_k=3, domain=None):
        self.calls.append(query)
        if self.fail:
            raise ConnectionError("simulated retrieval backend outage")
        return self._chunks[:top_k]


def _real_handoff_detector() -> HandoffDetector:
    return HandoffDetector(config_path=HANDOFF_CONFIG_PATH)


def _real_clinical_guard() -> HandoffDetector:
    return HandoffDetector(config_path=CLINICAL_CONFIG_PATH)


def _real_intent_engine() -> IntentEngine:
    return IntentEngine(config_path=INTENT_TAXONOMY_PATH)


def _authenticated_user(user_id: str = "u1"):
    """
    A fully-permissioned, authenticated USER identity (Phase 7) for tests
    that exercise tool-backed flows -- uses identity.py's real
    Role/Permission taxonomy (permissions_for_roles()) rather than a
    hand-rolled permission list, so these tests stay accurate if the
    role->permission mapping ever changes.
    """
    from action_models import AuthContext
    from identity import Role, permissions_for_roles

    return AuthContext(
        user_id=user_id,
        authenticated=True,
        roles=(Role.USER.value,),
        permissions=permissions_for_roles((Role.USER,)),
        authentication_method="test",
    )


class SpyIntentEngine:
    """
    Wraps a real IntentEngine (or a fixed answer) and records every call,
    so tests can prove IntentEngine was -- or, critically, was NOT --
    consulted for a given turn (e.g. when the clinical guard fires first).
    """

    def __init__(self, fixed_decision: "RoutingDecision | None" = None):
        self._engine = _real_intent_engine()
        self._fixed_decision = fixed_decision
        self.calls: list[str] = []

    def classify(self, message, history=None):
        self.calls.append(message)
        if self._fixed_decision is not None:
            return self._fixed_decision
        return self._engine.classify(message, history=history)


def _run_turn(cm: ConversationManager, message, history=None):
    """Drain handle_turn(), returning (streamed_chunks, final_dict)."""
    chunks = []
    final = None
    for item in cm.handle_turn(message, history=history):
        if isinstance(item, str):
            chunks.append(item)
        else:
            final = item
    return chunks, final


# ── Tests ────────────────────────────────────────────────────────────────────


class TestNormalSafeRequest(unittest.TestCase):
    def test_full_turn_happy_path(self):
        llm = FakeLLMService(response_text="Our return window is thirty days.")
        retriever = FakeRetriever([FakeChunk("faq_returns", "faqs", "Returns", "30 day policy", 0.9)])
        cm = ConversationManager(
            llm_service=llm,
            retriever=retriever,
            clinical_guard=_real_clinical_guard(),
            handoff_detector=_real_handoff_detector(),
        )
        chunks, final = _run_turn(cm, "What's your return policy?")

        self.assertTrue(chunks, "tokens should have been streamed")
        self.assertEqual(final["response"], "Our return window is thirty days.")
        self.assertFalse(final["is_handoff"])
        self.assertFalse(final["clinical_guard_triggered"])
        self.assertFalse(final["degraded"])
        self.assertIsNone(final["error"])
        self.assertEqual(final["retrieved_chunks"][0]["id"], "faq_returns")
        self.assertEqual(len(llm.calls), 1)

    def test_relevant_chunks_are_injected_as_context(self):
        llm = FakeLLMService(response_text="Yes, we ship internationally.")
        retriever = FakeRetriever(
            [FakeChunk("faq_intl", "faqs", "International shipping", "We ship to 40 countries.", 0.95)]
        )
        cm = ConversationManager(
            llm_service=llm,
            retriever=retriever,
            clinical_guard=_real_clinical_guard(),
            handoff_detector=_real_handoff_detector(),
        )
        _run_turn(cm, "Do you ship internationally?")

        sent_messages = llm.calls[0]
        system_messages = [m["content"] for m in sent_messages if m["role"] == "system"]
        self.assertTrue(any("We ship to 40 countries." in m for m in system_messages))

    def test_low_score_chunks_are_not_injected(self):
        llm = FakeLLMService(response_text="Not sure, let me check.")
        retriever = FakeRetriever([FakeChunk("faq_x", "faqs", "Unrelated", "Unrelated content.", 0.1)])
        cm = ConversationManager(
            llm_service=llm,
            retriever=retriever,
            rag_score_threshold=0.35,
            clinical_guard=_real_clinical_guard(),
            handoff_detector=_real_handoff_detector(),
        )
        _run_turn(cm, "Random question")

        sent_messages = llm.calls[0]
        self.assertEqual(len(sent_messages), 2)  # system prompt + user turn only, no context message


class TestClinicalSafetyTrigger(unittest.TestCase):
    def test_clinical_question_short_circuits_before_llm(self):
        llm = FakeLLMService()
        cm = ConversationManager(
            llm_service=llm,
            retriever=FakeRetriever(),
            clinical_guard=_real_clinical_guard(),
            handoff_detector=_real_handoff_detector(),
        )
        _, final = _run_turn(cm, "How many mg of ibuprofen should I take?")

        self.assertTrue(final["is_handoff"])
        self.assertTrue(final["clinical_guard_triggered"])
        self.assertEqual(final["response"], ConversationManager.CLINICAL_HANDOFF_RESPONSE)
        self.assertEqual(llm.calls, [], "LLM must never be called when the clinical guard fires")

    def test_non_clinical_question_reaches_llm(self):
        llm = FakeLLMService(response_text="We're open 9 to 5.")
        cm = ConversationManager(
            llm_service=llm,
            retriever=FakeRetriever(),
            clinical_guard=_real_clinical_guard(),
            handoff_detector=_real_handoff_detector(),
        )
        _, final = _run_turn(cm, "What are your business hours?")

        self.assertFalse(final["clinical_guard_triggered"])
        self.assertEqual(len(llm.calls), 1)

    def test_no_clinical_guard_configured_skips_the_check(self):
        llm = FakeLLMService(response_text="I can't diagnose that, but here's some general info.")
        cm = ConversationManager(
            llm_service=llm,
            retriever=FakeRetriever(),
            clinical_guard=None,
            handoff_detector=_real_handoff_detector(),
        )
        _, final = _run_turn(cm, "Can you diagnose what's wrong with me?")

        self.assertFalse(final["clinical_guard_triggered"])
        self.assertEqual(len(llm.calls), 1, "with no clinical guard configured, every turn reaches the LLM")


class TestRetrievalFailure(unittest.TestCase):
    def test_retrieval_error_degrades_gracefully(self):
        llm = FakeLLMService(response_text="Happy to help another way.")
        retriever = FakeRetriever(fail=True)
        cm = ConversationManager(
            llm_service=llm,
            retriever=retriever,
            clinical_guard=_real_clinical_guard(),
            handoff_detector=_real_handoff_detector(),
        )
        _, final = _run_turn(cm, "What are your hours?")

        self.assertEqual(final["response"], "Happy to help another way.")
        self.assertEqual(final["retrieved_chunks"], [])
        self.assertTrue(final["degraded"])
        self.assertEqual(len(llm.calls), 1, "generation should still proceed, ungrounded")

    def test_no_retriever_configured_still_generates(self):
        llm = FakeLLMService(response_text="General answer.")
        cm = ConversationManager(
            llm_service=llm,
            retriever=None,
            clinical_guard=_real_clinical_guard(),
            handoff_detector=_real_handoff_detector(),
        )
        _, final = _run_turn(cm, "Tell me something")

        self.assertEqual(final["retrieved_chunks"], [])
        self.assertFalse(final["degraded"])


class TestLLMFailure(unittest.TestCase):
    def test_llm_crash_returns_safe_fallback(self):
        llm = FakeLLMService(fail=True)
        cm = ConversationManager(
            llm_service=llm,
            retriever=FakeRetriever(),
            clinical_guard=_real_clinical_guard(),
            handoff_detector=_real_handoff_detector(),
        )
        _, final = _run_turn(cm, "Tell me a joke.")

        self.assertTrue(final["is_handoff"])
        self.assertEqual(final["response"], ConversationManager.LLM_FAILURE_RESPONSE)
        self.assertEqual(final["error"], "llm_generation_failed")
        self.assertNotIn("CUDA OOM", final["response"])
        self.assertNotIn(
            "simulated model crash", final["response"], "internal exception text must never leak to the client"
        )


class TestHandoffDetection(unittest.TestCase):
    def test_llm_response_signals_handoff(self):
        llm = FakeLLMService(response_text="Let me connect you to a human agent right now.")
        cm = ConversationManager(
            llm_service=llm,
            retriever=FakeRetriever(),
            clinical_guard=_real_clinical_guard(),
            handoff_detector=_real_handoff_detector(),
        )
        _, final = _run_turn(cm, "I want to speak to a manager.")

        self.assertTrue(final["is_handoff"])
        self.assertGreater(final["handoff_confidence"], 0.0)
        self.assertFalse(
            final["clinical_guard_triggered"], "handoff must be attributed separately from the clinical guard"
        )

    def test_plain_response_does_not_signal_handoff(self):
        llm = FakeLLMService(response_text="Your order ships tomorrow.")
        cm = ConversationManager(
            llm_service=llm,
            retriever=FakeRetriever(),
            clinical_guard=_real_clinical_guard(),
            handoff_detector=_real_handoff_detector(),
        )
        _, final = _run_turn(cm, "When will my order arrive?")

        self.assertFalse(final["is_handoff"])
        self.assertEqual(final["handoff_confidence"], 0.0)


class TestEmptyInput(unittest.TestCase):
    def test_blank_message_is_handled_without_calling_llm(self):
        llm = FakeLLMService()
        cm = ConversationManager(
            llm_service=llm,
            retriever=FakeRetriever(),
            clinical_guard=_real_clinical_guard(),
            handoff_detector=_real_handoff_detector(),
        )
        _, final = _run_turn(cm, "   ")

        self.assertFalse(final["is_handoff"])
        self.assertEqual(llm.calls, [])
        self.assertEqual(final["response"], ConversationManager.EMPTY_INPUT_RESPONSE)

    def test_empty_string_is_handled(self):
        llm = FakeLLMService()
        cm = ConversationManager(
            llm_service=llm,
            retriever=FakeRetriever(),
            clinical_guard=_real_clinical_guard(),
            handoff_detector=_real_handoff_detector(),
        )
        _, final = _run_turn(cm, "")

        self.assertEqual(llm.calls, [])
        self.assertIsNotNone(final["response"])


class TestMalformedInput(unittest.TestCase):
    def test_non_string_message_does_not_crash(self):
        llm = FakeLLMService()
        cm = ConversationManager(
            llm_service=llm,
            retriever=FakeRetriever(),
            clinical_guard=_real_clinical_guard(),
            handoff_detector=_real_handoff_detector(),
        )
        for bad_message in (None, 12345, ["not", "a", "string"], {"message": "nested"}):
            with self.subTest(bad_message=bad_message):
                _, final = _run_turn(cm, bad_message)
                self.assertEqual(llm.calls, [])
                self.assertFalse(final["is_handoff"])
                llm.calls.clear()

    def test_malformed_history_entries_are_dropped_not_fatal(self):
        llm = FakeLLMService(response_text="OK.")
        cm = ConversationManager(
            llm_service=llm,
            retriever=FakeRetriever(),
            clinical_guard=_real_clinical_guard(),
            handoff_detector=_real_handoff_detector(),
        )
        bad_history = [
            "not a dict",
            {"role": "user"},  # missing content
            {"content": "missing role"},  # missing role
            {"role": "user", "content": "What time do you open?"},  # well-formed
            None,
            42,
        ]
        _, final = _run_turn(cm, "Thanks", history=bad_history)

        self.assertEqual(final["response"], "OK.")
        sent_messages = llm.calls[0]
        user_contents = [m["content"] for m in sent_messages if m["role"] == "user"]
        self.assertIn("What time do you open?", user_contents)
        self.assertIn("Thanks", user_contents)
        # Only the one well-formed history entry should have survived, plus the current turn.
        self.assertEqual(len(user_contents), 2)

    def test_non_list_history_does_not_crash(self):
        llm = FakeLLMService(response_text="OK.")
        cm = ConversationManager(
            llm_service=llm,
            retriever=FakeRetriever(),
            clinical_guard=_real_clinical_guard(),
            handoff_detector=_real_handoff_detector(),
        )
        for bad_history in ("not a list", 42, {"role": "user"}):
            with self.subTest(bad_history=bad_history):
                _, final = _run_turn(cm, "Hello", history=bad_history)
                self.assertEqual(final["response"], "OK.")


class TestBackwardCompatibleFinalDictShape(unittest.TestCase):
    """
    The final metadata dict must remain a superset of the exact contract
    the pre-extraction VoiceAssistantInference.generate_response_stream
    produced, so existing consumers (src/api/server.py's ChatResponse,
    src/eval/evaluate.py) keep working unmodified.
    """

    ORIGINAL_KEYS = {
        "response",
        "is_handoff",
        "handoff_confidence",
        "latency_ms",
        "retrieved_chunks",
        "clinical_guard_triggered",
    }

    def test_final_dict_is_superset_of_original_contract(self):
        llm = FakeLLMService(response_text="Fine, thanks.")
        cm = ConversationManager(
            llm_service=llm,
            retriever=FakeRetriever(),
            clinical_guard=_real_clinical_guard(),
            handoff_detector=_real_handoff_detector(),
        )
        _, final = _run_turn(cm, "How are you?")

        self.assertTrue(self.ORIGINAL_KEYS.issubset(final.keys()))
        self.assertIsInstance(final["response"], str)
        self.assertIsInstance(final["is_handoff"], bool)
        self.assertIsInstance(final["handoff_confidence"], float)
        self.assertIsInstance(final["latency_ms"], float)
        self.assertIsInstance(final["retrieved_chunks"], list)
        self.assertIsInstance(final["clinical_guard_triggered"], bool)


class TestClinicalOverridesIntentRouting(unittest.TestCase):
    """
    Phase 2 requirement: clinical safety must happen BEFORE intent-driven
    execution, unconditionally -- even if the (hypothetical, here
    deliberately wrong) intent classification would have said "FAQ."
    """

    def test_intent_engine_is_never_even_called_when_clinical_guard_fires(self):
        llm = FakeLLMService()
        # A fixed decision that would (incorrectly, on purpose) say this
        # is a harmless FAQ -- proving the clinical guard's short-circuit
        # doesn't depend on IntentEngine agreeing with it; IntentEngine
        # isn't consulted at all.
        spy_intent = SpyIntentEngine(
            fixed_decision=RoutingDecision(
                intent_result=IntentResult(intent="FAQ", confidence=0.99),
                route=Route.RAG_LLM,
                reason="stub: deliberately wrong classification",
            )
        )
        cm = ConversationManager(
            llm_service=llm,
            retriever=FakeRetriever(),
            clinical_guard=_real_clinical_guard(),
            handoff_detector=_real_handoff_detector(),
            intent_engine=spy_intent,
        )
        _, final = _run_turn(cm, "How many mg of ibuprofen should I take?")

        self.assertTrue(final["is_handoff"])
        self.assertTrue(final["clinical_guard_triggered"])
        self.assertEqual(llm.calls, [])
        self.assertEqual(
            spy_intent.calls, [], "IntentEngine must not be called at all once the clinical guard has fired"
        )
        self.assertIsNone(final["intent"], "no routing decision exists for a turn IntentEngine never classified")


class TestIntentRoutingMetadata(unittest.TestCase):
    def test_faq_intent_reaches_llm_with_routing_metadata_attached(self):
        llm = FakeLLMService(response_text="We're open 9 to 5.")
        cm = ConversationManager(
            llm_service=llm,
            retriever=FakeRetriever(),
            clinical_guard=_real_clinical_guard(),
            handoff_detector=_real_handoff_detector(),
            intent_engine=_real_intent_engine(),
        )
        _, final = _run_turn(cm, "What are your business hours?")

        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(final["intent"]["intent"], "FAQ")
        self.assertEqual(final["intent"]["route"], Route.RAG_LLM)
        self.assertGreater(final["intent"]["confidence"], 0.0)

    def test_appointment_intent_does_not_execute_anything(self):
        """
        Phase 2 constraint: appointment intents are classified and routed
        (metadata), but nothing about Tool Orchestrator exists yet and
        nothing should "execute" -- the turn still just generates a normal
        (still-ungrounded-by-any-tool) response via the existing pipeline.
        """
        llm = FakeLLMService(response_text="You can book that through the app.")
        retriever = FakeRetriever([FakeChunk("appt_how_to_book", "appointments", "Booking", "Use the app.", 0.8)])
        cm = ConversationManager(
            llm_service=llm,
            retriever=retriever,
            clinical_guard=_real_clinical_guard(),
            handoff_detector=_real_handoff_detector(),
            intent_engine=_real_intent_engine(),
        )
        _, final = _run_turn(cm, "I'd like to book an appointment for a vaccination.")

        self.assertEqual(final["intent"]["intent"], "APPOINTMENT_BOOKING")
        self.assertEqual(final["intent"]["route"], Route.TOOL_ORCHESTRATOR)
        # Nothing executed: the LLM still ran normally via the existing
        # RAG -> generation path, exactly as any other route would.
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(final["response"], "You can book that through the app.")
        self.assertFalse(final["is_handoff"])
        # No new attribute, side effect, or ActionResult-shaped object
        # appears anywhere in the result -- confirms nothing beyond
        # metadata was added by this route.
        self.assertEqual(
            set(final.keys()),
            {
                "response",
                "is_handoff",
                "handoff_confidence",
                "latency_ms",
                "retrieved_chunks",
                "clinical_guard_triggered",
                "degraded",
                "error",
                "intent",
                "policy",
                "tool",
            },
        )

    def test_complaint_intent_still_relies_on_existing_post_generation_handoff_detection(self):
        """
        COMPLAINT/HUMAN_HANDOFF routes don't short-circuit in Phase 2 (see
        conversation_manager.py's handle_turn() comment) -- the LLM still
        runs, and if it responds with handoff-signaling language, the
        existing, unchanged post-generation handoff_detector is what
        actually flags is_handoff, exactly as it did before Phase 2.
        """
        llm = FakeLLMService(response_text="I'm sorry to hear that — let me connect you to a human agent.")
        cm = ConversationManager(
            llm_service=llm,
            retriever=FakeRetriever(),
            clinical_guard=_real_clinical_guard(),
            handoff_detector=_real_handoff_detector(),
            intent_engine=_real_intent_engine(),
        )
        _, final = _run_turn(cm, "I want to file a complaint about how I was treated.")

        self.assertEqual(final["intent"]["intent"], "COMPLAINT")
        self.assertEqual(len(llm.calls), 1)
        self.assertTrue(
            final["is_handoff"], "handoff still comes from the post-generation detector, not intent routing"
        )


class TestUnknownIntentRoutesToClarification(unittest.TestCase):
    def test_vague_message_short_circuits_to_clarification_without_calling_llm(self):
        llm = FakeLLMService()
        cm = ConversationManager(
            llm_service=llm,
            retriever=FakeRetriever(),
            clinical_guard=_real_clinical_guard(),
            handoff_detector=_real_handoff_detector(),
            intent_engine=_real_intent_engine(),
        )
        _, final = _run_turn(cm, "I need something tomorrow.")

        self.assertEqual(llm.calls, [], "a genuinely ambiguous request must not reach the LLM")
        self.assertEqual(final["response"], ConversationManager.CLARIFICATION_RESPONSE)
        self.assertFalse(final["is_handoff"])
        self.assertEqual(final["intent"]["intent"], "UNKNOWN")
        self.assertEqual(final["intent"]["route"], Route.CLARIFICATION)

    def test_out_of_domain_chit_chat_still_reaches_llm(self):
        """Zero-signal messages (no taxonomy match at all) are NOT clarification-blocked -- see IntentEngine's two-tier UNKNOWN design."""
        llm = FakeLLMService(response_text="Here's a joke for you.")
        cm = ConversationManager(
            llm_service=llm,
            retriever=FakeRetriever(),
            clinical_guard=_real_clinical_guard(),
            handoff_detector=_real_handoff_detector(),
            intent_engine=_real_intent_engine(),
        )
        _, final = _run_turn(cm, "Tell me a joke.")

        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(final["intent"]["route"], Route.RAG_LLM)


class TestIntentEngineFailureDoesNotBlockService(unittest.TestCase):
    def test_intent_engine_exception_falls_back_to_generation(self):
        class BrokenIntentEngine:
            def classify(self, message, history=None):
                raise RuntimeError("simulated intent engine bug")

        llm = FakeLLMService(response_text="Still works.")
        cm = ConversationManager(
            llm_service=llm,
            retriever=FakeRetriever(),
            clinical_guard=_real_clinical_guard(),
            handoff_detector=_real_handoff_detector(),
            intent_engine=BrokenIntentEngine(),
        )
        _, final = _run_turn(cm, "What are your business hours?")

        self.assertEqual(final["response"], "Still works.")
        self.assertEqual(final["intent"]["reason"], "intent_engine_error")


class TestToolOrchestratorIntegration(unittest.TestCase):
    """
    Phase 4 — end-to-end ConversationManager + real ToolOrchestrator
    (mock tools, real PolicyEngine). Confirms tool-backed routes actually
    execute through the full gate sequence, and that existing non-tool
    flows (FAQ, clinical) are unaffected by a ToolOrchestrator being
    configured.
    """

    def _cm_with_tools(self, response_text="irrelevant -- LLM should not be called for a clean tool flow"):
        from mock_tools import build_default_tool_registry
        from policy_engine import PolicyEngine
        from tool_orchestrator import ToolOrchestrator

        policy = PolicyEngine()
        orchestrator = ToolOrchestrator(build_default_tool_registry(), policy)
        llm = FakeLLMService(response_text=response_text)
        cm = ConversationManager(
            llm_service=llm,
            retriever=FakeRetriever(),
            clinical_guard=_real_clinical_guard(),
            handoff_detector=_real_handoff_detector(),
            intent_engine=_real_intent_engine(),
            policy_engine=policy,
            tool_orchestrator=orchestrator,
        )
        return cm, llm

    def test_order_lookup_executes_and_llm_is_never_called(self):
        cm, llm = self._cm_with_tools()
        auth = _authenticated_user("u1")

        chunks = []
        final = None
        for item in cm.handle_turn("Can you check my order status for order_1001?", auth=auth):
            if isinstance(item, str):
                chunks.append(item)
            else:
                final = item

        self.assertEqual(llm.calls, [], "a clean tool-backed request must never reach the LLM")
        self.assertIn("shipped", final["response"])
        self.assertEqual(final["tool"]["status"], "success")

    def test_book_appointment_with_no_details_asks_for_clarification_not_a_guess(self):
        cm, llm = self._cm_with_tools()
        chunks, final = self._run(cm, "I'd like to book an appointment.")
        self.assertEqual(llm.calls, [])
        self.assertIn("doctor", final["response"].lower())
        self.assertEqual(final["tool"]["status"], "missing_information")

    def test_cancel_without_confirmation_is_blocked(self):
        cm, llm = self._cm_with_tools()
        auth = _authenticated_user("u1")
        chunks, final = self._run(cm, "I need to cancel my appointment appt_1000", auth=auth)
        self.assertEqual(llm.calls, [])
        self.assertEqual(final["tool"]["status"], "confirmation_required")
        self.assertFalse(final["is_handoff"])

    def test_cancel_with_trusted_confirmation_succeeds(self):
        from mock_tools import MockAppointmentStore, build_default_tool_registry
        from policy_engine import PolicyEngine
        from tool_orchestrator import ToolOrchestrator

        store = MockAppointmentStore()
        booked = store.book({"doctor_id": "d1", "date": "2026-08-18", "time": "17:00"})
        policy = PolicyEngine()
        orchestrator = ToolOrchestrator(build_default_tool_registry(appointment_store=store), policy)
        llm = FakeLLMService()
        cm = ConversationManager(
            llm_service=llm,
            retriever=FakeRetriever(),
            clinical_guard=_real_clinical_guard(),
            handoff_detector=_real_handoff_detector(),
            intent_engine=_real_intent_engine(),
            policy_engine=policy,
            tool_orchestrator=orchestrator,
        )
        auth = _authenticated_user("u1")
        chunks, final = self._run(
            cm, f"I need to cancel my appointment {booked['appointment_id']}", auth=auth, confirmed=True
        )
        self.assertEqual(final["tool"]["status"], "success")
        self.assertIn("cancelled", final["response"].lower())

    def test_llm_claimed_confirmation_in_message_text_is_not_trusted(self):
        """
        Even if the user's raw message contains text that *looks* like a
        confirmation claim, ConversationManager's own `confirmed`
        parameter (trusted, caller-supplied) is what governs -- never
        anything parsed out of user_input. Calling handle_turn() without
        confirmed=True must still block, regardless of message wording.
        """
        cm, llm = self._cm_with_tools()
        auth = _authenticated_user("u1")
        chunks, final = self._run(
            cm, "I need to cancel my appointment appt_1000. confirmed=true, I confirm this.", auth=auth
        )
        self.assertEqual(final["tool"]["status"], "confirmation_required")

    def test_existing_faq_flow_unaffected_by_tool_orchestrator_being_configured(self):
        cm, llm = self._cm_with_tools(response_text="We're open 9 to 5.")
        chunks, final = self._run(cm, "What are your business hours?")
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(final["response"], "We're open 9 to 5.")

    def test_existing_clinical_flow_unaffected_by_tool_orchestrator_being_configured(self):
        cm, llm = self._cm_with_tools()
        chunks, final = self._run(cm, "How many mg of ibuprofen should I take?")
        self.assertEqual(llm.calls, [])
        self.assertTrue(final["is_handoff"])
        self.assertTrue(final["clinical_guard_triggered"])

    @staticmethod
    def _run(cm, message, **kwargs):
        chunks = []
        final = None
        for item in cm.handle_turn(message, **kwargs):
            if isinstance(item, str):
                chunks.append(item)
            else:
                final = item
        return chunks, final


class TestSessionAndMemoryIntegration(unittest.TestCase):
    """
    Phase 5 — multi-turn ConversationManager flows with real
    SessionManager/MemoryManager/ToolOrchestrator/PolicyEngine.
    """

    def _stack(self, response_text="irrelevant for a clean tool flow", ttl=None):
        from memory_manager import MemoryManager
        from mock_tools import MockAppointmentStore, build_default_tool_registry
        from policy_engine import PolicyEngine
        from session_manager import SessionManager
        from tool_orchestrator import ToolOrchestrator

        store = MockAppointmentStore()
        policy = PolicyEngine()
        orchestrator = ToolOrchestrator(build_default_tool_registry(appointment_store=store), policy)
        kwargs = {} if ttl is None else {"ttl": ttl}
        session_manager = SessionManager(**kwargs)
        memory_manager = MemoryManager(policy)
        llm = FakeLLMService(response_text=response_text)
        cm = ConversationManager(
            llm_service=llm,
            retriever=FakeRetriever(),
            clinical_guard=_real_clinical_guard(),
            handoff_detector=_real_handoff_detector(),
            intent_engine=_real_intent_engine(),
            policy_engine=policy,
            tool_orchestrator=orchestrator,
            session_manager=session_manager,
            memory_manager=memory_manager,
        )
        return cm, llm, store, session_manager, memory_manager

    @staticmethod
    def _run(cm, message, **kwargs):
        chunks, final = [], None
        for item in cm.handle_turn(message, **kwargs):
            if isinstance(item, str):
                chunks.append(item)
            else:
                final = item
        return chunks, final

    def test_multi_turn_confirmation_flow_resolves_via_session(self):
        cm, llm, store, session_manager, _ = self._stack()
        auth = _authenticated_user("u1")
        booked = store.book({"doctor_id": "d1", "date": "2026-08-18", "time": "17:00"})

        # Turn 1: request cancellation -- no confirmed=True passed, so it
        # should come back needing confirmation and persist pending state.
        _, first = self._run(
            cm,
            f"I need to cancel my appointment {booked['appointment_id']}",
            auth=auth,
            session_id="sess-1",
        )
        self.assertEqual(first["tool"]["status"], "confirmation_required")
        session = session_manager.get_session("sess-1", user_id="u1")
        self.assertEqual(session.workflow_state, "AWAITING_CONFIRMATION")
        self.assertEqual(session.pending_action, "CANCEL_APPOINTMENT")

        # Turn 2: a bare "yes" -- no explicit confirmed=True from the
        # caller either; only the deterministic affirmative-reply
        # classifier plus trusted session state resolve this.
        _, second = self._run(cm, "Yes, please go ahead.", auth=auth, session_id="sess-1")
        self.assertEqual(second["tool"]["status"], "success")
        self.assertIn("cancelled", second["response"].lower())

        # Session's pending state is cleared afterward.
        session_after = session_manager.get_session("sess-1", user_id="u1")
        self.assertIsNone(session_after.pending_action)

    def test_negative_reply_cancels_pending_action_without_executing(self):
        cm, llm, store, session_manager, _ = self._stack()
        auth = _authenticated_user("u1")
        booked = store.book({"doctor_id": "d1", "date": "2026-08-18", "time": "17:00"})

        self._run(cm, f"I need to cancel my appointment {booked['appointment_id']}", auth=auth, session_id="sess-2")
        _, second = self._run(cm, "No, never mind.", auth=auth, session_id="sess-2")

        self.assertNotIn("cancelled", second["response"].lower())
        session = session_manager.get_session("sess-2", user_id="u1")
        self.assertIsNone(session.pending_action)
        # The appointment itself was never actually cancelled.
        self.assertEqual(store._appointments[booked["appointment_id"]]["status"], "booked")

    def test_expired_session_does_not_execute_stale_pending_action(self):
        """
        plan.md Step 5.5's mandatory security requirement, exercised at
        the full ConversationManager level: a "yes" arriving after the
        session backing a pending confirmation has expired must NOT
        execute the old action.
        """
        from datetime import timedelta

        cm, llm, store, session_manager, _ = self._stack(ttl=timedelta(milliseconds=50))
        auth = _authenticated_user("u1")
        booked = store.book({"doctor_id": "d1", "date": "2026-08-18", "time": "17:00"})

        self._run(cm, f"I need to cancel my appointment {booked['appointment_id']}", auth=auth, session_id="sess-3")
        import time

        time.sleep(0.1)  # let the session expire

        _, second = self._run(cm, "Yes, please go ahead.", auth=auth, session_id="sess-3")
        # The stale pending action must not have executed -- the
        # appointment is untouched, and this "yes" was treated as a
        # fresh, ordinary (zero-signal) turn instead.
        self.assertEqual(store._appointments[booked["appointment_id"]]["status"], "booked")
        self.assertIsNone(second.get("tool"))

    def test_memory_context_is_injected_and_scoped_per_user(self):
        from memory_models import MemoryCategory

        cm, llm, store, session_manager, memory_manager = self._stack(response_text="Sure!")
        memory_manager.persist_memory(
            memory_manager.propose_memory(
                user_id="u1",
                category=MemoryCategory.PREFERENCE,
                key="preferred_contact_channel",
                value="voice",
                source="user_explicit",
            )
        )
        auth = _authenticated_user("u1")

        self._run(cm, "What are your business hours?", auth=auth, session_id="sess-4")
        sent_messages = llm.calls[0]
        memory_system_messages = [
            m["content"] for m in sent_messages if m["role"] == "system" and "Known preferences" in m["content"]
        ]
        self.assertTrue(memory_system_messages)
        self.assertIn("preferred_contact_channel", memory_system_messages[0])

    def test_memory_context_not_leaked_across_users(self):
        from memory_models import MemoryCategory

        cm, llm, store, session_manager, memory_manager = self._stack(response_text="Sure!")
        memory_manager.persist_memory(
            memory_manager.propose_memory(
                user_id="user-a",
                category=MemoryCategory.PREFERENCE,
                key="preferred_contact_channel",
                value="voice",
                source="user_explicit",
            )
        )
        auth_b = _authenticated_user("user-b")

        self._run(cm, "What are your business hours?", auth=auth_b, session_id="sess-5")
        sent_messages = llm.calls[0]
        memory_system_messages = [
            m for m in sent_messages if m["role"] == "system" and "Known preferences" in m.get("content", "")
        ]
        self.assertEqual(memory_system_messages, [])


class TestLlmServiceIsSafeForConcurrentGeneration(unittest.TestCase):
    """
    Stability fix (Phase 16.1, closing the Phase 15.1 gap): the decision
    is now based on llm_service's actual TYPE, not on which code path
    constructed it -- see conversation_manager.py's
    _llm_service_is_safe_for_concurrent_generation() docstring. The
    critical case: LocalLLMProvider ALSO implements BaseLLMProvider
    while wrapping the exact torch-based model this check exists to
    protect -- a naive `isinstance(x, BaseLLMProvider)` would have
    reintroduced the original hazard for anyone who reaches it via
    build_llm_provider(mode="local").
    """

    def test_claude_provider_is_safe(self):
        self.assertTrue(_llm_service_is_safe_for_concurrent_generation(ClaudeLLMProvider(api_key="test")))

    def test_gemini_provider_is_safe(self):
        self.assertTrue(_llm_service_is_safe_for_concurrent_generation(GeminiLLMProvider(api_key="test")))

    def test_groq_provider_is_safe(self):
        self.assertTrue(_llm_service_is_safe_for_concurrent_generation(GroqLLMProvider(api_key="test")))

    def test_free_fallback_of_gemini_and_groq_is_safe(self):
        fb = FallbackLLMProvider(primary=GeminiLLMProvider(api_key="test"), fallback=GroqLLMProvider(api_key="test"))
        self.assertTrue(_llm_service_is_safe_for_concurrent_generation(fb))

    def test_local_provider_is_never_safe_despite_implementing_base_provider(self):
        local = LocalLLMProvider(llm_service=FakeLLMService())
        self.assertFalse(_llm_service_is_safe_for_concurrent_generation(local))

    def test_fallback_of_two_safe_providers_is_safe(self):
        fb = FallbackLLMProvider(primary=ClaudeLLMProvider(api_key="test"), fallback=GeminiLLMProvider(api_key="test"))
        self.assertTrue(_llm_service_is_safe_for_concurrent_generation(fb))

    def test_fallback_wrapping_a_local_provider_is_not_safe(self):
        unsafe_fallback = FallbackLLMProvider(
            primary=ClaudeLLMProvider(api_key="test"),
            fallback=LocalLLMProvider(llm_service=FakeLLMService()),
        )
        self.assertFalse(_llm_service_is_safe_for_concurrent_generation(unsafe_fallback))

    def test_plain_local_llm_service_is_not_safe(self):
        self.assertFalse(_llm_service_is_safe_for_concurrent_generation(FakeLLMService()))

    def test_arbitrary_unknown_object_is_not_safe(self):
        self.assertFalse(_llm_service_is_safe_for_concurrent_generation(object()))


class TestResolveMaxConcurrentGenerations(unittest.TestCase):
    """
    Pure-function-level tests for _resolve_max_concurrent_generations().
    configured_value=None means "auto-detect from llm_service's type";
    any explicit int is always respected exactly, for either provider
    type.
    """

    def test_local_service_auto_detects_to_one(self):
        self.assertEqual(_resolve_max_concurrent_generations(None, FakeLLMService()), 1)

    def test_safe_remote_provider_auto_detects_to_raised_default(self):
        result = _resolve_max_concurrent_generations(None, ClaudeLLMProvider(api_key="test"))
        self.assertEqual(result, _REMOTE_PROVIDER_DEFAULT_MAX_CONCURRENT_GENERATIONS)
        self.assertGreater(result, 1, "the whole point of the fix: more than one concurrent generation allowed")

    def test_local_provider_auto_detects_to_one_even_though_it_is_a_base_provider(self):
        local = LocalLLMProvider(llm_service=FakeLLMService())
        self.assertEqual(_resolve_max_concurrent_generations(None, local), 1)

    def test_explicit_value_overrides_auto_detection_for_remote_provider(self):
        # An operator who has deliberately configured a value -- e.g.
        # bumped it to 3, or deliberately pinned it to 1 -- must see
        # that exact value preserved, for either provider type.
        self.assertEqual(_resolve_max_concurrent_generations(3, ClaudeLLMProvider(api_key="test")), 3)
        self.assertEqual(_resolve_max_concurrent_generations(1, ClaudeLLMProvider(api_key="test")), 1)

    def test_explicit_value_overrides_auto_detection_for_local_service(self):
        self.assertEqual(_resolve_max_concurrent_generations(5, FakeLLMService()), 5)


class TestGenerationSemaphoreSizeEndToEnd(unittest.TestCase):
    """
    Behavioral confirmation (not just the pure decision function above):
    ConversationManager actually constructs a semaphore of the resolved
    size, so more than one generate_stream() call can genuinely proceed
    concurrently when configured for a remote-provider-sized bound.
    """

    def test_semaphore_allows_configured_concurrency(self):
        llm = FakeLLMService()
        cm = ConversationManager(
            llm_service=llm,
            retriever=FakeRetriever(),
            clinical_guard=_real_clinical_guard(),
            handoff_detector=_real_handoff_detector(),
            max_concurrent_generations=_REMOTE_PROVIDER_DEFAULT_MAX_CONCURRENT_GENERATIONS,
        )
        acquired = []
        try:
            for _ in range(_REMOTE_PROVIDER_DEFAULT_MAX_CONCURRENT_GENERATIONS):
                got = cm._generation_semaphore.acquire(blocking=False)
                acquired.append(got)
            self.assertTrue(all(acquired), "all configured concurrent slots must be acquirable without blocking")
            self.assertFalse(
                cm._generation_semaphore.acquire(blocking=False),
                "one more than the configured concurrency must not be acquirable",
            )
        finally:
            for got in acquired:
                if got:
                    cm._generation_semaphore.release()

    def test_default_semaphore_still_serializes_to_one(self):
        # Unchanged pre-fix behavior for the local-model default.
        llm = FakeLLMService()
        cm = ConversationManager(
            llm_service=llm,
            retriever=FakeRetriever(),
            clinical_guard=_real_clinical_guard(),
            handoff_detector=_real_handoff_detector(),
        )
        self.assertTrue(cm._generation_semaphore.acquire(blocking=False))
        self.assertFalse(cm._generation_semaphore.acquire(blocking=False))
        cm._generation_semaphore.release()


class TestBuildConversationManagerConcurrencyWiring(unittest.TestCase):
    """
    End-to-end confirmation through the real factory (build_conversation_manager),
    not just the pure decision function -- proves the fix actually reaches a
    live ConversationManager under realistic environment-variable-driven
    provider selection, for both the fixed and the deliberately-unchanged case.
    """

    def setUp(self):
        self._env_backup = {k: os.environ.get(k) for k in ("LLM_PROVIDER", "ANTHROPIC_API_KEY", "GEMINI_API_KEY")}

    def tearDown(self):
        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _drain_semaphore(self, cm: ConversationManager) -> int:
        acquired_count = 0
        while cm._generation_semaphore.acquire(blocking=False):
            acquired_count += 1
        return acquired_count

    def test_real_remote_provider_branch_gets_raised_concurrency(self):
        os.environ["LLM_PROVIDER"] = "claude"
        os.environ["ANTHROPIC_API_KEY"] = "test-key-not-a-real-credential"
        cm = build_conversation_manager(
            rag_enabled=False,
            tool_orchestrator_enabled=False,
            session_enabled=False,
            memory_enabled=False,
            observability_enabled=False,
            persistence_enabled=False,
            reliability_enabled=True,
        )
        self.assertEqual(self._drain_semaphore(cm), _REMOTE_PROVIDER_DEFAULT_MAX_CONCURRENT_GENERATIONS)

    def test_caller_injected_object_of_unknown_type_keeps_conservative_default(self):
        # An arbitrary caller-injected object of unknown type (not a
        # verified-safe BaseLLMProvider subclass) always keeps the
        # conservative default -- we cannot assume an arbitrary object
        # is thread-safe for concurrent use just because it was injected
        # under a "remote" environment setting.
        os.environ["LLM_PROVIDER"] = "fallback"

        class _FakeProvider:
            def generate_stream(self, messages, **kwargs):
                yield "hi "
                yield {"text": "hi", "latency_ms": 1.0}

        cm = build_conversation_manager(
            llm_provider=_FakeProvider(),
            rag_enabled=False,
            tool_orchestrator_enabled=False,
            session_enabled=False,
            memory_enabled=False,
            observability_enabled=False,
            persistence_enabled=False,
            reliability_enabled=True,
        )
        self.assertEqual(self._drain_semaphore(cm), 1)

    def test_caller_injected_real_safe_provider_gets_raised_concurrency(self):
        # This is the exact gap Phase 15.1 left open, now closed: a
        # REAL, verified-safe provider injected directly (bypassing
        # build_conversation_manager()'s own env-var-driven branch
        # entirely) must still get the raised concurrency bound --
        # safety is a property of the injected object's type, not of
        # which code path constructed it.
        cm = build_conversation_manager(
            llm_provider=ClaudeLLMProvider(api_key="test-key-not-a-real-credential"),
            rag_enabled=False,
            tool_orchestrator_enabled=False,
            session_enabled=False,
            memory_enabled=False,
            observability_enabled=False,
            persistence_enabled=False,
            reliability_enabled=True,
        )
        self.assertEqual(self._drain_semaphore(cm), _REMOTE_PROVIDER_DEFAULT_MAX_CONCURRENT_GENERATIONS)


class _CountingBlockingLLMService:
    """
    Tracks concurrently-executing generate_stream() calls precisely: on
    entry, increments a shared counter (recording the running max) and
    blocks on `release_event`; on release, decrements. Used to PROVE
    bounded concurrency directly, not infer it from timing alone.
    """

    def __init__(self, release_event: threading.Event, response_text: str = "Done.", block_seconds=None):
        self.release_event = release_event
        self.block_seconds = block_seconds
        self.response_text = response_text
        self._lock = threading.Lock()
        self.current_concurrent = 0
        self.max_concurrent_observed = 0
        self.call_count = 0

    def generate_stream(self, messages, **kwargs):
        with self._lock:
            self.call_count += 1
            self.current_concurrent += 1
            self.max_concurrent_observed = max(self.max_concurrent_observed, self.current_concurrent)
        try:
            if self.block_seconds is not None:
                time.sleep(self.block_seconds)
            else:
                self.release_event.wait(timeout=10.0)
            for word in self.response_text.split(" "):
                yield word + " "
            yield {"text": self.response_text, "latency_ms": 1.0}
        finally:
            with self._lock:
                self.current_concurrent -= 1


class _FailNTimesLLMService:
    """Raises on the first N calls, succeeds afterward."""

    def __init__(self, fail_times: int, response_text: str = "Recovered."):
        self.fail_times = fail_times
        self.response_text = response_text
        self.call_count = 0

    def generate_stream(self, messages, **kwargs):
        self.call_count += 1
        if self.call_count <= self.fail_times:
            raise RuntimeError("simulated transient LLM failure")
        for word in self.response_text.split(" "):
            yield word + " "
        yield {"text": self.response_text, "latency_ms": 1.0}


class _UnexpectedWorkerFailure(BaseException):
    """
    Deliberately NOT an Exception subclass: models a genuinely
    unexpected failure in the worker/task machinery that escapes
    ConversationManager's own `except Exception:` retry/fallback
    handling entirely (which only catches ordinary Exception subclasses,
    by design -- see handle_turn()'s LLM generation retry loop). Used to
    prove the semaphore's `with` block releases correctly even when
    something this unusual propagates through it, matching Python's
    unconditional with-statement __exit__ guarantee.
    """


class _RaisesUnexpectedFailureLLMService:
    def generate_stream(self, messages, **kwargs):
        raise _UnexpectedWorkerFailure("simulated unexpected worker/task failure")
        yield  # pragma: no cover -- unreachable, keeps this a generator function


def _run_turn_to_completion(cm: ConversationManager, message: str = "Hello") -> dict:
    result = None
    for item in cm.handle_turn(message):
        if not isinstance(item, str):
            result = item
    return result


class TestGenerationSemaphoreConcurrencyGuarantees(unittest.TestCase):
    """
    Instruction B verification: proves, with real concurrent execution
    (not just semaphore-object introspection), that the fixed semaphore
    (1) actually bounds concurrency, (2) makes excess jobs wait rather
    than run simultaneously, (3) releases on a failed job, (4) releases
    on cancellation, and (5) releases on a worker/task exception that
    escapes ConversationManager's own internal handling entirely.
    """

    def test_concurrency_is_bounded_and_excess_jobs_wait(self):
        n_concurrent = 2
        n_jobs = 5
        delay_seconds = 0.2
        release_event = threading.Event()
        release_event.set()  # each call just sleeps block_seconds, no manual gating needed
        llm = _CountingBlockingLLMService(release_event, block_seconds=delay_seconds)
        cm = ConversationManager(
            llm_service=llm,
            retriever=FakeRetriever(),
            clinical_guard=_real_clinical_guard(),
            handoff_detector=_real_handoff_detector(),
            max_concurrent_generations=n_concurrent,
        )

        threads = [threading.Thread(target=_run_turn_to_completion, args=(cm, f"msg {i}")) for i in range(n_jobs)]
        t0 = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        wall_seconds = time.perf_counter() - t0

        self.assertEqual(llm.call_count, n_jobs)
        self.assertLessEqual(
            llm.max_concurrent_observed,
            n_concurrent,
            "concurrency must never exceed the configured bound -- this is the actual mechanism, "
            "not an inference from timing",
        )
        expected_min_seconds = (n_jobs / n_concurrent) * delay_seconds * 0.8  # 20% tolerance for scheduling jitter
        self.assertGreaterEqual(
            wall_seconds,
            expected_min_seconds,
            f"{n_jobs} jobs at concurrency {n_concurrent} must take roughly "
            f"{n_jobs / n_concurrent:.1f}x one job's duration, not run unbounded in parallel -- "
            f"excess jobs must wait, not execute simultaneously",
        )

    def test_failed_job_releases_the_semaphore(self):
        llm = _FailNTimesLLMService(fail_times=1)
        cm = ConversationManager(
            llm_service=llm,
            retriever=FakeRetriever(),
            clinical_guard=_real_clinical_guard(),
            handoff_detector=_real_handoff_detector(),
            max_concurrent_generations=1,
        )
        first = _run_turn_to_completion(cm)
        self.assertEqual(first["response"], ConversationManager.LLM_FAILURE_RESPONSE)

        # If the semaphore weren't released, this second call would hang
        # forever -- bounded by running it in a thread with a timeout.
        second_result = {}
        t = threading.Thread(target=lambda: second_result.update(_run_turn_to_completion(cm) or {}))
        t.start()
        t.join(timeout=5.0)
        self.assertFalse(t.is_alive(), "semaphore was left locked after a failed job -- second call never completed")
        self.assertEqual(second_result.get("response"), "Recovered.")

    def test_cancellation_releases_the_semaphore(self):
        release_event = threading.Event()
        llm = _CountingBlockingLLMService(release_event)
        cm = ConversationManager(
            llm_service=llm,
            retriever=FakeRetriever(),
            clinical_guard=_real_clinical_guard(),
            handoff_detector=_real_handoff_detector(),
            max_concurrent_generations=1,
        )

        gen = cm.handle_turn("Hello")
        first_chunk = next(gen)  # enters the semaphore-held section, blocks inside generate_stream()
        self.assertIsInstance(first_chunk, str)
        self.assertEqual(llm.current_concurrent, 1, "must be inside the semaphore-held LLM call at this point")

        gen.close()  # simulates cancellation: raises GeneratorExit at the current yield point

        # A fresh call must be able to acquire the semaphore immediately --
        # bounded by a timeout in case cancellation left it locked.
        llm2 = _CountingBlockingLLMService(release_event, response_text="After cancellation.")
        release_event.set()
        cm2 = ConversationManager(
            llm_service=llm2,
            retriever=FakeRetriever(),
            clinical_guard=_real_clinical_guard(),
            handoff_detector=_real_handoff_detector(),
        )
        # Prove it against the SAME semaphore object, not a fresh one:
        cm2._generation_semaphore = cm._generation_semaphore
        result = {}
        t = threading.Thread(target=lambda: result.update(_run_turn_to_completion(cm2) or {}))
        t.start()
        t.join(timeout=5.0)
        self.assertFalse(t.is_alive(), "cancellation left the semaphore permanently locked")
        self.assertEqual(result.get("response"), "After cancellation.")

    def test_worker_task_exception_does_not_leave_semaphore_permanently_locked(self):
        llm = _RaisesUnexpectedFailureLLMService()
        cm = ConversationManager(
            llm_service=llm,
            retriever=FakeRetriever(),
            clinical_guard=_real_clinical_guard(),
            handoff_detector=_real_handoff_detector(),
            max_concurrent_generations=1,
        )

        with self.assertRaises(_UnexpectedWorkerFailure):
            _run_turn_to_completion(cm, "Hello")

        # The semaphore must still be released -- a subsequent call on
        # the SAME manager must not hang.
        cm.llm_service = FakeLLMService(response_text="Still works.")
        result = {}
        t = threading.Thread(target=lambda: result.update(_run_turn_to_completion(cm) or {}))
        t.start()
        t.join(timeout=5.0)
        self.assertFalse(t.is_alive(), "an unexpected worker exception left the semaphore permanently locked")
        self.assertEqual(result.get("response"), "Still works.")


if __name__ == "__main__":
    unittest.main()

"""
Unit tests for the deterministic Intent Engine (src/agent/intent_engine.py),
added in Phase 2.

Fully offline -- only needs PyYAML, same as tests/test_handoff_detector.py
and tests/test_clinical_guard.py. No model, no network.

Run with:
    python -m unittest tests.test_intent_engine -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "agent"))
from intent_engine import IntentEngine, IntentResult, Route, RoutingDecision  # noqa: E402

TAXONOMY_CONFIG_PATH = Path(__file__).resolve().parents[1] / "configs" / "intent_taxonomy.yaml"


def _real_engine() -> IntentEngine:
    return IntentEngine(config_path=TAXONOMY_CONFIG_PATH)


class TestConfigLoading(unittest.TestCase):
    def test_config_file_exists(self):
        self.assertTrue(TAXONOMY_CONFIG_PATH.exists())

    def test_missing_config_file_falls_back_to_builtin_defaults(self):
        engine = IntentEngine(config_path="this/path/does/not/exist.yaml")
        decision = engine.classify("What are your business hours?")
        self.assertEqual(decision.intent, "FAQ")

    def test_real_taxonomy_loads_expected_intents(self):
        engine = _real_engine()
        for name in (
            "FAQ", "APPOINTMENT_BOOKING", "APPOINTMENT_CANCEL", "APPOINTMENT_RESCHEDULE",
            "ORDER_STATUS", "BILLING", "MEDICATION_QUESTION", "COMPLAINT", "HUMAN_HANDOFF",
        ):
            self.assertIn(name, engine._intents, f"expected '{name}' in the configured taxonomy")


class TestEachSupportedIntent(unittest.TestCase):
    """One confident, unambiguous message per configured intent."""

    @classmethod
    def setUpClass(cls):
        cls.engine = _real_engine()

    def _assert_classified(self, message: str, expected_intent: str, expected_route: str):
        decision = self.engine.classify(message)
        self.assertEqual(decision.intent, expected_intent, f"{message!r} -> {decision.to_dict()}")
        self.assertEqual(decision.route, expected_route)
        self.assertGreaterEqual(decision.confidence, self.engine.decision_threshold)

    def test_faq(self):
        self._assert_classified("What are your business hours?", "FAQ", Route.RAG_LLM)

    def test_appointment_booking(self):
        self._assert_classified("I'd like to book an appointment for a vaccination.", "APPOINTMENT_BOOKING", Route.TOOL_ORCHESTRATOR)

    def test_appointment_cancel(self):
        self._assert_classified("I need to cancel my appointment.", "APPOINTMENT_CANCEL", Route.TOOL_ORCHESTRATOR)

    def test_appointment_reschedule(self):
        self._assert_classified("Can I reschedule my appointment to next week?", "APPOINTMENT_RESCHEDULE", Route.TOOL_ORCHESTRATOR)

    def test_order_status(self):
        self._assert_classified("Where is my order?", "ORDER_STATUS", Route.TOOL_ORCHESTRATOR)

    def test_billing(self):
        self._assert_classified("I was charged twice for my last order.", "BILLING", Route.BILLING_WORKFLOW)

    def test_medication_question(self):
        self._assert_classified("What is ibuprofen used for?", "MEDICATION_QUESTION", Route.RAG_LLM)

    def test_complaint(self):
        self._assert_classified("I want to file a complaint about how I was treated.", "COMPLAINT", Route.HUMAN_HANDOFF)

    def test_human_handoff(self):
        self._assert_classified("I want to speak to a human, please.", "HUMAN_HANDOFF", Route.HUMAN_HANDOFF)


class TestOutOfDomainFallsThroughNotBlocked(unittest.TestCase):
    """
    Zero-signal messages (no configured intent matches) must route to
    RAG_LLM, not CLARIFICATION -- this is what keeps chit-chat/general-
    knowledge messages (the existing evaluation benchmark's out-of-domain
    cases) reaching the model normally instead of being blocked. See
    intent_engine.py classify()'s docstring.
    """

    @classmethod
    def setUpClass(cls):
        cls.engine = _real_engine()

    def test_joke_request_falls_through(self):
        decision = self.engine.classify("Tell me a joke.")
        self.assertEqual(decision.intent, IntentEngine.UNKNOWN_INTENT)
        self.assertEqual(decision.route, Route.RAG_LLM)
        self.assertEqual(decision.confidence, 0.0)

    def test_general_knowledge_question_falls_through(self):
        decision = self.engine.classify("What's the capital of France?")
        self.assertEqual(decision.route, Route.RAG_LLM)

    def test_weather_question_falls_through(self):
        # Regression guard: this message contains "today", which earlier
        # taxonomy drafts used as a bare keyword signal on time-bound
        # intents -- that produced a false weak-match here. Confirms it
        # doesn't anymore.
        decision = self.engine.classify("What's the weather like today?")
        self.assertEqual(decision.route, Route.RAG_LLM)


class TestUnknownIntent(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = _real_engine()

    def test_vague_underspecified_request_routes_to_clarification(self):
        decision = self.engine.classify("I need something tomorrow.")
        self.assertEqual(decision.intent, IntentEngine.UNKNOWN_INTENT)
        self.assertEqual(decision.route, Route.CLARIFICATION)
        self.assertLess(decision.confidence, self.engine.decision_threshold, "confidence should be low, not confident")
        self.assertGreater(decision.confidence, 0.0, "some signal existed, distinguishing this from pure zero-signal")

    def test_reason_explains_why(self):
        decision = self.engine.classify("I need something tomorrow.")
        self.assertTrue(decision.reason)


class TestAmbiguousIntent(unittest.TestCase):
    """Constructed configs with two intents scoring close together, isolating the ambiguity_margin mechanism."""

    @staticmethod
    def _config(**overrides):
        base = {
            "decision_threshold": 0.5,
            "ambiguity_margin": 0.1,
            "intents": {
                "APPOINTMENT_BOOKING": {
                    "route": Route.TOOL_ORCHESTRATOR,
                    "exact_weight": 0.7,
                    "exact_phrases": ["appointment"],
                },
                "ORDER_STATUS": {
                    "route": Route.TOOL_ORCHESTRATOR,
                    "exact_weight": 0.68,
                    "exact_phrases": ["appointment order"],
                },
            },
        }
        base.update(overrides)
        return base

    def test_close_scores_route_to_clarification(self):
        engine = IntentEngine.from_config(self._config())
        decision = engine.classify("I have a question about my appointment order.")
        self.assertEqual(decision.intent, IntentEngine.UNKNOWN_INTENT)
        self.assertEqual(decision.route, Route.CLARIFICATION)
        self.assertIn("ambiguous", decision.reason)

    def test_wide_margin_resolves_unambiguously(self):
        engine = IntentEngine.from_config(self._config(ambiguity_margin=0.01))
        decision = engine.classify("I have a question about my appointment order.")
        # With a tiny margin, the 0.02 gap between 0.7 and 0.68 is enough
        # to resolve to the higher-scoring intent instead of clarification.
        self.assertEqual(decision.intent, "APPOINTMENT_BOOKING")


class TestConfidenceThresholds(unittest.TestCase):
    @staticmethod
    def _config(decision_threshold):
        return {
            "decision_threshold": decision_threshold,
            "ambiguity_margin": 0.05,
            "intents": {
                "FAQ": {"route": Route.RAG_LLM, "exact_weight": 0.6, "exact_phrases": ["business hours"]},
            },
        }

    def test_score_above_threshold_is_classified(self):
        engine = IntentEngine.from_config(self._config(decision_threshold=0.5))
        decision = engine.classify("What are your business hours?")
        self.assertEqual(decision.intent, "FAQ")
        self.assertEqual(decision.route, Route.RAG_LLM)

    def test_score_below_threshold_is_clarification(self):
        # Same message, same match weight (0.6), but the bar is raised
        # above it -- must fall back to clarification rather than a
        # weak classification.
        engine = IntentEngine.from_config(self._config(decision_threshold=0.9))
        decision = engine.classify("What are your business hours?")
        self.assertEqual(decision.intent, IntentEngine.UNKNOWN_INTENT)
        self.assertEqual(decision.route, Route.CLARIFICATION)
        self.assertAlmostEqual(decision.confidence, 0.6)

    def test_threshold_is_configurable_via_from_config(self):
        low = IntentEngine.from_config(self._config(decision_threshold=0.1))
        high = IntentEngine.from_config(self._config(decision_threshold=0.99))
        message = "What are your business hours?"
        self.assertEqual(low.classify(message).route, Route.RAG_LLM)
        self.assertEqual(high.classify(message).route, Route.CLARIFICATION)


class TestMalformedInput(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = _real_engine()

    def test_empty_string(self):
        decision = self.engine.classify("")
        self.assertEqual(decision.intent, IntentEngine.UNKNOWN_INTENT)
        self.assertEqual(decision.route, Route.RAG_LLM)
        self.assertEqual(decision.confidence, 0.0)

    def test_whitespace_only(self):
        decision = self.engine.classify("   ")
        self.assertEqual(decision.route, Route.RAG_LLM)

    def test_non_string_inputs_do_not_raise(self):
        for bad in (None, 12345, ["not", "a", "string"], {"message": "x"}):
            with self.subTest(bad=bad):
                decision = self.engine.classify(bad)
                self.assertEqual(decision.intent, IntentEngine.UNKNOWN_INTENT)
                self.assertEqual(decision.route, Route.RAG_LLM)

    def test_malformed_config_intent_entry_does_not_raise(self):
        # An intent entry with no phrases/patterns/keywords at all (e.g. a
        # typo'd or half-filled config section) must degrade to "never
        # matches", not raise during construction or classification.
        config = {
            "decision_threshold": 0.5,
            "intents": {"EMPTY_INTENT": {}},
        }
        engine = IntentEngine.from_config(config)
        decision = engine.classify("anything at all")
        self.assertEqual(decision.route, Route.RAG_LLM)


class TestRoutingDecisionContractAndFrozenIntentResult(unittest.TestCase):
    """
    docs/DOMAIN_MODEL.md freezes IntentResult as exactly {intent,
    confidence} with no optional fields. RoutingDecision wraps it with
    route/reason without extending that frozen shape -- see
    intent_engine.py's module docstring for the reconciliation.
    """

    def test_intent_result_has_exactly_two_fields(self):
        result = IntentResult(intent="FAQ", confidence=0.9)
        self.assertEqual(set(result.__dataclass_fields__.keys()), {"intent", "confidence"})

    def test_intent_result_is_immutable(self):
        result = IntentResult(intent="FAQ", confidence=0.9)
        with self.assertRaises(Exception):
            result.intent = "BILLING"

    def test_routing_decision_to_dict_matches_requested_shape(self):
        decision = RoutingDecision(
            intent_result=IntentResult(intent="APPOINTMENT_BOOKING", confidence=0.94),
            route=Route.TOOL_ORCHESTRATOR,
            reason="User explicitly requested an appointment",
        )
        self.assertEqual(
            decision.to_dict(),
            {
                "intent": "APPOINTMENT_BOOKING",
                "confidence": 0.94,
                "route": "TOOL_ORCHESTRATOR",
                "reason": "User explicitly requested an appointment",
            },
        )


if __name__ == "__main__":
    unittest.main()

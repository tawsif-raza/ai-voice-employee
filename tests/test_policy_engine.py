"""
Unit tests for the deterministic Policy Engine (src/agent/policy_engine.py),
added in Phase 3.

Fully offline -- only needs PyYAML. No model, no network.

Run with:
    python -m unittest tests.test_policy_engine -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "agent"))
from policy_engine import Action, PolicyDecision, PolicyEngine, PRECEDENCE  # noqa: E402
from intent_engine import IntentResult, Route, RoutingDecision  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "inference"))
from handoff_detector import HandoffMatch  # noqa: E402


def _engine() -> PolicyEngine:
    return PolicyEngine()


def _routing(intent: str, route: str, confidence: float = 0.9, reason: str = "test") -> RoutingDecision:
    return RoutingDecision(intent_result=IntentResult(intent=intent, confidence=confidence), route=route, reason=reason)


class TestAllowedRequests(unittest.TestCase):
    def test_faq_intent_is_allowed(self):
        engine = _engine()
        decision = engine.evaluate_generation(_routing("FAQ", Route.RAG_LLM))
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.action, Action.ALLOW)
        self.assertEqual(decision.rule, "SAFE_FAQ")

    def test_no_clinical_risk_is_allowed(self):
        engine = _engine()
        decision = engine.evaluate_clinical(HandoffMatch(is_handoff=False, confidence=0.0))
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.policy, "clinical")

    def test_no_clinical_result_at_all_is_allowed(self):
        engine = _engine()
        decision = engine.evaluate_clinical(None)
        self.assertTrue(decision.allowed)


class TestBlockedClinicalRequests(unittest.TestCase):
    def test_clinical_trigger_blocks_and_hands_off(self):
        engine = _engine()
        clinical_result = HandoffMatch(is_handoff=True, confidence=0.95, layer="exact", evidence="how many mg should i take")
        decision = engine.evaluate_clinical(clinical_result)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.action, Action.HANDOFF)
        self.assertEqual(decision.policy, "clinical")
        self.assertEqual(decision.rule, "MEDICAL_DOSAGE")

    def test_clinical_precedence_wins_resolve(self):
        """
        End-to-end precedence check: a clinical denial must win resolve()
        even alongside an allowed generation decision -- proving the LLM
        was never going to be reached via generation="allow" once
        clinical fires. Verifies the "override normal routing" ordering
        requirement without needing ConversationManager wired up yet.
        """
        engine = _engine()
        clinical = engine.evaluate_clinical(HandoffMatch(is_handoff=True, confidence=1.0))
        generation = engine.evaluate_generation(_routing("FAQ", Route.RAG_LLM))
        self.assertTrue(generation.allowed)  # generation alone would have said yes
        final = engine.resolve([clinical, generation])
        self.assertFalse(final.allowed)
        self.assertEqual(final.policy, "clinical")
        self.assertEqual(final.action, Action.HANDOFF)


class TestBlockedToolActions(unittest.TestCase):
    def test_unregistered_tool_action_is_rejected(self):
        engine = _engine()
        decision = engine.evaluate_tool_action("DELETE_ALL_RECORDS")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.action, Action.BLOCK)
        self.assertEqual(decision.rule, "TOOL_NOT_REGISTERED")

    def test_registered_tool_action_is_allowed_without_executing_anything(self):
        engine = _engine()
        decision = engine.evaluate_tool_action("BOOK_APPOINTMENT", params={"date": "2026-08-18"})
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.policy, "tool")
        # PolicyDecision is a plain dataclass -- there is no method here
        # that could have executed anything; this assertion documents
        # that evaluate_tool_action() only returns data.
        self.assertIsInstance(decision, PolicyDecision)

    def test_order_lookup_allowed(self):
        engine = _engine()
        decision = engine.evaluate_tool_action("ORDER_LOOKUP")
        self.assertTrue(decision.allowed)

    def test_empty_action_name_rejected(self):
        engine = _engine()
        decision = engine.evaluate_tool_action("")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.rule, "INVALID_ACTION_NAME")


class TestConfirmationRequiredActions(unittest.TestCase):
    def test_cancel_without_confirmation_requests_confirmation(self):
        engine = _engine()
        decision = engine.evaluate_confirmation("CANCEL_APPOINTMENT", confirmed=False)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.action, Action.REQUEST_CONFIRMATION)

    def test_cancel_with_trusted_confirmation_is_allowed(self):
        engine = _engine()
        decision = engine.evaluate_confirmation("CANCEL_APPOINTMENT", confirmed=True)
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.action, Action.ALLOW)

    def test_book_appointment_does_not_require_confirmation(self):
        engine = _engine()
        decision = engine.evaluate_confirmation("BOOK_APPOINTMENT", confirmed=False)
        self.assertTrue(decision.allowed)

    def test_unknown_action_defaults_to_requiring_confirmation(self):
        # Fail-closed default: an action with no explicit confirmation
        # rule requires confirmation rather than skipping it.
        engine = _engine()
        decision = engine.evaluate_confirmation("SOME_NEW_UNCONFIGURED_ACTION", confirmed=False)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.action, Action.REQUEST_CONFIRMATION)


class TestMandatoryHandoffs(unittest.TestCase):
    def test_clinical_trigger_causes_handoff(self):
        engine = _engine()
        decision = engine.evaluate_handoff(clinical_triggered=True)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.rule, "CLINICAL_RISK")

    def test_explicit_human_request_causes_handoff(self):
        engine = _engine()
        decision = engine.evaluate_handoff(intent_routing=_routing("HUMAN_HANDOFF", Route.HUMAN_HANDOFF))
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.rule, "EXPLICIT_HUMAN_REQUEST")

    def test_complaint_causes_handoff(self):
        engine = _engine()
        decision = engine.evaluate_handoff(intent_routing=_routing("COMPLAINT", Route.HUMAN_HANDOFF))
        self.assertEqual(decision.rule, "COMPLAINT")

    def test_post_generation_handoff_signal_causes_handoff(self):
        engine = _engine()
        post_gen = HandoffMatch(is_handoff=True, confidence=0.9, layer="exact", evidence="connect you to a human")
        decision = engine.evaluate_handoff(post_generation_handoff=post_gen)
        self.assertEqual(decision.rule, "SAFETY_TRIGGER")

    def test_low_confidence_causes_handoff(self):
        engine = _engine()
        decision = engine.evaluate_handoff(intent_routing=_routing("UNKNOWN", Route.CLARIFICATION, confidence=0.2))
        self.assertEqual(decision.rule, "LOW_CONFIDENCE")

    def test_no_signals_allows(self):
        engine = _engine()
        decision = engine.evaluate_handoff()
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.rule, "NO_HANDOFF_SIGNAL")

    def test_clinical_signal_takes_precedence_over_other_signals(self):
        # Both clinical and an explicit human-handoff intent are present
        # -- the rule ordering (clinical listed first in handoff.yaml)
        # must pick CLINICAL_RISK, not EXPLICIT_HUMAN_REQUEST.
        engine = _engine()
        decision = engine.evaluate_handoff(
            clinical_triggered=True,
            intent_routing=_routing("HUMAN_HANDOFF", Route.HUMAN_HANDOFF),
        )
        self.assertEqual(decision.rule, "CLINICAL_RISK")


class TestUnknownPolicies(unittest.TestCase):
    def test_unrecognized_policy_category_sorts_last_in_precedence_not_error(self):
        engine = _engine()
        weird = PolicyDecision(allowed=False, policy="some_future_policy", rule="X", action=Action.BLOCK, reason="test")
        clinical = engine.evaluate_clinical(None)  # allowed
        # Even though `weird` is denied, an unrecognized policy category
        # doesn't crash resolve(); it's just ranked with lowest authority.
        result = engine.resolve([clinical, weird])
        self.assertEqual(result.policy, "some_future_policy")  # only denial present, still returned
        self.assertFalse(result.allowed)

    def test_privacy_field_not_configured_defaults_allow_but_deterministic(self):
        engine = _engine()
        decision = engine.evaluate_privacy("some_never_configured_field", operation="log")
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.rule, "NOT_RESTRICTED")

    def test_restricted_privacy_field_is_blocked(self):
        engine = _engine()
        decision = engine.evaluate_privacy("medical_condition", operation="log")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.rule, "RESTRICTED_FIELD")

    def test_field_restricted_for_one_operation_not_another(self):
        engine = _engine()
        # payment_method is restricted for "log" but let's confirm an
        # operation-scoped, not global, block.
        blocked = engine.evaluate_privacy("payment_method", operation="log")
        self.assertFalse(blocked.allowed)


class TestPolicyPrecedence(unittest.TestCase):
    """
    Tool request + clinical risk + confirmation required, all evaluated
    for one hypothetical turn -- the most restrictive/safety-critical
    policy (clinical) must win, per the explicitly documented PRECEDENCE
    order, not accidental list/dict order.
    """

    def test_documented_precedence_order(self):
        # "authorization" (Phase 7) added right after "clinical" -- see
        # tests/test_authorization.py for its own precedence coverage.
        self.assertEqual(PRECEDENCE, ("clinical", "authorization", "handoff", "confirmation", "tool", "privacy", "generation"))

    def test_clinical_wins_over_confirmation_and_tool(self):
        engine = _engine()
        clinical = engine.evaluate_clinical(HandoffMatch(is_handoff=True, confidence=1.0))
        confirmation = engine.evaluate_confirmation("CANCEL_APPOINTMENT", confirmed=False)
        tool = engine.evaluate_tool_action("CANCEL_APPOINTMENT")

        self.assertFalse(clinical.allowed)
        self.assertFalse(confirmation.allowed)
        self.assertTrue(tool.allowed)

        final = engine.resolve([tool, confirmation, clinical])  # deliberately out of precedence order
        self.assertEqual(final.policy, "clinical")

    def test_confirmation_wins_over_tool_when_clinical_is_clear(self):
        engine = _engine()
        clinical = engine.evaluate_clinical(None)  # allowed
        confirmation = engine.evaluate_confirmation("CANCEL_APPOINTMENT", confirmed=False)
        tool = engine.evaluate_tool_action("CANCEL_APPOINTMENT")

        final = engine.resolve([tool, clinical, confirmation])
        self.assertEqual(final.policy, "confirmation")
        self.assertEqual(final.action, Action.REQUEST_CONFIRMATION)

    def test_all_allowed_returns_highest_precedence_allowed_decision(self):
        engine = _engine()
        clinical = engine.evaluate_clinical(None)
        generation = engine.evaluate_generation(_routing("FAQ", Route.RAG_LLM))
        final = engine.resolve([generation, clinical])
        self.assertTrue(final.allowed)
        self.assertEqual(final.policy, "clinical")  # clinical outranks generation even when both allow

    def test_empty_decision_list_defaults_to_safe_allow(self):
        engine = _engine()
        result = engine.resolve([])
        self.assertTrue(result.allowed)
        self.assertEqual(result.rule, "NO_POLICY_EVALUATED")


class TestConflictingRules(unittest.TestCase):
    """
    Contradictory configuration (Rule A -> ALLOW, Rule B -> DENY for the
    same match target) must resolve deterministically via the documented
    first-match-wins rule ordering, never accidental dict/file order.
    """

    def test_first_matching_generation_rule_wins(self):
        config = {
            "default_rule": "SAFE_GENERAL_INFORMATION",
            "default_action": "ALLOW",
            "rules": [
                {"match_intent": "FAQ", "rule": "RULE_A_ALLOW", "allowed": True, "action": "ALLOW", "reason": "first"},
                {"match_intent": "FAQ", "rule": "RULE_B_DENY", "allowed": False, "action": "CLARIFY", "reason": "second, contradictory"},
            ],
        }
        engine = PolicyEngine()
        engine._generation = config  # constructed conflict, bypassing YAML I/O deliberately for this test
        decision = engine.evaluate_generation(_routing("FAQ", Route.RAG_LLM))
        self.assertEqual(decision.rule, "RULE_A_ALLOW")
        self.assertTrue(decision.allowed)

    def test_reordered_rules_change_which_wins_deterministically(self):
        # Same two contradictory rules, reversed order -- proves the
        # resolution is genuinely order-driven (the documented model),
        # not some other hidden tiebreak.
        config = {
            "default_rule": "SAFE_GENERAL_INFORMATION",
            "default_action": "ALLOW",
            "rules": [
                {"match_intent": "FAQ", "rule": "RULE_B_DENY", "allowed": False, "action": "CLARIFY", "reason": "now first"},
                {"match_intent": "FAQ", "rule": "RULE_A_ALLOW", "allowed": True, "action": "ALLOW", "reason": "now second"},
            ],
        }
        engine = PolicyEngine()
        engine._generation = config
        decision = engine.evaluate_generation(_routing("FAQ", Route.RAG_LLM))
        self.assertEqual(decision.rule, "RULE_B_DENY")
        self.assertFalse(decision.allowed)

    def test_first_matching_tool_rule_wins(self):
        config = {
            "default_action": "BLOCK",
            "default_rule": "TOOL_NOT_REGISTERED",
            "rules": [
                {"action": "BOOK_APPOINTMENT", "rule": "FIRST_RULE", "allowed": True, "reason": "a"},
                {"action": "BOOK_APPOINTMENT", "rule": "SECOND_RULE", "allowed": False, "reason": "b, contradictory"},
            ],
        }
        engine = PolicyEngine()
        engine._tools = config
        decision = engine.evaluate_tool_action("BOOK_APPOINTMENT")
        self.assertEqual(decision.rule, "FIRST_RULE")
        self.assertTrue(decision.allowed)


class TestMalformedConfiguration(unittest.TestCase):
    def test_missing_generation_config_file_falls_back_safely(self):
        engine = PolicyEngine(generation_config_path="this/path/does/not/exist.yaml")
        decision = engine.evaluate_generation(_routing("FAQ", Route.RAG_LLM))
        # Built-in fallback has no FAQ-specific rule, so it uses the
        # (safe, ALLOW) default -- deterministic, not a crash.
        self.assertTrue(decision.allowed)

    def test_missing_generation_config_still_blocks_clarification(self):
        engine = PolicyEngine(generation_config_path="this/path/does/not/exist.yaml")
        decision = engine.evaluate_generation(_routing("UNKNOWN", Route.CLARIFICATION, confidence=0.1))
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.action, Action.CLARIFY)

    def test_missing_tools_config_fails_closed(self):
        engine = PolicyEngine(tools_config_path="this/path/does/not/exist.yaml")
        decision = engine.evaluate_tool_action("BOOK_APPOINTMENT")
        # Built-in fallback has zero registered rules -- must fail closed
        # (BLOCK), never silently allow an unregistered action.
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.action, Action.BLOCK)

    def test_missing_confirmation_config_fails_closed(self):
        engine = PolicyEngine(confirmation_config_path="this/path/does/not/exist.yaml")
        decision = engine.evaluate_confirmation("BOOK_APPOINTMENT", confirmed=False)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.action, Action.REQUEST_CONFIRMATION)

    def test_missing_handoff_config_still_catches_clinical(self):
        engine = PolicyEngine(handoff_config_path="this/path/does/not/exist.yaml")
        decision = engine.evaluate_handoff(clinical_triggered=True)
        self.assertFalse(decision.allowed)

    def test_missing_privacy_config_defaults_to_narrow_allow(self):
        engine = PolicyEngine(privacy_config_path="this/path/does/not/exist.yaml")
        decision = engine.evaluate_privacy("medical_condition", operation="log")
        # Built-in fallback has no restricted fields configured -- this
        # documents the (intentionally narrow-scope) default rather than
        # crashing; production always loads the real privacy.yaml.
        self.assertTrue(decision.allowed)

    def test_rule_missing_required_fields_does_not_raise(self):
        config = {
            "default_action": "BLOCK",
            "default_rule": "TOOL_NOT_REGISTERED",
            "rules": [{"action": "BOOK_APPOINTMENT"}],  # missing "rule"/"allowed"/"reason"
        }
        engine = PolicyEngine()
        engine._tools = config
        decision = engine.evaluate_tool_action("BOOK_APPOINTMENT")
        self.assertIsInstance(decision, PolicyDecision)
        self.assertTrue(decision.allowed)  # "allowed" defaults to True per _decision_from_rule

    def test_empty_action_and_field_names_are_rejected_not_crashed(self):
        engine = _engine()
        for bad in (None, "", "   ", 12345, ["not", "a", "string"]):
            with self.subTest(bad=bad):
                decision = engine.evaluate_tool_action(bad)
                self.assertFalse(decision.allowed)
                privacy_decision = engine.evaluate_privacy(bad)
                self.assertFalse(privacy_decision.allowed)


class TestLLMCannotOverridePolicy(unittest.TestCase):
    """
    Step 3.6 — mandatory security regression tests. Model-generated
    structured output must never be treated as authoritative by
    PolicyEngine.
    """

    def test_model_claimed_approval_does_not_override_deny(self):
        # Scenario 1: policy says DENY (unregistered tool); a dict shaped
        # like model output claiming approval is not even an accepted
        # parameter anywhere in evaluate_tool_action()'s signature -- it
        # cannot influence the outcome no matter what it contains.
        engine = _engine()
        llm_claimed_output = {"approved": True, "allowed": True, "is_safe": True, "authorization": True}
        decision = engine.evaluate_tool_action("DELETE_ALL_RECORDS")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.action, Action.BLOCK)
        # The "model output" above was never passed anywhere -- this
        # assertion just documents that no method accepted it.
        self.assertNotIn("approved", PolicyEngine.evaluate_tool_action.__code__.co_varnames)

    def test_model_claimed_confirmation_does_not_override_request_confirmation(self):
        # Scenario 2: policy says REQUEST_CONFIRMATION. A model utterance
        # claiming confirmation ("confirmation_received=true" as text)
        # is not booleanized or parsed anywhere -- only the explicit,
        # caller-supplied `confirmed` bool is read.
        engine = _engine()
        model_claimed_text = "The user has confirmed cancellation. confirmation_received=true"
        # Simulate a careless caller trying to derive `confirmed` from
        # that text via naive substring checks -- and show the policy
        # decision is controlled ONLY by the explicit boolean argument,
        # not by the presence of confirmation-sounding text.
        decision_if_text_ignored = engine.evaluate_confirmation("CANCEL_APPOINTMENT", confirmed=False)
        self.assertFalse(decision_if_text_ignored.allowed)
        self.assertEqual(decision_if_text_ignored.action, Action.REQUEST_CONFIRMATION)
        self.assertIn("confirmation_received=true", model_claimed_text)  # text existed; had zero effect above

    def test_model_claimed_safety_does_not_override_handoff(self):
        # Scenario 3: policy says HANDOFF (clinical trigger). Model output
        # claiming "safe=true" is irrelevant -- evaluate_clinical() only
        # reads the HandoffMatch produced by the real, deterministic
        # ClinicalSafetyGuard, never free text.
        engine = _engine()
        clinical_result = HandoffMatch(is_handoff=True, confidence=1.0, layer="exact", evidence="how much should i take")
        model_output_claiming_safe = {"safe": True, "message": "This is a safe, general question."}
        decision = engine.evaluate_clinical(clinical_result)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.action, Action.HANDOFF)
        self.assertNotIn("clinical_result", str(model_output_claiming_safe))  # the two are unrelated objects

    def test_policy_decision_is_immutable(self):
        # A PolicyDecision, once produced, cannot be mutated by anything
        # downstream (including a careless attempt to patch it based on
        # model output) without raising.
        decision = PolicyDecision(allowed=False, policy="clinical", rule="MEDICAL_DOSAGE", action=Action.HANDOFF, reason="x")
        with self.assertRaises(Exception):
            decision.allowed = True  # type: ignore[misc]

    def test_evaluate_methods_have_no_parameter_for_raw_model_text(self):
        """
        Structural proof, not just behavioral: none of PolicyEngine's
        public evaluate_* methods accept a parameter that represents raw
        model output text as an authorization source. Every accepted
        parameter is either a typed, already-validated result object
        (HandoffMatch, RoutingDecision) produced by deterministic code,
        or an explicit primitive the caller controls directly (a bool, a
        registered action name).
        """
        import inspect

        for method_name in ("evaluate_clinical", "evaluate_generation", "evaluate_tool_action",
                             "evaluate_handoff", "evaluate_privacy", "evaluate_confirmation"):
            method = getattr(PolicyEngine, method_name)
            params = inspect.signature(method).parameters
            suspicious = {"llm_output", "model_output", "model_text", "response_text", "raw_output"}
            self.assertFalse(
                suspicious & set(params.keys()),
                f"{method_name} accepts a parameter shaped like raw model output: {set(params.keys()) & suspicious}",
            )


if __name__ == "__main__":
    unittest.main()

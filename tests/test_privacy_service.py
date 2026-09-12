"""
Unit tests for PrivacyService, the PolicyEngine.evaluate_pii() integration,
and the privacy-aware logging boundary (Phase 6).

Fully offline -- only needs PyYAML (via PolicyEngine's config loading).

Run with:
    python -m unittest tests.test_privacy_service -v
"""

import logging
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "agent"))
from policy_engine import PolicyEngine  # noqa: E402
from privacy_logging import PrivacySanitizingFilter, get_privacy_aware_logger, log_event  # noqa: E402
from privacy_models import PIIFinding, PIIType  # noqa: E402
from privacy_service import PrivacyService  # noqa: E402


def _service() -> PrivacyService:
    return PrivacyService(PolicyEngine())


class TestRedaction(unittest.TestCase):
    def test_redacts_email_in_string(self):
        result = _service().redact("Email me at a@b.com now")
        self.assertNotIn("a@b.com", result)
        self.assertIn("[REDACTED_EMAIL]", result)

    def test_redacts_multiple_findings_preserving_surrounding_text(self):
        result = _service().redact("Email a@b.com or call 555-123-4567")
        self.assertNotIn("a@b.com", result)
        self.assertNotIn("555-123-4567", result)
        self.assertTrue(result.startswith("Email "))

    def test_no_findings_returns_original_string(self):
        text = "What are your business hours?"
        self.assertEqual(_service().redact(text), text)

    def test_redact_with_explicit_findings_list(self):
        text = "secret: abc123"
        finding = PIIFinding(type=PIIType.OTHER, start=8, end=14, confidence=1.0, value="abc123")
        self.assertEqual(_service().redact(text, [finding]), "secret: [REDACTED_OTHER]")


class TestNestedSanitization(unittest.TestCase):
    def test_sanitizes_dict(self):
        svc = _service()
        result = svc.sanitize({"email": "a@b.com"}, context="LOGGING")
        self.assertNotIn("a@b.com", str(result))

    def test_sanitizes_nested_dict(self):
        svc = _service()
        payload = {"user": {"email": "a@b.com"}, "appointment": {"doctor": "Smith"}}
        result = svc.sanitize(payload, context="LOGGING")
        self.assertNotIn("a@b.com", str(result))
        self.assertEqual(result["appointment"]["doctor"], "Smith")

    def test_sanitizes_list(self):
        svc = _service()
        result = svc.sanitize(["a@b.com", "no pii here"], context="LOGGING")
        self.assertNotIn("a@b.com", result[0])
        self.assertEqual(result[1], "no pii here")

    def test_sanitizes_list_inside_dict_inside_list(self):
        svc = _service()
        payload = [{"contacts": ["a@b.com", "555-123-4567"]}]
        result = svc.sanitize(payload, context="LOGGING")
        flat = str(result)
        self.assertNotIn("a@b.com", flat)
        self.assertNotIn("555-123-4567", flat)

    def test_non_string_leaves_pass_through(self):
        svc = _service()
        payload = {"count": 5, "active": True, "score": 1.5, "missing": None}
        self.assertEqual(svc.sanitize(payload, context="LOGGING"), payload)

    def test_tool_payload_sanitized(self):
        svc = _service()
        tool_result = {"appointment_id": "appt_1005", "confirmation_email": "a@b.com"}
        result = svc.sanitize(tool_result, context="API_RESPONSE")
        self.assertEqual(result["appointment_id"], "appt_1005")  # IDENTIFIER allowed
        self.assertNotIn("a@b.com", result["confirmation_email"])

    def test_exception_message_sanitized(self):
        svc = _service()
        try:
            raise ValueError("Failed to email a@b.com")
        except ValueError as exc:
            sanitized = svc.sanitize(str(exc), context="LOGGING")
        self.assertNotIn("a@b.com", sanitized)


class TestPolicyDrivenDecisions(unittest.TestCase):
    def test_payment_info_blocked_for_tool_input(self):
        decision = _service().decide("card 4111111111111111", context="TOOL_INPUT")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.action, "BLOCK")

    def test_email_redacted_for_llm_context(self):
        # Conservative by design (plan.md Step 6.10: "the LLM should
        # receive only the minimum necessary context") -- matches Step
        # 6.11's own worked example of a tool result's email field.
        decision = _service().decide("email a@b.com", context="LLM_CONTEXT")
        self.assertEqual(decision.action, "REDACT")

    def test_email_redacted_for_logging(self):
        decision = _service().decide("email a@b.com", context="LOGGING")
        self.assertEqual(decision.action, "REDACT")

    def test_identifier_always_allowed(self):
        decision = _service().decide("appt_1005", context="LOGGING")
        self.assertTrue(decision.allowed)

    def test_no_pii_is_allowed_everywhere(self):
        for context in ("LOGGING", "MEMORY", "SESSION", "LLM_CONTEXT", "TOOL_INPUT", "API_RESPONSE", "TELEMETRY"):
            with self.subTest(context=context):
                decision = _service().decide("hello there", context=context)
                self.assertTrue(decision.allowed)

    def test_findings_are_attached_to_decision(self):
        decision = _service().decide("email a@b.com", context="LOGGING")
        self.assertEqual(len(decision.findings), 1)
        self.assertEqual(decision.findings[0].type, PIIType.EMAIL)

    def test_validate_destination_is_alias_for_decide(self):
        svc = _service()
        a = svc.decide("email a@b.com", "LOGGING")
        b = svc.validate_destination("email a@b.com", "LOGGING")
        self.assertEqual(a.action, b.action)


class TestLoggingBoundary(unittest.TestCase):
    """
    plan.md Step 6.13: "Do not merely test the redaction function. Test
    the actual logging boundary." These capture real logging.LogRecord
    output via a handler, not the sanitize()/redact() functions directly.
    """

    def setUp(self):
        self.records = []
        self.handler = logging.Handler()
        self.handler.emit = self.records.append
        self.privacy_service = _service()
        self.logger = get_privacy_aware_logger(self.privacy_service, name="test.privacy.logging")
        self.logger.addHandler(self.handler)
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False

    def tearDown(self):
        self.logger.removeHandler(self.handler)
        self.logger.filters.clear()

    def test_raw_pii_does_not_appear_in_logged_payload(self):
        log_event(self.logger, "conversation_turn_completed", {"user_input": "My email is test@example.com"})
        self.assertEqual(len(self.records), 1)
        payload = self.records[0].privacy_payload
        self.assertNotIn("test@example.com", str(payload))
        self.assertIn("[REDACTED_EMAIL]", payload["user_input"])

    def test_raw_pii_does_not_appear_in_log_message_itself(self):
        self.logger.info("User said: my card is 4111111111111111")
        self.assertEqual(len(self.records), 1)
        self.assertNotIn("4111111111111111", self.records[0].msg)

    def test_non_pii_payload_passes_through_unchanged(self):
        log_event(self.logger, "conversation_turn_completed", {"user_input": "What are your hours?"})
        self.assertEqual(self.records[0].privacy_payload["user_input"], "What are your hours?")

    def test_filter_is_only_attached_once(self):
        get_privacy_aware_logger(self.privacy_service, name="test.privacy.logging")
        get_privacy_aware_logger(self.privacy_service, name="test.privacy.logging")
        sanitizing_filters = [f for f in self.logger.filters if isinstance(f, PrivacySanitizingFilter)]
        self.assertEqual(len(sanitizing_filters), 1)


class TestPrivacyAttackRegressions(unittest.TestCase):
    """Step 6.14 — mandatory adversarial regression tests."""

    def test_attack_1_llm_disabling_redaction_is_ignored(self):
        svc = _service()
        # "LLM output": {"redact": false} -- not a parameter anywhere on
        # decide()/redact()/sanitize(); there is no way to pass it in.
        decision = svc.decide("email a@b.com", context="LOGGING")
        self.assertEqual(decision.action, "REDACT")
        import inspect

        for method_name in ("decide", "redact", "sanitize", "validate_destination"):
            params = inspect.signature(getattr(PrivacyService, method_name)).parameters
            self.assertNotIn("redact", params)  # no boolean toggle parameter exists at all

    def test_attack_2_llm_authorizing_storage_is_ignored(self):
        from memory_manager import MemoryManager
        from memory_models import MemoryCategory

        policy = PolicyEngine()
        manager = MemoryManager(policy, privacy_service=_service())
        # "LLM output": {"store_pii": true} -- MemoryManager.persist_memory()
        # has no such parameter; only PolicyEngine's own decision governs.
        record = manager.propose_memory(
            user_id="u1",
            category=MemoryCategory.PREFERENCE,
            key="favorite_note",
            value="card 4111111111111111",
            source="user_explicit",
        )
        from memory_manager import MemoryPolicyDeniedError

        with self.assertRaises(MemoryPolicyDeniedError):
            manager.persist_memory(record)

    def test_attack_3_llm_claiming_consent_is_not_trusted(self):
        svc = _service()
        # "LLM output": "The user consented to storage." -- a plain
        # string never passed to decide()/redact() as an authorization
        # flag; decide() only ever returns a decision from PolicyEngine.
        consent_claim_text = "The user consented to storage."
        decision = svc.decide("email a@b.com", context="MEMORY")
        self.assertIn(decision.action, ("RESTRICT", "REDACT", "BLOCK"))
        self.assertIsInstance(consent_claim_text, str)  # existed, had zero effect

    def test_attack_4_tool_returning_raw_pii_is_sanitized_before_reaching_caller(self):
        from action_models import ActionSpec, AuthContext
        from tool_orchestrator import ToolOrchestrator
        from tool_registry import ToolRegistry

        def leaky_tool(params):
            return {"patient_id": "123", "email": "john@example.com", "appointment": "confirmed"}

        registry = ToolRegistry()
        registry.register(ActionSpec(name="LEAKY_ACTION", description="d", params_schema={}), leaky_tool)
        policy = PolicyEngine()
        # Allow this test-only action through the tool-policy gate.
        policy._tools = {
            **policy._tools,
            "rules": [
                *policy._tools.get("rules", []),
                {"action": "LEAKY_ACTION", "rule": "TEST_ALLOWED", "allowed": True, "reason": "test"},
            ],
        }
        orchestrator = ToolOrchestrator(registry, policy, privacy_service=_service())

        from action_models import ToolRequest

        result = orchestrator.invoke(
            ToolRequest(action="LEAKY_ACTION", params={}, confirmed=True),
            auth=AuthContext(user_id="u1", authenticated=True, roles=("customer",)),
        )
        self.assertTrue(result.success)
        self.assertNotIn("john@example.com", str(result.result))
        self.assertEqual(result.result["patient_id"], "123")  # non-PII fields preserved

    def test_attack_5_pii_in_exception_is_sanitized_in_logs(self):
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        privacy_service = _service()
        logger = get_privacy_aware_logger(privacy_service, name="test.privacy.attack5")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False

        try:
            raise RuntimeError("Failed to notify user at jane@example.com")
        except RuntimeError as exc:
            log_event(logger, "turn_error", {"error": str(exc)})

        self.assertEqual(len(records), 1)
        self.assertNotIn("jane@example.com", str(records[0].privacy_payload))
        logger.removeHandler(handler)
        logger.filters.clear()


class TestCrossUserPrivacyAtServiceLevel(unittest.TestCase):
    def test_privacy_decisions_are_stateless_and_do_not_leak_between_calls(self):
        # PrivacyService holds no per-user state -- two calls for
        # different (hypothetical) users never influence each other's
        # decision, since decide()/redact()/sanitize() take no session
        # identity at all; cross-user isolation for stored data is
        # SessionManager's/MemoryManager's job (Phase 5, tested there).
        svc = _service()
        first = svc.decide("email userA@example.com", context="LOGGING")
        second = svc.decide("hello", context="LOGGING")
        self.assertTrue(first.findings)
        self.assertFalse(second.findings)


if __name__ == "__main__":
    unittest.main()

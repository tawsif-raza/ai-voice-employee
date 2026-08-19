"""
Unit tests for the deterministic PII detector (Phase 6).

Fully offline, stdlib only.

Run with:
    python -m unittest tests.test_pii_detector -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "agent"))
from pii_detector import PIIDetector  # noqa: E402
from privacy_models import PIIType  # noqa: E402


def _detector() -> PIIDetector:
    return PIIDetector()


class TestEmailDetection(unittest.TestCase):
    def test_detects_email(self):
        findings = _detector().detect("Contact me at john.doe@example.com please.")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].type, PIIType.EMAIL)
        self.assertEqual(findings[0].value, "john.doe@example.com")

    def test_no_email_no_finding(self):
        self.assertEqual(_detector().detect_email("no email here"), [])


class TestPhoneDetection(unittest.TestCase):
    def test_detects_phone_with_dashes(self):
        findings = _detector().detect("Call me at 555-123-4567 tomorrow.")
        types = [f.type for f in findings]
        self.assertIn(PIIType.PHONE, types)

    def test_detects_phone_with_parens(self):
        findings = _detector().detect("My number is (555) 123-4567.")
        self.assertTrue(any(f.type == PIIType.PHONE for f in findings))

    def test_no_phone_no_finding(self):
        self.assertEqual(_detector().detect_phone("just some regular text"), [])


class TestIdentifierDetection(unittest.TestCase):
    def test_detects_appointment_id(self):
        findings = _detector().detect("Please cancel appt_1005 for me.")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].type, PIIType.IDENTIFIER)
        self.assertEqual(findings[0].value, "appt_1005")

    def test_detects_order_id(self):
        findings = _detector().detect("What's the status of order_2002?")
        self.assertTrue(any(f.value == "order_2002" for f in findings))


class TestPaymentDetection(unittest.TestCase):
    def test_detects_16_digit_card_number(self):
        findings = _detector().detect("My card is 4111111111111111.")
        self.assertTrue(any(f.type == PIIType.PAYMENT_INFORMATION for f in findings))

    def test_detects_card_number_with_spaces(self):
        findings = _detector().detect("Card: 4111 1111 1111 1111")
        self.assertTrue(any(f.type == PIIType.PAYMENT_INFORMATION for f in findings))

    def test_short_digit_sequence_not_flagged_as_payment(self):
        findings = _detector().detect_payment("call 12345 for info")
        self.assertEqual(findings, [])


class TestNestedJSONAndMixedText(unittest.TestCase):
    def test_multiple_pii_values_in_one_message(self):
        text = "Email me at a@b.com or call 555-987-6543 about appt_1001."
        findings = _detector().detect(text)
        types = {f.type for f in findings}
        self.assertEqual(types, {PIIType.EMAIL, PIIType.PHONE, PIIType.IDENTIFIER})

    def test_mixed_text_with_no_pii(self):
        findings = _detector().detect("What are your business hours today?")
        self.assertEqual(findings, [])

    def test_findings_do_not_overlap(self):
        text = "Card 4111111111111111 or email a@b.com"
        findings = _detector().detect(text)
        spans = sorted((f.start, f.end) for f in findings)
        for (s1, e1), (s2, e2) in zip(spans, spans[1:]):
            self.assertLessEqual(e1, s2, "detected spans must not overlap")


class TestFalsePositiveAwareness(unittest.TestCase):
    """
    Honest limitation checks (plan.md Step 6.13: "false positives where
    practical") -- these document known, accepted false-negative/positive
    behavior rather than claiming perfection.
    """

    def test_appointment_date_is_not_flagged_as_date_of_birth(self):
        # DATE_OF_BIRTH is not detected at all (see privacy_models.py's
        # module docstring) -- an appointment date must not be
        # misclassified as one, since no DOB detector exists to do so.
        findings = _detector().detect("Your appointment is on 2026-08-18 at 5pm.")
        self.assertNotIn(PIIType.DATE_OF_BIRTH, {f.type for f in findings})

    def test_free_text_name_is_not_detected(self):
        # Documented gap -- names have no reliable regex signature.
        findings = _detector().detect("My name is John Smith.")
        self.assertNotIn(PIIType.NAME, {f.type for f in findings})

    def test_non_string_input_does_not_raise(self):
        for bad in (None, 12345, ["not", "a", "string"], {}):
            with self.subTest(bad=bad):
                self.assertEqual(_detector().detect(bad), [])

    def test_empty_string_no_findings(self):
        self.assertEqual(_detector().detect(""), [])


if __name__ == "__main__":
    unittest.main()

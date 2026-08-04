"""
Unit tests for the clinical-question guard (configs/clinical_triggers.yaml).

This guard reuses HandoffDetector as a generic layered phrase-intent
matcher (see that file's header comment in configs/clinical_triggers.yaml
for why) -- no new matching code, just a second config. These tests are
fully offline, same as tests/test_handoff_detector.py.

Run with:
    python -m unittest tests.test_clinical_guard -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "inference"))

from handoff_detector import HandoffDetector  # noqa: E402

CLINICAL_CONFIG_PATH = Path(__file__).resolve().parents[1] / "configs" / "clinical_triggers.yaml"


class TestClinicalGuard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.guard = HandoffDetector(config_path=CLINICAL_CONFIG_PATH)

    # -- should trigger --

    def test_dosage_question_triggers(self):
        self.assertTrue(self.guard.detect("How many mg of ibuprofen should I take?"))

    def test_interaction_question_triggers(self):
        self.assertTrue(self.guard.detect("Is it safe to take this with my blood pressure medication?"))

    def test_side_effects_question_triggers(self):
        self.assertTrue(self.guard.detect("What are the side effects of loratadine?"))

    def test_pregnancy_question_triggers(self):
        self.assertTrue(self.guard.detect("Can I take this while pregnant?"))

    def test_overdose_question_triggers(self):
        self.assertTrue(self.guard.detect("I think I took too much acetaminophen, what should I do?"))

    def test_allergy_question_triggers(self):
        self.assertTrue(self.guard.detect("Is aspirin safe for someone with a peanut allergy?"))

    def test_diagnosis_request_triggers(self):
        self.assertTrue(self.guard.detect("Can you diagnose what's wrong with me?"))

    # -- regression: gaps found via the 200-case eval (outputs/evaluation/) --

    def test_symptom_inquiry_without_word_symptom_triggers(self):
        self.assertTrue(self.guard.detect("What could be causing my stomach pain?"))

    def test_how_much_is_safe_phrasing_triggers(self):
        self.assertTrue(self.guard.detect("How much melatonin is safe for a child?"))

    def test_combine_with_without_safe_to_prefix_triggers(self):
        self.assertTrue(self.guard.detect("Can I combine this antihistamine with alcohol?"))

    def test_dosage_of_phrasing_triggers(self):
        self.assertTrue(self.guard.detect("What's the correct dosage of vitamin D for an adult?"))

    # -- should NOT trigger (general product/store questions) --

    def test_general_product_question_does_not_trigger(self):
        self.assertFalse(self.guard.detect("What is ibuprofen used for?"))

    def test_stock_question_does_not_trigger(self):
        self.assertFalse(self.guard.detect("Do you carry loratadine in liquid form?"))

    def test_appointment_question_does_not_trigger(self):
        self.assertFalse(self.guard.detect("How do I book a vaccination appointment?"))

    def test_unrelated_question_does_not_trigger(self):
        self.assertFalse(self.guard.detect("What are your business hours?"))

    # -- config sanity: guards against silently using HandoffDetector's
    # own (handoff, not clinical) built-in defaults if this path is wrong --

    def test_config_file_exists(self):
        self.assertTrue(CLINICAL_CONFIG_PATH.exists())

    def test_loaded_clinical_phrases_not_handoff_phrases(self):
        self.assertIn("how many mg should i take", self.guard._exact_phrases)
        self.assertNotIn("connect you to a human", self.guard._exact_phrases)


if __name__ == "__main__":
    unittest.main()

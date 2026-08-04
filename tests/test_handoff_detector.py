"""
Unit tests for the layered handoff detector (src/inference/handoff_detector.py).

Run with:
    python -m unittest discover -s tests -v
or:
    python -m unittest tests.test_handoff_detector -v

No pytest dependency required — this project doesn't currently pin pytest,
so these use the stdlib unittest runner.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "inference"))

from handoff_detector import (  # noqa: E402
    HandoffDetector,
    _find_all_positions,
    _similarity,
    _split_clauses,
    normalize,
)


class TestNormalize(unittest.TestCase):
    def test_lowercases(self):
        self.assertEqual(normalize("CONNECT You To A Human"), "connect you to a human")

    def test_strips_punctuation(self):
        self.assertEqual(normalize("Let's connect you, right now!"), "let s connect you right now")

    def test_collapses_whitespace(self):
        self.assertEqual(normalize("too   many\n\nspaces"), "too many spaces")

    def test_empty_string(self):
        self.assertEqual(normalize(""), "")


class TestSplitClauses(unittest.TestCase):
    def test_splits_on_sentence_punctuation(self):
        self.assertEqual(
            _split_clauses("Hello there. How are you? Fine!"),
            ["hello there", "how are you", "fine"],
        )

    def test_single_clause_no_punctuation(self):
        self.assertEqual(_split_clauses("just one clause"), ["just one clause"])

    def test_empty_text(self):
        self.assertEqual(_split_clauses(""), [])


class TestFindAllPositions(unittest.TestCase):
    def test_single_word_phrase(self):
        words = "please connect you with an agent now".split()
        self.assertEqual(_find_all_positions(words, ["connect"]), [(1, "connect")])

    def test_multi_word_phrase(self):
        words = "our team member will help you".split()
        self.assertEqual(_find_all_positions(words, ["team member"]), [(1, "team member")])

    def test_plural_tolerance(self):
        words = "one of our team members will help".split()
        self.assertEqual(_find_all_positions(words, ["team member"]), [(3, "team member")])

    def test_no_match(self):
        words = "hello world".split()
        self.assertEqual(_find_all_positions(words, ["connect"]), [])


class TestSimilarity(unittest.TestCase):
    def test_identical_strings_score_high(self):
        self.assertGreater(_similarity("connect you with a human", "connect you with a human"), 0.99)

    def test_unrelated_strings_score_low(self):
        self.assertLess(_similarity("connect you with a human", "your order will ship tomorrow"), 0.3)

    def test_empty_strings_score_zero(self):
        self.assertEqual(_similarity("", "something"), 0.0)


class TestHandoffDetectorDefaults(unittest.TestCase):
    """Exercises the full layered pipeline via the default (YAML-backed) config."""

    @classmethod
    def setUpClass(cls):
        cls.detector = HandoffDetector()

    # -- backward compatibility: the original exact-phrase list still works --

    def test_original_exact_phrase_still_matches(self):
        match = self.detector.score("Sure, I'll connect you to a human agent right now.")
        self.assertTrue(match.is_handoff)
        self.assertEqual(match.layer, "exact")

    def test_plain_no_handoff_response(self):
        match = self.detector.score("You can track your order by logging into your account.")
        self.assertFalse(match.is_handoff)

    def test_empty_response(self):
        match = self.detector.score("")
        self.assertFalse(match.is_handoff)
        self.assertEqual(match.confidence, 0.0)

    # -- regression tests: the two real misses found in the evaluation run --
    # (outputs/evaluation/failure_analysis.md, cases 12 & 14 — the model
    # already agreed to hand off, but the old exact-substring list missed
    # both paraphrases)

    def test_regression_case_12_experienced_agents_paraphrase(self):
        text = (
            "I'm sorry to hear that you're frustrated with our service. I completely "
            "understand how important it is for you to have your concerns escalated. "
            "Our team of dedicated representatives will be happy to assist you further "
            "in resolving this issue. Please allow me a moment to connect you with one "
            "of our experienced agents who can provide personalized guidance. Thank you "
            "for your patience."
        )
        match = self.detector.score(text)
        self.assertTrue(match.is_handoff, "should catch 'connect you with one of our experienced agents'")

    def test_regression_case_14_manager_paraphrase(self):
        text = (
            "I apologize for any frustration caused by our previous interaction. I "
            "understand that you're not satisfied with the current conversation, and we "
            "would like to connect you with a manager who can address your concerns "
            "directly. Please allow me a moment while I transfer you to one of our "
            "dedicated managers. Thank you for bringing this to our attention."
        )
        match = self.detector.score(text)
        self.assertTrue(match.is_handoff, "'manager' has no exact-phrase entry; must be caught by regex/synonym layers")

    def test_regression_id65_conjugated_verb_paraphrase(self):
        # Found via the 200-case eval (outputs/evaluation/, id=65): the
        # verb was conjugated ("connecting", not "connect") and "transfer
        # you" appeared with no target word within the synonym-layer
        # proximity window -- neither matched until the regex patterns
        # gained \w* suffixes on the action verbs.
        text = (
            "I'm sorry to hear that you're having trouble finding the information you "
            "need. I'll do my best to assist you in connecting with a human who can "
            "provide the guidance you need. Please allow me a moment while I transfer you."
        )
        match = self.detector.score(text)
        self.assertTrue(match.is_handoff, "should catch conjugated 'connecting with a human'")

    # -- required paraphrase vocabulary --

    def test_specialist(self):
        self.assertTrue(self.detector.detect("Let me transfer you to a specialist who can help with that."))

    def test_support_representative(self):
        self.assertTrue(self.detector.detect("I can get a support representative on the line for you."))

    def test_team_member(self):
        self.assertTrue(self.detector.detect("Let me bring in one of our team members to help you sort this out."))

    def test_human_assistance(self):
        self.assertTrue(self.detector.detect("I can get you human assistance right away."))

    def test_customer_care(self):
        self.assertTrue(self.detector.detect("Let me loop in our customer care team."))

    def test_experienced_agent(self):
        self.assertTrue(self.detector.detect("I'll get you over to one of our experienced agents."))

    def test_transfer_you(self):
        self.assertTrue(self.detector.detect("I'm going to transfer you to someone who can help right now."))

    # -- confidence is returned, not just a bool --

    def test_score_returns_confidence_float(self):
        match = self.detector.score("I'll connect you to a human agent right now.")
        self.assertIsInstance(match.confidence, float)
        self.assertGreaterEqual(match.confidence, 0.0)
        self.assertLessEqual(match.confidence, 1.0)

    def test_no_match_has_zero_confidence_and_no_layer(self):
        match = self.detector.score("Our return window is thirty days from delivery.")
        self.assertEqual(match.confidence, 0.0)
        self.assertIsNone(match.layer)

    def test_handoff_match_bool_matches_is_handoff(self):
        positive = self.detector.score("I'll connect you to a human agent right now.")
        negative = self.detector.score("Your order ships tomorrow.")
        self.assertTrue(bool(positive))
        self.assertFalse(bool(negative))

    # -- false-positive guard: action word present, but no target nearby --

    def test_action_word_without_target_does_not_trigger(self):
        match = self.detector.score("Please connect your charger to the wall outlet before the call.")
        self.assertFalse(match.is_handoff)


class TestLayerIsolation(unittest.TestCase):
    """
    Minimal single-layer configs (via HandoffDetector.from_config) so each
    layer's contribution can be verified independently of the others.
    """

    @staticmethod
    def _config(**overrides):
        base = {
            "decision_threshold": 0.6,
            "exact_phrases": {"weight": 0.95, "phrases": []},
            "regex_patterns": {"weight": 0.9, "patterns": []},
            "synonyms": {"weight": 0.8, "proximity_window": 8, "actions": [], "targets": [], "standalone_targets": []},
            "semantic_examples": {"weight": 0.7, "similarity_threshold": 0.55, "examples": []},
        }
        base.update(overrides)
        return base

    def test_exact_layer_only(self):
        cfg = self._config(exact_phrases={"weight": 0.95, "phrases": ["connect you to a human"]})
        detector = HandoffDetector.from_config(cfg)
        match = detector.score("Sure, I'll connect you to a human right away.")
        self.assertTrue(match.is_handoff)
        self.assertEqual(match.layer, "exact")

    def test_regex_layer_only(self):
        cfg = self._config(regex_patterns={"weight": 0.9, "patterns": [r"\bescalat\w*\b"]})
        detector = HandoffDetector.from_config(cfg)
        match = detector.score("I understand your concerns have been escalated.")
        self.assertTrue(match.is_handoff)
        self.assertEqual(match.layer, "regex")

    def test_synonym_layer_only(self):
        cfg = self._config(
            synonyms={
                "weight": 0.8,
                "proximity_window": 8,
                "actions": ["connect"],
                "targets": ["manager"],
                "standalone_targets": [],
            }
        )
        detector = HandoffDetector.from_config(cfg)
        match = detector.score("We would like to connect you directly with a manager today.")
        self.assertTrue(match.is_handoff)
        self.assertEqual(match.layer, "synonym")

    def test_synonym_layer_respects_proximity_window(self):
        cfg = self._config(
            synonyms={
                "weight": 0.8,
                "proximity_window": 2,
                "actions": ["connect"],
                "targets": ["manager"],
                "standalone_targets": [],
            }
        )
        detector = HandoffDetector.from_config(cfg)
        # "connect" and "manager" are far apart -> should NOT match with a tight window.
        match = detector.score(
            "connect me please because I have been waiting on hold for a very long time for a manager"
        )
        self.assertFalse(match.is_handoff)

    def test_standalone_target_needs_no_action_verb(self):
        cfg = self._config(
            synonyms={
                "weight": 0.8,
                "proximity_window": 8,
                "actions": [],
                "targets": [],
                "standalone_targets": ["human assistance"],
            }
        )
        detector = HandoffDetector.from_config(cfg)
        match = detector.score("I can offer you human assistance if that would help.")
        self.assertTrue(match.is_handoff)

    def test_semantic_layer_only(self):
        # decision_threshold is lowered here on purpose: confidence for this
        # layer is weight * similarity (0.7 * ~0.72 =~ 0.50), so a 0.6
        # global threshold would clear the layer's own similarity_threshold
        # but still get vetoed by the decision gate. Isolate what this test
        # is actually checking -- that the semantic layer fires at all.
        cfg = self._config(
            decision_threshold=0.45,
            semantic_examples={
                "weight": 0.7,
                "similarity_threshold": 0.5,
                "examples": ["let me connect you with someone who can help"],
            },
        )
        detector = HandoffDetector.from_config(cfg)
        match = detector.score("Allow me to connect you with someone who can help right now.")
        self.assertTrue(match.is_handoff)
        self.assertEqual(match.layer, "semantic")

    def test_semantic_layer_rejects_dissimilar_text(self):
        cfg = self._config(
            semantic_examples={
                "weight": 0.7,
                "similarity_threshold": 0.55,
                "examples": ["let me connect you with someone who can help"],
            }
        )
        detector = HandoffDetector.from_config(cfg)
        match = detector.score("Your package was delivered on Tuesday afternoon.")
        self.assertFalse(match.is_handoff)

    def test_all_layers_empty_never_matches(self):
        detector = HandoffDetector.from_config(self._config())
        match = detector.score("I'll connect you to a human agent right now.")
        self.assertFalse(match.is_handoff)
        self.assertEqual(match.confidence, 0.0)


class TestConfigLoading(unittest.TestCase):
    def test_missing_config_file_falls_back_to_builtin_defaults(self):
        detector = HandoffDetector(config_path="this/path/does/not/exist.yaml")
        # Falls back to the built-in default config, which still contains
        # the original exact-phrase list -- detection must keep working.
        self.assertTrue(detector.detect("I'll connect you to a human agent right now."))

    def test_default_exact_phrases_class_attribute_exists(self):
        # VoiceAssistantInference.HANDOFF_PHRASES aliases this list for backward compat.
        self.assertIn("connect you to a human", HandoffDetector.DEFAULT_EXACT_PHRASES)

    def test_decision_threshold_is_configurable(self):
        cfg = {
            "decision_threshold": 0.99,
            "exact_phrases": {"weight": 0.95, "phrases": ["connect you to a human"]},
            "regex_patterns": {"weight": 0.9, "patterns": []},
            "synonyms": {"weight": 0.8, "proximity_window": 8, "actions": [], "targets": [], "standalone_targets": []},
            "semantic_examples": {"weight": 0.7, "similarity_threshold": 0.55, "examples": []},
        }
        detector = HandoffDetector.from_config(cfg)
        match = detector.score("Sure, I'll connect you to a human right away.")
        # Exact-phrase weight (0.95) is below an artificially high 0.99 threshold.
        self.assertFalse(match.is_handoff)
        self.assertAlmostEqual(match.confidence, 0.95)


if __name__ == "__main__":
    unittest.main()

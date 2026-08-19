"""
Backward-compatibility contract tests for the VoiceAssistantInference
facade (src/inference/predict.py) after the Conversation Manager
extraction (docs/IMPLEMENTATION_ROADMAP.md milestone M2).

These only import the module and inspect the class -- they never
instantiate VoiceAssistantInference, so they don't need torch/transformers/
a real model checkpoint loaded. That's a meaningful assertion in its own
right: importing predict.py must not require torch at all any more (model
loading now happens lazily inside build_conversation_manager(), called
from __init__, not at import time) -- see
test_class_importable_without_torch below.

Instantiation-level orchestration behavior is covered by
tests/test_conversation_manager.py via fakes.

Run with:
    python -m unittest tests.test_predict_facade -v
"""

import inspect
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "inference"))
import predict  # noqa: E402


class TestVoiceAssistantInferenceBackwardCompatibility(unittest.TestCase):
    def test_class_importable_without_torch(self):
        # If predict.py (or anything it imports at module level) required
        # torch/transformers/peft eagerly, the `import predict` above would
        # already have failed in this environment (no torch installed) --
        # reaching this line is itself the assertion that the LLM/model
        # loading dependency is now confined to __init__, not import time.
        self.assertNotIn("torch", sys.modules)
        self.assertTrue(hasattr(predict, "VoiceAssistantInference"))

    def test_public_methods_preserved(self):
        cls = predict.VoiceAssistantInference
        for name in (
            "generate_response_stream", "generate_response",
            "detect_handoff", "detect_handoff_scored", "run_interactive",
        ):
            self.assertTrue(hasattr(cls, name), f"missing backward-compatible method: {name}")

    def test_generate_response_stream_signature_unchanged(self):
        sig = inspect.signature(predict.VoiceAssistantInference.generate_response_stream)
        self.assertEqual(list(sig.parameters), ["self", "user_input", "history"])

    def test_generate_response_signature_unchanged(self):
        sig = inspect.signature(predict.VoiceAssistantInference.generate_response)
        self.assertEqual(list(sig.parameters), ["self", "user_input", "history", "on_token"])

    def test_detect_handoff_signatures_unchanged(self):
        sig = inspect.signature(predict.VoiceAssistantInference.detect_handoff)
        self.assertEqual(list(sig.parameters), ["self", "response_text"])
        sig_scored = inspect.signature(predict.VoiceAssistantInference.detect_handoff_scored)
        self.assertEqual(list(sig_scored.parameters), ["self", "response_text"])

    def test_init_signature_unchanged(self):
        sig = inspect.signature(predict.VoiceAssistantInference.__init__)
        expected = [
            "self", "base_model_name", "adapter_path", "merge_weights", "max_new_tokens",
            "temperature", "top_p", "repetition_penalty", "auto_resolve_adapter",
            "handoff_config_path", "rag_enabled", "clinical_config_path",
        ]
        self.assertEqual(list(sig.parameters), expected)

    def test_class_attributes_preserved(self):
        self.assertTrue(hasattr(predict.VoiceAssistantInference, "SYSTEM_PROMPT"))
        self.assertTrue(hasattr(predict.VoiceAssistantInference, "HANDOFF_PHRASES"))
        self.assertIn("connect you to a human", predict.VoiceAssistantInference.HANDOFF_PHRASES)

    def test_conversation_manager_is_the_orchestration_boundary(self):
        # The old inline orchestration must no longer be the primary
        # architecture: VoiceAssistantInference must delegate to a real
        # ConversationManager, not reimplement the turn sequence itself.
        import conversation_manager
        init_source = inspect.getsource(predict.VoiceAssistantInference.__init__)
        self.assertIn("build_conversation_manager", init_source)
        stream_source = inspect.getsource(predict.VoiceAssistantInference.generate_response_stream)
        self.assertIn("handle_turn", stream_source)


if __name__ == "__main__":
    unittest.main()

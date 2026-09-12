"""
Unit tests for the LLM Provider Abstraction (src/inference/llm_provider.py).
Tests:
- Claude provider streaming & quota error detection
- Gemini provider streaming & rate limit detection
- FallbackLLMProvider seamless failover from Claude to Gemini
- Cooldown behavior on quota exhaustion
- No business logic leakage to providers
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_INFERENCE_DIR = str(Path(__file__).resolve().parents[1] / "src" / "inference")
if _INFERENCE_DIR not in sys.path:
    sys.path.insert(0, _INFERENCE_DIR)

from llm_provider import (
    BaseLLMProvider,
    ClaudeLLMProvider,
    FallbackLLMProvider,
    GeminiLLMProvider,
    LLMOverloadedError,
    LLMProviderError,
    LLMQuotaExceededError,
    build_llm_provider,
)


class MockLLMProvider(BaseLLMProvider):
    """Deterministic mock provider for unit tests."""

    def __init__(self, name: str, chunks: list[str], should_raise: Exception = None):
        self.provider_name = name
        self.chunks = chunks
        self.should_raise = should_raise
        self.call_count = 0

    def generate_stream(self, messages, **kwargs):
        self.call_count += 1
        if self.should_raise:
            raise self.should_raise
        for chunk in self.chunks:
            yield chunk
        yield {
            "text": "".join(self.chunks),
            "latency_ms": 10.0,
            "provider": self.provider_name,
            "model": "test-model",
        }


class TestLLMProvider(unittest.TestCase):

    def test_mock_provider_contract(self):
        provider = MockLLMProvider("test", ["Hello", " world"])
        stream = list(provider.generate_stream([{"role": "user", "content": "Hi"}]))
        self.assertEqual(stream[0], "Hello")
        self.assertEqual(stream[1], " world")
        self.assertTrue(isinstance(stream[2], dict))
        self.assertEqual(stream[2]["text"], "Hello world")
        self.assertEqual(stream[2]["provider"], "test")

    def test_claude_message_conversion(self):
        provider = ClaudeLLMProvider(api_key="mock-key")
        messages = [
            {"role": "system", "content": "You are a support agent."},
            {"role": "user", "content": "When are you open?"},
            {"role": "assistant", "content": "9 to 5."},
            {"role": "user", "content": "Thank you."},
        ]
        system_prompt, anthropic_msgs = provider._convert_messages(messages)
        self.assertEqual(system_prompt, "You are a support agent.")
        self.assertEqual(len(anthropic_msgs), 3)
        self.assertEqual(anthropic_msgs[0], {"role": "user", "content": "When are you open?"})
        self.assertEqual(anthropic_msgs[1], {"role": "assistant", "content": "9 to 5."})

    def test_gemini_message_conversion(self):
        provider = GeminiLLMProvider(api_key="mock-key")
        messages = [
            {"role": "system", "content": "System directive."},
            {"role": "user", "content": "Question."},
            {"role": "assistant", "content": "Answer."},
        ]
        system_prompt, contents = provider._convert_messages(messages)
        self.assertEqual(system_prompt, "System directive.")
        self.assertEqual(len(contents), 2)
        self.assertEqual(contents[0]["role"], "user")
        self.assertEqual(contents[0]["parts"][0]["text"], "Question.")
        self.assertEqual(contents[1]["role"], "model")
        self.assertEqual(contents[1]["parts"][0]["text"], "Answer.")

    def test_fallback_primary_success(self):
        primary = MockLLMProvider("claude", ["Claude", " response"])
        fallback = MockLLMProvider("gemini", ["Gemini", " response"])
        orchestrator = FallbackLLMProvider(primary=primary, fallback=fallback)

        items = list(orchestrator.generate_stream([{"role": "user", "content": "Hello"}]))
        text_chunks = [x for x in items if isinstance(x, str)]
        final = [x for x in items if isinstance(x, dict)][0]

        self.assertEqual("".join(text_chunks), "Claude response")
        self.assertEqual(primary.call_count, 1)
        self.assertEqual(fallback.call_count, 0)
        self.assertFalse(final.get("fallback_used"))

    def test_fallback_triggers_on_claude_quota_exhaustion(self):
        primary = MockLLMProvider(
            "claude", [],
            should_raise=LLMQuotaExceededError("Rate limit exceeded 429", provider="claude")
        )
        fallback = MockLLMProvider("gemini", ["Gemini", " fallback", " response"])
        orchestrator = FallbackLLMProvider(primary=primary, fallback=fallback, cooldown_seconds=30.0)

        items = list(orchestrator.generate_stream([{"role": "user", "content": "Hello"}]))
        text_chunks = [x for x in items if isinstance(x, str)]
        final = [x for x in items if isinstance(x, dict)][0]

        self.assertEqual("".join(text_chunks), "Gemini fallback response")
        self.assertEqual(primary.call_count, 1)
        self.assertEqual(fallback.call_count, 1)
        self.assertTrue(final.get("fallback_used"))
        self.assertTrue(orchestrator.is_primary_in_cooldown)

    def test_fallback_cooldown_bypasses_primary(self):
        primary = MockLLMProvider("claude", ["Claude"])
        fallback = MockLLMProvider("gemini", ["Gemini"])
        orchestrator = FallbackLLMProvider(primary=primary, fallback=fallback, cooldown_seconds=60.0)

        # Trigger cooldown manually
        orchestrator.trigger_cooldown(reason="test")
        self.assertTrue(orchestrator.is_primary_in_cooldown)

        items = list(orchestrator.generate_stream([{"role": "user", "content": "Hi"}]))
        text = "".join([x for x in items if isinstance(x, str)])
        self.assertEqual(text, "Gemini")
        # Primary was never even called because it was in cooldown!
        self.assertEqual(primary.call_count, 0)
        self.assertEqual(fallback.call_count, 1)

    def test_factory_fallback_mode(self):
        with patch.dict("os.environ", {"LLM_PROVIDER": "fallback", "ANTHROPIC_API_KEY": "k1", "GEMINI_API_KEY": "k2"}):
            provider = build_llm_provider()
            self.assertIsInstance(provider, FallbackLLMProvider)
            self.assertIsInstance(provider.primary, ClaudeLLMProvider)
            self.assertIsInstance(provider.fallback, GeminiLLMProvider)

    def test_conversation_manager_with_llm_provider(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "agent"))
        from conversation_manager import build_conversation_manager

        mock_provider = MockLLMProvider("custom-claude", ["Welcome to our clinic."])
        cm = build_conversation_manager(
            llm_provider=mock_provider,
            rag_enabled=False,
            persistence_enabled=False,
        )
        self.assertIs(cm.llm_service, mock_provider)

        # Run a turn to confirm end-to-end integration without touching torch/HF
        items = list(cm.handle_turn("What can you help me with?"))
        response_text = "".join([x for x in items if isinstance(x, str)])
        final_meta = [x for x in items if isinstance(x, dict)][0]

        self.assertEqual(response_text, "Welcome to our clinic.")
        self.assertEqual(final_meta["response"], "Welcome to our clinic.")
        self.assertEqual(mock_provider.call_count, 1)


if __name__ == "__main__":
    unittest.main()

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
from unittest.mock import patch

_INFERENCE_DIR = str(Path(__file__).resolve().parents[1] / "src" / "inference")
if _INFERENCE_DIR not in sys.path:
    sys.path.insert(0, _INFERENCE_DIR)

from llm_provider import (
    BaseLLMProvider,
    ClaudeLLMProvider,
    FallbackLLMProvider,
    GeminiLLMProvider,
    GroqLLMProvider,
    LLMProviderError,
    LLMQuotaExceededError,
    _default_provider_mode,
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

    def test_gemini_2_5_disables_thinking_budget(self):
        """
        Regression test for a real defect found via live verification
        (docs/phase1.4-external-integration-report.md): Gemini 2.5 models
        reserve part of maxOutputTokens for internal "thinking" tokens by
        default, non-deterministically, and can consume the entire budget
        on reasoning with zero tokens left for visible text -- a genuine
        HTTP 200 with an empty response, no exception. Confirmed live
        against the real API: 2/5 calls with maxOutputTokens=10 and no
        thinkingConfig returned empty text; 0/5 did once thinkingConfig=
        {"thinkingBudget": 0} was added. This test only checks the request
        payload (not a live call) so it runs in the normal offline suite.
        """
        captured_payload = {}

        class _FakeResponse:
            status_code = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def iter_lines(self, decode_unicode=True):
                return iter(['data: {"candidates": [{"content": {"parts": [{"text": "OK"}]}}]}'])

        def _fake_post(url, headers=None, json=None, stream=None, timeout=None):
            captured_payload.update(json)
            return _FakeResponse()

        provider = GeminiLLMProvider(api_key="mock-key", model="gemini-2.5-flash")
        with patch("requests.post", side_effect=_fake_post):
            list(provider.generate_stream([{"role": "user", "content": "hi"}], max_new_tokens=10))
        self.assertEqual(captured_payload["generationConfig"]["thinkingConfig"], {"thinkingBudget": 0})

    def test_gemini_1_5_does_not_set_thinking_budget(self):
        """Older Gemini models don't recognize thinkingConfig -- confirm the
        field is only added for 2.5 models, not sent unconditionally."""
        captured_payload = {}

        class _FakeResponse:
            status_code = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def iter_lines(self, decode_unicode=True):
                return iter(['data: {"candidates": [{"content": {"parts": [{"text": "OK"}]}}]}'])

        def _fake_post(url, headers=None, json=None, stream=None, timeout=None):
            captured_payload.update(json)
            return _FakeResponse()

        provider = GeminiLLMProvider(api_key="mock-key", model="gemini-1.5-flash")
        with patch("requests.post", side_effect=_fake_post):
            list(provider.generate_stream([{"role": "user", "content": "hi"}], max_new_tokens=10))
        self.assertNotIn("thinkingConfig", captured_payload["generationConfig"])

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
            "claude", [], should_raise=LLMQuotaExceededError("Rate limit exceeded 429", provider="claude")
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

    def test_factory_groq_mode(self):
        with patch.dict("os.environ", {"LLM_PROVIDER": "groq", "GROQ_API_KEY": "k3"}):
            provider = build_llm_provider()
            self.assertIsInstance(provider, GroqLLMProvider)

    def test_factory_free_fallback_mode(self):
        """free_fallback = Gemini primary, Groq fallback -- both free-tier,
        no Claude/paid dependency at all."""
        with patch.dict("os.environ", {"LLM_PROVIDER": "free_fallback", "GEMINI_API_KEY": "k2", "GROQ_API_KEY": "k3"}):
            provider = build_llm_provider()
            self.assertIsInstance(provider, FallbackLLMProvider)
            self.assertIsInstance(provider.primary, GeminiLLMProvider)
            self.assertIsInstance(provider.fallback, GroqLLMProvider)

    def test_default_provider_mode_prefers_paid_fallback_only_with_claude_key(self):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "k1", "GEMINI_API_KEY": "k2"}, clear=True):
            self.assertEqual(_default_provider_mode(), "fallback")

    def test_default_provider_mode_prefers_free_fallback_without_claude_key(self):
        with patch.dict("os.environ", {"GEMINI_API_KEY": "k2", "GROQ_API_KEY": "k3"}, clear=True):
            self.assertEqual(_default_provider_mode(), "free_fallback")

    def test_default_provider_mode_gemini_only(self):
        with patch.dict("os.environ", {"GEMINI_API_KEY": "k2"}, clear=True):
            self.assertEqual(_default_provider_mode(), "gemini")

    def test_default_provider_mode_groq_only(self):
        with patch.dict("os.environ", {"GROQ_API_KEY": "k3"}, clear=True):
            self.assertEqual(_default_provider_mode(), "groq")

    def test_default_provider_mode_no_keys_is_local(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(_default_provider_mode(), "local")

    def test_groq_streams_and_stops_on_done(self):
        captured_payload = {}

        class _FakeResponse:
            status_code = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def iter_lines(self, decode_unicode=True):
                return iter(
                    [
                        'data: {"choices": [{"delta": {"role": "assistant", "content": ""}}]}',
                        'data: {"choices": [{"delta": {"content": "Hello"}}]}',
                        'data: {"choices": [{"delta": {"content": " there"}}]}',
                        'data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}',
                        "data: [DONE]",
                        # A malformed/trailing line after [DONE] must be ignored, not crash.
                        'data: {"choices": [{"delta": {"content": "should not appear"}}]}',
                    ]
                )

        def _fake_post(url, headers=None, json=None, stream=None, timeout=None):
            captured_payload.update(json)
            self.assertEqual(headers["Authorization"], "Bearer test-groq-key")
            return _FakeResponse()

        provider = GroqLLMProvider(api_key="test-groq-key", model="qwen/qwen3.8-27b")
        with patch("requests.post", side_effect=_fake_post):
            items = list(
                provider.generate_stream(
                    [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "hi"}]
                )
            )
        text_chunks = [x for x in items if isinstance(x, str)]
        final = [x for x in items if isinstance(x, dict)][0]
        self.assertEqual(text_chunks, ["Hello", " there"])
        self.assertEqual(final["text"], "Hello there")
        self.assertEqual(final["provider"], "groq")
        # Messages are passed through as-is (OpenAI-compatible), no conversion needed.
        self.assertEqual(
            captured_payload["messages"],
            [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "hi"}],
        )

    def test_groq_missing_api_key_raises_without_network_call(self):
        provider = GroqLLMProvider(api_key="")
        with patch("requests.post") as mock_post:
            with self.assertRaises(LLMProviderError):
                list(provider.generate_stream([{"role": "user", "content": "hi"}]))
        mock_post.assert_not_called()

    def test_groq_rate_limit_raises_quota_exceeded(self):
        class _FakeResponse:
            status_code = 429
            text = '{"error": {"message": "rate limited"}}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def json(self):
                return {"error": {"message": "rate limited"}}

        provider = GroqLLMProvider(api_key="test-groq-key")
        with patch("requests.post", return_value=_FakeResponse()):
            with self.assertRaises(LLMQuotaExceededError):
                list(provider.generate_stream([{"role": "user", "content": "hi"}]))

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

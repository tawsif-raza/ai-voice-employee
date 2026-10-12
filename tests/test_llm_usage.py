"""
Offline tests for provider-reported token usage (src/inference/llm_usage.py and its
wiring in src/inference/llm_provider.py).

Everything here is synthetic: events are hand-built dicts shaped like the providers'
documented streaming payloads, and the provider classes are driven through a faked
``requests.post``. A fixture refuses any real socket connection, so this module
cannot reach a provider (paid or free) even by accident. Shapes are SIMULATED, not
captured from live calls.
"""

import json
import socket
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_INFERENCE_DIR = str(Path(__file__).resolve().parents[1] / "src" / "inference")
if _INFERENCE_DIR not in sys.path:
    sys.path.insert(0, _INFERENCE_DIR)

from llm_provider import (  # noqa: E402
    BaseLLMProvider,
    ClaudeLLMProvider,
    FallbackLLMProvider,
    GeminiLLMProvider,
    GroqLLMProvider,
    LLMProviderError,
)
from llm_usage import (  # noqa: E402
    USAGE_SOURCE,
    USAGE_VERSION,
    AnthropicUsageAccumulator,
    GeminiUsageAccumulator,
    OpenAIStyleUsageAccumulator,
)

PROMPT_SENTINEL = "SENTINEL-PROMPT-TEXT-7f3a"
KEY_SENTINEL = "test-key-SENTINEL-9c1d"
USAGE_KEYS = {
    "usage_version",
    "source",
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "reasoning_tokens",
    "response_model",
}


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    def _refuse(*args, **kwargs):
        raise AssertionError("a test attempted a real network connection")

    monkeypatch.setattr(socket.socket, "connect", _refuse)
    monkeypatch.setattr(socket, "create_connection", _refuse)


def _run(accumulator, events):
    for event in events:
        accumulator.observe(event)
    return accumulator.result()


# ── Anthropic ─────────────────────────────────────────────────────────────────


def _claude_events(output=50, **start_usage):
    usage = {"input_tokens": 100, "output_tokens": 1}
    usage.update(start_usage)
    return [
        {"type": "message_start", "message": {"model": "claude-test-model-1", "usage": usage}},
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hello"}},
        {"type": "message_delta", "usage": {"output_tokens": output}},
    ]


def test_anthropic_reports_canonical_usage_with_cache_fields():
    usage = _run(
        AnthropicUsageAccumulator(),
        _claude_events(cache_read_input_tokens=20, cache_creation_input_tokens=5),
    )
    assert usage == {
        "usage_version": USAGE_VERSION,
        "source": USAGE_SOURCE,
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_input_tokens": 20,
        "cache_creation_input_tokens": 5,
        "response_model": "claude-test-model-1",
    }


def test_anthropic_input_tokens_are_not_adjusted_for_cache():
    # Anthropic's input_tokens already EXCLUDES cache reads/writes; it must pass through unchanged.
    usage = _run(AnthropicUsageAccumulator(), _claude_events(cache_read_input_tokens=900))
    assert usage["input_tokens"] == 100 and usage["cache_read_input_tokens"] == 900


def test_anthropic_without_cache_fields_does_not_invent_them():
    usage = _run(AnthropicUsageAccumulator(), _claude_events())
    assert "cache_read_input_tokens" not in usage and "cache_creation_input_tokens" not in usage


def test_anthropic_message_delta_is_cumulative_and_last_wins():
    events = _claude_events(output=10) + [{"type": "message_delta", "usage": {"output_tokens": 55}}]
    assert _run(AnthropicUsageAccumulator(), events)["output_tokens"] == 55


def test_anthropic_message_start_output_is_a_placeholder_not_usage():
    # A stream cut before message_delta has only message_start's provisional output_tokens=1.
    assert _run(AnthropicUsageAccumulator(), _claude_events()[:2]) is None


def test_anthropic_requires_message_start_input():
    assert _run(AnthropicUsageAccumulator(), [{"type": "message_delta", "usage": {"output_tokens": 5}}]) is None


def test_anthropic_zero_tokens_is_real_usage_not_missing():
    events = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 0, "output_tokens": 0}}},
        {"type": "message_delta", "usage": {"output_tokens": 0}},
    ]
    usage = _run(AnthropicUsageAccumulator(), events)
    assert usage["input_tokens"] == 0 and usage["output_tokens"] == 0


@pytest.mark.parametrize("bad", [-1, "12", 1.5, True, [3]])
def test_anthropic_malformed_counts_discard_usage_entirely(bad):
    assert _run(AnthropicUsageAccumulator(), _claude_events(input_tokens=bad)) is None


def test_anthropic_malformed_after_valid_data_still_discards():
    events = _claude_events() + [{"type": "message_delta", "usage": {"output_tokens": -4}}]
    assert _run(AnthropicUsageAccumulator(), events) is None


def test_anthropic_missing_model_is_omitted_not_guessed():
    events = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 3, "output_tokens": 1}}},
        {"type": "message_delta", "usage": {"output_tokens": 4}},
    ]
    assert "response_model" not in _run(AnthropicUsageAccumulator(), events)


# ── Gemini ────────────────────────────────────────────────────────────────────


def _gemini_chunk(**meta):
    return {
        "candidates": [{"content": {"parts": [{"text": "x"}]}}],
        "usageMetadata": meta,
        "modelVersion": "gem-test-1",
    }


def test_gemini_basic_usage():
    usage = _run(GeminiUsageAccumulator(), [_gemini_chunk(promptTokenCount=100, candidatesTokenCount=40)])
    assert usage == {
        "usage_version": USAGE_VERSION,
        "source": USAGE_SOURCE,
        "input_tokens": 100,
        "output_tokens": 40,
        "response_model": "gem-test-1",
    }


def test_gemini_cached_tokens_are_subtracted_from_the_prompt_count():
    usage = _run(
        GeminiUsageAccumulator(),
        [_gemini_chunk(promptTokenCount=100, cachedContentTokenCount=30, candidatesTokenCount=40)],
    )
    assert usage["input_tokens"] == 70 and usage["cache_read_input_tokens"] == 30


def test_gemini_thinking_tokens_are_billed_as_output_and_reported_separately():
    usage = _run(
        GeminiUsageAccumulator(),
        [_gemini_chunk(promptTokenCount=10, candidatesTokenCount=40, thoughtsTokenCount=15)],
    )
    assert usage["output_tokens"] == 55 and usage["reasoning_tokens"] == 15


def test_gemini_tool_use_prompt_tokens_count_as_input():
    usage = _run(
        GeminiUsageAccumulator(),
        [_gemini_chunk(promptTokenCount=10, toolUsePromptTokenCount=7, candidatesTokenCount=4)],
    )
    assert usage["input_tokens"] == 17


def test_gemini_last_usage_metadata_wins():
    chunks = [
        _gemini_chunk(promptTokenCount=100, candidatesTokenCount=5),
        _gemini_chunk(promptTokenCount=100, candidatesTokenCount=40),
    ]
    assert _run(GeminiUsageAccumulator(), chunks)["output_tokens"] == 40


def test_gemini_chunks_without_usage_metadata_do_not_erase_earlier_usage():
    chunks = [_gemini_chunk(promptTokenCount=9, candidatesTokenCount=3), {"candidates": []}]
    assert _run(GeminiUsageAccumulator(), chunks)["input_tokens"] == 9


@pytest.mark.parametrize(
    "meta",
    [
        {"candidatesTokenCount": 40},  # no prompt count
        {"promptTokenCount": 100},  # no candidates count
        {"promptTokenCount": 10, "cachedContentTokenCount": 11, "candidatesTokenCount": 1},  # cached > prompt
        {"promptTokenCount": "100", "candidatesTokenCount": 1},
        {"promptTokenCount": -5, "candidatesTokenCount": 1},
        {"promptTokenCount": 5, "candidatesTokenCount": True},
    ],
)
def test_gemini_incomplete_or_inconsistent_usage_is_omitted(meta):
    assert _run(GeminiUsageAccumulator(), [_gemini_chunk(**meta)]) is None


def test_gemini_no_usage_metadata_at_all():
    assert _run(GeminiUsageAccumulator(), [{"candidates": [{"content": {"parts": [{"text": "x"}]}}]}]) is None


# ── Groq / OpenAI-compatible ──────────────────────────────────────────────────


def test_groq_top_level_usage():
    chunk = {"model": "groq-test-1", "choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 8}}
    assert _run(OpenAIStyleUsageAccumulator(), [chunk]) == {
        "usage_version": USAGE_VERSION,
        "source": USAGE_SOURCE,
        "input_tokens": 12,
        "output_tokens": 8,
        "response_model": "groq-test-1",
    }


def test_groq_x_groq_usage_cached_and_reasoning():
    chunk = {
        "x_groq": {
            "usage": {
                "prompt_tokens": 50,
                "completion_tokens": 20,
                "prompt_tokens_details": {"cached_tokens": 10},
                "completion_tokens_details": {"reasoning_tokens": 6},
            }
        }
    }
    usage = _run(OpenAIStyleUsageAccumulator(), [chunk])
    assert usage["input_tokens"] == 40 and usage["cache_read_input_tokens"] == 10
    assert usage["output_tokens"] == 20 and usage["reasoning_tokens"] == 6


def test_groq_without_usage_yields_none_which_is_the_expected_default_today():
    chunks = [{"choices": [{"delta": {"content": "hi"}}]}, {"choices": [{"delta": {}}]}]
    assert _run(OpenAIStyleUsageAccumulator(), chunks) is None


@pytest.mark.parametrize(
    "usage",
    [
        {"prompt_tokens": 5},
        {"completion_tokens": 5},
        {"prompt_tokens": 5, "completion_tokens": 5, "prompt_tokens_details": {"cached_tokens": 6}},
        {"prompt_tokens": 5, "completion_tokens": 5, "completion_tokens_details": {"reasoning_tokens": 6}},
        {"prompt_tokens": "5", "completion_tokens": 5},
        {"prompt_tokens": 5, "completion_tokens": -1},
    ],
)
def test_groq_incomplete_or_inconsistent_usage_is_omitted(usage):
    assert _run(OpenAIStyleUsageAccumulator(), [{"usage": usage}]) is None


# ── Robustness shared by every accumulator ────────────────────────────────────


@pytest.mark.parametrize("cls", [AnthropicUsageAccumulator, GeminiUsageAccumulator, OpenAIStyleUsageAccumulator])
def test_accumulators_never_raise_on_garbage(cls):
    accumulator = cls()
    for junk in (None, 5, "text", [], [1, 2], {}, {"usage": None}, {"usageMetadata": 7}, {"x_groq": "no"}, {"type": 3}):
        accumulator.observe(junk)
    assert accumulator.result() is None
    assert accumulator.result() is None  # idempotent


@pytest.mark.parametrize("cls", [AnthropicUsageAccumulator, GeminiUsageAccumulator, OpenAIStyleUsageAccumulator])
def test_fresh_accumulator_has_no_usage(cls):
    assert cls().result() is None


# ── Provider wiring (faked requests.post; no network) ─────────────────────────


class _FakeStream:
    status_code = 200

    def __init__(self, events):
        self._lines = ["data: " + json.dumps(e) for e in events]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_lines(self, decode_unicode=True):
        return iter(self._lines)


def _generate(provider, events):
    sent = {}

    def _fake_post(url, headers=None, json=None, stream=None, timeout=None):
        sent["payload"] = json
        return _FakeStream(events)

    with patch("requests.post", side_effect=_fake_post):
        items = list(provider.generate_stream([{"role": "user", "content": PROMPT_SENTINEL}]))
    return items[:-1], items[-1], sent["payload"]


def test_claude_final_dict_carries_usage_and_keeps_existing_keys():
    provider = ClaudeLLMProvider(api_key=KEY_SENTINEL, model="claude-test-model-1")
    chunks, final, _ = _generate(provider, _claude_events(cache_read_input_tokens=20))
    assert chunks == ["Hello"]
    assert {"text", "latency_ms", "provider", "model"} <= set(final)
    assert final["text"] == "Hello" and final["provider"] == "claude" and final["model"] == "claude-test-model-1"
    assert final["usage"]["input_tokens"] == 100 and final["usage"]["output_tokens"] == 50
    assert final["usage"]["cache_read_input_tokens"] == 20


def test_claude_without_usage_events_returns_exactly_the_old_contract():
    events = [e for e in _claude_events() if e["type"] == "content_block_delta"]
    _, final, _ = _generate(ClaudeLLMProvider(api_key=KEY_SENTINEL, model="m"), events)
    assert set(final) == {"text", "latency_ms", "provider", "model"}


def test_gemini_final_dict_carries_usage():
    provider = GeminiLLMProvider(api_key=KEY_SENTINEL, model="gemini-test-1")
    events = [_gemini_chunk(promptTokenCount=100, cachedContentTokenCount=40, candidatesTokenCount=9)]
    _, final, _ = _generate(provider, events)
    assert final["provider"] == "gemini"
    assert final["usage"]["input_tokens"] == 60 and final["usage"]["cache_read_input_tokens"] == 40
    assert final["usage"]["output_tokens"] == 9 and final["usage"]["response_model"] == "gem-test-1"


def test_groq_final_dict_has_usage_only_if_the_stream_carried_it():
    events = [
        {"choices": [{"delta": {"content": "Hi"}}]},
        {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 2}},
    ]
    _, with_usage, _ = _generate(GroqLLMProvider(api_key=KEY_SENTINEL, model="g"), events)
    assert with_usage["usage"]["input_tokens"] == 7
    _, without, _ = _generate(GroqLLMProvider(api_key=KEY_SENTINEL, model="g"), events[:1])
    assert "usage" not in without and set(without) == {"text", "latency_ms", "provider", "model"}


def test_requests_sent_to_providers_are_unchanged_by_usage_tracking():
    # No stream_options / extra request fields were added to obtain usage.
    _, _, claude_payload = _generate(ClaudeLLMProvider(api_key=KEY_SENTINEL, model="m"), _claude_events())
    assert set(claude_payload) == {"model", "messages", "max_tokens", "temperature", "stream"}
    groq_events = [{"choices": [{"delta": {"content": "Hi"}}]}]
    _, _, groq_payload = _generate(GroqLLMProvider(api_key=KEY_SENTINEL, model="g"), groq_events)
    assert set(groq_payload) == {"model", "messages", "max_tokens", "temperature", "stream"}


def test_usage_never_contains_prompt_response_or_credentials():
    provider = ClaudeLLMProvider(api_key=KEY_SENTINEL, model="m")
    _, final, _ = _generate(provider, _claude_events(cache_read_input_tokens=1, cache_creation_input_tokens=1))
    assert set(final["usage"]) <= USAGE_KEYS
    blob = repr(final["usage"])
    assert PROMPT_SENTINEL not in blob and KEY_SENTINEL not in blob and "Hello" not in blob
    assert all(isinstance(v, (int, str)) for v in final["usage"].values())


def test_malformed_usage_does_not_break_the_response():
    events = _claude_events(input_tokens=-9)
    chunks, final, _ = _generate(ClaudeLLMProvider(api_key=KEY_SENTINEL, model="m"), events)
    assert chunks == ["Hello"] and final["text"] == "Hello" and "usage" not in final


def test_provider_errors_and_empty_responses_still_raise_and_carry_no_usage():
    provider = ClaudeLLMProvider(api_key=KEY_SENTINEL, model="m")
    only_usage = [e for e in _claude_events() if e["type"] != "content_block_delta"]
    with patch("requests.post", return_value=_FakeStream(only_usage)):
        with pytest.raises(LLMProviderError, match="empty response"):
            list(provider.generate_stream([{"role": "user", "content": "hi"}]))

    class _Err(_FakeStream):
        status_code = 500
        text = "boom"

    with patch("requests.post", return_value=_Err([])):
        with pytest.raises(LLMProviderError):
            list(provider.generate_stream([{"role": "user", "content": "hi"}]))


class _StubProvider(BaseLLMProvider):
    def __init__(self, name, usage=None, fail=False):
        self.provider_name = name
        self._usage = usage
        self._fail = fail

    def generate_stream(self, messages, **kwargs):
        if self._fail:
            raise LLMProviderError("down", provider=self.provider_name, retryable=True)
        yield "ok"
        final = {"text": "ok", "latency_ms": 1.0, "provider": self.provider_name, "model": "stub"}
        if self._usage:
            final["usage"] = self._usage
        yield final


def test_fallback_provider_preserves_the_serving_providers_usage():
    usage = {"usage_version": 1, "source": USAGE_SOURCE, "input_tokens": 3, "output_tokens": 4}
    served_by_primary = FallbackLLMProvider(_StubProvider("claude", usage), _StubProvider("gemini"))
    final = list(served_by_primary.generate_stream([]))[-1]
    assert final["usage"] == usage and final["fallback_used"] is False

    failed_over = FallbackLLMProvider(_StubProvider("claude", fail=True), _StubProvider("gemini", usage))
    final = list(failed_over.generate_stream([]))[-1]
    assert final["usage"] == usage and final["fallback_used"] is True and final["provider"] == "gemini"

    # Only the provider that answered is reported; a failed primary attempt contributes nothing.
    no_usage_fallback = FallbackLLMProvider(_StubProvider("claude", fail=True), _StubProvider("gemini"))
    assert "usage" not in list(no_usage_fallback.generate_stream([]))[-1]

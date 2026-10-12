"""
Provider-reported token usage extraction (src/inference/llm_usage.py).

Turns the usage metadata that remote LLM providers put in their streaming
responses into ONE canonical, additive structure that llm_provider.py attaches
to a successful response's final dict as ``"usage"``:

    {
        "usage_version": 1,
        "source": "provider_reported",
        "input_tokens": int,                 # prompt tokens NOT served from a cache
        "output_tokens": int,                # billed output; includes reasoning/thinking tokens
        "cache_read_input_tokens": int,      # only if the provider reported it
        "cache_creation_input_tokens": int,  # only if the provider reported it (Anthropic)
        "reasoning_tokens": int,             # only if reported; a SUBSET of output_tokens (informational)
        "response_model": str,               # the model the provider says it ran, if it said
    }

Rules (docs/AI_COST_TRACKING.md has the full table):

- Nothing is ever estimated or filled in. If a provider did not report both an
  input and an output count, or reported something malformed (negative,
  non-integer, bool, inconsistent), NO usage is emitted -- the response is still
  returned unchanged. Absent means "unknown", never zero.
- Providers disagree on whether the prompt count includes cached tokens
  (Anthropic: no; Gemini / OpenAI-compatible: yes). The canonical
  ``input_tokens`` always EXCLUDES cached tokens so a cost calculation can price
  the three input categories separately without double counting. The
  subtraction uses only values the provider itself reported.
- Extraction must never break generation: every ``observe`` swallows its own
  errors and the provider's stream carries on. Nothing here logs request or
  response content -- at most a provider name at DEBUG level.

This module reads only integers and a model name out of provider events; it
never stores or returns message text.
"""

import logging
from typing import Any, Mapping, Optional

logger = logging.getLogger("ai_voice_agent.llm_usage")

USAGE_VERSION = 1
USAGE_SOURCE = "provider_reported"


def _count(value: Any) -> Optional[int]:
    """A non-negative int token count, or None. bool, float, str, negatives and None are all rejected."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


class _Malformed(Exception):
    """Internal: a usage field was present but invalid, so the whole usage is discarded."""


def _optional_count(container: Mapping[str, Any], key: str) -> Optional[int]:
    """None if the key is absent; a valid count if present; raises _Malformed if present but invalid."""
    if key not in container or container[key] is None:
        return None
    count = _count(container[key])
    if count is None:
        raise _Malformed(key)
    return count


def _required_count(container: Mapping[str, Any], key: str) -> int:
    count = _optional_count(container, key)
    if count is None:
        raise _Malformed(key)
    return count


def _model_name(value: Any) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _canonical(
    input_tokens: int,
    output_tokens: int,
    *,
    cache_read: Optional[int] = None,
    cache_creation: Optional[int] = None,
    reasoning: Optional[int] = None,
    response_model: Optional[str] = None,
) -> dict[str, Any]:
    usage: dict[str, Any] = {
        "usage_version": USAGE_VERSION,
        "source": USAGE_SOURCE,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }
    if cache_read is not None:
        usage["cache_read_input_tokens"] = cache_read
    if cache_creation is not None:
        usage["cache_creation_input_tokens"] = cache_creation
    if reasoning is not None:
        usage["reasoning_tokens"] = reasoning
    if response_model is not None:
        usage["response_model"] = response_model
    return usage


class UsageAccumulator:
    """
    Feed every parsed streaming event to ``observe``; call ``result`` once at the
    end. ``result`` returns the canonical dict, or None when real usage is not
    available (never a partial or zero-filled dict).
    """

    provider = "base"

    def __init__(self) -> None:
        self._failed = False

    def observe(self, event: Any) -> None:
        if self._failed:
            return
        try:
            if isinstance(event, Mapping):
                self._observe(event)
        except Exception:  # never let usage parsing break a generation
            self._failed = True

    def result(self) -> Optional[dict[str, Any]]:
        if self._failed:
            logger.debug("%s usage metadata was malformed; usage omitted", self.provider)
            return None
        try:
            usage = self._build()
        except Exception:
            usage = None
        if usage is None:
            logger.debug("%s usage metadata unavailable; usage omitted", self.provider)
        return usage

    def _observe(self, event: Mapping[str, Any]) -> None:
        raise NotImplementedError

    def _build(self) -> Optional[dict[str, Any]]:
        raise NotImplementedError


class AnthropicUsageAccumulator(UsageAccumulator):
    """
    Anthropic Messages streaming: ``message_start`` carries ``message.usage`` (input
    and cache counts, plus an initial output count) and ``message_delta`` carries
    CUMULATIVE ``usage`` (output_tokens, and possibly others). A later value
    replaces an earlier one, except that message_start's ``output_tokens`` is
    ignored (a placeholder; see ``_take``). ``input_tokens`` already excludes
    cache reads and cache writes, which are reported separately.
    """

    provider = "claude"

    def __init__(self) -> None:
        super().__init__()
        self._fields: dict[str, int] = {}
        self._model: Optional[str] = None

    def _take(self, usage: Any, *, final: bool) -> None:
        if not isinstance(usage, Mapping):
            return
        keys = ["input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"]
        if final:
            # message_start's output_tokens is only a placeholder for the tokens
            # generated so far; the real total arrives with message_delta. A stream
            # cut before message_delta therefore has NO usable output count.
            keys.append("output_tokens")
        for key in keys:
            count = _optional_count(usage, key)
            if count is not None:
                self._fields[key] = count

    def _observe(self, event: Mapping[str, Any]) -> None:
        kind = event.get("type")
        if kind == "message_start":
            message = event.get("message")
            if isinstance(message, Mapping):
                self._model = _model_name(message.get("model")) or self._model
                self._take(message.get("usage"), final=False)
        elif kind == "message_delta":
            self._take(event.get("usage"), final=True)

    def _build(self) -> Optional[dict[str, Any]]:
        if "input_tokens" not in self._fields or "output_tokens" not in self._fields:
            return None
        return _canonical(
            self._fields["input_tokens"],
            self._fields["output_tokens"],
            cache_read=self._fields.get("cache_read_input_tokens"),
            cache_creation=self._fields.get("cache_creation_input_tokens"),
            response_model=self._model,
        )


class GeminiUsageAccumulator(UsageAccumulator):
    """
    Gemini streamGenerateContent: ``usageMetadata`` appears on streamed chunks and
    the LAST one seen is kept. ``promptTokenCount`` INCLUDES ``cachedContentTokenCount``
    (cached tokens are subtracted to get the canonical uncached input), and
    ``thoughtsTokenCount`` is billed as output but reported separately from
    ``candidatesTokenCount`` (they are summed). ``toolUsePromptTokenCount`` is input.
    Gemini's JSON omits zero-valued fields, so an absent optional field counts as 0
    in the arithmetic; it is simply not asserted in the result.
    """

    provider = "gemini"

    def __init__(self) -> None:
        super().__init__()
        self._metadata: Optional[Mapping[str, Any]] = None
        self._model: Optional[str] = None

    def _observe(self, event: Mapping[str, Any]) -> None:
        self._model = _model_name(event.get("modelVersion")) or self._model
        metadata = event.get("usageMetadata")
        if isinstance(metadata, Mapping):
            self._metadata = metadata

    def _build(self) -> Optional[dict[str, Any]]:
        meta = self._metadata
        if meta is None:
            return None
        prompt = _required_count(meta, "promptTokenCount")
        candidates = _required_count(meta, "candidatesTokenCount")
        cached = _optional_count(meta, "cachedContentTokenCount")
        thoughts = _optional_count(meta, "thoughtsTokenCount")
        tool_use = _optional_count(meta, "toolUsePromptTokenCount")
        if cached is not None and cached > prompt:
            raise _Malformed("cachedContentTokenCount")
        return _canonical(
            (prompt - (cached or 0)) + (tool_use or 0),
            candidates + (thoughts or 0),
            cache_read=cached,
            reasoning=thoughts,
            response_model=self._model,
        )


class OpenAIStyleUsageAccumulator(UsageAccumulator):
    """
    OpenAI-compatible chat-completions streaming (Groq). Usage is read from a
    chunk's top-level ``usage`` or from ``x_groq.usage`` if a chunk carries one;
    the last seen wins. ``prompt_tokens`` INCLUDES ``prompt_tokens_details.cached_tokens``
    and ``completion_tokens`` includes reasoning tokens.

    Whether Groq actually sends usage on streamed chunks is NOT confirmed (this
    repo does not set ``stream_options``, and changing the request is out of
    scope), so this is expected to yield no usage until verified live.
    """

    provider = "groq"

    def __init__(self) -> None:
        super().__init__()
        self._usage: Optional[Mapping[str, Any]] = None
        self._model: Optional[str] = None

    def _observe(self, event: Mapping[str, Any]) -> None:
        self._model = _model_name(event.get("model")) or self._model
        for holder in (event, event.get("x_groq")):
            if isinstance(holder, Mapping) and isinstance(holder.get("usage"), Mapping):
                self._usage = holder["usage"]

    def _build(self) -> Optional[dict[str, Any]]:
        usage = self._usage
        if usage is None:
            return None
        prompt = _required_count(usage, "prompt_tokens")
        completion = _required_count(usage, "completion_tokens")
        cached = reasoning = None
        prompt_details = usage.get("prompt_tokens_details")
        if isinstance(prompt_details, Mapping):
            cached = _optional_count(prompt_details, "cached_tokens")
        completion_details = usage.get("completion_tokens_details")
        if isinstance(completion_details, Mapping):
            reasoning = _optional_count(completion_details, "reasoning_tokens")
        if cached is not None and cached > prompt:
            raise _Malformed("cached_tokens")
        if reasoning is not None and reasoning > completion:
            raise _Malformed("reasoning_tokens")
        return _canonical(
            prompt - (cached or 0),
            completion,
            cache_read=cached,
            reasoning=reasoning,
            response_model=self._model,
        )

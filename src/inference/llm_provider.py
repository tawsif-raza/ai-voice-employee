"""
LLM Provider Abstraction Layer (src/inference/llm_provider.py)

Decouples LLM reasoning from any specific model provider, supporting:
1. Claude (Anthropic) as Primary provider (Claude 3.5 Haiku / Sonnet).
2. Gemini (Google) as Automatic Fallback provider (Gemini 2.5 Flash / 2.0 Flash)
   when Claude quota is exhausted, rate-limited (HTTP 429), or overloaded (HTTP 529).
3. Local (Qwen 2.5) provider preserving full backward compatibility with existing
   in-repo models and offline tests.
4. FallbackLLMProvider orchestrating seamless failover without coupling business
   logic to either vendor.

Contract:
Every provider implements `generate_stream(messages, **kwargs)` yielding text tokens
and ending with `{"text": full_text, "latency_ms": latency_ms, "provider": name, ...}`
dict, exactly matching ConversationManager's expectations.
"""

import json
import logging
import os
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional, Union

import yaml

# Ensure agent directory is in sys.path for observability models if needed
_AGENT_DIR = str(Path(__file__).resolve().parents[1] / "agent")
if _AGENT_DIR not in sys.path:
    sys.path.insert(0, _AGENT_DIR)

logger = logging.getLogger("ai_voice_agent.llm_provider")


# ── Exceptions ───────────────────────────────────────────────────────────────


class LLMProviderError(RuntimeError):
    """Base exception for LLM provider errors."""

    def __init__(self, message: str, provider: str, status_code: Optional[int] = None, retryable: bool = False):
        super().__init__(f"[{provider}] {message}")
        self.provider = provider
        self.status_code = status_code
        self.retryable = retryable


class LLMQuotaExceededError(LLMProviderError):
    """Raised when an LLM provider returns 429 (rate limit) or quota/credit exhaustion."""

    def __init__(self, message: str, provider: str, status_code: int = 429):
        super().__init__(message, provider=provider, status_code=status_code, retryable=True)


class LLMOverloadedError(LLMProviderError):
    """Raised when an LLM provider returns 529 or 503 (server overloaded)."""

    def __init__(self, message: str, provider: str, status_code: int = 529):
        super().__init__(message, provider=provider, status_code=status_code, retryable=True)


# ── Abstract Base Provider ──────────────────────────────────────────────────


class BaseLLMProvider(ABC):
    """
    Abstract interface for LLM providers.
    All business logic and safety checks in ConversationManager interface with
    this base class, never with provider-specific SDKs directly.
    """

    provider_name: str = "base"

    @abstractmethod
    def generate_stream(
        self,
        messages: list[dict],
        max_new_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        **kwargs,
    ) -> Iterator[Union[str, dict]]:
        """
        Stream text response from the provider.
        Yields:
            str chunks as they arrive.
            final dict: {"text": str, "latency_ms": float, "provider": str, "model": str}
        """
        pass


# ── Claude (Anthropic) Provider ─────────────────────────────────────────────


class ClaudeLLMProvider(BaseLLMProvider):
    """
    Anthropic Claude provider (Primary).
    Supports Claude 3.5 Haiku (recommended for voice latency) and Claude 3.5 Sonnet.
    Uses direct HTTP/SSE streaming over requests/httpx to eliminate third-party
    SDK version pinning conflicts.
    """

    provider_name = "claude"
    DEFAULT_MODEL = "claude-3-5-haiku-latest"
    API_URL = "https://api.anthropic.com/v1/messages"
    ANTHROPIC_VERSION = "2023-06-01"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout_seconds: float = 30.0,
        default_max_tokens: int = 350,
        default_temperature: float = 0.7,
    ):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.model = model or os.environ.get("ANTHROPIC_MODEL", self.DEFAULT_MODEL)
        self.timeout_seconds = timeout_seconds
        self.default_max_tokens = default_max_tokens
        self.default_temperature = default_temperature

    def _convert_messages(self, messages: list[dict]) -> tuple[Optional[str], list[dict]]:
        """Extract system prompt and convert standard message list to Anthropic format."""
        system_prompt = None
        anthropic_msgs = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "system":
                if system_prompt:
                    system_prompt += "\n\n" + content
                else:
                    system_prompt = content
            else:
                anthropic_role = "assistant" if role == "assistant" else "user"
                anthropic_msgs.append({"role": anthropic_role, "content": content})
        return system_prompt, anthropic_msgs

    def generate_stream(
        self,
        messages: list[dict],
        max_new_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        **kwargs,
    ) -> Iterator[Union[str, dict]]:
        import requests

        if not self.api_key:
            raise LLMProviderError("ANTHROPIC_API_KEY is not configured", provider=self.provider_name)

        system_prompt, formatted_msgs = self._convert_messages(messages)
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": self.ANTHROPIC_VERSION,
            "content-type": "application/json",
            "accept": "text/event-stream",
        }
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": formatted_msgs,
            "max_tokens": max_new_tokens or self.default_max_tokens,
            "temperature": temperature if temperature is not None else self.default_temperature,
            "stream": True,
        }
        if system_prompt:
            payload["system"] = system_prompt
        if top_p is not None:
            payload["top_p"] = top_p

        start_time = time.perf_counter()
        accumulated_text = []

        try:
            with requests.post(
                self.API_URL,
                headers=headers,
                json=payload,
                stream=True,
                timeout=self.timeout_seconds,
            ) as resp:
                if resp.status_code == 429:
                    error_msg = resp.text
                    try:
                        error_msg = resp.json().get("error", {}).get("message", error_msg)
                    except Exception:
                        pass
                    raise LLMQuotaExceededError(
                        f"Claude rate limit/quota exhausted: {error_msg}", provider=self.provider_name
                    )

                if resp.status_code in (529, 503):
                    raise LLMOverloadedError(
                        f"Claude server overloaded (status {resp.status_code})", provider=self.provider_name
                    )

                if resp.status_code != 200:
                    raise LLMProviderError(
                        f"Claude API failed with status {resp.status_code}: {resp.text}",
                        provider=self.provider_name,
                        status_code=resp.status_code,
                    )

                for line in resp.iter_lines(decode_unicode=True):
                    if not line or not line.startswith("data: "):
                        continue
                    data_str = line[6:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        event = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    evt_type = event.get("type")
                    if evt_type == "content_block_delta":
                        delta = event.get("delta", {})
                        if delta.get("type") == "text_delta":
                            chunk = delta.get("text", "")
                            if chunk:
                                accumulated_text.append(chunk)
                                yield chunk
                    elif evt_type == "error":
                        err_obj = event.get("error", {})
                        err_type = err_obj.get("type")
                        if err_type in ("rate_limit_error", "quota_exceeded"):
                            raise LLMQuotaExceededError(
                                err_obj.get("message", "Quota exceeded"), provider=self.provider_name
                            )
                        raise LLMProviderError(err_obj.get("message", "Streaming error"), provider=self.provider_name)

        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            raise LLMProviderError(
                f"Claude network connection error: {exc}", provider=self.provider_name, retryable=True
            ) from exc

        latency_ms = (time.perf_counter() - start_time) * 1000
        full_text = "".join(accumulated_text).strip()
        yield {
            "text": full_text,
            "latency_ms": latency_ms,
            "provider": self.provider_name,
            "model": self.model,
        }


# ── Gemini (Google) Provider ────────────────────────────────────────────────


class GeminiLLMProvider(BaseLLMProvider):
    """
    Google Gemini provider (Fallback / Alternative).
    Supports Gemini 2.5 Flash / 2.0 Flash / 1.5 Flash.
    Uses streaming REST API via Server-Sent Events (SSE) directly.
    """

    provider_name = "gemini"
    DEFAULT_MODEL = "gemini-2.5-flash"
    API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout_seconds: float = 30.0,
        default_max_tokens: int = 350,
        default_temperature: float = 0.7,
    ):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        self.model = model or os.environ.get("GEMINI_MODEL", self.DEFAULT_MODEL)
        self.timeout_seconds = timeout_seconds
        self.default_max_tokens = default_max_tokens
        self.default_temperature = default_temperature

    def _convert_messages(self, messages: list[dict]) -> tuple[Optional[str], list[dict]]:
        """Extract system instructions and format user/model contents for Gemini."""
        system_instruction = None
        gemini_contents = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "system":
                if system_instruction:
                    system_instruction += "\n\n" + content
                else:
                    system_instruction = content
            else:
                gemini_role = "model" if role == "assistant" else "user"
                gemini_contents.append(
                    {
                        "role": gemini_role,
                        "parts": [{"text": content}],
                    }
                )
        return system_instruction, gemini_contents

    def generate_stream(
        self,
        messages: list[dict],
        max_new_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        **kwargs,
    ) -> Iterator[Union[str, dict]]:
        import requests

        if not self.api_key:
            raise LLMProviderError("GEMINI_API_KEY is not configured", provider=self.provider_name)

        system_instruction, contents = self._convert_messages(messages)
        endpoint = f"{self.API_BASE}/{self.model}:streamGenerateContent?alt=sse&key={self.api_key}"

        gen_config: dict[str, Any] = {
            "maxOutputTokens": max_new_tokens or self.default_max_tokens,
            "temperature": temperature if temperature is not None else self.default_temperature,
        }
        if top_p is not None:
            gen_config["topP"] = top_p
        if "2.5" in self.model:
            # Gemini 2.5 models reserve part of maxOutputTokens for internal
            # "thinking" tokens by default, and how much they spend is
            # non-deterministic per call. With a modest token budget this can
            # consume the entire budget on reasoning and leave zero tokens
            # for visible text -- a real HTTP 200 with a genuinely empty
            # response, no exception raised. Demonstrated live against the
            # real API (docs/phase1.4-external-integration-report.md);
            # disabled here since this app needs a visible reply within its
            # configured token budget, not internal reasoning. Only applied
            # to 2.5 models -- older models (1.5/2.0) don't recognize this
            # field.
            gen_config["thinkingConfig"] = {"thinkingBudget": 0}

        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": gen_config,
        }
        if system_instruction:
            payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}

        start_time = time.perf_counter()
        accumulated_text = []

        try:
            with requests.post(
                endpoint,
                headers={"Content-Type": "application/json"},
                json=payload,
                stream=True,
                timeout=self.timeout_seconds,
            ) as resp:
                if resp.status_code == 429:
                    error_msg = resp.text
                    try:
                        error_msg = resp.json().get("error", {}).get("message", error_msg)
                    except Exception:
                        pass
                    raise LLMQuotaExceededError(
                        f"Gemini rate limit/quota exhausted: {error_msg}", provider=self.provider_name
                    )

                if resp.status_code in (503, 500):
                    raise LLMOverloadedError(
                        f"Gemini server error (status {resp.status_code})", provider=self.provider_name
                    )

                if resp.status_code != 200:
                    raise LLMProviderError(
                        f"Gemini API failed with status {resp.status_code}: {resp.text}",
                        provider=self.provider_name,
                        status_code=resp.status_code,
                    )

                for line in resp.iter_lines(decode_unicode=True):
                    if not line or not line.startswith("data: "):
                        continue
                    data_str = line[6:].strip()
                    try:
                        event = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    candidates = event.get("candidates", [])
                    if candidates:
                        parts = candidates[0].get("content", {}).get("parts", [])
                        for part in parts:
                            text_chunk = part.get("text", "")
                            if text_chunk:
                                accumulated_text.append(text_chunk)
                                yield text_chunk

        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            raise LLMProviderError(
                f"Gemini network connection error: {exc}", provider=self.provider_name, retryable=True
            ) from exc

        latency_ms = (time.perf_counter() - start_time) * 1000
        full_text = "".join(accumulated_text).strip()
        yield {
            "text": full_text,
            "latency_ms": latency_ms,
            "provider": self.provider_name,
            "model": self.model,
        }


# ── Local Model (Qwen) Provider ─────────────────────────────────────────────


class LocalLLMProvider(BaseLLMProvider):
    """
    Wraps the existing in-repo LLMService (Qwen 2.5) for offline tests,
    development without cloud credentials, or CPU/CUDA local inference.
    """

    provider_name = "local"

    def __init__(self, llm_service=None, **llm_kwargs):
        if llm_service is not None:
            self._service = llm_service
        else:
            from llm_service import LLMService

            self._service = LLMService(**llm_kwargs)

    def generate_stream(
        self,
        messages: list[dict],
        max_new_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        repetition_penalty: Optional[float] = None,
        **kwargs,
    ) -> Iterator[Union[str, dict]]:
        result_stream = self._service.generate_stream(
            messages,
            max_new_tokens=max_new_tokens,
            temperature=temperature,
            top_p=top_p,
            repetition_penalty=repetition_penalty,
        )
        for item in result_stream:
            if isinstance(item, dict):
                item["provider"] = self.provider_name
                item["model"] = getattr(self._service, "base_model_name", "local-qwen")
                yield item
            else:
                yield item


# ── Automatic Fallback Provider (Claude Primary ➔ Gemini Fallback) ──────────


class FallbackLLMProvider(BaseLLMProvider):
    """
    Resilient provider that attempts generation via `primary` (Claude),
    and automatically falls back to `fallback` (Gemini) when the primary:
    1. Raises LLMQuotaExceededError (HTTP 429 / Quota exhausted)
    2. Raises LLMOverloadedError (HTTP 529 / Service unavailable)
    3. Fails before streaming any tokens to the client.

    Maintains a temporary cooldown window after a quota failure so subsequent
    turns route directly to the fallback without paying the failed primary's latency.
    """

    provider_name = "fallback_orchestrator"

    def __init__(
        self,
        primary: BaseLLMProvider,
        fallback: BaseLLMProvider,
        cooldown_seconds: float = 60.0,
        audit_logger=None,
        metrics=None,
    ):
        self.primary = primary
        self.fallback = fallback
        self.cooldown_seconds = cooldown_seconds
        self.audit_logger = audit_logger
        self.metrics = metrics
        self._primary_cooldown_until: float = 0.0

    @property
    def is_primary_in_cooldown(self) -> bool:
        return time.time() < self._primary_cooldown_until

    def trigger_cooldown(self, reason: str = "Quota exhausted"):
        self._primary_cooldown_until = time.time() + self.cooldown_seconds
        logger.warning(
            "Primary LLM (%s) placed in cooldown for %ss. Reason: %s",
            self.primary.provider_name,
            self.cooldown_seconds,
            reason,
        )
        if self.metrics:
            self.metrics.increment("llm_fallback_cooldown_triggered_total")

    def generate_stream(
        self,
        messages: list[dict],
        max_new_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        **kwargs,
    ) -> Iterator[Union[str, dict]]:
        # If primary is currently in quota cooldown, route directly to fallback
        if self.is_primary_in_cooldown:
            logger.info(
                "Primary (%s) in cooldown; routing turn directly to fallback (%s)",
                self.primary.provider_name,
                self.fallback.provider_name,
            )
            if self.metrics:
                self.metrics.increment("llm_fallback_used_total")
            yield from self._run_fallback(
                messages, max_new_tokens, temperature, top_p, reason="primary_in_cooldown", **kwargs
            )
            return

        # Attempt primary provider
        stream_started = False
        chunks: list[str] = []
        try:
            for item in self.primary.generate_stream(
                messages, max_new_tokens=max_new_tokens, temperature=temperature, top_p=top_p, **kwargs
            ):
                if isinstance(item, str):
                    stream_started = True
                    chunks.append(item)
                    yield item
                else:
                    item["fallback_used"] = False
                    yield item
            return
        except (LLMQuotaExceededError, LLMOverloadedError, LLMProviderError) as exc:
            if stream_started:
                # If output has already been sent to the caller, we cannot cleanly
                # restart from fallback without confusing the user. Re-raise for ConversationManager.
                logger.error("Primary (%s) failed mid-stream after tokens yielded: %s", self.primary.provider_name, exc)
                raise

            # Automatic Failover triggered!
            logger.warning(
                "Primary (%s) failed before output: %s. Initiating automatic failover to %s.",
                self.primary.provider_name,
                exc,
                self.fallback.provider_name,
            )
            if isinstance(exc, LLMQuotaExceededError):
                self.trigger_cooldown(str(exc))

            if self.audit_logger:
                try:
                    from observability_models import EventType

                    self.audit_logger.record(
                        EventType.RETRY_ATTEMPT,
                        outcome="failover",
                        action="llm_generate",
                        reason=f"Primary {self.primary.provider_name} failed ({type(exc).__name__}), switching to {self.fallback.provider_name}",
                        metadata={"primary": self.primary.provider_name, "fallback": self.fallback.provider_name},
                    )
                except Exception:
                    pass

            if self.metrics:
                self.metrics.increment("llm_failover_events_total")

            # Stream from fallback
            yield from self._run_fallback(
                messages, max_new_tokens, temperature, top_p, reason=f"primary_failed_{type(exc).__name__}", **kwargs
            )

    def _run_fallback(
        self,
        messages: list[dict],
        max_new_tokens: Optional[int],
        temperature: Optional[float],
        top_p: Optional[float],
        reason: str,
        **kwargs,
    ) -> Iterator[Union[str, dict]]:
        for item in self.fallback.generate_stream(
            messages, max_new_tokens=max_new_tokens, temperature=temperature, top_p=top_p, **kwargs
        ):
            if isinstance(item, dict):
                item["fallback_used"] = True
                item["fallback_reason"] = reason
                yield item
            else:
                yield item


# ── Configuration Loader & Provider Factory ─────────────────────────────────

_CONFIG_PATH = Path(__file__).resolve().parents[2] / "configs" / "config.yaml"


@dataclass(frozen=True)
class ProviderRuntimeConfig:
    model: str
    timeout_seconds: float
    max_tokens: int
    temperature: float


@dataclass(frozen=True)
class LLMRuntimeConfig:
    provider: str
    claude: ProviderRuntimeConfig
    gemini: ProviderRuntimeConfig
    fallback_cooldown_seconds: float


def load_llm_config(config_path: Optional[Path] = None) -> LLMRuntimeConfig:
    """
    Load LLM configuration merging configs/config.yaml and environment variable overrides.
    Priority: Explicit Env Var > config.yaml > Built-in Defaults.
    """
    path = config_path or _CONFIG_PATH
    yaml_cfg: dict[str, Any] = {}
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
                yaml_cfg = data.get("llm", {})
        except Exception as e:
            logger.warning("Failed to parse config.yaml for LLM config: %s", e)

    claude_cfg = yaml_cfg.get("claude", {})
    gemini_cfg = yaml_cfg.get("gemini", {})
    fallback_cfg = yaml_cfg.get("fallback", {})

    provider = (
        (
            os.environ.get("LLM_PROVIDER")
            or yaml_cfg.get("provider")
            or ("fallback" if (os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("GEMINI_API_KEY")) else "local")
        )
        .strip()
        .lower()
    )

    claude_model = os.environ.get("ANTHROPIC_MODEL") or claude_cfg.get("model") or "claude-3-5-haiku-latest"
    claude_timeout = float(os.environ.get("LLM_TIMEOUT_SECONDS") or claude_cfg.get("timeout_seconds") or 30.0)
    claude_max_tokens = int(os.environ.get("LLM_MAX_TOKENS") or claude_cfg.get("max_tokens") or 350)
    claude_temp = float(os.environ.get("LLM_TEMPERATURE") or claude_cfg.get("temperature") or 0.7)

    gemini_model = os.environ.get("GEMINI_MODEL") or gemini_cfg.get("model") or "gemini-2.5-flash"
    gemini_timeout = float(os.environ.get("LLM_TIMEOUT_SECONDS") or gemini_cfg.get("timeout_seconds") or 30.0)
    gemini_max_tokens = int(os.environ.get("LLM_MAX_TOKENS") or gemini_cfg.get("max_tokens") or 350)
    gemini_temp = float(os.environ.get("LLM_TEMPERATURE") or gemini_cfg.get("temperature") or 0.7)

    cooldown = float(os.environ.get("LLM_FALLBACK_COOLDOWN_SECONDS") or fallback_cfg.get("cooldown_seconds") or 60.0)

    return LLMRuntimeConfig(
        provider=provider,
        claude=ProviderRuntimeConfig(
            model=claude_model, timeout_seconds=claude_timeout, max_tokens=claude_max_tokens, temperature=claude_temp
        ),
        gemini=ProviderRuntimeConfig(
            model=gemini_model, timeout_seconds=gemini_timeout, max_tokens=gemini_max_tokens, temperature=gemini_temp
        ),
        fallback_cooldown_seconds=cooldown,
    )


def build_llm_provider(
    provider_mode: Optional[str] = None,
    audit_logger=None,
    metrics=None,
    config_path: Optional[Path] = None,
    **kwargs,
) -> BaseLLMProvider:
    """
    Factory to construct the configured LLM provider hierarchy.
    Reads declarative configuration from configs/config.yaml and environment variables.

    Provider modes:
      - "fallback" (default in production): Claude Primary -> Gemini Fallback
      - "claude": Claude only
      - "gemini": Gemini only
      - "local": Local Qwen model (HuggingFace)
    """
    config = load_llm_config(config_path)
    mode = (provider_mode or config.provider).strip().lower()

    if mode == "fallback":
        primary = ClaudeLLMProvider(
            model=config.claude.model,
            timeout_seconds=config.claude.timeout_seconds,
            default_max_tokens=config.claude.max_tokens,
            default_temperature=config.claude.temperature,
        )
        fallback = GeminiLLMProvider(
            model=config.gemini.model,
            timeout_seconds=config.gemini.timeout_seconds,
            default_max_tokens=config.gemini.max_tokens,
            default_temperature=config.gemini.temperature,
        )
        return FallbackLLMProvider(
            primary=primary,
            fallback=fallback,
            cooldown_seconds=config.fallback_cooldown_seconds,
            audit_logger=audit_logger,
            metrics=metrics,
        )
    elif mode == "claude":
        return ClaudeLLMProvider(
            model=config.claude.model,
            timeout_seconds=config.claude.timeout_seconds,
            default_max_tokens=config.claude.max_tokens,
            default_temperature=config.claude.temperature,
        )
    elif mode == "gemini":
        return GeminiLLMProvider(
            model=config.gemini.model,
            timeout_seconds=config.gemini.timeout_seconds,
            default_max_tokens=config.gemini.max_tokens,
            default_temperature=config.gemini.temperature,
        )
    elif mode == "local":
        return LocalLLMProvider(**kwargs)
    else:
        logger.warning("Unrecognized LLM_PROVIDER='%s', defaulting to local provider", mode)
        return LocalLLMProvider(**kwargs)

"""
Phase 1.4 external integration test harness -- REAL Claude/Gemini provider
verification (docs/phase1.4-external-integration-report.md Section 1).

This script is deliberately NOT a pytest test file (it lives in scripts/,
not tests/) so the normal test suite never collects or runs it, and it
never runs in CI unless a CI job explicitly opts in and supplies real
credentials.

Opt-in contract:
  - Requires RUN_LIVE_PROVIDER_TESTS=1 in the environment. Without it, this
    script refuses to run ANY live network call and exits immediately,
    explaining why (see main()).
  - Requires real ANTHROPIC_API_KEY / GEMINI_API_KEY. A missing key marks
    that provider's scenarios UNVERIFIED (or NOT RUN if BOTH are missing),
    never PASS, and never fabricates a response.
  - Never logs an API key value, a full Gemini endpoint URL (which embeds
    the key as a query parameter), or any header. Only booleans
    ("configured: true/false") and structured results (provider, scenario,
    latency_ms, result, error_category) are ever printed or written to the
    results file.

Usage:
    $env:RUN_LIVE_PROVIDER_TESTS = "1"
    $env:ANTHROPIC_API_KEY = "<test-safe key>"
    $env:GEMINI_API_KEY = "<test-safe key>"
    python scripts/live_provider_verification.py

Output: prints a human-readable summary and writes structured results to
docs/phase1.4-live-provider-results.json for the phase report to consume.
"""

import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "inference"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "agent"))

RESULTS_PATH = Path(__file__).resolve().parents[1] / "docs" / "phase1.4-live-provider-results.json"

_RESULTS: list[dict] = []


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record(
    provider: str,
    scenario: str,
    result: str,
    source: str,
    latency_ms=None,
    error_category=None,
    remediation=None,
    note=None,
) -> None:
    entry = {
        "provider": provider,
        "scenario": scenario,
        "timestamp": _now_iso(),
        "latency_ms": round(latency_ms, 1) if latency_ms is not None else None,
        "result": result,  # PASS / FAIL / UNVERIFIED / NOT RUN
        "source": source,  # LIVE / LOCAL / SIMULATED
        "error_category": error_category,
        "remediation": remediation,
        "note": note,
    }
    _RESULTS.append(entry)
    print(f"[{source}] {provider}/{scenario}: {result}" + (f" ({latency_ms:.0f}ms)" if latency_ms else ""))


def _not_run_all(provider: str, scenarios: list[str], reason: str) -> None:
    for s in scenarios:
        _record(provider, s, "NOT RUN", "LIVE", error_category="missing_credentials", remediation=reason)


# ── Claude scenarios ─────────────────────────────────────────────────────────


def run_claude_scenarios(ClaudeLLMProvider, LLMProviderError, api_key: str) -> "object | None":
    """Returns a working ClaudeLLMProvider instance if the successful-request
    scenario passed, else None (so the failover test below can decide
    whether a real Claude failure vs Claude success is available)."""
    provider = ClaudeLLMProvider(api_key=api_key)
    working_provider = None

    # 1. Successful real request + streaming response
    try:
        t0 = time.perf_counter()
        chunks = []
        final = None
        for item in provider.generate_stream(
            [{"role": "user", "content": "Reply with exactly the word: OK"}], max_new_tokens=10
        ):
            if isinstance(item, str):
                chunks.append(item)
            else:
                final = item
        latency_ms = (time.perf_counter() - t0) * 1000
        if final is not None and len(chunks) >= 1:
            _record("claude", "successful_real_request", "PASS", "LIVE", latency_ms)
            _record(
                "claude",
                "streaming_response",
                "PASS",
                "LIVE",
                latency_ms,
                note=f"{len(chunks)} streamed chunk(s) before the final summary item",
            )
            working_provider = provider
        else:
            _record("claude", "successful_real_request", "FAIL", "LIVE", latency_ms, error_category="empty_response")
            _record("claude", "streaming_response", "FAIL", "LIVE", latency_ms, error_category="no_chunks_streamed")
    except Exception as exc:
        _record(
            "claude",
            "successful_real_request",
            "FAIL",
            "LIVE",
            error_category=type(exc).__name__,
            remediation="Check ANTHROPIC_API_KEY validity and network egress.",
        )
        _record("claude", "streaming_response", "FAIL", "LIVE", error_category=type(exc).__name__)

    # 2. Timeout handling -- a real network call with an unreasonably short timeout.
    try:
        timeout_provider = ClaudeLLMProvider(api_key=api_key, timeout_seconds=0.001)
        t0 = time.perf_counter()
        try:
            for _ in timeout_provider.generate_stream([{"role": "user", "content": "hi"}], max_new_tokens=5):
                pass
            _record(
                "claude",
                "timeout_handling",
                "FAIL",
                "LIVE",
                remediation="Expected a timeout error but the call succeeded.",
            )
        except LLMProviderError as exc:
            latency_ms = (time.perf_counter() - t0) * 1000
            _record(
                "claude",
                "timeout_handling",
                "PASS",
                "LIVE",
                latency_ms,
                note=f"raised {type(exc).__name__} as expected, did not hang or crash the process",
            )
    except Exception as exc:
        _record("claude", "timeout_handling", "FAIL", "LIVE", error_category=type(exc).__name__)

    # 4. Provider error handling -- a real call with a deliberately invalid
    # model name, so the provider genuinely returns a real 4xx.
    try:
        bad_model_provider = ClaudeLLMProvider(api_key=api_key, model="not-a-real-claude-model-xyz")
        t0 = time.perf_counter()
        try:
            for _ in bad_model_provider.generate_stream([{"role": "user", "content": "hi"}], max_new_tokens=5):
                pass
            _record(
                "claude",
                "provider_error_handling",
                "FAIL",
                "LIVE",
                remediation="Expected a provider error for an invalid model but the call succeeded.",
            )
        except LLMProviderError as exc:
            latency_ms = (time.perf_counter() - t0) * 1000
            _record(
                "claude",
                "provider_error_handling",
                "PASS",
                "LIVE",
                latency_ms,
                note=f"raised {type(exc).__name__} as expected, did not crash the process",
            )
    except Exception as exc:
        _record("claude", "provider_error_handling", "FAIL", "LIVE", error_category=type(exc).__name__)

    return working_provider


def _run_claude_malformed_response_simulation(ClaudeLLMProvider) -> None:
    """LOCAL/SIMULATED: monkeypatches requests.post to return a deliberately
    malformed SSE stream (garbage JSON, an unexpected event type, and no
    terminal [DONE]), and confirms generate_stream() degrades gracefully
    (yields whatever valid content it found, then the final summary item)
    instead of hanging or raising an unhandled exception."""
    import requests

    class _FakeResponse:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def iter_lines(self, decode_unicode=True):
            yield "data: not valid json at all"
            yield 'data: {"type": "some_unexpected_event_type", "foo": "bar"}'
            yield ""
            yield 'data: {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "partial"}}'
            # deliberately no [DONE] terminator

    provider = ClaudeLLMProvider(api_key="fake-key-for-local-simulation-only")
    orig_post = requests.post
    requests.post = lambda *a, **k: _FakeResponse()
    try:
        chunks = []
        final = None
        for item in provider.generate_stream([{"role": "user", "content": "hi"}]):
            if isinstance(item, str):
                chunks.append(item)
            else:
                final = item
        ok = final is not None and "partial" in chunks
        _record(
            "claude",
            "malformed_response_handling",
            "PASS" if ok else "FAIL",
            "LOCAL",
            note="garbage/unexpected SSE lines ignored, valid partial content still surfaced, no hang/crash",
        )
    finally:
        requests.post = orig_post


# ── Gemini scenarios ─────────────────────────────────────────────────────────


def run_gemini_scenarios(GeminiLLMProvider, LLMProviderError, api_key: str) -> "object | None":
    provider = GeminiLLMProvider(api_key=api_key)
    working_provider = None

    try:
        t0 = time.perf_counter()
        chunks = []
        final = None
        for item in provider.generate_stream(
            [{"role": "user", "content": "Reply with exactly the word: OK"}], max_new_tokens=10
        ):
            if isinstance(item, str):
                chunks.append(item)
            else:
                final = item
        latency_ms = (time.perf_counter() - t0) * 1000
        if final is not None and len(chunks) >= 1:
            _record("gemini", "successful_real_request", "PASS", "LIVE", latency_ms)
            _record("gemini", "streaming_response", "PASS", "LIVE", latency_ms, note=f"{len(chunks)} streamed chunk(s)")
            working_provider = provider
        else:
            _record("gemini", "successful_real_request", "FAIL", "LIVE", latency_ms, error_category="empty_response")
            _record("gemini", "streaming_response", "FAIL", "LIVE", latency_ms, error_category="no_chunks_streamed")
    except Exception as exc:
        _record(
            "gemini",
            "successful_real_request",
            "FAIL",
            "LIVE",
            error_category=type(exc).__name__,
            remediation="Check GEMINI_API_KEY validity and network egress.",
        )
        _record("gemini", "streaming_response", "FAIL", "LIVE", error_category=type(exc).__name__)

    try:
        timeout_provider = GeminiLLMProvider(api_key=api_key, timeout_seconds=0.001)
        t0 = time.perf_counter()
        try:
            for _ in timeout_provider.generate_stream([{"role": "user", "content": "hi"}], max_new_tokens=5):
                pass
            _record(
                "gemini",
                "timeout_handling",
                "FAIL",
                "LIVE",
                remediation="Expected a timeout error but the call succeeded.",
            )
        except LLMProviderError as exc:
            latency_ms = (time.perf_counter() - t0) * 1000
            _record(
                "gemini",
                "timeout_handling",
                "PASS",
                "LIVE",
                latency_ms,
                note=f"raised {type(exc).__name__} as expected",
            )
    except Exception as exc:
        _record("gemini", "timeout_handling", "FAIL", "LIVE", error_category=type(exc).__name__)

    try:
        bad_model_provider = GeminiLLMProvider(api_key=api_key, model="not-a-real-gemini-model-xyz")
        t0 = time.perf_counter()
        try:
            for _ in bad_model_provider.generate_stream([{"role": "user", "content": "hi"}], max_new_tokens=5):
                pass
            _record(
                "gemini",
                "provider_error_handling",
                "FAIL",
                "LIVE",
                remediation="Expected a provider error for an invalid model but the call succeeded.",
            )
        except LLMProviderError as exc:
            latency_ms = (time.perf_counter() - t0) * 1000
            _record(
                "gemini",
                "provider_error_handling",
                "PASS",
                "LIVE",
                latency_ms,
                note=f"raised {type(exc).__name__} as expected",
            )
    except Exception as exc:
        _record("gemini", "provider_error_handling", "FAIL", "LIVE", error_category=type(exc).__name__)

    return working_provider


# ── Failover scenario (Claude fails for real -> Gemini succeeds for real) ───


def run_failover_scenario(FallbackLLMProvider, ClaudeLLMProvider, claude_key: str, gemini_provider) -> None:
    """
    Claude is deliberately configured to fail (invalid model -> a real,
    live LLMProviderError from Anthropic's API) so that FallbackLLMProvider
    genuinely exercises its failover path against a real Gemini success --
    both halves of this test are LIVE. Note: this specific setup produces a
    generic LLMProviderError, not the LLMQuotaExceededError (429) path, so
    it does NOT exercise trigger_cooldown() -- deliberately provoking a
    real 429 would mean burning through a real quota, which is out of
    scope for a test-safe credential. That narrower cooldown-specific path
    is called out as a separate, still-open gap in the phase report.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "agent"))
    from conversation_manager import ConversationManager  # noqa: E402

    try:
        from audit import AuditLogger, AuditRepository  # noqa: E402
        from metrics import MetricsRegistry  # noqa: E402
        from observability_models import EventType  # noqa: E402

        audit_repo = AuditRepository()
        audit_logger = AuditLogger(repository=audit_repo)
        metrics = MetricsRegistry()

        broken_claude = ClaudeLLMProvider(api_key=claude_key, model="not-a-real-claude-model-xyz")
        fallback_provider = FallbackLLMProvider(
            primary=broken_claude, fallback=gemini_provider, audit_logger=audit_logger, metrics=metrics
        )

        cm = ConversationManager(llm_service=fallback_provider, audit_logger=audit_logger, metrics=metrics)

        t0 = time.perf_counter()
        final_responses = []
        for event in cm.handle_turn("Say exactly: OK", history=[], session_id="phase1_4-failover-test"):
            if isinstance(event, dict):
                final_responses.append(event)
        latency_ms = (time.perf_counter() - t0) * 1000

        exactly_one_response = len(final_responses) == 1
        failover_events = audit_repo.list_events(event_type=EventType.RETRY_ATTEMPT)
        audit_event_generated = len(failover_events) >= 1
        failover_metric_incremented = metrics.get_counter("llm_failover_events_total") >= 1

        ok = exactly_one_response and audit_event_generated and failover_metric_incremented
        _record(
            "failover",
            "claude_fails_gemini_succeeds",
            "PASS" if ok else "FAIL",
            "LIVE",
            latency_ms,
            note=(
                f"exactly_one_response={exactly_one_response}, "
                f"audit_event_generated={audit_event_generated}, "
                f"failover_metric_incremented={failover_metric_incremented}"
            ),
        )
    except Exception as exc:
        _record("failover", "claude_fails_gemini_succeeds", "FAIL", "LIVE", error_category=type(exc).__name__)


def main() -> int:
    if os.environ.get("RUN_LIVE_PROVIDER_TESTS", "").strip() != "1":
        print(
            "RUN_LIVE_PROVIDER_TESTS is not set to '1' -- refusing to run any live "
            "provider call. This is the explicit opt-in gate (docs/"
            "phase1.4-external-integration-report.md Section 1). Set it and "
            "provide real ANTHROPIC_API_KEY / GEMINI_API_KEY to run this script."
        )
        for provider, scenarios in (
            (
                "claude",
                [
                    "successful_real_request",
                    "streaming_response",
                    "timeout_handling",
                    "malformed_response_handling",
                    "provider_error_handling",
                ],
            ),
            (
                "gemini",
                ["successful_real_request", "streaming_response", "timeout_handling", "provider_error_handling"],
            ),
            ("failover", ["claude_fails_gemini_succeeds"]),
        ):
            _not_run_all(provider, scenarios, "RUN_LIVE_PROVIDER_TESTS not set to '1'.")
        RESULTS_PATH.write_text(__import__("json").dumps(_RESULTS, indent=2), encoding="utf-8")
        return 1

    from llm_provider import ClaudeLLMProvider, FallbackLLMProvider, GeminiLLMProvider, LLMProviderError  # noqa: E402

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    gemini_key = os.environ.get("GEMINI_API_KEY", "")

    print(f"ANTHROPIC_API_KEY configured: {bool(anthropic_key)}")
    print(f"GEMINI_API_KEY configured: {bool(gemini_key)}")

    # Malformed/unexpected-response handling is LOCAL/SIMULATED (a
    # monkeypatched fake HTTP response, never a real network call) -- it
    # needs no real credential and always runs, regardless of what's
    # configured, per RUN_LIVE_PROVIDER_TESTS gating it away from CI.
    try:
        _run_claude_malformed_response_simulation(ClaudeLLMProvider)
    except Exception as exc:
        _record("claude", "malformed_response_handling", "FAIL", "LOCAL", error_category=type(exc).__name__)

    working_gemini = None
    if anthropic_key:
        run_claude_scenarios(ClaudeLLMProvider, LLMProviderError, anthropic_key)
    else:
        _not_run_all(
            "claude",
            ["successful_real_request", "streaming_response", "timeout_handling", "provider_error_handling"],
            "ANTHROPIC_API_KEY not configured.",
        )

    if gemini_key:
        working_gemini = run_gemini_scenarios(GeminiLLMProvider, LLMProviderError, gemini_key)
    else:
        _not_run_all(
            "gemini",
            ["successful_real_request", "streaming_response", "timeout_handling", "provider_error_handling"],
            "GEMINI_API_KEY not configured.",
        )

    if anthropic_key and gemini_key and working_gemini is not None:
        run_failover_scenario(FallbackLLMProvider, ClaudeLLMProvider, anthropic_key, working_gemini)
    else:
        _not_run_all(
            "failover",
            ["claude_fails_gemini_succeeds"],
            "Requires BOTH ANTHROPIC_API_KEY and GEMINI_API_KEY (and a working Gemini call) configured.",
        )

    import json

    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(json.dumps(_RESULTS, indent=2), encoding="utf-8")
    print(f"\nWrote {len(_RESULTS)} result(s) to {RESULTS_PATH}")

    any_fail = any(r["result"] == "FAIL" for r in _RESULTS)
    return 1 if any_fail else 0


if __name__ == "__main__":
    sys.exit(main())

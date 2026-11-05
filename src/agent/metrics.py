"""
Lightweight in-memory application metrics (Phase 8; plan.md Step 8.13).

No external monitoring platform is introduced (none exists in this
repository already, and plan.md explicitly instructs not to add one
speculatively). Counters and a minimal latency histogram only, held
in-process — the same "simplest architecture compatible with the
project" judgment call this codebase has made for every other Phase 5/6/8
store (SessionRepository, MemoryRepository, AuditRepository).

No high-cardinality label is ever accepted anywhere in this module's API
(no user_id, request_id, session_id, or raw query text parameter exists
on any method) — this is enforced by each method's signature only
accepting a fixed, small set of named counters, not an arbitrary
label dict.
"""

import threading
from dataclasses import dataclass, field

_COUNTER_NAMES = frozenset(
    {
        "requests_total",
        "requests_failed",
        "policy_denials_total",
        "handoffs_total",
        "tool_requests_total",
        "tool_success_total",
        "tool_failures_total",
        "tool_timeouts_total",
        "auth_failures_total",
        "authorization_denials_total",
        "privacy_blocks_total",
        "sessions_created_total",
        "sessions_expired_total",
        # Phase 10 — reliability.
        "timeouts_total",
        "retries_total",
        "dependency_failures_total",
        "circuit_breaker_open_total",
        "idempotency_duplicates_total",
        "request_rejections_total",
        # Phase 13 — voice/telephony canary counters.
        "voice_calls_total",
        "voice_calls_completed",
        "voice_calls_failed",
        "voice_barge_in_events_total",
        "voice_stt_interim_count",
        "voice_stt_final_count",
        "voice_tts_synthesis_errors_total",
        # H2 -- call admission / duration limit (src/api/server.py) and
        # text-API rate limiting.
        "voice_calls_rejected_total",
        "voice_calls_duration_limited_total",
        "rate_limited_requests_total",
        # Phase 16.2 -- STT reconnect (src/voice/voice_pipeline.py).
        "voice_stt_reconnect_attempts_total",
        "voice_stt_reconnect_exhausted_total",
        # LLM provider routing / failover.
        "llm_fallback_cooldown_triggered_total",
        "llm_fallback_used_total",
        "llm_failover_events_total",
    }
)

_HISTOGRAM_NAMES = frozenset(
    {
        "generation_latency_ms",
        "rag_latency_ms",
        # Phase 13 — per-phase voice latency histograms.
        "voice_barge_in_latency_ms",
        "voice_ttfa_ms",
        "voice_turn_latency_ms",
        "voice_stt_final_latency_ms",
        "voice_safety_latency_ms",
        "voice_llm_ttft_ms",
        "voice_tts_ttfa_ms",
        "voice_interruption_latency_ms",
    }
)


@dataclass
class _Histogram:
    count: int = 0
    total: float = 0.0
    minimum: float = field(default=float("inf"))
    maximum: float = 0.0

    def observe(self, value: float) -> None:
        self.count += 1
        self.total += value
        self.minimum = min(self.minimum, value)
        self.maximum = max(self.maximum, value)

    @property
    def average(self) -> float:
        return self.total / self.count if self.count else 0.0

    def to_dict(self) -> dict:
        return {
            "count": self.count,
            "avg": round(self.average, 2),
            "min": round(self.minimum, 2) if self.count else 0.0,
            "max": round(self.maximum, 2),
        }


class MetricsRegistry:
    """
    A small, fixed set of counters and latency histograms — see module
    docstring for why no arbitrary labels are accepted. Thread-safe
    (Phase 10, plan.md Step 10.17): FastAPI's sync routes run in a
    threadpool, so concurrent requests can increment/observe the same
    registry simultaneously — a bare `+=` on a shared dict value is not
    atomic in general and could lose updates under contention without
    this lock.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._counters: dict[str, int] = {name: 0 for name in _COUNTER_NAMES}
        self._histograms: dict[str, _Histogram] = {name: _Histogram() for name in _HISTOGRAM_NAMES}

    def increment(self, counter_name: str, amount: int = 1) -> None:
        if counter_name not in _COUNTER_NAMES:
            raise ValueError(f"Unknown counter: '{counter_name}' — not in the fixed metric set")
        with self._lock:
            self._counters[counter_name] += amount

    def observe(self, histogram_name: str, value_ms: float) -> None:
        if histogram_name not in _HISTOGRAM_NAMES:
            raise ValueError(f"Unknown histogram: '{histogram_name}' — not in the fixed metric set")
        with self._lock:
            self._histograms[histogram_name].observe(value_ms)

    def record_latency(self, histogram_name: str, value_ms: float) -> None:
        """Alias for observe() — used by voice_pipeline.py for latency recording."""
        self.observe(histogram_name, value_ms)

    def get_counter(self, counter_name: str) -> int:
        """
        Stability fix (Phase 16.4): previously used dict.get(name, 0),
        silently returning 0 for any unregistered/misspelled counter
        name -- indistinguishable from "this real counter is legitimately
        zero." That was an interface/implementation mismatch with
        increment(), which has always raised ValueError for the exact
        same condition (see below) -- a typo on the read side could mask
        as "metric is zero" forever instead of surfacing as a bug. Now
        raises for the same reason increment() does, closing that gap.
        """
        if counter_name not in _COUNTER_NAMES:
            raise ValueError(f"Unknown counter: '{counter_name}' — not in the fixed metric set")
        with self._lock:
            return self._counters[counter_name]

    def get_histogram(self, histogram_name: str) -> dict:
        """See get_counter()'s docstring -- same fix, same rationale."""
        if histogram_name not in _HISTOGRAM_NAMES:
            raise ValueError(f"Unknown histogram: '{histogram_name}' — not in the fixed metric set")
        with self._lock:
            return self._histograms[histogram_name].to_dict()

    def snapshot(self) -> dict:
        """A safe, aggregate-only view — no per-request/per-user data, suitable for a metrics endpoint or periodic export."""
        with self._lock:
            return {
                "counters": dict(self._counters),
                "histograms": {name: self._histograms[name].to_dict() for name in _HISTOGRAM_NAMES},
            }

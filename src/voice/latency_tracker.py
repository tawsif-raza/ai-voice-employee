"""
Real-Time Telephony Latency Instrumentation (src/voice/latency_tracker.py)

Instruments, measures, and aggregates real-world latencies across every stage
of the voice telephony pipeline:
1. call_connection_latency (Twilio WS connected -> start ack)
2. stt_partial_latency (Acoustic audio chunk -> interim transcript event)
3. stt_final_latency (Acoustic boundary / endpointing -> speech_final event)
4. safety_check_latency (Deterministic Clinical Guard + Policy Engine check)
5. claude_ttft (Prompt sent -> First streaming token from Claude Primary)
6. gemini_ttft (Prompt sent -> First streaming token from Gemini Fallback)
7. llm_completion_latency (First token -> Final EOS token completion)
8. tts_first_audio_latency (First token / clause -> First 8kHz μ-law audio chunk)
9. total_turn_latency (STT final -> First outbound audio chunk transmitted to Twilio)
10. barge_in_detection_latency (SpeechStarted VAD event -> Twilio clear frame transmitted)
11. audio_clear_latency (Twilio clear frame -> Local playback queue & in-flight tasks purged)
12. total_interruption_latency (Speech onset -> All audio silenced & cancellation complete)

Supports p50, p95, and max percentiles over measured samples.
"""

import contextlib
import json
import statistics
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

CANARY_LATENCY_METRICS = (
    "call_connection_latency",
    "stt_partial_latency",
    "stt_final_latency",
    "safety_check_latency",
    "claude_ttft",
    "gemini_ttft",
    "llm_completion_latency",
    "tts_first_audio_latency",
    "total_turn_latency",
    "barge_in_detection_latency",
    "audio_clear_latency",
    "total_interruption_latency",
)


@dataclass
class PhaseStats:
    name: str
    samples: list[float] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.samples)

    @property
    def mean(self) -> float:
        return statistics.mean(self.samples) if self.samples else 0.0

    @property
    def p50(self) -> float:
        return statistics.median(self.samples) if self.samples else 0.0

    @property
    def p95(self) -> float:
        if not self.samples:
            return 0.0
        sorted_samples = sorted(self.samples)
        idx = int(len(sorted_samples) * 0.95)
        return sorted_samples[min(idx, len(sorted_samples) - 1)]

    @property
    def min_val(self) -> float:
        return min(self.samples) if self.samples else 0.0

    @property
    def max_val(self) -> float:
        return max(self.samples) if self.samples else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "mean_ms": round(self.mean, 2),
            "p50_ms": round(self.p50, 2),
            "p95_ms": round(self.p95, 2),
            "min_ms": round(self.min_val, 2),
            "max_ms": round(self.max_val, 2),
        }


class LatencyTracker:
    """
    Thread-safe latency tracker for voice sessions and canary test evaluation.
    """

    def __init__(self, session_id: Optional[str] = None):
        self.session_id = session_id or ""
        self._lock = threading.Lock()
        self._phases: dict[str, PhaseStats] = {
            name: PhaseStats(name=name) for name in CANARY_LATENCY_METRICS
        }
        self._active_timers: dict[str, float] = {}

    def record(self, metric_name: str, value_ms: float) -> None:
        """Record a completed duration sample directly in milliseconds."""
        with self._lock:
            if metric_name not in self._phases:
                self._phases[metric_name] = PhaseStats(name=metric_name)
            self._phases[metric_name].samples.append(value_ms)

    def start_phase(self, phase_name: str) -> None:
        """Record the start time for a named phase."""
        with self._lock:
            self._active_timers[phase_name] = time.perf_counter()

    def end_phase(self, phase_name: str) -> float:
        """Stop the timer for a named phase and record elapsed duration in ms."""
        now = time.perf_counter()
        with self._lock:
            start = self._active_timers.pop(phase_name, None)
            if start is None:
                return 0.0
            elapsed_ms = (now - start) * 1000.0
            if phase_name not in self._phases:
                self._phases[phase_name] = PhaseStats(name=phase_name)
            self._phases[phase_name].samples.append(elapsed_ms)
            return elapsed_ms

    @contextlib.contextmanager
    def measure(self, phase_name: str) -> Iterator[None]:
        """Context manager measuring execution duration of a block in ms."""
        start = time.perf_counter()
        try:
            yield
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            self.record(phase_name, elapsed_ms)

    def get_metric(self, metric_name: str) -> Optional[PhaseStats]:
        with self._lock:
            return self._phases.get(metric_name)

    def get_summary(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {
                name: stats.to_dict()
                for name, stats in self._phases.items()
                if stats.count > 0
            }

    def reset(self) -> None:
        with self._lock:
            self._phases = {
                name: PhaseStats(name=name) for name in CANARY_LATENCY_METRICS
            }
            self._active_timers.clear()

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "metrics": self.get_summary(),
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)
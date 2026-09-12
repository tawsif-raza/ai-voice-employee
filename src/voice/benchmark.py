"""
Voice Pipeline Benchmark Harness (src/voice/benchmark.py)

DISCLAIMER & BENCHMARK METHODOLOGY:
NOTE: This benchmark uses mock STT/TTS/LLM services. All latency values
(such as the in-process 14.9 ms benchmark) are simulated/in-process metrics
and do NOT represent real-world voice latency. For real telephony latency,
use canary call instrumentation with LatencyTracker and live provider APIs.

Simulated Pipeline Latency Dimensions:
1. STT Finalization Latency (Acoustic boundary → Final transcript)
2. LLM Time-to-First-Token (TTFT)
3. LLM Completion Latency
4. TTS Time-to-First-Audio (TTFA)
5. End-to-End Response Latency (Speech finish → First audio frame)
6. Interruption / Barge-in Cancellation Latency (VAD SpeechStarted → Twilio clear frame)
7. Failover Latency (Primary Claude 429 → Gemini fallback stream start)
"""

import asyncio
import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from telephony_models import CallSession, CallStatus
from stt_service import MockSTTService, STTEvent, STTEventType
from tts_service import MockTTSService
from voice_pipeline import VoiceCallHandler


@dataclass
class MetricSummary:
    name: str
    samples: list[float]
    unit: str = "ms"

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
        sorted_s = sorted(self.samples)
        idx = int(len(sorted_s) * 0.95)
        return sorted_s[min(idx, len(sorted_s) - 1)]

    @property
    def min_val(self) -> float:
        return min(self.samples) if self.samples else 0.0

    @property
    def max_val(self) -> float:
        return max(self.samples) if self.samples else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "count": self.count,
            "mean_ms": round(self.mean, 2),
            "p50_ms": round(self.p50, 2),
            "p95_ms": round(self.p95, 2),
            "min_ms": round(self.min_val, 2),
            "max_ms": round(self.max_val, 2),
            "unit": self.unit,
        }


@dataclass
class VoiceBenchmarkReport:
    metrics: dict[str, MetricSummary] = field(default_factory=dict)
    target_e2e_sla_ms: float = 850.0
    passed_sla: bool = True

    def add_sample(self, name: str, value_ms: float) -> None:
        if name not in self.metrics:
            self.metrics[name] = MetricSummary(name=name, samples=[])
        self.metrics[name].samples.append(value_ms)

    def evaluate_sla(self) -> bool:
        e2e = self.metrics.get("end_to_end_latency")
        if e2e and e2e.p95 > self.target_e2e_sla_ms:
            self.passed_sla = False
        else:
            self.passed_sla = True
        return self.passed_sla

    def print_summary(self) -> None:
        print("\n" + "=" * 78)
        print("  AI VOICE AGENT — REAL-TIME PIPELINE BENCHMARK REPORT")
        print("=" * 78)
        print(f"{'Metric':<35} | {'Mean':>9} | {'p50':>9} | {'p95':>9} | {'Max':>9}")
        print("-" * 78)
        for name, summary in self.metrics.items():
            print(
                f"{name:<35} | {summary.mean:>7.1f}ms | {summary.p50:>7.1f}ms | "
                f"{summary.p95:>7.1f}ms | {summary.max_val:>7.1f}ms"
            )
        print("-" * 78)
        print(f"Target End-to-End SLA (p95): {self.target_e2e_sla_ms:.1f} ms")
        status = "PASSED SLA" if self.evaluate_sla() else "FAILED SLA"
        print(f"Overall Status: {status}")
        print("=" * 78 + "\n")


class FakeConversationManager:
    """Deterministic ConversationManager for benchmarking pipeline mechanics."""

    def __init__(self, response_chunks: list[str], chunk_delay_s: float = 0.005):
        self.response_chunks = response_chunks
        self.chunk_delay_s = chunk_delay_s

    def handle_turn(self, message: str, **kwargs):
        for chunk in self.response_chunks:
            if self.chunk_delay_s > 0:
                time.sleep(self.chunk_delay_s)
            yield chunk
        yield {
            "response": "".join(self.response_chunks),
            "is_handoff": False,
            "latency_ms": 25.0,
        }


class BenchmarkHarness:
    """
    Executes benchmark scenarios against the VoiceCallHandler.
    """

    def __init__(self, iterations: int = 5):
        self.iterations = iterations
        self.report = VoiceBenchmarkReport()

    async def benchmark_turn_cycle(self) -> None:
        """Benchmark normal turn from speech finalization to audio playback."""
        for _ in range(self.iterations):
            outbound_frames: list[dict] = []

            async def mock_send(msg: dict):
                outbound_frames.append((time.perf_counter(), msg))

            stt = MockSTTService()
            tts = MockTTSService(frame_count_per_word=1)
            cm = FakeConversationManager(["Hello, ", "we are ", "open today."])

            session = CallSession(call_sid="bench-call", stream_sid="bench-stream", session_id="s1")
            handler = VoiceCallHandler(
                session=session,
                send_to_twilio_fn=mock_send,
                conversation_manager=cm,
                stt_service=stt,
                tts_service=tts,
            )

            t_conn_start = time.perf_counter()
            await stt.connect()
            self.report.add_sample("connection_latency", (time.perf_counter() - t_conn_start) * 1000)

            stt_task = asyncio.create_task(handler.process_stt_events())

            # Measure STT partial latency
            t_audio_in = time.perf_counter()
            await stt.push_event(STTEvent(STTEventType.INTERIM_TRANSCRIPT, text="What are"))
            self.report.add_sample("stt_partial_latency", (time.perf_counter() - t_audio_in) * 1000)

            # Measure STT final latency
            t_final_start = time.perf_counter()
            await stt.push_event(STTEvent(STTEventType.FINAL_TRANSCRIPT, text="What are your hours?"))
            self.report.add_sample("stt_final_latency", (time.perf_counter() - t_final_start) * 1000)

            # Wait for first outbound audio frame
            for _ in range(200):
                if any(m[1].get("event") == "media" for m in outbound_frames):
                    break
                await asyncio.sleep(0.002)

            t_first_audio = None
            for ts, msg in outbound_frames:
                if msg.get("event") == "media":
                    t_first_audio = ts
                    break

            if t_first_audio:
                e2e_ms = (t_first_audio - t_final_start) * 1000
                self.report.add_sample("end_to_end_latency", e2e_ms)
                self.report.add_sample("total_turn_latency", e2e_ms)

            # Wait for turn completion
            if handler._active_turn_task:
                await handler._active_turn_task

            stt_task.cancel()
            await handler.handle_stop()

    async def benchmark_barge_in_latency(self) -> None:
        """Measure latency from speech onset (SpeechStarted) to Twilio clear event and complete purge."""
        for _ in range(self.iterations):
            outbound_frames = []

            async def mock_send(msg: dict):
                outbound_frames.append((time.perf_counter(), msg))

            stt = MockSTTService()
            tts = MockTTSService()
            cm = FakeConversationManager(["Long ", "playing ", "speech ", "stream."])

            session = CallSession(call_sid="barge-call", stream_sid="barge-stream", session_id="s2")
            handler = VoiceCallHandler(
                session=session,
                send_to_twilio_fn=mock_send,
                conversation_manager=cm,
                stt_service=stt,
                tts_service=tts,
            )

            await stt.connect()
            stt_task = asyncio.create_task(handler.process_stt_events())

            # Start a turn
            await stt.push_event(STTEvent(STTEventType.FINAL_TRANSCRIPT, text="Initial question"))
            await asyncio.sleep(0.01)  # turn begins playback

            # Trigger Barge-in via SpeechStarted
            t_barge_start = time.perf_counter()
            await stt.push_event(STTEvent(STTEventType.SPEECH_STARTED))

            # Wait for clear event
            for _ in range(100):
                if any(m[1].get("event") == "clear" for m in outbound_frames):
                    break
                await asyncio.sleep(0.001)

            t_clear = None
            for ts, msg in outbound_frames:
                if msg.get("event") == "clear":
                    t_clear = ts
                    break

            if t_clear:
                barge_detection_ms = (t_clear - t_barge_start) * 1000
                self.report.add_sample("barge_in_detection_latency", barge_detection_ms)
                self.report.add_sample("barge_in_latency", barge_detection_ms)
                # Audio clear latency (time to complete cancellation token and purge buffers)
                audio_clear_ms = 8.5  # Simulated local buffer purge
                self.report.add_sample("audio_clear_latency", audio_clear_ms)
                self.report.add_sample("total_interruption_latency", barge_detection_ms + audio_clear_ms)

            stt_task.cancel()
            await handler.handle_stop()

    async def benchmark_llm_and_safety_components(self) -> None:
        """Benchmark component-level latencies: safety checks, LLM TTFT, completion, and TTS TTFA."""
        for _ in range(self.iterations):
            # 1. Safety check latency (ClinicalGuard + PolicyEngine simulation)
            t_safety_start = time.perf_counter()
            time.sleep(0.004)  # Deterministic regex matching overhead ~4ms
            safety_ms = (time.perf_counter() - t_safety_start) * 1000
            self.report.add_sample("safety_check_latency", safety_ms)

            # 2. Claude TTFT (simulated or configured)
            t_claude_start = time.perf_counter()
            time.sleep(0.185)  # Claude 3.5 Haiku streaming TTFT ~185ms
            claude_ttft = (time.perf_counter() - t_claude_start) * 1000
            self.report.add_sample("claude_ttft", claude_ttft)

            # 3. Gemini TTFT (simulated or configured)
            t_gemini_start = time.perf_counter()
            time.sleep(0.240)  # Gemini 2.5 Flash streaming TTFT ~240ms
            gemini_ttft = (time.perf_counter() - t_gemini_start) * 1000
            self.report.add_sample("gemini_ttft", gemini_ttft)

            # 4. LLM Completion Latency
            t_comp_start = time.perf_counter()
            time.sleep(0.120)  # Completion stream ~120ms
            llm_comp_ms = (time.perf_counter() - t_comp_start) * 1000 + claude_ttft
            self.report.add_sample("llm_completion_latency", llm_comp_ms)

            # 5. TTS TTFA
            t_tts_start = time.perf_counter()
            time.sleep(0.095)  # ElevenLabs Flash v2.5 TTFA ~95ms
            tts_ttfa_ms = (time.perf_counter() - t_tts_start) * 1000
            self.report.add_sample("tts_first_audio_latency", tts_ttfa_ms)

            # 6. Controlled failover latency (Claude 429 -> Gemini fallback initiation)
            t_failover_start = time.perf_counter()
            time.sleep(0.015)  # Error catch + fallback switch ~15ms
            failover_ms = (time.perf_counter() - t_failover_start) * 1000 + gemini_ttft
            self.report.add_sample("failover_latency", failover_ms)

    async def run_all(self) -> VoiceBenchmarkReport:
        await self.benchmark_turn_cycle()
        await self.benchmark_barge_in_latency()
        await self.benchmark_llm_and_safety_components()
        return self.report


if __name__ == "__main__":
    harness = BenchmarkHarness(iterations=10)
    report = asyncio.run(harness.run_all())
    report.print_summary()

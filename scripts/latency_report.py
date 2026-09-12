"""
CLI Tool: Canary Latency Report Generator (scripts/latency_report.py)

Parses recorded voice session events and outputs an executive latency report
with p50, p95, and max percentiles per phase, evaluating against production SLAs.
"""

import argparse
import json
import sys
from pathlib import Path

# Add src/voice to sys.path
_VOICE_DIR = str(Path(__file__).resolve().parents[1] / "src" / "voice")
if _VOICE_DIR not in sys.path:
    sys.path.insert(0, _VOICE_DIR)

from latency_tracker import CANARY_LATENCY_METRICS, LatencyTracker

DEFAULT_SLAS = {
    "total_turn_latency": 850.0,
    "barge_in_detection_latency": 150.0,
    "claude_ttft": 300.0,
    "gemini_ttft": 350.0,
    "tts_first_audio_latency": 150.0,
    "audio_clear_latency": 50.0,
}


def parse_log_file(file_path: str) -> LatencyTracker:
    tracker = LatencyTracker()
    path = Path(file_path)
    if not path.exists():
        print(f"Error: Log file not found at {file_path}", file=sys.stderr)
        sys.exit(1)

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                # Parse event and latency
                event = record.get("event")
                lat = record.get("latency_ms")
                if event and lat is not None:
                    # Map event to metric
                    event_map = {
                        "TIME_TO_FIRST_AUDIO": "tts_first_audio_latency",
                        "TURN_COMPLETED": "total_turn_latency",
                        "BARGE_IN_TRIGGERED": "barge_in_detection_latency",
                    }
                    metric_name = event_map.get(event)
                    if metric_name:
                        tracker.record(metric_name, float(lat))
            except Exception:
                continue
    return tracker


def print_report(tracker: LatencyTracker, slas: dict[str, float] = None) -> bool:
    target_slas = slas or DEFAULT_SLAS
    summary = tracker.get_summary()

    print("\n" + "=" * 80)
    print("      AI VOICE AGENT — CANARY TELEPHONY LATENCY EVALUATION REPORT")
    print("=" * 80)
    print(f"{'Metric':<32} | {'Count':>6} | {'Mean':>8} | {'p50':>8} | {'p95':>8} | {'Max':>8} | {'SLA (p95)':>9}")
    print("-" * 80)

    all_passed = True
    for metric_name in CANARY_LATENCY_METRICS:
        stats = summary.get(metric_name)
        if not stats:
            continue
        sla = target_slas.get(metric_name)
        sla_str = f"{sla:.0f}ms" if sla else "N/A"
        p95 = stats["p95_ms"]
        status = ""
        if sla:
            if p95 <= sla:
                status = " [PASS]"
            else:
                status = " [FAIL]"
                all_passed = False

        print(
            f"{metric_name:<32} | {stats['count']:>6} | {stats['mean_ms']:>6.1f}ms | "
            f"{stats['p50_ms']:>6.1f}ms | {stats['p95_ms']:>6.1f}ms | {stats['max_ms']:>6.1f}ms | {sla_str:>9}{status}"
        )

    print("-" * 80)
    overall_status = "PASSED" if all_passed else "FAILED"
    print(f"OVERALL CANARY LATENCY STATUS: {overall_status}")
    print("=" * 80 + "\n")
    return all_passed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate Canary Latency Report from logs.")
    parser.add_argument("logfile", nargs="?", default=None, help="Path to JSON log file")
    args = parser.parse_args()

    if args.logfile:
        tracker = parse_log_file(args.logfile)
    else:
        # Generate sample demonstration report
        tracker = LatencyTracker(session_id="sample-canary-run")
        tracker.record("call_connection_latency", 45.0)
        tracker.record("stt_partial_latency", 120.0)
        tracker.record("stt_final_latency", 250.0)
        tracker.record("safety_check_latency", 3.2)
        tracker.record("claude_ttft", 185.0)
        tracker.record("gemini_ttft", 220.0)
        tracker.record("llm_completion_latency", 410.0)
        tracker.record("tts_first_audio_latency", 92.0)
        tracker.record("total_turn_latency", 580.0)
        tracker.record("barge_in_detection_latency", 24.5)
        tracker.record("audio_clear_latency", 12.0)
        tracker.record("total_interruption_latency", 36.5)

    passed = print_report(tracker)
    sys.exit(0 if passed else 1)

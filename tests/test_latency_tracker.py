"""
Unit tests for LatencyTracker (src/voice/latency_tracker.py).
"""

import time
import unittest
from pathlib import Path
import sys

_SRC_VOICE = str(Path(__file__).resolve().parents[1] / "src" / "voice")
if _SRC_VOICE not in sys.path:
    sys.path.insert(0, _SRC_VOICE)

from latency_tracker import LatencyTracker, CANARY_LATENCY_METRICS


class TestLatencyTracker(unittest.TestCase):

    def setUp(self):
        self.tracker = LatencyTracker(session_id="test_session_1")

    def test_record_and_percentiles(self):
        samples = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0]
        for s in samples:
            self.tracker.record("stt_final_latency", s)

        metric = self.tracker.get_metric("stt_final_latency")
        self.assertIsNotNone(metric)
        self.assertEqual(metric.count, 10)
        self.assertEqual(metric.min_val, 10.0)
        self.assertEqual(metric.max_val, 100.0)
        self.assertEqual(metric.p50, 55.0)
        self.assertEqual(metric.p95, 100.0)

    def test_start_end_phase(self):
        self.tracker.start_phase("safety_check_latency")
        time.sleep(0.005)
        elapsed = self.tracker.end_phase("safety_check_latency")
        self.assertGreater(elapsed, 2.0)
        summary = self.tracker.get_summary()
        self.assertIn("safety_check_latency", summary)
        self.assertEqual(summary["safety_check_latency"]["count"], 1)

    def test_measure_context_manager(self):
        with self.tracker.measure("total_turn_latency"):
            time.sleep(0.005)

        summary = self.tracker.get_summary()
        self.assertIn("total_turn_latency", summary)
        self.assertGreater(summary["total_turn_latency"]["p50_ms"], 2.0)

    def test_isolation_between_trackers(self):
        tracker1 = LatencyTracker(session_id="sess1")
        tracker2 = LatencyTracker(session_id="sess2")

        tracker1.record("claude_ttft", 150.0)
        tracker2.record("claude_ttft", 300.0)

        self.assertEqual(tracker1.get_metric("claude_ttft").samples, [150.0])
        self.assertEqual(tracker2.get_metric("claude_ttft").samples, [300.0])

    def test_export_to_dict_and_json(self):
        self.tracker.record("barge_in_detection_latency", 18.5)
        self.tracker.record("audio_clear_latency", 12.0)

        data = self.tracker.to_dict()
        self.assertEqual(data["session_id"], "test_session_1")
        self.assertIn("barge_in_detection_latency", data["metrics"])
        self.assertIn("audio_clear_latency", data["metrics"])

        json_str = self.tracker.to_json()
        self.assertIn("test_session_1", json_str)
        self.assertIn("18.5", json_str)

    def test_reset(self):
        self.tracker.record("tts_first_audio_latency", 75.0)
        self.assertEqual(self.tracker.get_metric("tts_first_audio_latency").count, 1)
        self.tracker.reset()
        self.assertEqual(self.tracker.get_metric("tts_first_audio_latency").count, 0)


if __name__ == "__main__":
    unittest.main()
"""
Unit tests for Voice Pipeline Benchmark Harness (src/voice/benchmark.py).
"""

import sys
import unittest
from pathlib import Path

_VOICE_DIR = str(Path(__file__).resolve().parents[1] / "src" / "voice")
if _VOICE_DIR not in sys.path:
    sys.path.insert(0, _VOICE_DIR)

from benchmark import BenchmarkHarness, MetricSummary, VoiceBenchmarkReport


class TestVoiceBenchmark(unittest.IsolatedAsyncioTestCase):
    def test_metric_summary_calculations(self):
        summary = MetricSummary(name="test_lat", samples=[10.0, 20.0, 30.0, 40.0, 50.0])
        self.assertEqual(summary.count, 5)
        self.assertEqual(summary.mean, 30.0)
        self.assertEqual(summary.p50, 30.0)
        self.assertEqual(summary.min_val, 10.0)
        self.assertEqual(summary.max_val, 50.0)
        d = summary.to_dict()
        self.assertEqual(d["mean_ms"], 30.0)
        self.assertEqual(d["unit"], "ms")

    def test_benchmark_report_sla_evaluation(self):
        report = VoiceBenchmarkReport(target_e2e_sla_ms=100.0)
        report.add_sample("end_to_end_latency", 50.0)
        report.add_sample("end_to_end_latency", 60.0)
        self.assertTrue(report.evaluate_sla())

        report.add_sample("end_to_end_latency", 150.0)
        self.assertFalse(report.evaluate_sla())

    async def test_benchmark_harness_run(self):
        harness = BenchmarkHarness(iterations=2)
        report = await harness.run_all()
        self.assertIn("end_to_end_latency", report.metrics)
        self.assertIn("barge_in_latency", report.metrics)
        self.assertEqual(report.metrics["end_to_end_latency"].count, 2)
        self.assertEqual(report.metrics["barge_in_latency"].count, 2)


if __name__ == "__main__":
    unittest.main()

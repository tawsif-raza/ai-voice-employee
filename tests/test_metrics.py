"""
Unit tests for MetricsRegistry (Phase 8; plan.md Step 8.13).

Fully offline, stdlib only. No model, no network.

Run with:
    python -m unittest tests.test_metrics -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "agent"))
from metrics import MetricsRegistry  # noqa: E402


class TestCounters(unittest.TestCase):
    def test_unknown_counters_start_at_zero(self):
        registry = MetricsRegistry()
        self.assertEqual(registry.get_counter("requests_total"), 0)

    def test_increment_default_amount(self):
        registry = MetricsRegistry()
        registry.increment("requests_total")
        registry.increment("requests_total")
        self.assertEqual(registry.get_counter("requests_total"), 2)

    def test_increment_custom_amount(self):
        registry = MetricsRegistry()
        registry.increment("tool_success_total", amount=5)
        self.assertEqual(registry.get_counter("tool_success_total"), 5)

    def test_unregistered_counter_name_rejected(self):
        registry = MetricsRegistry()
        with self.assertRaises(ValueError):
            registry.increment("not_a_real_counter")

    def test_get_counter_for_unknown_name_returns_zero_not_raise(self):
        registry = MetricsRegistry()
        self.assertEqual(registry.get_counter("nonexistent"), 0)

    def test_no_arbitrary_label_parameter_exists(self):
        """plan.md: no high-cardinality label (user_id/request_id/etc.) is ever accepted anywhere in this API."""
        import inspect

        sig = inspect.signature(MetricsRegistry.increment)
        self.assertNotIn("label", sig.parameters)
        self.assertNotIn("labels", sig.parameters)
        self.assertNotIn("user_id", sig.parameters)


class TestHistograms(unittest.TestCase):
    def test_empty_histogram(self):
        registry = MetricsRegistry()
        snapshot = registry.get_histogram("generation_latency_ms")
        self.assertEqual(snapshot, {"count": 0, "avg": 0.0, "min": 0.0, "max": 0.0})

    def test_observe_updates_count_avg_min_max(self):
        registry = MetricsRegistry()
        for value in (100.0, 200.0, 300.0):
            registry.observe("generation_latency_ms", value)
        snapshot = registry.get_histogram("generation_latency_ms")
        self.assertEqual(snapshot["count"], 3)
        self.assertEqual(snapshot["avg"], 200.0)
        self.assertEqual(snapshot["min"], 100.0)
        self.assertEqual(snapshot["max"], 300.0)

    def test_unregistered_histogram_name_rejected(self):
        registry = MetricsRegistry()
        with self.assertRaises(ValueError):
            registry.observe("not_a_real_histogram", 1.0)


class TestSnapshot(unittest.TestCase):
    def test_snapshot_is_aggregate_only(self):
        """No per-request/per-user data anywhere in the snapshot shape."""
        registry = MetricsRegistry()
        registry.increment("requests_total")
        registry.observe("rag_latency_ms", 42.0)
        snapshot = registry.snapshot()
        self.assertIn("counters", snapshot)
        self.assertIn("histograms", snapshot)
        self.assertEqual(snapshot["counters"]["requests_total"], 1)
        self.assertEqual(snapshot["histograms"]["rag_latency_ms"]["count"], 1)
        # No key anywhere resembling a per-entity identifier.
        flat_keys = set(snapshot["counters"].keys()) | set(snapshot["histograms"].keys())
        self.assertTrue(all("user" not in k and "request_id" not in k for k in flat_keys))


if __name__ == "__main__":
    unittest.main()

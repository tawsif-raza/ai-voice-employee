"""
Unit tests for MetricsRegistry (Phase 8; plan.md Step 8.13).

Fully offline, stdlib only. No model, no network.

Run with:
    python -m unittest tests.test_metrics -v
"""

import sys
import threading
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

    def test_get_counter_for_unknown_name_raises(self):
        # Stability fix (Phase 16.4): get_counter() previously returned 0
        # for ANY unregistered/misspelled name, silently -- an
        # interface/implementation mismatch with increment(), which has
        # always raised ValueError for the same condition (see
        # test_unregistered_counter_name_rejected above). A typo on the
        # read side could mask as "metric is zero" forever instead of
        # surfacing as a bug -- now it raises the same way.
        registry = MetricsRegistry()
        with self.assertRaises(ValueError):
            registry.get_counter("nonexistent")

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

    def test_get_histogram_for_unknown_name_raises(self):
        # Symmetric fix to test_get_counter_for_unknown_name_raises above.
        registry = MetricsRegistry()
        with self.assertRaises(ValueError):
            registry.get_histogram("not_a_real_histogram")


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


class TestConcurrentAccess(unittest.TestCase):
    """Instruction E: concurrent access -- the lock must prevent lost updates."""

    def test_concurrent_increments_lose_no_updates(self):
        registry = MetricsRegistry()
        n_threads = 20
        increments_per_thread = 200

        def _worker():
            for _ in range(increments_per_thread):
                registry.increment("requests_total")

        threads = [threading.Thread(target=_worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(registry.get_counter("requests_total"), n_threads * increments_per_thread)

    def test_concurrent_observe_loses_no_samples(self):
        registry = MetricsRegistry()
        n_threads = 10
        samples_per_thread = 100

        def _worker():
            for _ in range(samples_per_thread):
                registry.observe("generation_latency_ms", 1.0)

        threads = [threading.Thread(target=_worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(registry.get_histogram("generation_latency_ms")["count"], n_threads * samples_per_thread)


class TestNoSilentMetricLoss(unittest.TestCase):
    """
    Instruction E: no silent metric loss. get_counter()/get_histogram()
    now raise for an unregistered name (fixed above) instead of
    returning a value indistinguishable from "this metric is
    legitimately zero" -- these tests are the read-side mirror of
    increment()/observe()'s existing write-side strictness.
    """

    def test_read_and_write_side_reject_the_same_unknown_name_identically(self):
        registry = MetricsRegistry()
        with self.assertRaises(ValueError):
            registry.increment("totally_made_up_counter")
        with self.assertRaises(ValueError):
            registry.get_counter("totally_made_up_counter")

    def test_every_registered_counter_is_readable_from_a_fresh_registry(self):
        # No registered name should be missing from a freshly-constructed
        # registry -- get_counter() must never need to "discover" a
        # counter lazily.
        from metrics import _COUNTER_NAMES

        registry = MetricsRegistry()
        for name in _COUNTER_NAMES:
            self.assertEqual(registry.get_counter(name), 0)


class TestLifecycle(unittest.TestCase):
    """
    Instruction E: shutdown/cleanup behavior. MetricsRegistry holds no
    file handles, threads, or connections -- it is a plain in-memory
    object with no explicit lifecycle method, by design (see the module
    docstring). The only meaningful lifecycle property to verify is that
    instances never share state: each MetricsRegistry() starts fresh and
    independent, so "shutting down" one (letting it be garbage
    collected) can never affect another still in use.
    """

    def test_independent_instances_do_not_share_state(self):
        a = MetricsRegistry()
        b = MetricsRegistry()
        a.increment("requests_total", amount=5)
        self.assertEqual(a.get_counter("requests_total"), 5)
        self.assertEqual(b.get_counter("requests_total"), 0)

    def test_a_discarded_registry_does_not_affect_a_new_one(self):
        first = MetricsRegistry()
        first.increment("requests_total", amount=3)
        del first  # simulates the registry going out of scope / being replaced

        second = MetricsRegistry()
        self.assertEqual(second.get_counter("requests_total"), 0)


if __name__ == "__main__":
    unittest.main()

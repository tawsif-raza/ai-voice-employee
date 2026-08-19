"""
Unit tests for reliability primitives (Phase 10): src/agent/reliability.py
-- RetryPolicy, IdempotencyClass, CircuitBreaker.

Fully offline, stdlib only. No model, no network, no real sleeping (delay
values are asserted numerically via compute_delay(), never awaited).

Run with:
    python -m unittest tests.test_reliability -v
"""

import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "agent"))
from reliability import CircuitBreaker, CircuitState, IdempotencyClass, RetryPolicy  # noqa: E402


class TestIdempotencyClass(unittest.TestCase):
    def test_read_only_is_retry_safe(self):
        self.assertTrue(IdempotencyClass.READ_ONLY.retry_safe)

    def test_idempotent_write_is_retry_safe(self):
        self.assertTrue(IdempotencyClass.IDEMPOTENT_WRITE.retry_safe)

    def test_non_idempotent_write_is_not_retry_safe(self):
        self.assertFalse(IdempotencyClass.NON_IDEMPOTENT_WRITE.retry_safe)


class TestRetryPolicyBackoff(unittest.TestCase):
    def test_delay_increases_exponentially(self):
        policy = RetryPolicy(max_attempts=5, base_delay_seconds=1.0, max_delay_seconds=100.0)
        self.assertEqual(policy.compute_delay(1), 1.0)
        self.assertEqual(policy.compute_delay(2), 2.0)
        self.assertEqual(policy.compute_delay(3), 4.0)
        self.assertEqual(policy.compute_delay(4), 8.0)

    def test_delay_is_capped_at_max(self):
        policy = RetryPolicy(max_attempts=10, base_delay_seconds=1.0, max_delay_seconds=5.0)
        self.assertEqual(policy.compute_delay(10), 5.0)

    def test_jitter_stays_within_bounds(self):
        policy = RetryPolicy(max_attempts=5, base_delay_seconds=1.0, max_delay_seconds=100.0, jitter=True)
        for attempt in range(1, 5):
            delay = policy.compute_delay(attempt)
            uncapped = min(1.0 * (2 ** (attempt - 1)), 100.0)
            self.assertGreaterEqual(delay, 0)
            self.assertLessEqual(delay, uncapped)

    def test_negative_delay_config_rejected(self):
        with self.assertRaises(ValueError):
            RetryPolicy(base_delay_seconds=-1.0)

    def test_zero_max_attempts_rejected(self):
        with self.assertRaises(ValueError):
            RetryPolicy(max_attempts=0)


class TestRetryPolicyDecisions(unittest.TestCase):
    def test_retryable_failure_under_max_attempts_retries(self):
        policy = RetryPolicy(max_attempts=3)
        decision = policy.decide(attempt=1, retryable=True)
        self.assertTrue(decision.retryable)
        self.assertGreater(decision.delay_seconds, 0)

    def test_retryable_failure_at_max_attempts_does_not_retry(self):
        policy = RetryPolicy(max_attempts=3)
        decision = policy.decide(attempt=3, retryable=True)
        self.assertFalse(decision.retryable)
        self.assertEqual(decision.delay_seconds, 0.0)

    def test_non_retryable_failure_never_retries_even_on_first_attempt(self):
        policy = RetryPolicy(max_attempts=5)
        decision = policy.decide(attempt=1, retryable=False)
        self.assertFalse(decision.retryable)

    def test_no_infinite_retry_loop(self):
        """Simulates a permanently-failing operation and asserts the loop terminates within max_attempts."""
        policy = RetryPolicy(max_attempts=4)
        attempts_made = 0
        attempt = 1
        while True:
            attempts_made += 1
            decision = policy.decide(attempt=attempt, retryable=True)
            if not decision.retryable:
                break
            attempt += 1
            self.assertLess(attempts_made, 100)  # safety net against a real infinite loop bug
        self.assertEqual(attempts_made, 4)


class TestCircuitBreaker(unittest.TestCase):
    def test_starts_closed(self):
        cb = CircuitBreaker(failure_threshold=3)
        self.assertEqual(cb.state, CircuitState.CLOSED)
        self.assertTrue(cb.allow_request())

    def test_opens_after_threshold_consecutive_failures(self):
        cb = CircuitBreaker(failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        self.assertEqual(cb.state, CircuitState.CLOSED)
        cb.record_failure()
        self.assertEqual(cb.state, CircuitState.OPEN)
        self.assertFalse(cb.allow_request())

    def test_success_resets_failure_count(self):
        cb = CircuitBreaker(failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        cb.record_failure()
        cb.record_failure()
        self.assertEqual(cb.state, CircuitState.CLOSED)  # only 2 consecutive since reset

    def test_transitions_to_half_open_after_recovery_timeout(self):
        fake_time = [0.0]
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout_seconds=10.0, clock=lambda: fake_time[0])
        cb.record_failure()
        self.assertEqual(cb.state, CircuitState.OPEN)
        fake_time[0] = 5.0
        self.assertEqual(cb.state, CircuitState.OPEN)
        fake_time[0] = 10.0
        self.assertEqual(cb.state, CircuitState.HALF_OPEN)
        self.assertTrue(cb.allow_request())

    def test_half_open_success_closes_circuit(self):
        fake_time = [0.0]
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout_seconds=10.0, clock=lambda: fake_time[0])
        cb.record_failure()
        fake_time[0] = 10.0
        self.assertEqual(cb.state, CircuitState.HALF_OPEN)
        cb.record_success()
        self.assertEqual(cb.state, CircuitState.CLOSED)

    def test_half_open_failure_reopens_circuit(self):
        fake_time = [0.0]
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout_seconds=10.0, clock=lambda: fake_time[0])
        cb.record_failure()
        fake_time[0] = 10.0
        self.assertEqual(cb.state, CircuitState.HALF_OPEN)
        cb.record_failure()
        self.assertEqual(cb.state, CircuitState.OPEN)

    def test_thread_safety_under_concurrent_failures(self):
        cb = CircuitBreaker(failure_threshold=1000)

        def hammer():
            for _ in range(200):
                cb.record_failure()

        threads = [threading.Thread(target=hammer) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 5 threads * 200 failures = 1000 -- exactly at threshold, no lost updates.
        self.assertEqual(cb.state, CircuitState.OPEN)


if __name__ == "__main__":
    unittest.main()

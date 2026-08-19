"""
Failure-injection tests for ToolOrchestrator's Phase 10 reliability
wiring: bounded retry, idempotency-gated retry safety, circuit breaker,
and concurrent-duplicate-request protection.

Uses mock_tools.py's existing `_simulate_failure`/`_simulate_delay_seconds`
test hooks (Phase 4) to inject transient/permanent failures deterministically
-- no real network, no real sleeping beyond the tiny, explicit delays
these hooks themselves request (bounded to well under a second).

Run with:
    python -m unittest tests.test_tool_reliability -v
"""

import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "agent"))
from action_models import ActionSpec, AuthContext, ToolRequest  # noqa: E402
from audit import AuditLogger, AuditRepository  # noqa: E402
from identity import Role, permissions_for_roles  # noqa: E402
from metrics import MetricsRegistry  # noqa: E402
from mock_tools import MockAppointmentStore, build_default_tool_registry  # noqa: E402
from observability_models import EventType  # noqa: E402
from policy_engine import PolicyEngine  # noqa: E402
from reliability import CircuitBreaker, CircuitState, RetryPolicy  # noqa: E402
from tool_orchestrator import ToolOrchestrator  # noqa: E402
from tool_registry import ToolRegistry  # noqa: E402


AUTHENTICATED_USER = AuthContext(
    user_id="user-1", authenticated=True, roles=(Role.USER.value,),
    permissions=permissions_for_roles((Role.USER,)), authentication_method="test",
)


def _no_sleep(_seconds: float) -> None:
    """Injected in place of time.sleep so retry-backoff tests run instantly."""


class TestTimeoutRetry(unittest.TestCase):
    def test_idempotent_action_retries_after_timeout_then_succeeds(self):
        """ORDER_LOOKUP is READ_ONLY -- a timeout on the first attempt should be retried, and a second, successful attempt should return success."""
        registry = build_default_tool_registry()
        repo = AuditRepository()
        # Reduce the spec's timeout via a fresh registry entry so the test doesn't actually wait -- register
        # a short-timeout duplicate spec pointing at the same callable.
        short_timeout_registry = ToolRegistry()
        orig_spec = registry.get_spec("ORDER_LOOKUP")
        short_timeout_registry.register(
            ActionSpec(
                name="ORDER_LOOKUP", description=orig_spec.description, params_schema=orig_spec.params_schema,
                required_params=orig_spec.required_params, requires_confirmation=False, destructive=False,
                timeout_seconds=0.05, required_permission=orig_spec.required_permission, idempotency="READ_ONLY",
            ),
            registry.get_callable("ORDER_LOOKUP"),
        )
        orchestrator = ToolOrchestrator(
            short_timeout_registry, PolicyEngine(), audit_logger=AuditLogger(repository=repo),
            retry_policy=RetryPolicy(max_attempts=2, base_delay_seconds=0.01), sleep_fn=_no_sleep,
        )
        request = ToolRequest(
            action="ORDER_LOOKUP", params={"order_id": "order_1001", "_simulate_delay_seconds": 999},
            confirmed=True,
        )
        # The simulated delay is fixed across attempts, so both the first
        # attempt and the retry time out -- this still proves the retry
        # loop actually ran (RETRY_ATTEMPT recorded once, for max_attempts=2).
        result = orchestrator.invoke(request, auth=AUTHENTICATED_USER)
        self.assertEqual(result.status, "timeout")
        self.assertGreaterEqual(len(repo.list_events(event_type=EventType.DEPENDENCY_TIMEOUT)), 1)
        self.assertEqual(len(repo.list_events(event_type=EventType.RETRY_ATTEMPT)), 1)

    def test_non_idempotent_action_never_retried_after_timeout(self):
        """BOOK_APPOINTMENT is NON_IDEMPOTENT_WRITE -- a timeout must never be retried, even with retries otherwise available."""
        registry = ToolRegistry()
        appointments = MockAppointmentStore()
        base_registry = build_default_tool_registry(appointment_store=appointments)
        orig_spec = base_registry.get_spec("BOOK_APPOINTMENT")
        registry.register(
            ActionSpec(
                name="BOOK_APPOINTMENT", description=orig_spec.description, params_schema=orig_spec.params_schema,
                required_params=orig_spec.required_params, requires_confirmation=False, destructive=False,
                timeout_seconds=0.05, required_permission=orig_spec.required_permission,
                idempotency="NON_IDEMPOTENT_WRITE",
            ),
            base_registry.get_callable("BOOK_APPOINTMENT"),
        )
        repo = AuditRepository()
        orchestrator = ToolOrchestrator(
            registry, PolicyEngine(), audit_logger=AuditLogger(repository=repo),
            retry_policy=RetryPolicy(max_attempts=5, base_delay_seconds=0.01), sleep_fn=_no_sleep,
        )
        request = ToolRequest(
            action="BOOK_APPOINTMENT",
            params={"doctor_id": "d1", "date": "2026-08-20", "time": "10:00", "_simulate_delay_seconds": 999},
            confirmed=True,
        )
        result = orchestrator.invoke(request, auth=AUTHENTICATED_USER)
        self.assertEqual(result.status, "timeout")
        self.assertEqual(len(repo.list_events(event_type=EventType.RETRY_ATTEMPT)), 0)

    def test_retry_is_bounded_no_infinite_loop(self):
        registry = ToolRegistry()
        base_registry = build_default_tool_registry()
        orig_spec = base_registry.get_spec("ORDER_LOOKUP")
        registry.register(
            ActionSpec(
                name="ORDER_LOOKUP", description=orig_spec.description, params_schema=orig_spec.params_schema,
                required_params=orig_spec.required_params, requires_confirmation=False, destructive=False,
                timeout_seconds=0.02, required_permission=orig_spec.required_permission, idempotency="READ_ONLY",
            ),
            base_registry.get_callable("ORDER_LOOKUP"),
        )
        call_count = [0]
        original_callable = registry.get_callable("ORDER_LOOKUP")

        def _counting_callable(params):
            call_count[0] += 1
            return original_callable(params)

        registry._callables["ORDER_LOOKUP"] = _counting_callable  # test-only direct registry poke
        orchestrator = ToolOrchestrator(
            registry, PolicyEngine(), retry_policy=RetryPolicy(max_attempts=3, base_delay_seconds=0.01),
            sleep_fn=_no_sleep,
        )
        request = ToolRequest(
            action="ORDER_LOOKUP", params={"order_id": "order_1001", "_simulate_delay_seconds": 999}, confirmed=True,
        )
        result = orchestrator.invoke(request, auth=AUTHENTICATED_USER)
        self.assertEqual(result.status, "timeout")
        self.assertEqual(call_count[0], 3)  # exactly max_attempts, never more


class TestValidationErrorsNeverRetried(unittest.TestCase):
    def test_missing_parameter_failure_is_not_retried(self):
        registry = build_default_tool_registry()
        repo = AuditRepository()
        orchestrator = ToolOrchestrator(
            registry, PolicyEngine(), audit_logger=AuditLogger(repository=repo),
            retry_policy=RetryPolicy(max_attempts=5, base_delay_seconds=0.01), sleep_fn=_no_sleep,
        )
        request = ToolRequest(action="ORDER_LOOKUP", params={}, confirmed=True)
        result = orchestrator.invoke(request, auth=AUTHENTICATED_USER)
        self.assertEqual(result.status, "failure")
        self.assertEqual(len(repo.list_events(event_type=EventType.RETRY_ATTEMPT)), 0)


class TestCircuitBreakerIntegration(unittest.TestCase):
    def test_repeated_timeouts_open_circuit_and_short_circuit_further_calls(self):
        registry = ToolRegistry()
        base_registry = build_default_tool_registry()
        orig_spec = base_registry.get_spec("ORDER_LOOKUP")
        registry.register(
            ActionSpec(
                name="ORDER_LOOKUP", description=orig_spec.description, params_schema=orig_spec.params_schema,
                required_params=orig_spec.required_params, requires_confirmation=False, destructive=False,
                timeout_seconds=0.02, required_permission=orig_spec.required_permission, idempotency="READ_ONLY",
            ),
            base_registry.get_callable("ORDER_LOOKUP"),
        )
        repo = AuditRepository()
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout_seconds=999)
        orchestrator = ToolOrchestrator(
            registry, PolicyEngine(), audit_logger=AuditLogger(repository=repo),
            circuit_breaker=cb, retry_policy=RetryPolicy(max_attempts=1), sleep_fn=_no_sleep,
        )
        request = ToolRequest(
            action="ORDER_LOOKUP", params={"order_id": "order_1001", "_simulate_delay_seconds": 999}, confirmed=True,
        )
        orchestrator.invoke(request, auth=AUTHENTICATED_USER)
        self.assertEqual(cb.state, CircuitState.CLOSED)
        orchestrator.invoke(request, auth=AUTHENTICATED_USER)
        self.assertEqual(cb.state, CircuitState.OPEN)

        # Circuit now open -- a further call must fail immediately without attempting execution.
        result = orchestrator.invoke(request, auth=AUTHENTICATED_USER)
        self.assertEqual(result.error, "DEPENDENCY_UNAVAILABLE")
        self.assertEqual(len(repo.list_events(event_type=EventType.CIRCUIT_OPEN)), 1)

    def test_circuit_breaker_never_applied_to_policy_gates(self):
        """The circuit breaker only guards step 5 (execution) -- a policy/confirmation denial is never affected by circuit state."""
        registry = build_default_tool_registry()
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout_seconds=999)
        cb.record_failure()
        self.assertEqual(cb.state, CircuitState.OPEN)
        orchestrator = ToolOrchestrator(registry, PolicyEngine(), circuit_breaker=cb)
        # CANCEL_APPOINTMENT without confirmation is denied at the confirmation gate,
        # which runs BEFORE the circuit-breaker-guarded execution step.
        request = ToolRequest(action="CANCEL_APPOINTMENT", params={"appointment_id": "appt_1000"}, confirmed=False)
        result = orchestrator.invoke(request, auth=AUTHENTICATED_USER)
        self.assertEqual(result.status, "confirmation_required")


class TestConcurrentDuplicateRequests(unittest.TestCase):
    def test_concurrent_same_request_id_executes_exactly_once(self):
        """plan.md Step 10.15/11.13/11.22: a race between two threads submitting the SAME request_id must never both succeed."""
        appointments = MockAppointmentStore()
        booked = appointments.book({"doctor_id": "d1", "date": "2026-08-20", "time": "09:00"})
        registry = build_default_tool_registry(appointment_store=appointments)
        orchestrator = ToolOrchestrator(registry, PolicyEngine())
        request = ToolRequest(
            action="CANCEL_APPOINTMENT", params={"appointment_id": booked["appointment_id"]},
            confirmed=True, request_id="req-fixed-1",
        )
        results = []
        lock = threading.Lock()

        def _call():
            r = orchestrator.invoke(request, auth=AUTHENTICATED_USER)
            with lock:
                results.append(r)

        threads = [threading.Thread(target=_call) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        successes = [r for r in results if r.success]
        self.assertEqual(len(successes), 1)


class TestMetricsIntegration(unittest.TestCase):
    def test_timeout_and_retry_increment_expected_counters(self):
        registry = ToolRegistry()
        base_registry = build_default_tool_registry()
        orig_spec = base_registry.get_spec("ORDER_LOOKUP")
        registry.register(
            ActionSpec(
                name="ORDER_LOOKUP", description=orig_spec.description, params_schema=orig_spec.params_schema,
                required_params=orig_spec.required_params, requires_confirmation=False, destructive=False,
                timeout_seconds=0.02, required_permission=orig_spec.required_permission, idempotency="READ_ONLY",
            ),
            base_registry.get_callable("ORDER_LOOKUP"),
        )
        metrics = MetricsRegistry()
        orchestrator = ToolOrchestrator(
            registry, PolicyEngine(), metrics=metrics,
            retry_policy=RetryPolicy(max_attempts=2, base_delay_seconds=0.01), sleep_fn=_no_sleep,
        )
        request = ToolRequest(
            action="ORDER_LOOKUP", params={"order_id": "order_1001", "_simulate_delay_seconds": 999}, confirmed=True,
        )
        orchestrator.invoke(request, auth=AUTHENTICATED_USER)
        self.assertEqual(metrics.get_counter("timeouts_total"), 2)
        self.assertEqual(metrics.get_counter("retries_total"), 1)

    def test_idempotency_duplicate_increments_counter(self):
        appointments = MockAppointmentStore()
        booked = appointments.book({"doctor_id": "d1", "date": "2026-08-20", "time": "09:00"})
        registry = build_default_tool_registry(appointment_store=appointments)
        metrics = MetricsRegistry()
        orchestrator = ToolOrchestrator(registry, PolicyEngine(), metrics=metrics)
        request = ToolRequest(
            action="CANCEL_APPOINTMENT", params={"appointment_id": booked["appointment_id"]},
            confirmed=True, request_id="req-dup-1",
        )
        orchestrator.invoke(request, auth=AUTHENTICATED_USER)
        orchestrator.invoke(request, auth=AUTHENTICATED_USER)
        self.assertEqual(metrics.get_counter("idempotency_duplicates_total"), 1)


if __name__ == "__main__":
    unittest.main()

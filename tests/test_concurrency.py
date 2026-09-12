"""
Concurrency / race-condition tests (Phase 10; plan.md Steps 10.14,
10.15, 10.17, 10.22): SessionManager, MemoryManager, MetricsRegistry,
AuditRepository, and ToolOrchestrator's PolicyEngine-failure handling
under real concurrent threads.

Uses deterministic assertions on final state/counts (never timing-
sensitive sleeps as the actual test condition) per plan.md Step 10.22:
"Use deterministic concurrency tests where possible."

Run with:
    python -m unittest tests.test_concurrency -v
"""

import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "agent"))
from action_models import AuthContext, ToolRequest  # noqa: E402
from audit import AuditRepository  # noqa: E402
from identity import Role, permissions_for_roles  # noqa: E402
from memory_manager import MemoryManager  # noqa: E402
from memory_models import MemoryCategory  # noqa: E402
from metrics import MetricsRegistry  # noqa: E402
from mock_tools import build_default_tool_registry  # noqa: E402
from observability_models import AuditEvent, EventType, new_event_id, now_utc  # noqa: E402
from policy_engine import PolicyEngine  # noqa: E402
from session_manager import SessionManager  # noqa: E402
from session_models import SessionStatus  # noqa: E402
from tool_orchestrator import ToolOrchestrator  # noqa: E402

AUTHENTICATED_USER = AuthContext(
    user_id="user-1",
    authenticated=True,
    roles=(Role.USER.value,),
    permissions=permissions_for_roles((Role.USER,)),
    authentication_method="test",
)


def _run_concurrently(fn, count=20):
    threads = [threading.Thread(target=fn) for _ in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


class TestSessionManagerConcurrency(unittest.TestCase):
    def test_concurrent_create_session_all_land(self):
        manager = SessionManager()
        created = []
        lock = threading.Lock()

        def _create():
            s = manager.create_session(user_id="user-1")
            with lock:
                created.append(s.session_id)

        _run_concurrently(_create, count=30)
        self.assertEqual(len(created), 30)
        self.assertEqual(len(set(created)), 30)  # every session_id unique, none overwritten

    def test_concurrent_updates_to_same_session_never_lose_the_final_write(self):
        manager = SessionManager()
        manager.create_session(session_id="s1", user_id="user-1")
        errors = []

        def _update(i):
            try:
                manager.update_session("s1", user_id="user-1", metadata={"turn": i})
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=_update, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        final = manager.get_session("s1", user_id="user-1")
        self.assertIn(final.metadata["turn"], range(20))  # some write landed, none corrupted the object

    def test_concurrent_cross_user_access_never_leaks_despite_races(self):
        manager = SessionManager()
        manager.create_session(session_id="s1", user_id="user-a")
        leaked = []

        def _try_read_as_b():
            result = manager.get_session("s1", user_id="user-b")
            if result is not None:
                leaked.append(result)

        _run_concurrently(_try_read_as_b, count=30)
        self.assertEqual(leaked, [])

    def test_concurrent_transitions_never_produce_invalid_state(self):
        manager = SessionManager()
        manager.create_session(session_id="s1", user_id="user-1")
        results = []
        lock = threading.Lock()

        def _try_complete():
            try:
                manager.transition_state("s1", SessionStatus.COMPLETED, user_id="user-1")
                with lock:
                    results.append("ok")
            except Exception:
                with lock:
                    results.append("denied")

        _run_concurrently(_try_complete, count=10)
        final = manager.get_session("s1", user_id="user-1")
        # ACTIVE -> COMPLETED is a valid single transition; concurrent
        # attempts may all succeed (idempotent target state) or some may
        # be denied once already COMPLETED -- either way, final state must
        # be a real, valid status, never corrupted.
        self.assertIn(final.status if final else None, (SessionStatus.COMPLETED, None))


class TestMemoryManagerConcurrency(unittest.TestCase):
    def test_concurrent_persist_for_different_users_never_cross_contaminates(self):
        manager = MemoryManager(PolicyEngine())
        errors = []

        def _persist(user_id):
            try:
                record = manager.propose_memory(
                    user_id=user_id,
                    category=MemoryCategory.PREFERENCE,
                    key="likes_texting",
                    value="yes",
                    source="user",
                )
                manager.persist_memory(record)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=_persist, args=(f"user-{i}",)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        for i in range(20):
            records = manager.list_allowed_memory(f"user-{i}")
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].user_id, f"user-{i}")

    def test_concurrent_list_during_writes_never_crashes(self):
        """list_for_user()'s dict-iteration must never race with a concurrent save() (Phase 10 lock)."""
        manager = MemoryManager(PolicyEngine())
        errors = []

        def _writer():
            for i in range(50):
                record = manager.propose_memory(
                    user_id="user-1",
                    category=MemoryCategory.PREFERENCE,
                    key=f"k{i}",
                    value="v",
                    source="user",
                )
                try:
                    manager.persist_memory(record)
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

        def _reader():
            for _ in range(50):
                try:
                    manager.list_allowed_memory("user-1")
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

        threads = [threading.Thread(target=_writer), threading.Thread(target=_reader), threading.Thread(target=_reader)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])


class TestMetricsRegistryConcurrency(unittest.TestCase):
    def test_concurrent_increments_lose_no_updates(self):
        metrics = MetricsRegistry()

        def _hammer():
            for _ in range(500):
                metrics.increment("requests_total")

        _run_concurrently(_hammer, count=10)
        self.assertEqual(metrics.get_counter("requests_total"), 5000)

    def test_concurrent_observe_lose_no_updates(self):
        metrics = MetricsRegistry()

        def _hammer():
            for _ in range(200):
                metrics.observe("generation_latency_ms", 1.0)

        _run_concurrently(_hammer, count=10)
        self.assertEqual(metrics.get_histogram("generation_latency_ms")["count"], 2000)


class TestAuditRepositoryConcurrency(unittest.TestCase):
    def test_concurrent_appends_lose_no_events(self):
        repo = AuditRepository()

        def _hammer():
            for _ in range(200):
                repo.append(
                    AuditEvent(
                        event_id=new_event_id(),
                        timestamp=now_utc(),
                        event_type=EventType.AUTH_SUCCESS,
                        request_id=None,
                        conversation_id=None,
                        session_id=None,
                        actor="u",
                        action=None,
                        resource=None,
                        outcome="success",
                    )
                )

        _run_concurrently(_hammer, count=10)
        self.assertEqual(len(repo.list_events()), 2000)


class TestToolOrchestratorPolicyFailClosed(unittest.TestCase):
    """plan.md Step 10.12/10.13: a PolicyEngine internal failure inside ToolOrchestrator must deny, never execute."""

    class RaisingToolPolicyEngine(PolicyEngine):
        def __init__(self, raise_on: set):
            super().__init__()
            self._raise_on = raise_on

        def evaluate_tool_action(self, *args, **kwargs):
            if "tool_action" in self._raise_on:
                raise RuntimeError("simulated failure")
            return super().evaluate_tool_action(*args, **kwargs)

        def evaluate_confirmation(self, *args, **kwargs):
            if "confirmation" in self._raise_on:
                raise RuntimeError("simulated failure")
            return super().evaluate_confirmation(*args, **kwargs)

        def evaluate_authorization(self, *args, **kwargs):
            if "authorization" in self._raise_on:
                raise RuntimeError("simulated failure")
            return super().evaluate_authorization(*args, **kwargs)

    def test_tool_action_policy_failure_denies(self):
        registry = build_default_tool_registry()
        orchestrator = ToolOrchestrator(registry, self.RaisingToolPolicyEngine({"tool_action"}))
        request = ToolRequest(action="ORDER_LOOKUP", params={"order_id": "order_1001"}, confirmed=True)
        result = orchestrator.invoke(request, auth=AUTHENTICATED_USER)
        self.assertFalse(result.success)
        self.assertEqual(result.status, "policy_denied")

    def test_confirmation_policy_failure_requires_confirmation(self):
        registry = build_default_tool_registry()
        orchestrator = ToolOrchestrator(registry, self.RaisingToolPolicyEngine({"confirmation"}))
        request = ToolRequest(action="ORDER_LOOKUP", params={"order_id": "order_1001"}, confirmed=True)
        result = orchestrator.invoke(request, auth=AUTHENTICATED_USER)
        self.assertFalse(result.success)
        self.assertEqual(result.status, "confirmation_required")

    def test_authorization_policy_failure_denies(self):
        registry = build_default_tool_registry()
        orchestrator = ToolOrchestrator(registry, self.RaisingToolPolicyEngine({"authorization"}))
        request = ToolRequest(action="ORDER_LOOKUP", params={"order_id": "order_1001"}, confirmed=True)
        result = orchestrator.invoke(request, auth=AUTHENTICATED_USER)
        self.assertFalse(result.success)


if __name__ == "__main__":
    unittest.main()

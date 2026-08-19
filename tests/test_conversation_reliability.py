"""
Failure-injection tests for ConversationManager's Phase 10 reliability
wiring: RAG retry/circuit-breaker, LLM retry/circuit-breaker (pre-first-
chunk only), the generation concurrency semaphore, and the PolicyEngine
fail-closed fixes (clinical/generation/handoff evaluation raising
internally must never fall through to normal, unguarded generation).

Run with:
    python -m unittest tests.test_conversation_reliability -v
"""

import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "agent"))
from audit import AuditLogger, AuditRepository  # noqa: E402
from conversation_manager import ConversationManager  # noqa: E402
from metrics import MetricsRegistry  # noqa: E402
from observability_models import EventType  # noqa: E402
from policy_engine import PolicyEngine  # noqa: E402
from reliability import CircuitBreaker, CircuitState, RetryPolicy  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "inference"))
from handoff_detector import HandoffDetector  # noqa: E402

CLINICAL_CONFIG_PATH = Path(__file__).resolve().parents[1] / "configs" / "clinical_triggers.yaml"
HANDOFF_CONFIG_PATH = Path(__file__).resolve().parents[1] / "configs" / "handoff_phrases.yaml"


def _no_sleep(_seconds: float) -> None:
    pass


class FlakyLLMService:
    """Fails the first `fail_times` calls, then succeeds. Never yields any chunk on a failing attempt (matches a real pre-stream connection failure)."""

    def __init__(self, fail_times: int, response_text: str = "ok now"):
        self.fail_times = fail_times
        self.response_text = response_text
        self.calls = 0

    def generate_stream(self, messages, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ConnectionError("simulated transient LLM backend failure")
        for word in self.response_text.split(" "):
            yield word + " "
        yield {"text": self.response_text, "latency_ms": 5.0}


class PartialStreamThenFailLLMService:
    """Yields one real chunk, THEN raises -- simulates a connection drop mid-stream, where a retry would be unsafe."""

    def __init__(self):
        self.calls = 0

    def generate_stream(self, messages, **kwargs):
        self.calls += 1
        yield "partial "
        raise ConnectionError("simulated mid-stream drop")


class AlwaysFailLLMService:
    def __init__(self):
        self.calls = 0

    def generate_stream(self, messages, **kwargs):
        self.calls += 1
        raise ConnectionError("simulated permanent LLM outage")
        yield  # pragma: no cover -- keeps this a generator


class FlakyRetriever:
    def __init__(self, fail_times: int, chunks=None):
        self.fail_times = fail_times
        self.chunks = chunks or []
        self.calls = 0

    def retrieve(self, query, top_k=3, domain=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ConnectionError("simulated FAISS backend outage")
        return self.chunks[:top_k]


class AlwaysFailRetriever:
    def __init__(self):
        self.calls = 0

    def retrieve(self, query, top_k=3, domain=None):
        self.calls += 1
        raise ConnectionError("simulated permanent FAISS outage")


def _manager(llm_service, retriever=None, **kwargs) -> ConversationManager:
    return ConversationManager(
        llm_service=llm_service,
        retriever=retriever,
        clinical_guard=HandoffDetector(config_path=CLINICAL_CONFIG_PATH),
        handoff_detector=HandoffDetector(config_path=HANDOFF_CONFIG_PATH),
        sleep_fn=_no_sleep,
        **kwargs,
    )


def _run_turn(manager: ConversationManager, message: str = "What are your hours?") -> dict:
    result = None
    for item in manager.handle_turn(message):
        if not isinstance(item, str):
            result = item
    return result


class TestLLMRetry(unittest.TestCase):
    def test_transient_failure_before_any_chunk_is_retried_and_succeeds(self):
        llm = FlakyLLMService(fail_times=1)
        manager = _manager(llm, llm_retry_policy=RetryPolicy(max_attempts=2, base_delay_seconds=0.01))
        result = _run_turn(manager)
        self.assertEqual(llm.calls, 2)
        self.assertIn("ok now", result["response"])
        self.assertFalse(result["degraded"])

    def test_permanent_failure_exhausts_retries_and_returns_safe_fallback(self):
        llm = AlwaysFailLLMService()
        manager = _manager(llm, llm_retry_policy=RetryPolicy(max_attempts=3, base_delay_seconds=0.01))
        result = _run_turn(manager)
        self.assertEqual(llm.calls, 3)  # exactly max_attempts, no infinite loop
        self.assertEqual(result["response"], manager.LLM_FAILURE_RESPONSE)
        self.assertTrue(result["degraded"])
        self.assertEqual(result["error"], "llm_generation_failed")

    def test_repeated_failure_bounded_no_infinite_loop(self):
        llm = AlwaysFailLLMService()
        manager = _manager(llm, llm_retry_policy=RetryPolicy(max_attempts=5, base_delay_seconds=0.001))
        _run_turn(manager)
        self.assertEqual(llm.calls, 5)

    def test_failure_after_partial_stream_is_never_retried(self):
        """Once a chunk has been yielded, a retry would duplicate/confuse the caller -- must fall straight to the fallback."""
        llm = PartialStreamThenFailLLMService()
        manager = _manager(llm, llm_retry_policy=RetryPolicy(max_attempts=5, base_delay_seconds=0.01))
        result = _run_turn(manager)
        self.assertEqual(llm.calls, 1)  # never retried despite max_attempts=5
        self.assertEqual(result["response"], manager.LLM_FAILURE_RESPONSE)

    def test_no_retry_when_default_policy_used(self):
        """Omitting llm_retry_policy preserves exact pre-Phase-10 behavior: a single attempt, no retry."""
        llm = FlakyLLMService(fail_times=1)
        manager = _manager(llm)
        result = _run_turn(manager)
        self.assertEqual(llm.calls, 1)
        self.assertEqual(result["response"], manager.LLM_FAILURE_RESPONSE)

    def test_retry_emits_audit_events_and_metrics(self):
        repo = AuditRepository()
        metrics = MetricsRegistry()
        llm = FlakyLLMService(fail_times=1)
        manager = _manager(
            llm, llm_retry_policy=RetryPolicy(max_attempts=2, base_delay_seconds=0.01),
            audit_logger=AuditLogger(repository=repo), metrics=metrics,
        )
        _run_turn(manager)
        self.assertEqual(len(repo.list_events(event_type=EventType.RETRY_ATTEMPT)), 1)
        self.assertEqual(len(repo.list_events(event_type=EventType.DEPENDENCY_FAILURE)), 1)
        self.assertEqual(metrics.get_counter("retries_total"), 1)


class TestLLMCircuitBreaker(unittest.TestCase):
    def test_open_circuit_skips_generation_entirely(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout_seconds=999)
        cb.record_failure()
        self.assertEqual(cb.state, CircuitState.OPEN)
        llm = FlakyLLMService(fail_times=0)  # would succeed if ever called
        manager = _manager(llm, llm_circuit_breaker=cb)
        result = _run_turn(manager)
        self.assertEqual(llm.calls, 0)  # never attempted
        self.assertEqual(result["response"], manager.LLM_FAILURE_RESPONSE)

    def test_repeated_failures_open_the_circuit(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout_seconds=999)
        llm = AlwaysFailLLMService()
        manager = _manager(llm, llm_circuit_breaker=cb, llm_retry_policy=RetryPolicy(max_attempts=1))
        _run_turn(manager)
        _run_turn(manager)
        self.assertEqual(cb.state, CircuitState.OPEN)


class TestGenerationConcurrencySemaphore(unittest.TestCase):
    def test_concurrent_generations_are_serialized_when_limit_is_one(self):
        """With max_concurrent_generations=1, two concurrent handle_turn() calls must never overlap inside generate_stream()."""
        overlap_detected = [False]
        currently_inside = [0]
        lock = threading.Lock()

        class TrackingLLMService:
            def generate_stream(self, messages, **kwargs):
                with lock:
                    currently_inside[0] += 1
                    if currently_inside[0] > 1:
                        overlap_detected[0] = True
                import time as _time
                _time.sleep(0.05)
                with lock:
                    currently_inside[0] -= 1
                yield "hi "
                yield {"text": "hi", "latency_ms": 1.0}

        manager = _manager(TrackingLLMService(), max_concurrent_generations=1)
        threads = [threading.Thread(target=_run_turn, args=(manager,)) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertFalse(overlap_detected[0])


class TestRAGRetry(unittest.TestCase):
    def test_transient_retrieval_failure_is_retried_and_succeeds(self):
        retriever = FlakyRetriever(fail_times=1)
        llm = FlakyLLMService(fail_times=0)
        manager = _manager(llm, retriever=retriever, rag_retry_policy=RetryPolicy(max_attempts=2, base_delay_seconds=0.01))
        result = _run_turn(manager)
        self.assertEqual(retriever.calls, 2)
        self.assertFalse(result["degraded"])

    def test_permanent_retrieval_failure_degrades_without_hallucination(self):
        """No grounding available must remain a degraded-but-valid state -- never invented context."""
        retriever = AlwaysFailRetriever()
        llm = FlakyLLMService(fail_times=0)
        manager = _manager(llm, retriever=retriever, rag_retry_policy=RetryPolicy(max_attempts=2, base_delay_seconds=0.01))
        result = _run_turn(manager)
        self.assertEqual(result["retrieved_chunks"], [])
        self.assertTrue(result["degraded"])
        # The LLM is still called (ungrounded, degraded generation continues) -- never told to "invent" grounding.
        self.assertEqual(llm.calls, 1)

    def test_rag_circuit_breaker_skips_retrieval_when_open(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout_seconds=999)
        cb.record_failure()
        retriever = FlakyRetriever(fail_times=0)
        llm = FlakyLLMService(fail_times=0)
        manager = _manager(llm, retriever=retriever, rag_circuit_breaker=cb)
        result = _run_turn(manager)
        self.assertEqual(retriever.calls, 0)
        self.assertTrue(result["degraded"])


class TestPolicyEngineFailClosed(unittest.TestCase):
    """plan.md Step 10.12: an internal PolicyEngine failure must deny/fail closed, never fall through to unguarded behavior."""

    class RaisingPolicyEngine(PolicyEngine):
        def __init__(self, raise_on: set):
            super().__init__()
            self._raise_on = raise_on

        def evaluate_clinical(self, *args, **kwargs):
            if "clinical" in self._raise_on:
                raise RuntimeError("simulated PolicyEngine internal failure")
            return super().evaluate_clinical(*args, **kwargs)

        def evaluate_generation(self, *args, **kwargs):
            if "generation" in self._raise_on:
                raise RuntimeError("simulated PolicyEngine internal failure")
            return super().evaluate_generation(*args, **kwargs)

        def evaluate_handoff(self, *args, **kwargs):
            if "handoff" in self._raise_on:
                raise RuntimeError("simulated PolicyEngine internal failure")
            return super().evaluate_handoff(*args, **kwargs)

    def test_clinical_evaluation_failure_blocks_rather_than_allows(self):
        llm = FlakyLLMService(fail_times=0)
        manager = _manager(llm, policy_engine=self.RaisingPolicyEngine({"clinical"}))
        result = _run_turn(manager, message="What's the correct dosage of this medication?")
        self.assertTrue(result["is_handoff"])
        self.assertEqual(llm.calls, 0)  # never reached generation

    def test_generation_evaluation_failure_falls_to_clarification_not_generation(self):
        llm = FlakyLLMService(fail_times=0)
        manager = _manager(llm, policy_engine=self.RaisingPolicyEngine({"generation"}))
        result = _run_turn(manager, message="asdkj qwoeiu random gibberish")
        # Regardless of what routing produced, a PolicyEngine failure here must not silently ALLOW.
        self.assertEqual(result["response"], manager.CLARIFICATION_RESPONSE)
        self.assertEqual(llm.calls, 0)

    def test_handoff_evaluation_failure_does_not_crash_the_turn(self):
        llm = FlakyLLMService(fail_times=0)
        manager = _manager(llm, policy_engine=self.RaisingPolicyEngine({"handoff"}))
        result = _run_turn(manager)
        self.assertIn("ok now", result["response"])  # generation still completed normally


class TestAuditFailureIndependence(unittest.TestCase):
    """
    plan.md Step 10.23: a normal business operation must not fail solely
    because the audit BACKEND (repository) is broken. AuditLogger.record()
    itself is the documented best-effort boundary (Phase 8) -- this
    exercises that guarantee through a full conversational turn, not just
    AuditLogger in isolation.
    """

    def test_broken_audit_repository_does_not_break_a_normal_turn(self):
        class BrokenRepository:
            def append(self, event):
                raise RuntimeError("simulated audit backend outage")

        llm = FlakyLLMService(fail_times=0)
        manager = _manager(llm, audit_logger=AuditLogger(repository=BrokenRepository()))
        result = _run_turn(manager, message="What are your hours?")
        self.assertIn("ok now", result["response"])
        self.assertFalse(result["degraded"])


if __name__ == "__main__":
    unittest.main()

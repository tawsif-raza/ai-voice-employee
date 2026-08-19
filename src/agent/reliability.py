"""
Reliability primitives shared across dependency call sites (Phase 10;
plan.md Steps 10.3-10.9): typed timeout/failure errors, a deterministic
retry policy, an idempotency classification, and a circuit breaker.

This module contains no business logic of its own and calls nothing else
in this repository -- it is a small, dependency-free toolkit that
ConversationManager/ToolOrchestrator apply around their EXISTING calls to
the LLM, RAG, and tool dependencies (plan.md: "smallest compatible
change," "reuse repository-native abstractions").

Deliberately NOT applied here or anywhere in this repository to
PolicyEngine, ClinicalSafetyGuard, AuthenticationProvider, or
PrivacyService (plan.md Step 10.9's explicit list) -- those are local,
in-process security/control components that must fail closed on their
own, not be retried or circuit-broken as if they were flaky external
dependencies.
"""

import random
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional


class DependencyTimeoutError(Exception):
    """Raised (by callers, not this module) when a bounded wait on a dependency call expires."""


class DependencyFailureError(Exception):
    """Raised (by callers) to classify a dependency failure distinctly from a business/validation error."""


class IdempotencyClass(str, Enum):
    """
    plan.md Step 10.6's requested operation classification. Every
    ActionSpec (src/agent/action_models.py) declares one of these,
    defaulting to NON_IDEMPOTENT_WRITE (fail closed: never assume an
    action is safe to retry unless it explicitly says so).
    """

    READ_ONLY = "READ_ONLY"
    IDEMPOTENT_WRITE = "IDEMPOTENT_WRITE"
    NON_IDEMPOTENT_WRITE = "NON_IDEMPOTENT_WRITE"

    @property
    def retry_safe(self) -> bool:
        return self is not IdempotencyClass.NON_IDEMPOTENT_WRITE


@dataclass(frozen=True)
class RetryDecision:
    retryable: bool
    attempt: int
    max_attempts: int
    delay_seconds: float
    reason: str


class RetryPolicy:
    """
    Deterministic, bounded retry decision-making (plan.md Steps 10.4,
    10.5). Never decides retry based on the operation's *content* --
    only on `attempt`/`max_attempts` (bounded count) and the caller's own
    `retryable` classification (which the caller derives from error
    type/idempotency, never from this class). Backoff is pure/computable
    (`compute_delay`) so tests can assert on delay values without
    actually sleeping (plan.md Step 10.5: "Do not make tests actually
    sleep for long durations").
    """

    def __init__(self, max_attempts: int = 2, base_delay_seconds: float = 0.1, max_delay_seconds: float = 2.0, jitter: bool = False):
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if base_delay_seconds < 0 or max_delay_seconds < 0:
            raise ValueError("delay values must not be negative")
        self.max_attempts = max_attempts
        self.base_delay_seconds = base_delay_seconds
        self.max_delay_seconds = max_delay_seconds
        self.jitter = jitter

    def compute_delay(self, attempt: int) -> float:
        """attempt is 1-indexed (the attempt that just failed). Exponential backoff, capped, optional jitter."""
        delay = min(self.base_delay_seconds * (2 ** (attempt - 1)), self.max_delay_seconds)
        if self.jitter:
            delay = delay * random.uniform(0.5, 1.0)
        return delay

    def decide(self, attempt: int, retryable: bool, reason: str = "") -> RetryDecision:
        """`attempt` is the attempt number that just failed (1-indexed). Never retries past max_attempts, never retries a non-retryable failure."""
        can_retry = retryable and attempt < self.max_attempts
        return RetryDecision(
            retryable=can_retry, attempt=attempt, max_attempts=self.max_attempts,
            delay_seconds=self.compute_delay(attempt) if can_retry else 0.0,
            reason=reason or ("transient, retrying" if can_retry else "not retryable or attempts exhausted"),
        )


class CircuitState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreaker:
    """
    Lightweight circuit breaker (plan.md Step 10.9) for an external
    dependency (LLM provider, RAG index, a tool's business API — never a
    local security/control component, see module docstring). Thread-safe
    (`threading.Lock`) since FastAPI's sync routes run in a threadpool
    and multiple requests can hit the same dependency concurrently
    (plan.md Step 10.17).

    CLOSED -> (>= failure_threshold consecutive failures) -> OPEN
    OPEN -> (recovery_timeout_seconds elapsed) -> HALF_OPEN (next call is a trial)
    HALF_OPEN -> success -> CLOSED ; failure -> OPEN
    """

    def __init__(self, failure_threshold: int = 5, recovery_timeout_seconds: float = 30.0, clock: Callable[[], float] = time.monotonic):
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1")
        self._failure_threshold = failure_threshold
        self._recovery_timeout_seconds = recovery_timeout_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at: Optional[float] = None

    @property
    def state(self) -> CircuitState:
        with self._lock:
            return self._resolved_state_locked()

    def _resolved_state_locked(self) -> CircuitState:
        if self._state is CircuitState.OPEN and self._opened_at is not None:
            if self._clock() - self._opened_at >= self._recovery_timeout_seconds:
                self._state = CircuitState.HALF_OPEN
        return self._state

    def allow_request(self) -> bool:
        """True if a call may proceed (CLOSED or a HALF_OPEN trial); False if OPEN and still within the recovery window."""
        with self._lock:
            return self._resolved_state_locked() is not CircuitState.OPEN

    def record_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0
            self._state = CircuitState.CLOSED
            self._opened_at = None

    def record_failure(self) -> CircuitState:
        """Returns the resulting state, so callers can emit a CIRCUIT_OPEN event exactly on the transition, not on every failure."""
        with self._lock:
            self._consecutive_failures += 1
            previous_state = self._resolved_state_locked()
            if self._consecutive_failures >= self._failure_threshold or previous_state is CircuitState.HALF_OPEN:
                self._state = CircuitState.OPEN
                self._opened_at = self._clock()
            return self._state

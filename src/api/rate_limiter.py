"""
Per-identity token-bucket rate limiting for the text API
(docs/MASTER_PROJECT_PLAN.md H2, F-04).

In-process and dependency-free on purpose: the service runs as a single
process (see jobs.py for the same scope decision), so a shared store such
as Redis would add an operational dependency without changing behaviour.
If the service is ever scaled to several tasks, enforce the limit at the
edge (ALB/WAF rate rules) or move buckets to a shared store.

Each key gets `burst` tokens, refilled continuously at
`per_minute / 60` tokens per second; one request costs one token. Memory is
bounded: at most `max_keys` buckets are kept, least recently used evicted
first (an evicted key simply starts again with a full bucket).
"""

import math
import threading
import time
from collections import OrderedDict
from typing import Callable


class TokenBucketRateLimiter:
    def __init__(
        self,
        per_minute: int,
        burst: int,
        max_keys: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ):
        if per_minute < 1 or burst < 1 or max_keys < 1:
            raise ValueError("per_minute, burst and max_keys must all be >= 1")
        self._rate_per_second = per_minute / 60.0
        self._burst = float(burst)
        self._max_keys = max_keys
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: "OrderedDict[str, tuple[float, float]]" = OrderedDict()  # key -> (tokens, updated_at)

    def check(self, key: str) -> tuple[bool, int]:
        """Consumes one token for `key`. Returns (allowed, retry_after_seconds)."""
        now = self._clock()
        with self._lock:
            tokens, updated_at = self._buckets.pop(key, (self._burst, now))
            tokens = min(self._burst, tokens + (now - updated_at) * self._rate_per_second)
            if tokens >= 1.0:
                allowed, tokens, retry_after = True, tokens - 1.0, 0
            else:
                allowed, retry_after = False, max(1, math.ceil((1.0 - tokens) / self._rate_per_second))
            self._buckets[key] = (tokens, now)
            while len(self._buckets) > self._max_keys:
                self._buckets.popitem(last=False)
            return allowed, retry_after

    def tracked_keys(self) -> int:
        with self._lock:
            return len(self._buckets)

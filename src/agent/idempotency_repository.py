"""
IdempotencyRepository interface + in-memory default (Phase 12; plan.md
Step 12.9).

No in-memory predecessor class existed for this (audit §7) —
ToolOrchestrator previously tracked executed request_ids as a bare
`set()` directly on the instance (`_executed_request_ids`,
`tool_orchestrator.py`). `InMemoryIdempotencyRepository` here is a
richer, user/action-scoped alternative implementing this same interface,
but ToolOrchestrator's DEFAULT behavior when no `idempotency_repository`
is supplied remains its own untouched, original `_executed_request_ids`
set — this class is an explicit, opt-in alternative (and the interface
PostgresIdempotencyRepository also implements), not a silent replacement
of default behavior ("Do not replace working idempotency behavior
blindly," this step's own instruction).

Scoping (Step 12.9's explicit requirement): a record is keyed by
(user_id, request_id) — NOT request_id alone. Two different users
reusing the identical request_id string are isolated from each other
(each gets an independent slot); the SAME (user_id, request_id) pair
reused for a DIFFERENT action is treated as a conflict and denied exactly
like a same-action duplicate would be — an idempotency key must never be
silently repurposed for a different operation once claimed.

Reserve-before-execute, not record-after-success (a deliberate design
choice, not the same shape as ToolOrchestrator's original raw-set
check): `try_reserve()` is the atomic compare-and-swap gate, called
BEFORE the tool runs — only the caller that wins the reservation may
proceed to actually invoke the tool. This is what makes "exactly one
execution" hold even for a genuinely non-idempotent tool under real
concurrency; recording success only *after* execution (the original
set's approach) leaves a window where two concurrent callers could both
pass a pre-check and both execute before either finishes recording.
`update_result()` afterward is a simple, non-racing update — only the
caller that won the reservation ever calls it, for exactly the row it
already owns.
"""

import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

# Matches this module's own "24h is a conventional idempotency-key TTL"
# choice (e.g. Stripe's Idempotency-Key header uses the same window) --
# long enough that a legitimate client retry (network blip, redeploy)
# still hits the same key, short enough that the table doesn't grow
# unbounded. Overridable per-call for tests.
DEFAULT_TTL = timedelta(hours=24)


class InMemoryIdempotencyRepository:
    """
    Thread-safe (matching every other in-memory repository in this
    codebase — SessionRepository, MemoryRepository, AuditRepository, all
    guard their state with a lock). NOT the default ToolOrchestrator uses
    when no `idempotency_repository` is passed — see module docstring.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._records: dict = {}  # (user_id, request_id) -> {"action": str, "result_status": str, "reserved_at": datetime, "expires_at": datetime}

    def try_reserve(self, request_id: str, *, user_id: str, action: str, ttl: timedelta = DEFAULT_TTL) -> bool:
        """
        Atomic (single Python lock, sufficient for a single-process
        in-memory store): True if this call claimed the slot, False if
        already claimed by an unexpired record. An expired record is
        treated as absent — reclaimed by this call exactly like a truly
        missing key (Step 12.9: "expired key -> correct expiration
        behavior"), not deleted by a separate sweep.
        """
        with self._lock:
            key = (user_id, request_id)
            now = datetime.now(timezone.utc)
            existing = self._records.get(key)
            if existing is not None and existing["expires_at"] > now:
                return False
            self._records[key] = {
                "action": action, "result_status": "in_progress", "reserved_at": now, "expires_at": now + ttl,
            }
            return True

    def update_result(self, request_id: str, *, user_id: str, result_status: str) -> None:
        with self._lock:
            record = self._records.get((user_id, request_id))
            if record is not None:
                record["result_status"] = result_status

    def release(self, request_id: str, *, user_id: str) -> None:
        """
        Removes the reservation entirely — called when execution FAILED,
        so the idempotency key is not permanently consumed by a failed
        attempt (only a successful one should be "locked in"; standard
        idempotency-key semantics, matching this codebase's own pre-
        existing default behavior of adding to `_executed_request_ids`
        only `if result.success`). A subsequent invoke() with the same
        (user_id, request_id) after a release can legitimately retry.
        """
        with self._lock:
            self._records.pop((user_id, request_id), None)

    def has_executed(self, request_id: str, *, user_id: str, action: str) -> bool:
        """Read-only convenience check (e.g. for a pre-execution short-circuit) — not itself the atomicity guarantee; try_reserve() is. An expired record reports False, same as try_reserve()'s own expiration handling."""
        with self._lock:
            record = self._records.get((user_id, request_id))
            return record is not None and record["expires_at"] > datetime.now(timezone.utc)

    def get_recorded_action(self, request_id: str, *, user_id: str) -> Optional[str]:
        with self._lock:
            record = self._records.get((user_id, request_id))
            return record["action"] if record else None

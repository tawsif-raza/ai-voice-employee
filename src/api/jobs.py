"""
In-process background job store (Phase 1 stabilization for heavy AI
workloads -- see PHASE_15_REQUEST_ISOLATION_REPORT.md).

Problem this solves: POST /generate runs ConversationManager.handle_turn()
(RAG retrieval + LLM generation -- the heaviest work in this codebase)
synchronously within the request/response cycle. FastAPI already
dispatches that sync route to a worker thread (so the asyncio event loop
itself is never blocked), but the HTTP connection, that worker thread,
and this process's single generation semaphore (conversation_manager.py's
_generation_semaphore) are all held for the entire duration of one
generation. A client that submits heavy work has no way to get a fast
acknowledgement and check back later -- it must hold the connection open
for as long as generation takes.

This module adds the smallest mechanism that fixes exactly that: a job is
created (fast, no model access), the actual heavy work is dispatched to a
background thread via the same asyncio.loop.run_in_executor() pattern
voice_pipeline.py already uses for the identical underlying call, and the
caller polls for the result. The heavy work still runs in this SAME
process -- this is NOT a distributed task queue (no Redis/Celery, no
separate worker process/machine). That is a deliberate Phase 1 scope
boundary, not an oversight -- see the report for what a real Phase 2
would add on top of this.

Design notes:
- Thread-safe: `_lock` guards every read/write, matching this codebase's
  existing pattern (session_manager.py, tool_orchestrator.py's
  _executed_request_ids_lock) for in-memory state shared across the
  request threadpool.
- Bounded: at most `_MAX_JOBS` records are kept (oldest evicted first) so
  a client that never polls for its result cannot grow this store
  unboundedly -- the same "don't introduce a new unbounded-memory problem
  while fixing a stability problem" discipline this codebase already
  applies to every other in-memory store (AuditRepository, SessionManager,
  MemoryManager).
- Fail-closed per job, not per process: a job's own execution wrapper
  (server.py's _run_generate_job) catches every exception and records it
  as JobStatus.FAILED -- this module only ever sees a status transition,
  never a raised exception from a job's own work.
"""

import copy
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class JobRecord:
    job_id: str
    status: JobStatus
    created_at: float
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    result: Optional[dict[str, Any]] = field(default=None)
    error: Optional[str] = None


class JobStore:
    """Thread-safe, bounded, in-memory job registry. See module docstring."""

    def __init__(self, max_jobs: int = 1000):
        self._lock = threading.Lock()
        self._jobs: dict[str, JobRecord] = {}
        self._order: list[str] = []
        self._max_jobs = max_jobs

    def create(self, job_id: str) -> JobRecord:
        record = JobRecord(job_id=job_id, status=JobStatus.QUEUED, created_at=time.time())
        with self._lock:
            self._jobs[job_id] = record
            self._order.append(job_id)
            while len(self._order) > self._max_jobs:
                oldest_id = self._order.pop(0)
                self._jobs.pop(oldest_id, None)
        return copy.copy(record)

    def get(self, job_id: str) -> Optional[JobRecord]:
        with self._lock:
            record = self._jobs.get(job_id)
            return copy.copy(record) if record is not None else None

    def mark_running(self, job_id: str) -> None:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is not None:
                record.status = JobStatus.RUNNING
                record.started_at = time.time()

    def mark_completed(self, job_id: str, result: dict[str, Any]) -> None:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is not None:
                record.status = JobStatus.COMPLETED
                record.result = result
                record.completed_at = time.time()

    def mark_failed(self, job_id: str, error: str) -> None:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is not None:
                record.status = JobStatus.FAILED
                record.error = error
                record.completed_at = time.time()

    def size(self) -> int:
        with self._lock:
            return len(self._jobs)

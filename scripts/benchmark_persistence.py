"""
Persistence performance baseline (Phase 12; plan.md Step 12.14).

Measures each core repository operation plus behavior under moderate
concurrency, and verifies the connection pool stays bounded (no
connection leaks, no unbounded queue growth).

IMPORTANT — environment disclosure (see PHASE_12_14_PERFORMANCE_REPORT.md
for the full discussion): this script runs against SQLite, this
project's consistent PostgreSQL stand-in throughout Phase 12 (no
reachable PostgreSQL server exists in this development environment — see
PHASE_12_1_PERSISTENCE_AUDIT.md). The absolute latency numbers here are
NOT representative of a real, network-attached PostgreSQL server (no
network round-trip, no server-side query planning, no real disk I/O
contention) — they are useful only for spotting obviously pathological
per-operation overhead (e.g. accidental O(n) behavior, connection leaks,
gross N+1 patterns), not for capacity planning.

Run with:
    python scripts/benchmark_persistence.py
"""

import os
import statistics
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "agent"))

from audit_repository_postgres import PostgresAuditRepository  # noqa: E402
from db import Database, load_database_config  # noqa: E402
from db_models import Base  # noqa: E402
from idempotency_repository_postgres import PostgresIdempotencyRepository  # noqa: E402
from memory_models import MemoryCategory, MemoryRecord  # noqa: E402
from memory_repository_postgres import PostgresMemoryRepository  # noqa: E402
from observability_models import AuditEvent, EventType, new_event_id, now_utc  # noqa: E402
from session_models import SessionState  # noqa: E402
from session_repository_postgres import PostgresSessionRepository  # noqa: E402

N = 500  # iterations per single-operation measurement


def _percentile(values, pct):
    values = sorted(values)
    idx = min(int(len(values) * pct), len(values) - 1)
    return values[idx]


def _summarize(name, samples_seconds):
    ms = [s * 1000 for s in samples_seconds]
    print(
        f"{name:32s} n={len(ms):5d}  mean={statistics.mean(ms):7.3f}ms  "
        f"median={statistics.median(ms):7.3f}ms  p95={_percentile(ms, 0.95):7.3f}ms  "
        f"max={max(ms):7.3f}ms"
    )


def _timed(fn, n=N):
    samples = []
    for _ in range(n):
        start = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - start)
    return samples


def main():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="phase12_benchmark_")
    os.close(fd)
    os.remove(path)
    db_path = Path(path)
    try:
        database = Database(
            load_database_config(env={"DATABASE_URL": f"sqlite:///{db_path.as_posix()}", "DB_POOL_SIZE": "10"})
        )
        Base.metadata.create_all(database.engine)

        session_repo = PostgresSessionRepository(database)
        memory_repo = PostgresMemoryRepository(database)
        audit_repo = PostgresAuditRepository(database)
        idempotency_repo = PostgresIdempotencyRepository(database)

        print(f"=== Phase 12.14 Persistence Performance Baseline (SQLite stand-in, N={N}) ===\n")

        # ── Session ──────────────────────────────────────────────────
        now = datetime.now(timezone.utc)
        seed_session = SessionState(
            session_id="bench-session",
            user_id="bench-user",
            created_at=now,
            updated_at=now,
            expires_at=now + timedelta(hours=1),
        )
        session_repo.save(seed_session)
        _summarize("session read", _timed(lambda: session_repo.get("bench-session")))

        counter = {"n": 0}

        def _session_write():
            counter["n"] += 1
            s = SessionState(
                session_id=f"bench-session-write-{counter['n']}",
                user_id="bench-user",
                created_at=now,
                updated_at=now,
                expires_at=now + timedelta(hours=1),
            )
            session_repo.save(s)

        _summarize("session write", _timed(_session_write))

        # ── Memory ───────────────────────────────────────────────────
        seed_memory = MemoryRecord(
            id="bench-memory",
            user_id="bench-user",
            category=MemoryCategory.PREFERENCE,
            key="k",
            value="v",
            source="s",
            created_at=now,
            updated_at=now,
        )
        memory_repo.save(seed_memory)
        _summarize("memory read", _timed(lambda: memory_repo.get("bench-memory")))

        mem_counter = {"n": 0}

        def _memory_write():
            mem_counter["n"] += 1
            r = MemoryRecord(
                id=f"bench-memory-write-{mem_counter['n']}",
                user_id="bench-user",
                category=MemoryCategory.PREFERENCE,
                key="k",
                value="v",
                source="s",
                created_at=now,
                updated_at=now,
            )
            memory_repo.save(r)

        _summarize("memory write", _timed(_memory_write))

        # ── Audit ────────────────────────────────────────────────────
        def _audit_write():
            audit_repo.append(
                AuditEvent(
                    event_id=new_event_id(),
                    timestamp=now_utc(),
                    event_type=EventType.TOOL_REQUESTED,
                    request_id=None,
                    conversation_id=None,
                    session_id=None,
                    actor="bench-user",
                    action="BENCH",
                    resource=None,
                    outcome="requested",
                )
            )

        _summarize("audit write", _timed(_audit_write))

        # ── Idempotency ──────────────────────────────────────────────
        idempotency_repo.try_reserve("bench-idem-seed", user_id="bench-user", action="BENCH")
        _summarize(
            "idempotency lookup (has_executed)",
            _timed(lambda: idempotency_repo.has_executed("bench-idem-seed", user_id="bench-user", action="BENCH")),
        )

        idem_counter = {"n": 0}

        def _idempotency_reserve():
            idem_counter["n"] += 1
            idempotency_repo.try_reserve(f"bench-idem-{idem_counter['n']}", user_id="bench-user", action="BENCH")

        _summarize("idempotency reserve (write)", _timed(_idempotency_reserve))

        # ── Confirmation consumption ─────────────────────────────────
        confirm_counter = {"n": 0}

        def _confirmation_consumption():
            confirm_counter["n"] += 1
            sid = f"bench-confirm-{confirm_counter['n']}"
            session_repo.save(
                SessionState(
                    session_id=sid,
                    user_id="bench-user",
                    created_at=now,
                    updated_at=now,
                    expires_at=now + timedelta(hours=1),
                    workflow_state="AWAITING_CONFIRMATION",
                    pending_action="CANCEL_APPOINTMENT",
                    pending_parameters={"appointment_id": "1"},
                )
            )
            session_repo.try_consume_pending_confirmation(sid, user_id="bench-user")

        _summarize("confirmation consumption", _timed(_confirmation_consumption, n=200))

        # ── Concurrency ──────────────────────────────────────────────
        print("\n=== Moderate concurrency (20 threads x 20 ops each, mixed read/write) ===\n")
        conc_counter = {"n": 0}
        conc_lock = threading.Lock()
        latencies = []
        latencies_lock = threading.Lock()

        def _worker():
            for _ in range(20):
                with conc_lock:
                    conc_counter["n"] += 1
                    i = conc_counter["n"]
                start = time.perf_counter()
                session_repo.save(
                    SessionState(
                        session_id=f"bench-conc-{i}",
                        user_id="bench-user",
                        created_at=now,
                        updated_at=now,
                        expires_at=now + timedelta(hours=1),
                    )
                )
                session_repo.get(f"bench-conc-{i}")
                elapsed = time.perf_counter() - start
                with latencies_lock:
                    latencies.append(elapsed)

        threads = [threading.Thread(target=_worker) for _ in range(20)]
        overall_start = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        overall_elapsed = time.perf_counter() - overall_start

        _summarize("concurrent save+get (per op)", latencies)
        print(
            f"{'total wall time':32s} {overall_elapsed * 1000:7.3f}ms for {len(latencies)} operations across 20 threads"
        )

        # ── Pool health verification ─────────────────────────────────
        print("\n=== Connection pool health ===\n")
        pool = database.engine.pool
        print(f"pool checked-out connections after load: {pool.checkedout()}")
        print(f"pool size: {getattr(pool, 'size', lambda: 'n/a')()}")
        assert pool.checkedout() == 0, (
            "connection leak detected: connections still checked out after all operations completed"
        )
        print("OK: no connection leak (checkedout() == 0 after all operations)")

        database.dispose()
    finally:
        if db_path.exists():
            db_path.unlink()


if __name__ == "__main__":
    main()

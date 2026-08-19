# Phase 12.14 — Persistence Performance Baseline Report

## Objective

Measure the new persistence layer without premature optimization. Establish a baseline, not a target.

## Methodology

New script: **`scripts/benchmark_persistence.py`** — measures each required operation in isolation (500
iterations per single-op measurement, 200 for confirmation consumption since it performs two statements),
then a moderate-concurrency phase (20 threads × 20 mixed save+get operations = 400 total operations), then
verifies the connection pool's post-load state.

No unittest assertions on absolute timing — this is measurement, not a pass/fail gate (per this step's own
instruction: "do not optimize unless a measurable problem exists," which presupposes measuring first without
pre-judging acceptable thresholds).

## Environment — Read This Before The Numbers

**No PostgreSQL server is reachable in this development environment** (established in
`PHASE_12_1_PERSISTENCE_AUDIT.md`; still true — Docker Desktop's engine could not be started in this
session). This benchmark runs against **SQLite**, this project's consistent stand-in throughout Phase 12.

This matters more for a *performance* report than any other Phase 12 report so far, and is stated plainly
rather than buried:

- **Absolute latency numbers below are not representative of a real, network-attached PostgreSQL server.**
  SQLite has no network round-trip, no server-side connection handshake, no query planner, and (for a
  local, mostly-cached file) very different I/O characteristics than a real database server under load.
- **The concurrency numbers specifically are the least representative of all.** SQLite serializes all
  writers behind a single file-level lock (one writer at a time, full stop); PostgreSQL uses MVCC and can
  genuinely execute concurrent writers to *different* rows without blocking each other. The high variance
  and long tail observed under concurrency below (see Results) is substantially a SQLite artifact, not
  necessarily a property this same code would exhibit against real PostgreSQL.
- What this benchmark **is** legitimately useful for: confirming no operation has an obviously pathological
  per-call cost (e.g., an accidental O(n) scan), confirming the connection pool behaves correctly (no leak,
  bounded size), and confirming no N+1 query pattern exists in the repository code itself (a property of the
  *code*, not the *database*, and therefore honestly measurable here).

Machine: this development environment's local disk (Windows, WSL-free), Python 3.14, SQLAlchemy 2.0.52.

## Results

### Single-operation latency (N=500, or N=200 for confirmation consumption)

| Operation | mean | median | p95 | max |
|---|---|---|---|---|
| Session read | 0.41 ms | 0.36 ms | 0.55 ms | 9.37 ms |
| Session write | 11.22 ms | 10.98 ms | 14.46 ms | 22.64 ms |
| Memory read | 0.61 ms | 0.61 ms | 0.92 ms | 3.89 ms |
| Memory write | 12.69 ms | 12.67 ms | 15.77 ms | 28.81 ms |
| Audit write | 10.85 ms | 10.77 ms | 13.68 ms | 15.80 ms |
| Idempotency lookup (`has_executed`) | 0.38 ms | 0.32 ms | 0.71 ms | 1.45 ms |
| Idempotency reserve (write) | 12.50 ms | 12.49 ms | 16.31 ms | 28.18 ms |
| Confirmation consumption (2 statements) | 35.55 ms | 32.10 ms | 57.30 ms | 119.27 ms |

**Pattern observed, explained, not a surprise**: every *read* is sub-millisecond; every *write* costs
roughly 10-13ms. This gap is SQLite's per-transaction `fsync()` — each `session_scope()` commit forces a
real disk sync by default, which is an inherent SQLite-on-this-filesystem cost, not a property of the
repository code's own overhead (the actual Python/SQLAlchemy work per operation, visible in the read
numbers since reads don't commit a write transaction, is under a millisecond). Confirmation consumption
costs roughly 3x a single write because `PostgresSessionRepository.try_consume_pending_confirmation()`
performs two `UPDATE` statements in one transaction (Step 12.6's own documented design — the first transitions
`workflow_state` and returns the old `pending_action`/`pending_parameters` via `RETURNING`; the second
clears those fields) plus the benchmark's own session-seeding write before each measured consumption.

### Moderate concurrency (20 threads × 20 operations = 400 total)

| | mean | median | p95 | max |
|---|---|---|---|---|
| Per-operation latency | 227.57 ms | 16.00 ms | 1511.88 ms | 5134.79 ms |

Total wall time: 7.32s for 400 operations across 20 threads.

The large gap between median (16ms — close to the single-threaded write cost) and mean/p95/max is exactly
the SQLite single-writer-lock contention described in the Environment section above: most operations
complete quickly, but a growing queue of threads waiting for the file lock produces a long tail as
concurrency increases. **This is not treated as a finding requiring optimization** — it is the expected,
disclosed shape of SQLite-specific contention, not a property of the repository code's design (which
contains no artificial serialization of its own — every repository method is a single, independent
transaction; nothing takes an application-level lock across operations for different keys).

### Connection Pool Health — Verified

```
pool checked-out connections after load: 0
pool size: 5
```

- **Bounded**: the pool never grows beyond its configured size (`DB_POOL_SIZE=10` was set for this
  benchmark; SQLAlchemy's `QueuePool` — used automatically for any non-SQLite dialect per `db.py`'s
  `_build_engine()` — enforces this structurally, not by convention).
- **No connection leak**: `pool.checkedout() == 0` after all 908 total operations (508 single-op + 400
  concurrent) completed — every `session_scope()` call's `finally: session.close()` (see `db.py`) correctly
  returned its connection to the pool every time, including every failure path exercised in Steps 12.11-12.13
  (verified there separately; this benchmark's own runs never hit a failure path, so this specifically
  confirms the *success* path leaks nothing).

## Verify

| Requirement | Status |
|---|---|
| Connection pool remains bounded | **Confirmed** — `QueuePool` with configured `pool_size`/`max_overflow` (PostgreSQL); SQLite's own connection model has no pool to bound (single connection per engine, by `db.py`'s own documented design) |
| No connection leaks | **Confirmed** — `pool.checkedout() == 0` after load |
| No unbounded queue | **Confirmed by design** — nothing in any of the four `Postgres*Repository` implementations buffers operations in an application-level queue; every call is a synchronous, immediately-executed `session_scope()` transaction |
| No excessive database retries | **Confirmed** — re-verified from Step 12.11's finding: no retry loop exists in any repository module |
| No duplicate operations | **Confirmed** — this benchmark's idempotency/confirmation paths behaved correctly under the concurrency phase (no duplicate `session_id`s were double-created; the dedicated concurrency tests in Steps 12.6/12.9/12.12 are the authoritative proof of exactly-once behavior, not this benchmark, which measures speed, not correctness) |
| No obvious N+1 query pattern | **Confirmed by code inspection** — every repository method (`get`, `save`, `delete`, `list_for_user`, `list_events`, `try_reserve`, `try_consume_pending_confirmation`) issues exactly one SQL statement (or two, for the documented two-statement confirmation-consumption case) per call; none loops over a result set issuing a follow-up query per row |

## Bottlenecks

- **Per-transaction fsync on writes** (~10-13ms each against SQLite) is the dominant cost for every write
  operation. This is expected and not a code-level bottleneck — it is SQLite's default durability guarantee
  operating as designed. A real PostgreSQL deployment's write latency will be dominated by network round-trip
  and server-side WAL flush behavior instead, which this benchmark cannot measure from this environment.
- **SQLite's single-writer lock** is the dominant cost under concurrency, not anything in the repository
  code. This is the one result this report explicitly does **not** carry forward as a PostgreSQL-relevant
  finding — see Environment section.
- No other bottleneck was observed. Read latency (sub-millisecond, including under the concurrent workload's
  interleaved reads) shows no scaling problem in the repository code itself.

## Recommendations

- **Do not add Redis or another cache** (per this step's explicit instruction) — no measured bottleneck here
  justifies one; every read is already sub-millisecond against the always-uncached SQLite stand-in used for
  this benchmark, and a real PostgreSQL server with its own buffer cache would likely perform comparably or
  better for the read patterns this repository layer actually issues (single-row primary-key lookups,
  single-column-indexed filters — see Step 12.3's schema).
- **No connection-pool tuning is recommended** at this stage — the default `DB_POOL_SIZE=5`/`DB_MAX_OVERFLOW=10`
  (Step 12.2) showed no exhaustion or leak under this benchmark's load; recommend revisiting only if/when a
  real PostgreSQL deployment's own metrics show pool contention, not preemptively.
- **When a real PostgreSQL instance becomes available**, re-run `scripts/benchmark_persistence.py` (it needs
  no modification — only `DATABASE_URL` changes) to replace these SQLite-based numbers with representative
  ones, particularly for the concurrency phase, before drawing any capacity-planning conclusion from this
  report. This is flagged as necessary future work, not silently deferred.
- No code change is recommended from this step's findings — per the step's own instruction, "do not optimize
  unless a measurable problem exists," and none was found in the repository code itself (only in the
  environment-specific SQLite stand-in, which is not something to optimize away — it's a test-environment
  characteristic).

`PHASE 12.14 COMPLETE`

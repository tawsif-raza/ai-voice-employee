# aitk — AI engineering toolkit (Phase 1: statistics + results schema · Phase 2: usage + cost)

Dev tooling shared by the repo's benchmark, eval and verification scripts. Stdlib only; no runtime
dependency is added. **Not part of the production image** (`docker/Dockerfile.production` does not copy
`tools/`), so nothing under `src/` may import it.

## Importing

The repo has no `__init__.py` files and puts directories on `sys.path` explicitly. Do the same:

```python
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from aitk.core.stats import percentile  # noqa: E402
```

## `aitk.core.stats`

`percentile(values, pct, *, method="linear", default=None)` — `pct` is 0–100. Empty input raises
`EmptySampleError` unless `default` is given; NaN/inf, non-numeric and `bool` samples raise.
`summarize(values, pcts=(50, 95, 99), method="linear")` returns a `Summary`.

The repo already had four *different* percentile definitions, so the method must be named to keep a
migrated caller's numbers unchanged:

| method         | formula                              | existing callers it reproduces |
|----------------|--------------------------------------|--------------------------------|
| `linear`       | linear interpolation (numpy default) | `src/eval/evaluate.py::_percentile` |
| `floor_index`  | `s[min(int(n*q), n-1)]`              | `src/voice/latency_tracker.py` `PhaseStats.p95`, `src/voice/benchmark.py` `MetricSummary.p95`, `scripts/benchmark_persistence.py` |
| `nearest_rank` | `s[ceil(q*n) - 1]`                   | `scripts/benchmark_decision_router.py` |
| `rounded_rank` | `s[round(q*(n-1))]` (half-to-even)   | `scripts/performance_load_test.py::_percentiles` |

`tests/test_aitk_stats.py` pins each method against a verbatim copy of its legacy formula.

Migrated so far: `scripts/benchmark_decision_router.py` (`nearest_rank`) and
`scripts/benchmark_persistence.py` (`floor_index`). The others are intentionally untouched (see below).

## `aitk.core.schema` / `aitk.core.report`

Versioned (`schema: "aitk.results"`, `schema_version: "1.0"`) JSON reports. Every `Result` has a
**required** `evidence` label — `LOCAL`, `SIMULATED`, `MOCKED`, `INTEGRATION`, `LIVE`, `CANARY` or
`PRODUCTION` (`plan.md` Rule 9) — and a `status` of `PASS`, `FAIL`, `PARTIAL` or `NOT_RUN`.

```python
from aitk.core import report
from aitk.core.schema import Evidence, Report, Result, Status, metrics_from_summary
from aitk.core.stats import summarize

result = Result(
    name="decide_latency",
    status=Status.PASS,
    evidence=Evidence.LOCAL,
    metrics=metrics_from_summary(summarize(samples_ms), unit="ms"),
)
report.write_report(Report.create("benchmark_decision_router", [result]), "outputs/decision_router.json")
```

Output is deterministic strict JSON (sorted keys, no NaN/Infinity) and written atomically. Readers accept
only the exact schema version they know and reject unknown fields.

## `aitk.cost` (Phase 2)

Token-based **cost estimates** from provider-**reported** usage. Full guide, provider coverage table and the
estimate-vs-invoice explanation: [`docs/AI_COST_TRACKING.md`](../../docs/AI_COST_TRACKING.md).

- `pricing.py` — `PricingTable`: strict, versioned (`pricing_version: 1`) JSON prices with required source and
  effective dates, `Decimal` arithmetic, exact model/alias matching, ambiguity rejected at load.
- `pricing.json` — **shipped empty on purpose**; `pricing.example.json` is a synthetic template.
- `tracker.py` — `estimate_cost`, `CostTracker`; statuses `PRICED` / `UNPRICED` / `UNKNOWN_USAGE`. A cost exists only
  with real usage **and** a configured price; totals are withheld whenever any call is not `PRICED`.
- Results go through the Phase 1 schema (`tracker.build_report(tool, Evidence.…)`); the evidence label is required.

## Tests and CI

Toolkit tests (`tests/test_aitk_*.py`) import `tests/aitk_support.py`, which **skips** a module when `tools/aitk` is
absent (the production-image job mounts only `tests/`) but **fails** collection when `AITK_REQUIRE_TOOLS=1` — which
the `test` CI job sets, so they cannot be silently skipped there. `tools/` is also linted (`ruff`) and checked with
`mypy --strict` in the `test` job only. The benchmark smoke tests run the two migrated scripts in a subprocess with a
timeout, a credential-free environment and sockets blocked.

## Not migrated yet, and why

| Caller | Reason |
|--------|--------|
| `src/voice/latency_tracker.py`, `src/voice/benchmark.py`, `src/eval/evaluate.py` | In `src/`, which the production image ships; `tools/` is not in the image. Needs the image/CI change first. |
| `scripts/performance_load_test.py` | Produces the published Phase 21 figures; migrate with a before/after comparison. |

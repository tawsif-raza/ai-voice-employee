# aitk — AI engineering toolkit (Phase 1: statistics + results schema)

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
    name="decide_latency", status=Status.PASS, evidence=Evidence.LOCAL,
    metrics=metrics_from_summary(summarize(samples_ms), unit="ms"),
)
report.write_report(Report.create("benchmark_decision_router", [result]), "outputs/decision_router.json")
```

Output is deterministic strict JSON (sorted keys, no NaN/Infinity) and written atomically. Readers accept
only the exact schema version they know and reject unknown fields.

## Not migrated yet, and why

| Caller | Reason |
|--------|--------|
| `src/voice/latency_tracker.py`, `src/voice/benchmark.py`, `src/eval/evaluate.py` | In `src/`, which the production image ships; `tools/` is not in the image. Needs the image/CI change first. |
| `scripts/performance_load_test.py` | Produces the published Phase 21 figures; migrate with a before/after comparison. |

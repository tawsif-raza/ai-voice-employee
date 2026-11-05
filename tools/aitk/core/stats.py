"""
Shared percentile / summary statistics for AI-engineering tooling (aitk).

Why four percentile "methods" instead of one: this repository already
computes percentiles in several places, and they are NOT the same
definition -- they give different numbers for the same samples:

  linear        Linear interpolation between the two closest ranks (numpy's
                default). What src/eval/evaluate.py::_percentile does.
  floor_index   sorted[min(int(n * q), n - 1)]. What PhaseStats.p95 in
                src/voice/latency_tracker.py and MetricSummary.p95 in
                src/voice/benchmark.py do, and scripts/benchmark_persistence.py.
                Biased high: p95 of 10 samples is the maximum.
  nearest_rank  sorted[ceil(q * n) - 1] (the textbook nearest-rank method).
                What scripts/benchmark_decision_router.py does.
  rounded_rank  sorted[round(q * (n - 1))], Python's round-half-to-even.
                What scripts/performance_load_test.py does.

Callers are migrated one at a time by naming the method they already use,
so published numbers (e.g. the Phase 21 performance report) do not move.
tests/test_aitk_stats.py pins each method against a verbatim copy of the
legacy formula it replaces. New code should prefer "linear" unless it must
stay comparable with an older figure.

Input handling differs from the legacy helpers deliberately: they returned
0.0 for empty input in some places and raised IndexError in others, and
silently sorted NaN into an arbitrary position. Here, empty input raises
EmptySampleError unless the caller passes an explicit ``default``, and
non-numeric or non-finite samples raise instead of corrupting the result.

Dependency-free (stdlib only). Not importable from src/: docker/
Dockerfile.production does not copy tools/ into the production image.
"""

import math
import statistics
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal, Optional

PercentileMethod = Literal["linear", "floor_index", "nearest_rank", "rounded_rank"]
PERCENTILE_METHODS: tuple[str, ...] = ("linear", "floor_index", "nearest_rank", "rounded_rank")

DEFAULT_PERCENTILES: tuple[float, ...] = (50, 95, 99)


class EmptySampleError(ValueError):
    """Raised when a statistic is requested over zero samples and no default was given."""


def _check_number(value: Any, what: str) -> None:
    # bool is an int subclass; True/False as a latency or a percentile is always a bug.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{what} must be an int or float, got {type(value).__name__}: {value!r}")
    if not math.isfinite(value):
        raise ValueError(f"{what} must be finite, got {value!r}")


def _checked_samples(values: Iterable[float]) -> list[float]:
    try:
        items = list(values)
    except TypeError as exc:
        raise TypeError(f"values must be an iterable of numbers, got {type(values).__name__}") from exc
    for item in items:
        _check_number(item, "sample value")
    return items


def _checked_pct(pct: float) -> float:
    _check_number(pct, "pct")
    if not 0 <= pct <= 100:
        raise ValueError(f"pct must be within [0, 100], got {pct!r}")
    return pct


def _check_method(method: str) -> None:
    if method not in PERCENTILE_METHODS:
        raise ValueError(f"unknown percentile method {method!r}; expected one of {list(PERCENTILE_METHODS)}")


def _pick(sorted_values: list[float], pct: float, method: str) -> float:
    n = len(sorted_values)
    if method == "linear":
        # Expression order matches the legacy helper exactly so results are bit-identical.
        k = (n - 1) * (pct / 100)
        f, c = int(k), min(int(k) + 1, n - 1)
        if f == c:
            return sorted_values[f]
        return sorted_values[f] + (sorted_values[c] - sorted_values[f]) * (k - f)
    q = pct / 100
    if method == "floor_index":
        return sorted_values[min(int(n * q), n - 1)]
    if method == "nearest_rank":
        return sorted_values[min(n - 1, max(0, math.ceil(q * n) - 1))]
    # rounded_rank (validated by _checked_method)
    return sorted_values[min(n - 1, max(0, int(round(q * (n - 1)))))]


def percentile(
    values: Iterable[float],
    pct: float,
    *,
    method: PercentileMethod = "linear",
    default: Optional[float] = None,
) -> float:
    """
    The ``pct``-th percentile (0-100, e.g. 95 for p95) of ``values``.

    ``values`` need not be sorted and is not modified. Raises EmptySampleError
    for zero samples unless ``default`` is given, TypeError for non-numeric
    input (including bool), and ValueError for NaN/inf samples, an
    out-of-range ``pct`` or an unknown ``method``. pct 0 is the minimum and
    pct 100 the maximum under every method.
    """
    pct = _checked_pct(pct)
    _check_method(method)
    items = _checked_samples(values)
    if not items:
        if default is None:
            raise EmptySampleError("cannot compute a percentile of zero samples")
        return default
    items.sort()
    return _pick(items, pct, method)


@dataclass(frozen=True)
class Summary:
    """Count / mean / min / max plus the requested percentiles of one sample set."""

    count: int
    mean: float
    minimum: float
    maximum: float
    method: str
    percentiles: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "mean": self.mean,
            "min": self.minimum,
            "max": self.maximum,
            "method": self.method,
            "percentiles": dict(self.percentiles),
        }


def percentile_label(pct: float) -> str:
    """50 -> "p50", 99.9 -> "p99.9"."""
    return f"p{pct:g}"


def summarize(
    values: Iterable[float],
    *,
    pcts: Iterable[float] = DEFAULT_PERCENTILES,
    method: PercentileMethod = "linear",
) -> Summary:
    """Summary statistics over ``values``. Raises EmptySampleError for zero samples."""
    wanted = [_checked_pct(p) for p in pcts]
    _check_method(method)
    items = _checked_samples(values)
    if not items:
        raise EmptySampleError("cannot summarize zero samples")
    items.sort()
    return Summary(
        count=len(items),
        mean=float(statistics.mean(items)),
        minimum=items[0],
        maximum=items[-1],
        method=method,
        percentiles={percentile_label(p): _pick(items, p, method) for p in wanted},
    )

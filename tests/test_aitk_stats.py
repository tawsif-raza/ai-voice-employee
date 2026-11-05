"""
Tests for tools/aitk/core/stats.py -- the shared percentile / summary module.

The legacy-equivalence tests embed verbatim copies of the four percentile
formulas this repository already uses (see stats.py's module docstring) and
require the shared implementation to match them exactly, so migrating a
caller cannot silently move a published number.

See tests/aitk_support.py: the module skips when tools/ is absent (production image) and
FAILS instead when AITK_REQUIRE_TOOLS=1 (the normal CI job).
"""

import json
import math
import random

import pytest
from aitk_support import ensure_toolkit_on_path

ensure_toolkit_on_path()

from aitk.core import stats  # noqa: E402
from aitk.core.stats import EmptySampleError, percentile, summarize  # noqa: E402

METHODS = list(stats.PERCENTILE_METHODS)


# ── Verbatim copies of the legacy implementations (do not "improve" these) ────


def legacy_linear(values, pct):  # src/eval/evaluate.py::_percentile
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    k = (len(sorted_vals) - 1) * (pct / 100)
    f, c = int(k), min(int(k) + 1, len(sorted_vals) - 1)
    if f == c:
        return sorted_vals[f]
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def legacy_floor_index(values, q):  # latency_tracker.py PhaseStats.p95, benchmark_persistence.py
    sorted_s = sorted(values)
    idx = int(len(sorted_s) * q)
    return sorted_s[min(idx, len(sorted_s) - 1)]


def legacy_nearest_rank(sorted_samples, q):  # scripts/benchmark_decision_router.py
    return sorted_samples[max(0, math.ceil(q * len(sorted_samples)) - 1)]


def legacy_rounded_rank(latencies_ms, p):  # scripts/performance_load_test.py::_percentiles
    s = sorted(latencies_ms)
    n = len(s)
    idx = min(n - 1, max(0, int(round(p * (n - 1)))))
    return s[idx]


LEGACY = {
    "linear": lambda v, pct: legacy_linear(v, pct),
    "floor_index": lambda v, pct: legacy_floor_index(v, pct / 100),
    "nearest_rank": lambda v, pct: legacy_nearest_rank(sorted(v), pct / 100),
    "rounded_rank": lambda v, pct: legacy_rounded_rank(v, pct / 100),
}

PCTS = [0, 1, 5, 10, 25, 33, 50, 66, 75, 90, 95, 99, 100]


def _dataset(n, seed):
    rng = random.Random(seed)
    return [round(rng.uniform(0.5, 1500.0), 3) for _ in range(n)]


# ── Legacy equivalence ────────────────────────────────────────────────────────


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("n", [1, 2, 3, 4, 5, 7, 10, 11, 19, 20, 21, 50, 99, 100, 101, 500, 997])
def test_matches_legacy_formula_exactly(method, n):
    values = _dataset(n, seed=1000 + n)
    for pct in PCTS:
        assert percentile(values, pct, method=method) == LEGACY[method](values, pct), (method, n, pct)


@pytest.mark.parametrize("method", METHODS)
def test_matches_legacy_with_repeated_and_integer_samples(method):
    for values in ([5, 5, 5, 5], [1, 2, 2, 2, 3, 3, 9], [0, 0, 1], [3] * 40 + [4] * 60):
        for pct in PCTS:
            assert percentile(values, pct, method=method) == LEGACY[method](values, pct)


@pytest.mark.parametrize(
    "method,legacy,fraction",
    [
        ("floor_index", legacy_floor_index, 0.95),
        ("floor_index", legacy_floor_index, 0.99),
        ("floor_index", legacy_floor_index, 0.5),
        ("rounded_rank", legacy_rounded_rank, 0.95),
        ("rounded_rank", legacy_rounded_rank, 0.99),
        ("rounded_rank", legacy_rounded_rank, 0.50),
    ],
)
def test_matches_legacy_callers_literal_fractions(method, legacy, fraction):
    # The migrated callers pass literals like 0.95; the shared API takes percent. Pin the conversion.
    for n in range(1, 120):
        values = _dataset(n, seed=n)
        assert percentile(values, fraction * 100, method=method) == legacy(values, fraction)


@pytest.mark.parametrize("fraction", [0.5, 0.95, 0.99])
def test_nearest_rank_matches_decision_router_literals(fraction):
    for n in range(1, 120):
        values = sorted(_dataset(n, seed=n))
        assert percentile(values, round(fraction * 100), method="nearest_rank") == legacy_nearest_rank(values, fraction)


def test_documented_methods_disagree_so_callers_must_name_theirs():
    values = [float(x) for x in range(10, 110, 10)]  # 10..100
    assert percentile(values, 95, method="floor_index") == 100.0
    assert percentile(values, 95, method="nearest_rank") == 100.0
    assert percentile(values, 95, method="rounded_rank") == 100.0
    assert percentile(values, 95, method="linear") == pytest.approx(95.5)
    assert percentile(values, 50, method="linear") == 55.0
    assert percentile(values, 50, method="floor_index") == 60.0
    assert percentile(values, 50, method="nearest_rank") == 50.0
    assert percentile(values, 50, method="rounded_rank") == 50.0  # round(4.5) -> 4 (half-to-even)


# ── Empty input ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("method", METHODS)
def test_empty_raises_by_default(method):
    with pytest.raises(EmptySampleError):
        percentile([], 95, method=method)


def test_empty_error_is_a_value_error():
    assert issubclass(EmptySampleError, ValueError)


@pytest.mark.parametrize("method", METHODS)
def test_empty_with_default_returns_default(method):
    assert percentile([], 95, method=method, default=0.0) == 0.0
    assert percentile(iter(()), 50, method=method, default=-1.0) == -1.0


def test_default_is_ignored_when_there_are_samples():
    assert percentile([7.0], 50, default=0.0) == 7.0


def test_summarize_empty_raises_even_with_defaults_available():
    with pytest.raises(EmptySampleError):
        summarize([])


# ── Single and repeated values ────────────────────────────────────────────────


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("pct", PCTS)
def test_single_value_is_every_percentile(method, pct):
    assert percentile([42.5], pct, method=method) == 42.5


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("pct", PCTS)
def test_repeated_values_return_that_value(method, pct):
    assert percentile([3.25] * 17, pct, method=method) == 3.25


def test_two_values_interpolate_linearly_and_pick_a_member_otherwise():
    assert percentile([10.0, 20.0], 50, method="linear") == 15.0
    for method in ("floor_index", "nearest_rank", "rounded_rank"):
        assert percentile([10.0, 20.0], 50, method=method) in (10.0, 20.0)


# ── Boundaries ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("n", [1, 2, 3, 10, 101])
def test_pct_0_is_minimum_and_pct_100_is_maximum(method, n):
    values = _dataset(n, seed=n)
    assert percentile(values, 0, method=method) == min(values)
    assert percentile(values, 100, method=method) == max(values)


@pytest.mark.parametrize("method", METHODS)
def test_result_is_always_a_member_or_between_neighbours(method):
    values = _dataset(25, seed=7)
    lo, hi = min(values), max(values)
    for pct in PCTS + [12.5, 99.9]:
        result = percentile(values, pct, method=method)
        assert lo <= result <= hi
        if method != "linear":
            assert result in values


@pytest.mark.parametrize("method", METHODS)
def test_monotonic_in_pct(method):
    values = _dataset(40, seed=11)
    results = [percentile(values, pct, method=method) for pct in range(0, 101)]
    assert results == sorted(results)


@pytest.mark.parametrize("method", METHODS)
def test_input_order_does_not_matter_and_input_is_not_mutated(method):
    values = [9.0, 1.0, 5.0, 3.0, 7.0]
    snapshot = list(values)
    shuffled = [3.0, 7.0, 1.0, 9.0, 5.0]
    assert percentile(values, 75, method=method) == percentile(shuffled, 75, method=method)
    assert values == snapshot


def test_accepts_generators_tuples_and_ints():
    assert percentile((x for x in [1, 2, 3, 4, 5]), 50) == 3
    assert percentile((1, 2, 3, 4, 5), 100) == 5
    assert percentile([1, 2, 3, 4], 50) == 2.5


def test_is_deterministic():
    values = _dataset(200, seed=99)
    assert [percentile(values, p) for p in PCTS] == [percentile(values, p) for p in PCTS]


# ── Invalid input ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("pct", [-0.001, -1, 100.001, 101, 1000])
def test_pct_out_of_range_rejected(pct):
    with pytest.raises(ValueError, match="within"):
        percentile([1.0, 2.0], pct)


@pytest.mark.parametrize("pct", [float("nan"), float("inf"), float("-inf")])
def test_pct_non_finite_rejected(pct):
    with pytest.raises(ValueError, match="finite"):
        percentile([1.0, 2.0], pct)


@pytest.mark.parametrize("pct", ["95", None, True, [95]])
def test_pct_wrong_type_rejected(pct):
    with pytest.raises(TypeError, match="pct"):
        percentile([1.0, 2.0], pct)


def test_pct_is_validated_before_emptiness():
    with pytest.raises(ValueError, match="within"):
        percentile([], 200, default=0.0)


def test_unknown_method_rejected_and_lists_valid_ones():
    with pytest.raises(ValueError, match="unknown percentile method") as excinfo:
        percentile([1.0], 50, method="median")  # type: ignore[arg-type]
    for name in METHODS:
        assert name in str(excinfo.value)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_samples_rejected(bad):
    with pytest.raises(ValueError, match="finite"):
        percentile([1.0, bad, 3.0], 50)


@pytest.mark.parametrize("bad", ["1.0", None, True, [1.0], {"a": 1}])
def test_non_numeric_samples_rejected(bad):
    with pytest.raises(TypeError, match="sample value"):
        percentile([1.0, bad, 3.0], 50)


def test_non_iterable_values_rejected():
    with pytest.raises(TypeError, match="iterable"):
        percentile(5, 50)  # type: ignore[arg-type]


def test_a_string_is_not_a_sample_list():
    with pytest.raises(TypeError):
        percentile("123", 50)


# ── summarize() ───────────────────────────────────────────────────────────────


def test_summarize_basic_values():
    summary = summarize([10.0, 20.0, 30.0, 40.0, 50.0])
    assert summary.count == 5
    assert summary.mean == 30.0
    assert summary.minimum == 10.0
    assert summary.maximum == 50.0
    assert summary.method == "linear"
    assert summary.percentiles == {"p50": 30.0, "p95": pytest.approx(48.0), "p99": pytest.approx(49.6)}


def test_summarize_single_value():
    summary = summarize([4.0])
    assert (summary.count, summary.mean, summary.minimum, summary.maximum) == (1, 4.0, 4.0, 4.0)
    assert set(summary.percentiles.values()) == {4.0}


def test_summarize_repeated_values():
    summary = summarize([2.0] * 9, pcts=(1, 50, 99))
    assert summary.mean == 2.0
    assert summary.percentiles == {"p1": 2.0, "p50": 2.0, "p99": 2.0}


def test_summarize_percentile_labels_and_method():
    summary = summarize([1.0, 2.0, 3.0, 4.0], pcts=[50, 99.9, 0, 100], method="nearest_rank")
    assert list(summary.percentiles) == ["p50", "p99.9", "p0", "p100"]
    assert summary.method == "nearest_rank"
    assert summary.percentiles["p0"] == 1.0
    assert summary.percentiles["p100"] == 4.0


def test_summarize_uses_the_same_percentile_function():
    values = _dataset(60, seed=5)
    for method in METHODS:
        summary = summarize(values, pcts=PCTS, method=method)
        for pct in PCTS:
            assert summary.percentiles[stats.percentile_label(pct)] == percentile(values, pct, method=method)


def test_summarize_validates_its_inputs():
    with pytest.raises(ValueError, match="within"):
        summarize([1.0], pcts=[50, 120])
    with pytest.raises(ValueError, match="unknown percentile method"):
        summarize([1.0], method="bogus")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="finite"):
        summarize([1.0, float("nan")])
    with pytest.raises(TypeError):
        summarize([1.0, "x"])  # type: ignore[list-item]


def test_summary_to_dict_is_json_serializable_and_detached():
    summary = summarize([1.0, 2.0, 3.0])
    data = summary.to_dict()
    assert json.loads(json.dumps(data, allow_nan=False)) == data
    data["percentiles"]["p50"] = -1.0
    assert summary.percentiles["p50"] == 2.0


def test_summary_is_frozen():
    summary = summarize([1.0])
    with pytest.raises(AttributeError):
        summary.count = 2  # type: ignore[misc]

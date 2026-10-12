"""
Tests for tools/aitk/core/schema.py -- the versioned results schema.

See tests/test_aitk_stats.py for why this module skips when tools/ is absent.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_TOOLS = Path(__file__).resolve().parents[1] / "tools"
if not (_TOOLS / "aitk").is_dir():
    pytest.skip("tools/aitk is not present (not shipped in the production image)", allow_module_level=True)
sys.path.insert(0, str(_TOOLS))

from aitk.core import schema  # noqa: E402
from aitk.core.schema import (  # noqa: E402
    Evidence,
    Metric,
    Report,
    Result,
    SchemaError,
    Status,
    metrics_from_summary,
)
from aitk.core.stats import summarize  # noqa: E402

FIXED_NOW = datetime(2026, 10, 12, 9, 30, 0, tzinfo=timezone.utc)


def _result(**overrides):
    base = {"name": "decide_latency", "status": Status.PASS, "evidence": Evidence.LOCAL}
    base.update(overrides)
    return Result(**base)


def _report_dict():
    return Report.create(
        "benchmark_decision_router",
        [_result(metrics={"p95": Metric(0.044, "ms"), "count": Metric(1000)}, notes="n=1000")],
        now=FIXED_NOW,
    ).to_dict()


# ── Vocabulary ────────────────────────────────────────────────────────────────


def test_evidence_labels_are_exactly_plan_md_rule_9():
    assert [e.value for e in Evidence] == [
        "LOCAL",
        "SIMULATED",
        "MOCKED",
        "INTEGRATION",
        "LIVE",
        "CANARY",
        "PRODUCTION",
    ]


def test_status_vocabulary():
    assert [s.value for s in Status] == ["PASS", "FAIL", "PARTIAL", "NOT_RUN"]


def test_schema_constants():
    assert schema.SCHEMA_ID == "aitk.results"
    assert schema.SCHEMA_VERSION == "1.0"


# ── Evidence is explicit ──────────────────────────────────────────────────────


def test_result_cannot_be_built_without_evidence():
    with pytest.raises(TypeError):
        Result(name="x", status=Status.PASS)  # type: ignore[call-arg]


@pytest.mark.parametrize("bad", ["LOCAL", None, 1, "live"])
def test_result_evidence_must_be_an_evidence_member(bad):
    with pytest.raises(SchemaError, match="evidence"):
        _result(evidence=bad)


def test_every_serialized_result_has_an_evidence_label():
    report = Report.create("t", [_result(evidence=e, name=e.value) for e in Evidence], now=FIXED_NOW)
    labels = [r["evidence"] for r in report.to_dict()["results"]]
    assert labels == [e.value for e in Evidence]


# ── Metric ────────────────────────────────────────────────────────────────────


def test_metric_accepts_int_and_float_with_optional_unit():
    assert Metric(3).to_dict() == {"value": 3, "unit": None}
    assert Metric(0.5, "ms").to_dict() == {"value": 0.5, "unit": "ms"}


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_metric_rejects_non_finite(bad):
    with pytest.raises(SchemaError, match="finite"):
        Metric(bad)


@pytest.mark.parametrize("bad", [True, False, "1.0", None, [1]])
def test_metric_rejects_non_numeric(bad):
    with pytest.raises(SchemaError, match="int or float"):
        Metric(bad)  # type: ignore[arg-type]


def test_metric_rejects_non_string_unit():
    with pytest.raises(SchemaError, match="unit"):
        Metric(1.0, unit=5)  # type: ignore[arg-type]


# ── Result ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", ["", "   ", None, 5])
def test_result_name_must_be_a_non_empty_string(bad):
    with pytest.raises(SchemaError, match="name"):
        _result(name=bad)


def test_result_status_must_be_a_status():
    with pytest.raises(SchemaError, match="status"):
        _result(status="PASS")


def test_result_notes_must_be_a_string():
    with pytest.raises(SchemaError, match="notes"):
        _result(notes=None)


def test_result_metrics_must_be_metric_objects_with_named_keys():
    with pytest.raises(SchemaError, match="must be a Metric"):
        _result(metrics={"p95": 1.0})
    with pytest.raises(SchemaError, match="metric name"):
        _result(metrics={"": Metric(1.0)})


def test_result_detaches_from_callers_metrics_dict_and_is_frozen():
    metrics = {"p50": Metric(1.0, "ms")}
    result = _result(metrics=metrics)
    metrics["p99"] = Metric(9.0, "ms")
    assert set(result.metrics) == {"p50"}
    with pytest.raises(AttributeError):
        result.name = "other"  # type: ignore[misc]


# ── Report envelope ───────────────────────────────────────────────────────────


def test_report_envelope_is_versioned():
    data = _report_dict()
    assert data["schema"] == "aitk.results"
    assert data["schema_version"] == "1.0"
    assert data["tool"] == "benchmark_decision_router"
    assert data["created_at"] == "2026-10-12T09:30:00+00:00"
    assert data["results"][0]["metrics"]["p95"] == {"value": 0.044, "unit": "ms"}


def test_report_create_defaults_to_current_utc_time():
    before = datetime.now(timezone.utc)
    report = Report.create("t")
    after = datetime.now(timezone.utc)
    stamp = datetime.fromisoformat(report.created_at)
    assert before <= stamp <= after
    assert report.results == ()


def test_report_create_rejects_naive_now():
    with pytest.raises(SchemaError, match="timezone-aware"):
        Report.create("t", now=datetime(2026, 10, 12, 9, 30))


def test_report_create_keeps_the_callers_utc_offset():
    stamp = datetime(2026, 10, 12, 9, 30, tzinfo=timezone(timedelta(hours=5)))
    assert Report.create("t", now=stamp).created_at == "2026-10-12T09:30:00+05:00"


@pytest.mark.parametrize("bad", ["", "yesterday", "2026-10-12T09:30:00", None, 12345])
def test_report_created_at_must_be_a_timezone_aware_iso_timestamp(bad):
    with pytest.raises(SchemaError, match="created_at"):
        Report(tool="t", created_at=bad)


@pytest.mark.parametrize("bad", ["", None, 3])
def test_report_tool_must_be_a_non_empty_string(bad):
    with pytest.raises(SchemaError, match="tool"):
        Report(tool=bad, created_at=FIXED_NOW.isoformat())


def test_report_results_must_be_result_objects():
    with pytest.raises(SchemaError, match="Result objects"):
        Report(tool="t", created_at=FIXED_NOW.isoformat(), results=[{"name": "x"}])  # type: ignore[list-item]


# ── Round-trip and validation of untrusted input ──────────────────────────────


def test_dict_round_trip_is_lossless():
    report = Report.create(
        "t",
        [
            _result(metrics={"p95": Metric(1.25, "ms")}),
            _result(name="b", status=Status.NOT_RUN, evidence=Evidence.SIMULATED, notes="skipped"),
        ],
        now=FIXED_NOW,
    )
    assert Report.from_dict(report.to_dict()) == report


def test_from_dict_requires_a_mapping():
    for bad in (None, [], "report", 3):
        with pytest.raises(SchemaError, match="must be an object"):
            Report.from_dict(bad)


@pytest.mark.parametrize("missing", ["schema", "schema_version", "tool", "created_at", "results"])
def test_from_dict_requires_every_report_field(missing):
    data = _report_dict()
    del data[missing]
    with pytest.raises(SchemaError, match="missing required"):
        Report.from_dict(data)


def test_from_dict_rejects_wrong_schema_id_and_unsupported_versions():
    data = _report_dict()
    data["schema"] = "other.thing"
    with pytest.raises(SchemaError, match="not an aitk.results"):
        Report.from_dict(data)
    for version in ("2.0", "1.1", "0.9", 1, None):
        data = _report_dict()
        data["schema_version"] = version
        with pytest.raises(SchemaError, match="unsupported schema_version"):
            Report.from_dict(data)


def test_from_dict_rejects_unknown_fields_at_every_level():
    data = _report_dict()
    data["extra"] = 1
    with pytest.raises(SchemaError, match="unknown field"):
        Report.from_dict(data)
    data = _report_dict()
    data["results"][0]["extra"] = 1
    with pytest.raises(SchemaError, match="unknown field"):
        Report.from_dict(data)
    data = _report_dict()
    data["results"][0]["metrics"]["p95"]["extra"] = 1
    with pytest.raises(SchemaError, match="unknown field"):
        Report.from_dict(data)


def test_from_dict_rejects_a_result_without_evidence():
    data = _report_dict()
    del data["results"][0]["evidence"]
    with pytest.raises(SchemaError, match="evidence"):
        Report.from_dict(data)


@pytest.mark.parametrize("label", ["live", "Live", "REAL", "", None, 1])
def test_from_dict_rejects_unknown_or_miscased_evidence_labels(label):
    data = _report_dict()
    data["results"][0]["evidence"] = label
    with pytest.raises(SchemaError, match="evidence"):
        Report.from_dict(data)


@pytest.mark.parametrize("label", ["pass", "OK", "NOT RUN", None])
def test_from_dict_rejects_unknown_status(label):
    data = _report_dict()
    data["results"][0]["status"] = label
    with pytest.raises(SchemaError, match="status"):
        Report.from_dict(data)


def test_from_dict_rejects_bad_results_and_metrics_containers():
    data = _report_dict()
    data["results"] = {"not": "a list"}
    with pytest.raises(SchemaError, match="must be a list"):
        Report.from_dict(data)
    data = _report_dict()
    data["results"][0]["metrics"] = [1, 2]
    with pytest.raises(SchemaError, match="must be an object"):
        Report.from_dict(data)
    data = _report_dict()
    data["results"][0]["metrics"]["p95"] = 0.044
    with pytest.raises(SchemaError, match="must be an object"):
        Report.from_dict(data)
    data = _report_dict()
    data["results"][0]["metrics"]["p95"]["value"] = True
    with pytest.raises(SchemaError, match="int or float"):
        Report.from_dict(data)


def test_from_dict_defaults_optional_fields():
    data = _report_dict()
    del data["results"][0]["metrics"]
    del data["results"][0]["notes"]
    result = Report.from_dict(data).results[0]
    assert result.metrics == {} and result.notes == ""


def test_schema_error_is_a_value_error():
    assert issubclass(SchemaError, ValueError)


# ── stats.Summary -> metrics bridge ───────────────────────────────────────────


def test_metrics_from_summary_flattens_with_units():
    metrics = metrics_from_summary(summarize([10.0, 20.0, 30.0], pcts=(50, 99)), unit="ms")
    assert list(metrics) == ["count", "mean", "min", "max", "p50", "p99"]
    assert metrics["count"] == Metric(3, None)  # a count has no unit
    assert metrics["mean"] == Metric(20.0, "ms")
    assert metrics["p50"] == Metric(20.0, "ms")
    assert metrics["max"] == Metric(30.0, "ms")


def test_metrics_from_summary_without_unit():
    assert all(m.unit is None for m in metrics_from_summary(summarize([1.0, 2.0])).values())


def test_summary_metrics_fit_in_a_result():
    result = _result(metrics=metrics_from_summary(summarize([1.0, 2.0, 3.0]), unit="ms"))
    assert Result.from_dict(result.to_dict()) == result

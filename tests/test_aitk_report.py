"""
Tests for tools/aitk/core/report.py -- JSON serialization and file helpers.

See tests/aitk_support.py for when this module skips (production image) or fails (AITK_REQUIRE_TOOLS=1).
"""

import json
import os
from datetime import datetime, timezone

import pytest
from aitk_support import ensure_toolkit_on_path

ensure_toolkit_on_path()

from aitk.core import report as report_io  # noqa: E402
from aitk.core.schema import Evidence, Metric, Report, Result, SchemaError, Status  # noqa: E402

FIXED_NOW = datetime(2026, 10, 12, 9, 30, 0, tzinfo=timezone.utc)


def _report(**kwargs):
    results = [
        Result(
            name="decide_latency",
            status=Status.PASS,
            evidence=Evidence.LOCAL,
            metrics={"p95": Metric(0.044, "ms"), "count": Metric(1000)},
            notes="unicode ok: µs → ✓",
        ),
        Result(name="live_call", status=Status.NOT_RUN, evidence=Evidence.LIVE, notes="no credentials"),
    ]
    return Report.create("benchmark_decision_router", results, now=FIXED_NOW, **kwargs)


# ── Serialization ─────────────────────────────────────────────────────────────


def test_dumps_is_valid_strict_json_with_the_expected_envelope():
    text = report_io.dumps(_report())
    data = json.loads(text)  # plain stdlib parse: any JSON reader can read it
    assert data["schema"] == "aitk.results" and data["schema_version"] == "1.0"
    assert [r["evidence"] for r in data["results"]] == ["LOCAL", "LIVE"]
    assert "NaN" not in text and "Infinity" not in text


def test_dumps_is_deterministic_sorted_and_newline_terminated():
    a, b = report_io.dumps(_report()), report_io.dumps(_report())
    assert a == b
    assert a.endswith("}\n") and not a.endswith("\n\n")
    top_level_keys = list(json.loads(a))
    assert top_level_keys == sorted(top_level_keys)


def test_dumps_keeps_non_ascii_text_readable():
    assert "µs → ✓" in report_io.dumps(_report())


def test_dumps_indent_is_configurable():
    compact = report_io.dumps(_report(), indent=0)
    assert len(compact.splitlines()) > 1  # indent=0 still breaks lines; just no indentation
    assert not any(line.startswith(" ") for line in compact.splitlines())


def test_loads_round_trips_dumps():
    original = _report()
    assert report_io.loads(report_io.dumps(original)) == original


# ── Error handling on read ────────────────────────────────────────────────────


@pytest.mark.parametrize("text", ["", "   ", "{", "not json", '{"schema": ', "[1, 2"])
def test_loads_wraps_malformed_json_in_schema_error(text):
    with pytest.raises(SchemaError, match="not valid JSON"):
        report_io.loads(text)


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
def test_loads_rejects_non_finite_json_numbers(token):
    data = report_io.dumps(_report()).replace('"value": 0.044', f'"value": {token}')
    assert token in data
    with pytest.raises(SchemaError, match="NaN and Infinity"):
        report_io.loads(data)


def test_loads_validates_against_the_schema():
    bad = json.loads(report_io.dumps(_report()))
    bad["results"][0]["evidence"] = "REAL"
    with pytest.raises(SchemaError, match="evidence"):
        report_io.loads(json.dumps(bad))
    with pytest.raises(SchemaError, match="must be an object"):
        report_io.loads("[]")


def test_loads_rejects_non_string_input():
    with pytest.raises(TypeError, match="expects str"):
        report_io.loads(b"{}")  # type: ignore[arg-type]


# ── Files ─────────────────────────────────────────────────────────────────────


def test_write_then_read_round_trip(tmp_path):
    target = tmp_path / "out" / "nested" / "report.json"  # parents are created
    returned = report_io.write_report(_report(), target)
    assert returned == target and target.is_file()
    assert report_io.read_report(target) == _report()
    assert json.loads(target.read_text(encoding="utf-8"))["tool"] == "benchmark_decision_router"


def test_write_accepts_str_paths(tmp_path):
    report_io.write_report(_report(), str(tmp_path / "r.json"))
    assert report_io.read_report(str(tmp_path / "r.json")) == _report()


def test_write_overwrites_an_existing_report_and_leaves_no_temp_files(tmp_path):
    target = tmp_path / "report.json"
    report_io.write_report(Report.create("first", now=FIXED_NOW), target)
    report_io.write_report(_report(), target)
    assert report_io.read_report(target).tool == "benchmark_decision_router"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["report.json"]


def test_failed_write_keeps_the_previous_report_and_cleans_up(tmp_path, monkeypatch):
    target = tmp_path / "report.json"
    report_io.write_report(Report.create("good", now=FIXED_NOW), target)
    before = target.read_bytes()

    def boom(src, dst):
        raise OSError("disk exploded")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="disk exploded"):
        report_io.write_report(_report(), target)
    assert target.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["report.json"]


def test_write_to_a_directory_path_raises_os_error_and_leaves_no_temp_file(tmp_path):
    target = tmp_path / "is_a_dir"
    target.mkdir()
    with pytest.raises(OSError):
        report_io.write_report(_report(), target)
    assert [p.name for p in tmp_path.iterdir()] == ["is_a_dir"]
    assert list(target.iterdir()) == []


def test_write_does_not_touch_the_filesystem_when_serialization_fails(tmp_path):
    class NotAReport:
        def to_dict(self):
            return {"bad": float("nan")}  # json.dumps(allow_nan=False) must refuse this

    with pytest.raises(ValueError):
        report_io.write_report(NotAReport(), tmp_path / "x" / "r.json")  # type: ignore[arg-type]
    assert list(tmp_path.iterdir()) == []


def test_read_missing_file_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        report_io.read_report(tmp_path / "missing.json")


def test_read_invalid_content_raises_schema_error(tmp_path):
    target = tmp_path / "bad.json"
    target.write_text("{not json", encoding="utf-8")
    with pytest.raises(SchemaError):
        report_io.read_report(target)

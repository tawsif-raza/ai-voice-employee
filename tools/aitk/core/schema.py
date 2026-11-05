"""
Versioned, machine-readable results schema for AI-engineering tooling (aitk).

One Report holds the Results of one tool run. Every Result carries an
explicit ``evidence`` label -- it is a required field with no default, so a
result cannot be recorded without saying what kind of evidence it is. The
labels are exactly plan.md Rule 9's vocabulary ("Never mix these
categories"), and ``status`` uses the PASS / FAIL / PARTIAL / NOT RUN
vocabulary the Phase 16/21/25 verification scripts already report.

Wire format (``schema_version`` "1.0")::

    {
      "schema": "aitk.results",
      "schema_version": "1.0",
      "tool": "benchmark_decision_router",
      "created_at": "2026-10-12T09:30:00+00:00",
      "results": [
        {"name": "decide_latency", "status": "PASS", "evidence": "LOCAL",
         "metrics": {"p95": {"value": 0.044, "unit": "ms"}}, "notes": ""}
      ]
    }

Readers accept exactly the version they know and reject everything else
with SchemaError; a future change to the format bumps ``schema_version`` and
adds an explicit migration rather than being guessed at.

Stdlib only. Imports ``aitk.core.stats`` absolutely, so ``tools/`` must be on
sys.path (the repo's flat-module convention; see tools/aitk/README.md).
"""

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Iterable, Mapping, Optional

from aitk.core.stats import Summary

SCHEMA_ID = "aitk.results"
SCHEMA_VERSION = "1.0"


class SchemaError(ValueError):
    """A report or one of its parts violates the aitk.results schema."""


class Evidence(StrEnum):
    """What kind of evidence a result is (plan.md Rule 9). Never mix categories."""

    LOCAL = "LOCAL"
    SIMULATED = "SIMULATED"
    MOCKED = "MOCKED"
    INTEGRATION = "INTEGRATION"
    LIVE = "LIVE"
    CANARY = "CANARY"
    PRODUCTION = "PRODUCTION"


class Status(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    PARTIAL = "PARTIAL"
    NOT_RUN = "NOT_RUN"


def _require_str(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SchemaError(f"{what} must be a non-empty string, got {value!r}")
    return value


def _require_mapping(value: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SchemaError(f"{what} must be an object, got {type(value).__name__}")
    return value


def _reject_unknown(data: Mapping[str, Any], allowed: Iterable[str], what: str) -> None:
    unknown = sorted(set(data) - set(allowed))
    if unknown:
        raise SchemaError(f"{what} has unknown field(s): {unknown}")


def _require_fields(data: Mapping[str, Any], required: Iterable[str], what: str) -> None:
    missing = [name for name in required if name not in data]
    if missing:
        raise SchemaError(f"{what} is missing required field(s): {missing}")


def _parse_enum(enum_cls: type[StrEnum], value: Any, what: str) -> Any:
    # Exact, case-sensitive match: "live" or "Live" is a typo to surface, not to normalize.
    try:
        return enum_cls(value)
    except ValueError:
        raise SchemaError(f"{what} must be one of {[member.value for member in enum_cls]}, got {value!r}") from None


@dataclass(frozen=True)
class Metric:
    value: float
    unit: Optional[str] = None

    def __post_init__(self) -> None:
        # bool is an int subclass; a metric of True/False is always a bug.
        if isinstance(self.value, bool) or not isinstance(self.value, (int, float)):
            raise SchemaError(f"metric value must be an int or float, got {self.value!r}")
        if not math.isfinite(self.value):
            raise SchemaError(f"metric value must be finite (JSON has no NaN/inf), got {self.value!r}")
        if self.unit is not None and not isinstance(self.unit, str):
            raise SchemaError(f"metric unit must be a string or None, got {self.unit!r}")

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "unit": self.unit}

    @classmethod
    def from_dict(cls, data: Any) -> "Metric":
        obj = _require_mapping(data, "metric")
        _reject_unknown(obj, ("value", "unit"), "metric")
        _require_fields(obj, ("value",), "metric")
        return cls(value=obj["value"], unit=obj.get("unit"))


@dataclass(frozen=True)
class Result:
    """One named measurement or check. ``evidence`` is required: there is no default."""

    name: str
    status: Status
    evidence: Evidence
    metrics: dict[str, Metric] = field(default_factory=dict)
    notes: str = ""

    def __post_init__(self) -> None:
        _require_str(self.name, "result name")
        if not isinstance(self.status, Status):
            raise SchemaError(f"result status must be a Status, got {self.status!r}")
        if not isinstance(self.evidence, Evidence):
            raise SchemaError(f"result evidence must be an Evidence label, got {self.evidence!r}")
        if not isinstance(self.notes, str):
            raise SchemaError(f"result notes must be a string, got {self.notes!r}")
        for key, metric in self.metrics.items():
            _require_str(key, "metric name")
            if not isinstance(metric, Metric):
                raise SchemaError(f"metric {key!r} must be a Metric, got {metric!r}")
        object.__setattr__(self, "metrics", dict(self.metrics))  # detach from the caller's dict

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "evidence": self.evidence.value,
            "metrics": {key: metric.to_dict() for key, metric in self.metrics.items()},
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "Result":
        obj = _require_mapping(data, "result")
        _reject_unknown(obj, ("name", "status", "evidence", "metrics", "notes"), "result")
        _require_fields(obj, ("name", "status", "evidence"), "result")
        metrics = _require_mapping(obj.get("metrics", {}), "result metrics")
        return cls(
            name=obj["name"],
            status=_parse_enum(Status, obj["status"], "result status"),
            evidence=_parse_enum(Evidence, obj["evidence"], "result evidence"),
            metrics={key: Metric.from_dict(value) for key, value in metrics.items()},
            notes=obj.get("notes", ""),
        )


def _parse_timestamp(value: Any, what: str) -> datetime:
    text = _require_str(value, what)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise SchemaError(f"{what} must be an ISO 8601 timestamp, got {text!r}") from None
    if parsed.tzinfo is None:
        raise SchemaError(f"{what} must include a UTC offset (e.g. +00:00), got {text!r}")
    return parsed


@dataclass(frozen=True)
class Report:
    """The Results of one tool run, in a fixed, versioned envelope."""

    tool: str
    created_at: str
    results: tuple[Result, ...] = ()

    def __post_init__(self) -> None:
        _require_str(self.tool, "report tool")
        _parse_timestamp(self.created_at, "report created_at")
        items = tuple(self.results)
        for item in items:
            if not isinstance(item, Result):
                raise SchemaError(f"report results must be Result objects, got {item!r}")
        object.__setattr__(self, "results", items)

    @classmethod
    def create(cls, tool: str, results: Iterable[Result] = (), *, now: Optional[datetime] = None) -> "Report":
        """Build a Report stamped with ``now`` (default: the current UTC time; inject it in tests)."""
        stamp = now if now is not None else datetime.now(timezone.utc)
        if stamp.tzinfo is None:
            raise SchemaError("now must be timezone-aware")
        return cls(tool=tool, created_at=stamp.isoformat(), results=tuple(results))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA_ID,
            "schema_version": SCHEMA_VERSION,
            "tool": self.tool,
            "created_at": self.created_at,
            "results": [result.to_dict() for result in self.results],
        }

    @classmethod
    def from_dict(cls, data: Any) -> "Report":
        obj = _require_mapping(data, "report")
        _reject_unknown(obj, ("schema", "schema_version", "tool", "created_at", "results"), "report")
        _require_fields(obj, ("schema", "schema_version", "tool", "created_at", "results"), "report")
        if obj["schema"] != SCHEMA_ID:
            raise SchemaError(f"not an {SCHEMA_ID} report: schema is {obj['schema']!r}")
        if obj["schema_version"] != SCHEMA_VERSION:
            raise SchemaError(
                f"unsupported schema_version {obj['schema_version']!r}; this reader supports {SCHEMA_VERSION!r}"
            )
        raw_results = obj["results"]
        if not isinstance(raw_results, list):
            raise SchemaError(f"report results must be a list, got {type(raw_results).__name__}")
        return cls(
            tool=obj["tool"],
            created_at=obj["created_at"],
            results=tuple(Result.from_dict(item) for item in raw_results),
        )


def metrics_from_summary(summary: Summary, unit: Optional[str] = None) -> dict[str, Metric]:
    """Flatten a stats.Summary into Result metrics: count, mean, min, max, then one per percentile."""
    metrics = {
        "count": Metric(summary.count),
        "mean": Metric(summary.mean, unit),
        "min": Metric(summary.minimum, unit),
        "max": Metric(summary.maximum, unit),
    }
    for label, value in summary.percentiles.items():
        metrics[label] = Metric(value, unit)
    return metrics

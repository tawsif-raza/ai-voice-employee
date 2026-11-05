"""
JSON (de)serialization and file helpers for aitk.results reports.

Output is deterministic (sorted keys, fixed indent, trailing newline) and
strict JSON: ``allow_nan=False`` on the way out and NaN/Infinity tokens
rejected on the way in, so a report that parses here parses in any JSON
reader. Writes are atomic -- a crash mid-write never leaves a truncated
report where a previous good one used to be.
"""

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Union

from aitk.core.schema import Report, SchemaError


def dumps(report: Report, *, indent: int = 2) -> str:
    """Serialize ``report`` to a deterministic, strict-JSON string ending in a newline."""
    return json.dumps(report.to_dict(), indent=indent, sort_keys=True, allow_nan=False, ensure_ascii=False) + "\n"


def _reject_constant(token: str) -> Any:
    raise SchemaError(f"invalid JSON number {token!r}: NaN and Infinity are not allowed")


def loads(text: str) -> Report:
    """Parse and validate a serialized report. Raises SchemaError on malformed JSON or schema violations."""
    if not isinstance(text, str):
        raise TypeError(f"loads() expects str, got {type(text).__name__}")
    try:
        data = json.loads(text, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise SchemaError(f"report is not valid JSON: {exc}") from exc
    return Report.from_dict(data)


def write_report(report: Report, path: Union[str, "os.PathLike[str]"]) -> Path:
    """
    Atomically write ``report`` to ``path`` (parent directories are created).

    The text is written to a temporary file in the destination directory and
    renamed over ``path``; the temporary file is removed if anything fails.
    OSError from the filesystem propagates unchanged.
    """
    destination = Path(path)
    text = dumps(report)  # serialize first: a schema problem must not touch the filesystem
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp_name, destination)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return destination


def read_report(path: Union[str, "os.PathLike[str]"]) -> Report:
    """Read and validate a report file. FileNotFoundError / OSError propagate; bad content raises SchemaError."""
    return loads(Path(path).read_text(encoding="utf-8"))

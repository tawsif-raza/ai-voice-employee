"""
Bounded smoke tests for the two benchmark scripts migrated to aitk.core.stats in Phase 1:
scripts/benchmark_decision_router.py and scripts/benchmark_persistence.py.

Each script runs in a SUBPROCESS (the way a developer runs it) with:
  - a hard timeout, so a hang fails the test instead of the CI job;
  - an allow-listed environment -- no inherited API keys or tokens, HOME and TMPDIR inside
    the test's tmp dir, APP_ENV/PERSISTENCE_MODE/AUTH_MODE=dev;
  - a sitecustomize guard that makes ANY socket connection raise, so a script that tried to
    reach a provider (paid or free) would fail the test rather than spend money;
  - a reduced workload (--runs 1, BENCH_PERSISTENCE_ITERATIONS=20). Timings are not asserted,
    only that the run completes and the output is well-formed and internally consistent.

The scripts import aitk, which is not shipped in the production image, so this module is
gated like every toolkit test (tests/aitk_support.py): it skips there and FAILS in the
normal CI job (AITK_REQUIRE_TOOLS=1).
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from aitk_support import ensure_toolkit_on_path

ensure_toolkit_on_path()

REPO = Path(__file__).resolve().parents[1]
TIMEOUT_SECONDS = 120

_NO_NETWORK = """\
import socket


def _refuse(*args, **kwargs):
    raise RuntimeError("smoke test: network access is forbidden")


socket.socket.connect = _refuse
socket.socket.connect_ex = _refuse
socket.create_connection = _refuse
"""


def _run(script, args, tmp_path, extra_env=None):
    guard = tmp_path / "guard"
    guard.mkdir()
    (guard / "sitecustomize.py").write_text(_NO_NETWORK, encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "TMPDIR": str(home),
        "PYTHONPATH": str(guard),
        "PYTHONDONTWRITEBYTECODE": "1",
        "APP_ENV": "dev",
        "PERSISTENCE_MODE": "dev",
        "AUTH_MODE": "dev",
    }
    env.update(extra_env or {})
    try:
        return subprocess.run(
            [sys.executable, str(REPO / "scripts" / script), *args],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"{script} did not finish within {TIMEOUT_SECONDS}s", pytrace=False)


def _assert_clean_exit(result):
    assert result.returncode == 0, f"exit {result.returncode}\n{result.stdout[-1500:]}\n{result.stderr[-1500:]}"
    assert "Traceback" not in result.stderr and "Traceback" not in result.stdout
    assert "network access is forbidden" not in result.stdout + result.stderr


def test_benchmark_decision_router_smoke(tmp_path):
    out = tmp_path / "router.json"
    result = _run(
        "benchmark_decision_router.py", ["--runs", "1", "--llm-latency-ms", "1", "--json", str(out)], tmp_path
    )
    _assert_clean_exit(result)
    assert "decide() alone over" in result.stdout and "Shortcut rate" in result.stdout

    data = json.loads(out.read_text(encoding="utf-8"))
    decide = data["decide"]
    assert decide["samples"] > 0
    assert 0 <= decide["p50_ms"] <= decide["p95_ms"] <= decide["p99_ms"] <= decide["max_ms"]
    assert isinstance(data["rows"], list) and data["rows"]
    assert not any(isinstance(v, float) and v != v for v in decide.values())  # no NaN


_ROW = re.compile(
    r"^(?P<name>.+?)\s+n=\s*(?P<n>\d+)\s+mean=\s*(?P<mean>[\d.]+)ms\s+median=\s*(?P<median>[\d.]+)ms"
    r"\s+p95=\s*(?P<p95>[\d.]+)ms\s+max=\s*(?P<max>[\d.]+)ms\s*$",
    re.MULTILINE,
)


def test_benchmark_persistence_smoke(tmp_path):
    result = _run("benchmark_persistence.py", [], tmp_path, {"BENCH_PERSISTENCE_ITERATIONS": "20"})
    _assert_clean_exit(result)
    rows = {m["name"]: m for m in _ROW.finditer(result.stdout)}
    assert {"session read", "session write", "memory read", "memory write", "audit write"} <= set(rows)
    for name in ("session read", "session write", "memory read", "memory write", "audit write"):
        row = rows[name]
        assert int(row["n"]) == 20  # the override bounded the workload
        assert 0 < float(row["median"]) <= float(row["p95"]) <= float(row["max"])
        assert float(row["mean"]) > 0
    assert "N=20" in result.stdout

"""
Tests for tests/aitk_support.py -- the gate that makes a missing tools/ a skip in the
production image but a FAILURE in the normal CI checkout (AITK_REQUIRE_TOOLS=1).

This module itself never depends on tools/, so it runs (and must pass) in every
environment, including the production-image job.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from aitk_support import REQUIRE_ENV, ensure_toolkit_on_path

TESTS_DIR = Path(__file__).resolve().parent


@pytest.fixture
def restore_sys_path(monkeypatch):
    monkeypatch.setattr(sys, "path", list(sys.path))


def _fake_tools(tmp_path):
    (tmp_path / "tools" / "aitk").mkdir(parents=True)
    return tmp_path / "tools"


def test_present_toolkit_is_put_on_sys_path(tmp_path, restore_sys_path):
    tools = _fake_tools(tmp_path)
    assert ensure_toolkit_on_path(tools, environ={}) == tools
    assert str(tools) in sys.path
    ensure_toolkit_on_path(tools, environ={})  # idempotent: not inserted twice
    assert sys.path.count(str(tools)) == 1


@pytest.mark.parametrize(
    "env", [{}, {REQUIRE_ENV: ""}, {REQUIRE_ENV: "0"}, {REQUIRE_ENV: "true"}, {REQUIRE_ENV: "yes"}]
)
def test_missing_toolkit_skips_unless_exactly_one(tmp_path, env):
    with pytest.raises(pytest.skip.Exception, match="not shipped in the production image"):
        ensure_toolkit_on_path(tmp_path / "tools", environ=env)


def test_missing_toolkit_fails_when_required(tmp_path):
    with pytest.raises(pytest.fail.Exception, match="must not be skipped"):
        ensure_toolkit_on_path(tmp_path / "tools", environ={REQUIRE_ENV: "1"})


def test_a_tools_dir_without_aitk_counts_as_missing(tmp_path):
    (tmp_path / "tools").mkdir()
    with pytest.raises(pytest.fail.Exception):
        ensure_toolkit_on_path(tmp_path / "tools", environ={REQUIRE_ENV: "1"})


# ── Real pytest semantics, in a subprocess ────────────────────────────────────

_PROBE = """
import sys
from pathlib import Path
from aitk_support import ensure_toolkit_on_path

ensure_toolkit_on_path(Path(sys.argv[0]).resolve().parent / "no_such_tools")

def test_probe():
    assert True
"""


def _run_probe(tmp_path, require):
    (tmp_path / "test_probe.py").write_text(_PROBE, encoding="utf-8")
    # A passing sibling module, as in the real suite (a run whose ONLY module is skipped exits 5, not 0).
    (tmp_path / "test_sibling.py").write_text("def test_sibling():\n    assert True\n", encoding="utf-8")
    # Extend, never replace, the parent's PYTHONPATH: the production-image CI job installs pytest into a
    # separate --target directory that is reachable ONLY through PYTHONPATH.
    inherited = os.environ.get("PYTHONPATH", "")
    pythonpath = os.pathsep.join(part for part in (str(TESTS_DIR), inherited) if part)
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": pythonpath, "PYTHONDONTWRITEBYTECODE": "1"}
    if require:
        env[REQUIRE_ENV] = "1"
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(tmp_path),
            "-p",
            "no:cacheprovider",
            "-q",
            "-rs",
            "--rootdir",
            str(tmp_path),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_pytest_skips_the_module_when_the_toolkit_is_missing_and_not_required(tmp_path):
    result = _run_probe(tmp_path, require=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed, 1 skipped" in result.stdout and "not shipped in the production image" in result.stdout


def test_pytest_fails_the_run_when_the_toolkit_is_missing_and_required(tmp_path):
    result = _run_probe(tmp_path, require=True)
    assert result.returncode != 0, result.stdout + result.stderr
    assert not re.search(r"\d+ skipped", result.stdout), result.stdout  # failed, did not skip
    assert "error during collection" in result.stdout and "must not be skipped" in result.stdout
    assert "passed" not in result.stdout  # collection error stops the run: nothing slips through green

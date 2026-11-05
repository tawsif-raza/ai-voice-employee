"""
Shared gate for the toolkit tests (tests/test_aitk_*.py).

tools/ is development tooling and is deliberately NOT copied into the production
image (docker/Dockerfile.production), where the CI job mounts only tests/. Toolkit
tests therefore have to skip there. But in the normal CI checkout a skip would be a
silent loss of coverage, so the same condition must be a hard failure.

    ensure_toolkit_on_path()   at the top of every toolkit test module

  - tools/aitk present                       -> puts tools/ on sys.path and carries on.
  - tools/aitk absent, AITK_REQUIRE_TOOLS=1  -> FAILS collection (the normal `test` CI job sets this).
  - tools/aitk absent, otherwise             -> skips the module (the production-image job).

Only the exact value "1" requires the toolkit; anything else, or unset, allows the skip.
"""

import os
import sys
from pathlib import Path
from typing import Mapping, Optional

import pytest

REQUIRE_ENV = "AITK_REQUIRE_TOOLS"
_DEFAULT_TOOLS = Path(__file__).resolve().parents[1] / "tools"


def ensure_toolkit_on_path(tools_dir: Optional[Path] = None, environ: Optional[Mapping[str, str]] = None) -> Path:
    tools = Path(tools_dir) if tools_dir is not None else _DEFAULT_TOOLS
    env = os.environ if environ is None else environ
    if (tools / "aitk").is_dir():
        if str(tools) not in sys.path:
            sys.path.insert(0, str(tools))
        return tools
    if env.get(REQUIRE_ENV) == "1":
        pytest.fail(
            f"{REQUIRE_ENV}=1 but {tools / 'aitk'} is missing: toolkit tests must not be skipped in this environment",
            pytrace=False,
        )
    pytest.skip("tools/aitk is not present (not shipped in the production image)", allow_module_level=True)

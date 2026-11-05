"""
Asserts that the Python distributions installed in the running interpreter
are exactly the ones pinned in a lock file -- no missing package, no
version drift, no extra package (docs/MASTER_PROJECT_PLAN.md H4).

Run inside the production image by CI:

    docker run --rm ai-voice-agent:ci python scripts/check_image_dependencies.py requirements-production.lock

The base image's own packaging tools (pip, setuptools, wheel) are the only
distributions allowed beyond the lock.
"""

import re
import sys
from importlib import metadata

ALLOWED_EXTRAS = {"pip", "setuptools", "wheel"}
_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)")


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def read_lock(path: str) -> dict[str, str]:
    pins: dict[str, str] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = _PIN.match(line.strip())
            if m:
                pins[canonical(m.group(1))] = m.group(2)
    if not pins:
        raise SystemExit(f"No pins found in {path}")
    return pins


def installed() -> dict[str, str]:
    return {canonical(d.metadata["Name"]): d.version for d in metadata.distributions()}


def main(lock_path: str) -> int:
    expected, actual = read_lock(lock_path), installed()
    problems = []
    for name, version in sorted(expected.items()):
        if name not in actual:
            problems.append(f"missing   {name}=={version}")
        elif actual[name] != version:
            problems.append(f"drift     {name}: lock {version}, installed {actual[name]}")
    for name in sorted(set(actual) - set(expected) - ALLOWED_EXTRAS):
        problems.append(f"unlocked  {name}=={actual[name]}")
    if problems:
        print(f"Installed packages do not match {lock_path}:")
        print("\n".join(f"  {p}" for p in problems))
        return 1
    print(f"OK: {len(expected)} installed packages match {lock_path} exactly.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: check_image_dependencies.py <lock-file>")
    sys.exit(main(sys.argv[1]))

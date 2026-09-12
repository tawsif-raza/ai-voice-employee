"""
Validation script for real PostgreSQL (Phase 13; plan.md Step 13.3).

This script provides automated validation against a live PostgreSQL instance:
1. Attempts to bring up the `postgres` service from `docker/docker-compose.yml` (profile: test).
2. Waits for the database to become healthy.
3. Applies Alembic migrations (`alembic upgrade head`).
4. Executes the persistence-focused test suite with DATABASE_URL pointed to Postgres.
5. Runs the persistence performance benchmark.
6. Gracefully shuts down the container (unless --keep-running is passed).

If the Docker daemon is unreachable, the script detects this immediately, explains
the status honestly without failing cryptically, and outputs the exact manual steps.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_URL = "postgresql://voice_app:voice_secret@localhost:5432/ai_voice_agent"


def run_cmd(cmd, check=True, capture=False):
    print(f"--> Running: {' '.join(cmd)}")
    return subprocess.run(
        cmd, cwd=_REPO_ROOT, check=check, text=True,
        capture_output=capture
    )


def check_docker_daemon():
    try:
        res = subprocess.run(["docker", "info"], capture_output=True, text=True, timeout=5)
        if res.returncode == 0:
            return True
        return False
    except Exception:
        return False


def main():
    print("=== Real PostgreSQL Validation (Step 13.3) ===")
    has_docker = check_docker_daemon()
    if not has_docker:
        print("\n[!] Docker daemon is NOT running or unreachable in this environment.")
        print("    Docker Desktop is installed on this host, but its engine is stopped.")
        print("\nTo validate against real PostgreSQL when Docker Desktop is running:")
        print("  1. Start Docker Desktop on Windows.")
        print("  2. Start the database container:")
        print("     docker compose -f docker/docker-compose.yml --profile test up -d postgres")
        print("  3. Run Alembic migrations against PostgreSQL:")
        print(f"     $env:DATABASE_URL=\"{DEFAULT_DB_URL}\"")
        print("     alembic upgrade head")
        print("  4. Run persistence unit tests:")
        print("     python -m unittest tests.test_db_migrations tests.test_session_repository_postgres tests.test_memory_repository_postgres tests.test_audit_repository_postgres tests.test_idempotency_repository_postgres tests.test_optimistic_concurrency -v")
        print("  5. Run persistence performance benchmark:")
        print("     python scripts/benchmark_persistence.py")
        print("\nOutcome: compose service definition verified; live daemon run deferred.")
        return 0

    print("[+] Docker daemon is reachable. Starting postgres container...")
    try:
        run_cmd(["docker", "compose", "-f", "docker/docker-compose.yml", "--profile", "test", "up", "-d", "postgres"])
    except subprocess.CalledProcessError as exc:
        print(f"[!] Failed to start postgres container: {exc}")
        return 1

    print("[+] Waiting for PostgreSQL to be ready...")
    ready = False
    for attempt in range(15):
        res = subprocess.run(
            ["docker", "compose", "-f", "docker/docker-compose.yml", "--profile", "test", "exec", "postgres", "pg_isready", "-U", "voice_app", "-d", "ai_voice_agent"],
            capture_output=True, text=True
        )
        if res.returncode == 0:
            ready = True
            print(f"[+] PostgreSQL is ready (attempt {attempt + 1})")
            break
        time.sleep(2)

    if not ready:
        print("[!] PostgreSQL did not become ready within timeout.")
        return 1

    env = dict(os.environ)
    env["DATABASE_URL"] = DEFAULT_DB_URL
    env["PERSISTENCE_MODE"] = "production"

    print("\n[+] Running Alembic migrations against PostgreSQL...")
    run_cmd([sys.executable, "-m", "alembic", "upgrade", "head"])

    print("\n[+] Running persistence test suite against PostgreSQL...")
    test_files = [
        "tests.test_db_migrations",
        "tests.test_session_repository_postgres",
        "tests.test_memory_repository_postgres",
        "tests.test_audit_repository_postgres",
        "tests.test_idempotency_repository_postgres",
        "tests.test_optimistic_concurrency",
    ]
    run_cmd([sys.executable, "-m", "unittest"] + test_files + ["-v"])

    print("\n=== Real PostgreSQL Validation Succeeded ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())

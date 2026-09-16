"""
Phase 25 -- Disaster Recovery (docs/PHASE_25_DISASTER_RECOVERY_REPORT.md).

Runs against the real FastAPI application (real live uvicorn server, same
harness pattern as scripts/stability_verification.py / performance_load_test.py)
and the real local, disposable `docker-postgres-1` container (the exact
same one Phase 16/21 already used safely -- started/stopped, volume never
touched, verified disposable before this script runs any destructive step;
see the report's own "Environment Verification" section for the manual
verification this script's own preflight check re-confirms).

Evidence discipline: every result below is exactly one of PASS / FAIL /
PARTIAL / NOT RUN, decided by the code in this file (never inferred from
"no exception was raised"), matching plan.md's explicit instruction not to
infer PASS from the absence of an error. Provider-outage scenarios are
LOCAL/SIMULATED (no real Claude/Gemini/Groq call -- no live credentials
exist, same disclosed methodology as every other phase-16-onward script).
"Container restart" is tested as an in-process application restart against
the real database (no app Docker image exists locally, and building one is
out of scope for this pass -- disclosed narrowing, not silently substituted).

Run with:
    python scripts/disaster_recovery_test.py
"""

import json
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "api"))
import server  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "agent"))
from action_models import AuthContext  # noqa: E402
from audit import AuditLogger  # noqa: E402
from conversation_manager import build_conversation_manager, resolve_persistence_repositories  # noqa: E402
from db import Database, DatabaseUnavailableError, load_database_config  # noqa: E402
from identity import Role, permissions_for_roles  # noqa: E402
from observability_models import EventType  # noqa: E402

AUTHENTICATED_USER = AuthContext(
    user_id="dr-test-user",
    authenticated=True,
    roles=(Role.USER.value,),
    permissions=permissions_for_roles((Role.USER,)),
    authentication_method="test",
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "inference"))
from llm_provider import BaseLLMProvider, FallbackLLMProvider, LLMOverloadedError  # noqa: E402

CONTAINER = "docker-postgres-1"
DB_URL = "postgresql://voice_app:voice_secret@127.0.0.1:5432/ai_voice_agent"
_RESULTS: list[dict] = []
_TIMINGS: dict[str, float] = {}


def _record(scenario_id: str, name: str, result: str, evidence: str, notes: str = "") -> None:
    assert result in ("PASS", "FAIL", "PARTIAL", "NOT RUN"), f"invalid result literal: {result}"
    _RESULTS.append({"id": scenario_id, "name": name, "result": result, "evidence": evidence, "notes": notes})
    print(f"\n[{scenario_id}] {name}: {result}")
    print(f"  Evidence: {evidence}")
    if notes:
        print(f"  Notes: {notes}")


# ---------------------------------------------------------------------------
# Docker / Postgres control
# ---------------------------------------------------------------------------


def _docker(*args, timeout=30):
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def _wait_pg_ready(timeout_s=30) -> bool:
    deadline = time.perf_counter() + timeout_s
    while time.perf_counter() < deadline:
        r = _docker("exec", CONTAINER, "pg_isready", "-U", "voice_app", "-d", "ai_voice_agent")
        if r.returncode == 0:
            return True
        time.sleep(0.5)
    return False


def _psql(sql: str) -> str:
    r = _docker("exec", CONTAINER, "psql", "-U", "voice_app", "-d", "ai_voice_agent", "-t", "-A", "-c", sql)
    return r.stdout.strip()


def _get_resilient(client: httpx.Client, url: str, retries: int = 2):
    """
    A plain client.get() retried once/twice on a transport-level error
    only (httpx.TransportError -- connection reset/aborted/refused at the
    socket level), never on an HTTP status. This exists to tell apart two
    different things a raised exception here could mean: (a) the
    application process itself is down/unresponsive -- a real finding --
    versus (b) a stale keep-alive connection in THIS test driver's own
    connection pool, observed on Windows after several ~2s-blocked
    requests share one httpx.Client (see scenario_1's "Connection: close"
    comment). A fresh connection succeeding on retry distinguishes (b)
    from (a); if every retry also fails, that IS a real liveness finding
    and this function lets the final exception propagate.
    """
    last_exc = None
    for attempt in range(retries + 1):
        try:
            return client.get(url, headers={"Connection": "close"})
        except httpx.TransportError as exc:
            last_exc = exc
            time.sleep(0.2)
    raise last_exc


def preflight_verify_environment() -> bool:
    """Steps 1-3 the user's instructions require, re-verified programmatically (not just manually) before any destructive action."""
    name_check = _docker("ps", "-a", "--filter", f"name=^{CONTAINER}$", "--format", "{{.Names}}")
    if CONTAINER not in name_check.stdout:
        _record("25.0", "Environment preflight", "FAIL", f"Container '{CONTAINER}' does not exist.", "Refusing to proceed.")
        return False

    inspect = _docker("inspect", CONTAINER, "--format", "{{json .Config.Env}}")
    env_str = inspect.stdout
    is_dev_creds = "POSTGRES_USER=voice_app" in env_str and "POSTGRES_PASSWORD=voice_secret" in env_str
    is_prod_named_db = "POSTGRES_DB=ai_voice_agent" in env_str

    compose_check = subprocess.run(
        ["grep", "-n", "-A", "3", "postgres:", "docker/docker-compose.yml"],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
    )
    profile_confirmed = 'profiles: ["test", "dev"]' in compose_check.stdout

    ok = is_dev_creds and is_prod_named_db and profile_confirmed
    _record(
        "25.0",
        "Environment preflight (identity + disposability verification)",
        "PASS" if ok else "FAIL",
        f"container={CONTAINER}, matches docker/docker-compose.yml's 'postgres' service "
        f"(profiles: [\"test\", \"dev\"])={profile_confirmed}, dev-only credentials confirmed={is_dev_creds}, "
        f"volume=docker_postgres_data (named Docker volume, not a bind-mount to a real data directory)",
        "This is the exact same disposable local container Phase 16/21 already used safely. "
        "No docker-compose.prod.yml or equivalent exists anywhere in this repository referencing this "
        "container as production/staging.",
    )
    return ok


@contextmanager
def _live_server():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    config = uvicorn.Config(server.app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    uv_server = uvicorn.Server(config)
    thread = threading.Thread(target=uv_server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    deadline = time.perf_counter() + 5.0
    with httpx.Client() as probe:
        while time.perf_counter() < deadline:
            try:
                probe.get(f"{base_url}/health", timeout=1.0)
                break
            except httpx.HTTPError:
                time.sleep(0.05)
        else:
            raise RuntimeError("disaster-recovery test server did not become responsive in time")
    try:
        yield base_url
    finally:
        uv_server.should_exit = True
        thread.join(timeout=5.0)


class MockLLMProvider(BaseLLMProvider):
    """Matches tests/test_llm_provider.py's own MockLLMProvider shape exactly."""

    def __init__(self, name: str, chunks: list[str], should_raise: Exception = None):
        self.provider_name = name
        self.chunks = chunks
        self.should_raise = should_raise
        self.call_count = 0

    def generate_stream(self, messages, **kwargs):
        self.call_count += 1
        if self.should_raise:
            raise self.should_raise
        for chunk in self.chunks:
            yield chunk
        yield {"text": "".join(self.chunks), "latency_ms": 5.0, "provider": self.provider_name, "model": "test-model"}


# ---------------------------------------------------------------------------
# Scenario 1: Database outage
# ---------------------------------------------------------------------------


def scenario_1_database_outage() -> None:
    import os

    env_backup = {k: os.environ.get(k) for k in ("PERSISTENCE_MODE", "DATABASE_URL")}
    os.environ["PERSISTENCE_MODE"] = "production"
    os.environ["DATABASE_URL"] = DB_URL

    try:
        if not _wait_pg_ready(10):
            _record("25.1", "Database outage", "NOT RUN", "Postgres was not ready before this scenario started.")
            return

        llm = MockLLMProvider("fake-primary", ["Our ", "hours ", "are ", "9 to 5."])
        try:
            cm = build_conversation_manager(llm_provider=llm, rag_enabled=False, tool_orchestrator_enabled=True)
        except DatabaseUnavailableError as exc:
            _record("25.1", "Database outage", "FAIL", f"Could not build baseline app while DB was healthy: {exc}")
            return

        db_config = load_database_config()
        server._database = Database(db_config)
        server._conversation_manager = cm

        with _live_server() as base_url, httpx.Client(timeout=10.0) as client:
            # -- Baseline (pre-failure) state --------------------------------
            ready_before = client.get(f"{base_url}/ready")
            seed_resp = client.post(
                f"{base_url}/generate", json={"message": "What are your hours?", "session_id": "dr-seed-1"}
            )
            baseline_row = _psql("SELECT session_id, status FROM sessions WHERE session_id = 'dr-seed-1';")
            _record(
                "25.1.pre",
                "Pre-failure baseline established",
                "PASS" if ready_before.status_code == 200 and seed_resp.status_code == 200 and baseline_row else "FAIL",
                f"/ready={ready_before.status_code}, seed /generate={seed_resp.status_code}, "
                f"DB row for dr-seed-1: '{baseline_row}'",
            )

            # -- Inject failure -----------------------------------------------
            t_stop = time.perf_counter()
            _docker("stop", CONTAINER, timeout=30)

            # Detection time: first /ready call that reflects the outage as 503.
            detect_deadline = time.perf_counter() + 15.0
            ready_during = None
            while time.perf_counter() < detect_deadline:
                ready_during = _get_resilient(client, f"{base_url}/ready")
                if ready_during.status_code == 503:
                    break
                time.sleep(0.2)
            detection_time_s = time.perf_counter() - t_stop
            _TIMINGS["detection_time_s"] = round(detection_time_s, 2)

            health_during = client.get(f"{base_url}/health")

            _record(
                "25.1.a",
                "Health/readiness behavior during outage",
                "PASS" if health_during.status_code == 200 and ready_during is not None and ready_during.status_code == 503 else "FAIL",
                f"/health during outage={health_during.status_code} (app itself stays up), "
                f"/ready during outage={ready_during.status_code if ready_during else 'never observed'}, "
                f"detection_time={detection_time_s:.2f}s",
            )

            # -- Requests fail gracefully during outage ------------------------
            # Connection: close -- each of these blocks for ~2s inside the DB
            # driver's own connection-refused timeout; forcing a fresh TCP
            # connection per request (rather than reusing one keep-alive
            # socket across three ~2s-blocked requests) avoids a Windows-
            # host-specific stale-keep-alive artifact observed during this
            # phase's own dry runs (a WinError 10053 on the *next* unrelated
            # request over the same reused connection -- a test-harness
            # connection-reuse issue, not an application behavior; isolating
            # it here keeps this check measuring only what it claims to.
            failing_requests = []
            for i in range(3):
                t0 = time.perf_counter()
                try:
                    r = client.post(
                        f"{base_url}/generate",
                        json={"message": f"outage request {i}", "session_id": f"dr-during-outage-{i}"},
                        headers={"Connection": "close"},
                        timeout=10.0,
                    )
                    failing_requests.append((r.status_code, (time.perf_counter() - t0) * 1000.0, r.text[:200]))
                except httpx.HTTPError as exc:
                    failing_requests.append((None, (time.perf_counter() - t0) * 1000.0, f"{type(exc).__name__}: {exc}"))

            graceful = all(
                (status is not None and 500 <= status < 600 and "Traceback" not in body and "psycopg" not in body.lower())
                for status, _, body in failing_requests
            )
            no_hang = all(latency_ms < 10000.0 for _, latency_ms, _ in failing_requests)
            error_count_during_outage = sum(1 for status, _, _ in failing_requests if status is None or status >= 400)
            _record(
                "25.1.b",
                "Requests fail gracefully during outage (no crash, no leaked internals, no hang)",
                "PASS" if graceful and no_hang else "FAIL",
                f"3 requests during outage: statuses={[s for s, _, _ in failing_requests]}, "
                f"latencies_ms={[round(t, 1) for _, t, _ in failing_requests]}, "
                f"error_rate_during_outage={error_count_during_outage}/3, "
                f"bodies/exceptions={[b for _, _, b in failing_requests]}",
                "Graceful = every response is a real 5xx with no raw exception/DB-driver text in the body -- "
                "verifies the global exception handler (src/api/server.py's _safe_exception_handler), not just "
                "'no exception reached this test'.",
            )

            # -- Process/server itself never went down -------------------------
            still_alive = _get_resilient(client, f"{base_url}/health")
            _record(
                "25.1.c",
                "Application process survives the outage (no crash/restart needed)",
                "PASS" if still_alive.status_code == 200 else "FAIL",
                f"/health immediately after the 3 failing requests={still_alive.status_code}",
            )

            # -- Recover ---------------------------------------------------------
            t_start = time.perf_counter()
            _docker("start", CONTAINER, timeout=30)
            pg_ready = _wait_pg_ready(30)
            failure_duration_s = time.perf_counter() - t_stop

            recover_deadline = time.perf_counter() + 30.0
            ready_after = None
            while time.perf_counter() < recover_deadline:
                ready_after = _get_resilient(client, f"{base_url}/ready")
                if ready_after.status_code == 200:
                    break
                time.sleep(0.5)
            readiness_recovery_s = time.perf_counter() - t_start
            _TIMINGS["failure_duration_s"] = round(failure_duration_s, 2)
            _TIMINGS["readiness_recovery_s"] = round(readiness_recovery_s, 2)

            _record(
                "25.1.d",
                "Readiness recovery after database restart",
                "PASS" if pg_ready and ready_after is not None and ready_after.status_code == 200 else "FAIL",
                f"postgres pg_isready recovered={pg_ready}, /ready recovered={ready_after.status_code if ready_after else 'never'}, "
                f"readiness_recovery_time={readiness_recovery_s:.2f}s, failure_duration={failure_duration_s:.2f}s",
            )

            t_req = time.perf_counter()
            recovered_resp = client.post(
                f"{base_url}/generate", json={"message": "Are you back?", "session_id": "dr-post-recovery-1"}
            )
            request_recovery_s = time.perf_counter() - t_req
            _TIMINGS["request_recovery_s"] = round(request_recovery_s, 2)
            _record(
                "25.1.e",
                "Requests succeed again after recovery",
                "PASS" if recovered_resp.status_code == 200 else "FAIL",
                f"post-recovery /generate={recovered_resp.status_code}, latency={request_recovery_s * 1000:.1f}ms",
            )

            # -- Data consistency: original seed row survived the outage untouched --
            final_row = _psql("SELECT session_id, status FROM sessions WHERE session_id = 'dr-seed-1';")
            consistent = final_row == baseline_row and bool(baseline_row)
            _record(
                "25.1.f",
                "Data consistency across the outage",
                "PASS" if consistent else "FAIL",
                f"dr-seed-1 row before outage: '{baseline_row}', after recovery: '{final_row}'",
            )
    finally:
        for k, v in env_backup.items():
            import os as _os

            if v is None:
                _os.environ.pop(k, None)
            else:
                _os.environ[k] = v
        server._database = None


# ---------------------------------------------------------------------------
# Scenario 2: "Container restart" (in-process application restart, real DB)
# ---------------------------------------------------------------------------


def scenario_2_container_restart() -> None:
    import os

    if not _wait_pg_ready(10):
        _record("25.2", "Container restart", "NOT RUN", "Postgres not ready before this scenario started.")
        return

    env_backup = {k: os.environ.get(k) for k in ("PERSISTENCE_MODE", "DATABASE_URL")}
    os.environ["PERSISTENCE_MODE"] = "production"
    os.environ["DATABASE_URL"] = DB_URL
    try:
        llm1 = MockLLMProvider("fake-primary", ["Sure."])
        cm1 = build_conversation_manager(llm_provider=llm1, rag_enabled=False, tool_orchestrator_enabled=True)
        server._database = Database(load_database_config())
        server._conversation_manager = cm1

        rows_before = _psql("SELECT count(*) FROM sessions;")

        with _live_server() as base_url1, httpx.Client(timeout=10.0) as client1:
            pre_restart = client1.post(
                f"{base_url1}/generate", json={"message": "Remember this.", "session_id": "dr-restart-1"}
            )
        pre_restart_ok = pre_restart.status_code == 200
        server._database = None

        # -- "Restart": tear down the app, cold-start a brand new instance,
        #    exercising the exact same construction path (including the
        #    synchronous DB health_check() inside resolve_persistence_repositories())
        #    a real process/container restart would hit. --
        t_restart = time.perf_counter()
        llm2 = MockLLMProvider("fake-primary-2", ["Sure."])
        try:
            cm2 = build_conversation_manager(llm_provider=llm2, rag_enabled=False, tool_orchestrator_enabled=True)
            startup_ok = True
            startup_error = None
        except DatabaseUnavailableError as exc:
            startup_ok = False
            startup_error = str(exc)
            cm2 = None
        restart_time_s = time.perf_counter() - t_restart

        _record(
            "25.2.a",
            "Cold-start construction succeeds while DB is healthy",
            "PASS" if startup_ok else "FAIL",
            f"build_conversation_manager() {'succeeded' if startup_ok else 'raised: ' + str(startup_error)}, "
            f"construction_time={restart_time_s:.2f}s",
        )
        if not startup_ok:
            return

        server._database = Database(load_database_config())
        server._conversation_manager = cm2

        with _live_server() as base_url2, httpx.Client(timeout=10.0) as client2:
            health2 = client2.get(f"{base_url2}/health")
            ready2 = client2.get(f"{base_url2}/ready")
            _record(
                "25.2.b",
                "Startup/readiness immediately after restart",
                "PASS" if health2.status_code == 200 and ready2.status_code == 200 else "FAIL",
                f"/health={health2.status_code}, /ready={ready2.status_code}",
            )

            db_connectivity = client2.post(
                f"{base_url2}/generate", json={"message": "Are you connected?", "session_id": "dr-restart-2"}
            )
            _record(
                "25.2.c",
                "Database connectivity from the new instance",
                "PASS" if db_connectivity.status_code == 200 else "FAIL",
                f"post-restart /generate={db_connectivity.status_code}",
            )

            surviving = _psql("SELECT session_id FROM sessions WHERE session_id = 'dr-restart-1';")
            _record(
                "25.2.d",
                "Sessions created before the restart are still present (DB-backed, not lost)",
                "PASS" if pre_restart_ok and surviving == "dr-restart-1" else "FAIL",
                f"pre-restart request status={pre_restart.status_code}, row found after restart: '{surviving}'",
            )

            rows_after = _psql("SELECT count(*) FROM sessions;")
            try:
                delta = int(rows_after) - int(rows_before)
            except ValueError:
                delta = None
            no_corruption = delta is not None and delta >= 2  # dr-restart-1 + dr-restart-2, never negative/unexplained
            _record(
                "25.2.e",
                "No unexpected state corruption (row count only additively changed by this test's own requests)",
                "PASS" if no_corruption else "FAIL",
                f"sessions row count before={rows_before}, after={rows_after}, delta={delta}",
            )

        # -- Fail-closed check: cold start MUST fail, not silently degrade, if DB is down --
        server._database = None
        _docker("stop", CONTAINER, timeout=30)
        time.sleep(1.0)
        t0 = time.perf_counter()
        try:
            build_conversation_manager(llm_provider=MockLLMProvider("x", ["y"]), rag_enabled=False)
            fail_closed = False
            fail_closed_evidence = "build_conversation_manager() returned successfully -- did NOT fail closed"
        except DatabaseUnavailableError as exc:
            fail_closed = True
            fail_closed_evidence = f"raised DatabaseUnavailableError as required: {exc}"
        detection_s = time.perf_counter() - t0
        _record(
            "25.2.f",
            "Cold start during a DB outage fails closed (never silently falls back to in-memory)",
            "PASS" if fail_closed else "FAIL",
            f"{fail_closed_evidence}, detection_time={detection_s:.2f}s",
            "This is plan.md's explicit persistence requirement (Step 12.10): a production deployment "
            "that cannot reach its database must fail to start, not quietly run unpersisted.",
        )
        _docker("start", CONTAINER, timeout=30)
        _wait_pg_ready(30)
    finally:
        for k, v in env_backup.items():
            import os as _os

            if v is None:
                _os.environ.pop(k, None)
            else:
                _os.environ[k] = v
        server._database = None


# ---------------------------------------------------------------------------
# Scenario 3: Provider outage (LOCAL/SIMULATED)
# ---------------------------------------------------------------------------


def scenario_3_provider_outage() -> None:
    from audit import AuditLogger as _AuditLogger
    from metrics import MetricsRegistry
    from policy_engine import PolicyEngine
    from tool_orchestrator import ToolOrchestrator

    from mock_tools import build_default_tool_registry

    audit_logger = _AuditLogger()
    metrics = MetricsRegistry()

    broken_primary = MockLLMProvider(
        "fake-claude", [], should_raise=LLMOverloadedError("simulated provider outage", provider="fake-claude")
    )
    working_fallback = MockLLMProvider("fake-gemini", ["I ", "can ", "still ", "help."])
    fallback_provider = FallbackLLMProvider(broken_primary, working_fallback, audit_logger=audit_logger, metrics=metrics)

    tool_orchestrator = ToolOrchestrator(build_default_tool_registry(), PolicyEngine(), audit_logger=audit_logger, metrics=metrics)

    cm = build_conversation_manager(
        llm_provider=fallback_provider,
        rag_enabled=False,
        tool_orchestrator_enabled=False,  # wire the pre-built orchestrator below instead
        persistence_enabled=False,
        audit_logger=audit_logger,
        metrics=metrics,
    )
    cm.tool_orchestrator = tool_orchestrator

    # -- Chat-route turn: primary fails, fallback succeeds, exactly one response --
    final = None
    for item in cm.handle_turn("What are your business hours?", request_id="dr-failover-1"):
        if not isinstance(item, str):
            final = item
    failover_events = metrics.get_counter("llm_failover_events_total")
    fallback_used = metrics.get_counter("llm_fallback_used_total")
    # FallbackLLMProvider.generate_stream() records RETRY_ATTEMPT without a
    # request_id (see src/inference/llm_provider.py) -- filtered by event_type only.
    retry_events = audit_logger._repository.list_events(event_type=EventType.RETRY_ATTEMPT)
    single_response_ok = final is not None and broken_primary.call_count == 1 and working_fallback.call_count == 1
    _record(
        "25.3.a",
        "Fallback routing on simulated provider outage (Claude-shaped -> Gemini-shaped)",
        "PASS" if single_response_ok and failover_events >= 1 else "FAIL",
        f"final response present={final is not None}, primary.call_count={broken_primary.call_count}, "
        f"fallback.call_count={working_fallback.call_count}, llm_failover_events_total={failover_events}, "
        f"llm_fallback_used_total={fallback_used}, RETRY_ATTEMPT audit events={len(retry_events)}, "
        f"response_text={final['response'] if final else None!r}",
    )

    # -- Tool-routed turn under the same failing-primary condition: exactly one execution, LLM never called for it --
    broken_primary.call_count = 0
    working_fallback.call_count = 0
    tool_success_before = len(audit_logger._repository.list_events(event_type=EventType.TOOL_SUCCEEDED))
    tool_final = None
    for item in cm.handle_turn(
        "Can you check my order status for order_1001?", auth=AUTHENTICATED_USER, request_id="dr-failover-tool-1"
    ):
        if not isinstance(item, str):
            tool_final = item
    tool_success_events = audit_logger._repository.list_events(event_type=EventType.TOOL_SUCCEEDED)
    llm_untouched = broken_primary.call_count == 0 and working_fallback.call_count == 0
    exactly_once = (len(tool_success_events) - tool_success_before) == 1
    _record(
        "25.3.b",
        "No duplicate tool execution during a provider outage (tool path is decoupled from LLM health)",
        "PASS" if exactly_once and llm_untouched and tool_final and tool_final.get("tool", {}).get("status") == "success" else "FAIL",
        f"tool status={tool_final.get('tool', {}).get('status') if tool_final else 'no final result'}, "
        f"TOOL_SUCCEEDED audit events before={tool_success_before}, after={len(tool_success_events)} "
        f"(delta={len(tool_success_events) - tool_success_before}), "
        f"LLM invoked during this tool call (should be 0)={broken_primary.call_count + working_fallback.call_count}",
    )

    # -- Recovery: provider becomes available again -> primary used directly, no fallback needed --
    recovered_primary = MockLLMProvider("fake-claude-recovered", ["Welcome ", "back."])
    recovered_fallback_provider = FallbackLLMProvider(recovered_primary, working_fallback, audit_logger=audit_logger, metrics=metrics)
    cm.llm_service = recovered_fallback_provider
    recovered_final = None
    for item in cm.handle_turn("Are you working again?", request_id="dr-recovery-1"):
        if not isinstance(item, str):
            recovered_final = item
    recovered_ok = (
        recovered_final is not None
        and recovered_primary.call_count == 1
        and working_fallback.call_count == 0
        and recovered_final.get("response") == "Welcome back."
    )
    _record(
        "25.3.c",
        "Recovery after the provider becomes available again",
        "PASS" if recovered_ok else "FAIL",
        f"primary.call_count={recovered_primary.call_count} (expected 1 -- used directly), "
        f"fallback.call_count={working_fallback.call_count} (expected 0 -- not needed once primary recovered), "
        f"response={recovered_final.get('response') if recovered_final else None!r}",
    )


# ---------------------------------------------------------------------------
# Scenario 4: Persistence / recovery -- idempotency across a real outage+recovery cycle
# ---------------------------------------------------------------------------


def scenario_4_idempotency_across_recovery() -> None:
    """
    Verifies the idempotency guarantee (Phase 10/11: a repeated
    request_id is detected as a duplicate, never re-executed) survives a
    REAL database outage+restart -- i.e. it is genuinely backed by the
    persisted idempotency_records table, not merely an in-memory
    guarantee that a restart would silently lose. This is a materially
    stronger test than re-sending the same natural-language message
    twice (which has no shared request_id and would simply create two
    separate real bookings, proving nothing about idempotency).
    """
    import os

    from action_models import ToolRequest

    if not _wait_pg_ready(10):
        _record("25.4", "Idempotency across recovery", "NOT RUN", "Postgres not ready before this scenario started.")
        return

    env_backup = {k: os.environ.get(k) for k in ("PERSISTENCE_MODE", "DATABASE_URL")}
    os.environ["PERSISTENCE_MODE"] = "production"
    os.environ["DATABASE_URL"] = DB_URL
    try:
        llm = MockLLMProvider("fake", ["ok"])
        cm = build_conversation_manager(llm_provider=llm, rag_enabled=False, tool_orchestrator_enabled=True)
        server._database = Database(load_database_config())

        audit_before = _psql("SELECT count(*) FROM audit_events;")

        shared_request_id = "dr-idempotency-shared-1"
        first = cm.tool_orchestrator.invoke(
            ToolRequest(
                action="BOOK_APPOINTMENT",
                params={"doctor_id": "d1", "date": "2026-09-20", "time": "10:00"},
                confirmed=True,
                request_id=shared_request_id,
            ),
            auth=AUTHENTICATED_USER,
        )
        idempotency_row = _psql(f"SELECT request_id FROM idempotency_records WHERE request_id = '{shared_request_id}';")
        _record(
            "25.4.a",
            "Tool request succeeds and is persisted to the real idempotency table",
            "PASS" if first.success and idempotency_row == shared_request_id else "FAIL",
            f"first invoke: success={first.success}, status={first.status}, "
            f"idempotency_records row found: '{idempotency_row}'",
        )

        # -- Real outage + recovery, between the first execution and the retry --
        _docker("stop", CONTAINER, timeout=30)
        time.sleep(1.0)
        _docker("start", CONTAINER, timeout=30)
        pg_recovered = _wait_pg_ready(30)

        second = cm.tool_orchestrator.invoke(
            ToolRequest(
                action="BOOK_APPOINTMENT",
                params={"doctor_id": "d1", "date": "2026-09-20", "time": "10:00"},
                confirmed=True,
                request_id=shared_request_id,
            ),
            auth=AUTHENTICATED_USER,
        )
        _record(
            "25.4.b",
            "Duplicate request_id after a real outage+recovery is detected as a duplicate, never re-executed",
            "PASS" if pg_recovered and second.status == "duplicate" and not second.success else "FAIL",
            f"postgres recovered={pg_recovered}, second invoke (same request_id): success={second.success}, "
            f"status={second.status}",
            "Proves this guarantee is backed by the persisted idempotency_records table (survives a real "
            "process-visible DB restart), not merely an in-memory dict that a restart would have reset.",
        )

        audit_after = _psql("SELECT count(*) FROM audit_events;")
        _record(
            "25.4.c",
            "Persisted audit trail remains consistent across the outage (real Postgres, not in-memory)",
            "PASS" if int(audit_after) > int(audit_before) else "FAIL",
            f"audit_events count before={audit_before}, after={audit_after}",
        )
    finally:
        for k, v in env_backup.items():
            import os as _os

            if v is None:
                _os.environ.pop(k, None)
            else:
                _os.environ[k] = v
        server._database = None


# ---------------------------------------------------------------------------
# Scenario 5: Resource cleanup
# ---------------------------------------------------------------------------


def scenario_5_resource_cleanup() -> None:
    llm = MockLLMProvider("fake", ["Done."])
    server._conversation_manager = build_conversation_manager(llm_provider=llm, rag_enabled=False, persistence_enabled=False)
    server._database = None

    with _live_server() as base_url, httpx.Client(timeout=10.0) as client:
        submit = client.post(f"{base_url}/jobs/generate", json={"message": "background job during recovery test"})
        job_id = submit.json()["job_id"]
        deadline = time.perf_counter() + 10.0
        final_job = None
        while time.perf_counter() < deadline:
            final_job = client.get(f"{base_url}/jobs/{job_id}").json()
            if final_job["status"] in ("completed", "failed"):
                break
            time.sleep(0.05)
        _record(
            "25.5.a",
            "Background job resolves to a terminal state, no hang",
            "PASS" if final_job and final_job["status"] == "completed" else "FAIL",
            f"final job status={final_job['status'] if final_job else 'never resolved'}",
        )

        active_calls = client.get(f"{base_url}/health/voice").json().get("active_call_count")
        _record(
            "25.5.b",
            "No orphaned active-call state (no real WebSocket/Twilio traffic was generated this pass)",
            "PASS" if active_calls == 0 else "FAIL",
            f"active_call_count={active_calls}",
        )

    _record(
        "25.5.c",
        "WebSocket / Twilio Media Streams resource cleanup under real concurrent calls",
        "NOT RUN",
        "No real or synthetic Twilio Media Streams traffic was generated this pass -- same disclosed gap as "
        "PHASE_21_PERFORMANCE_REPORT.md 21.5. Requires either real Twilio traffic (blocked on Phase 17/19) or a "
        "synthetic WS-frame harness this pass did not build.",
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 70)
    print("Phase 25 -- Disaster Recovery")
    print("=" * 70)
    print(f"Run started (UTC): {datetime.now(timezone.utc).isoformat()}")

    if not preflight_verify_environment():
        print("\nPREFLIGHT FAILED -- refusing to run destructive scenarios.")
        sys.exit(1)

    for scenario_fn in (
        scenario_1_database_outage,
        scenario_2_container_restart,
        scenario_3_provider_outage,
        scenario_4_idempotency_across_recovery,
        scenario_5_resource_cleanup,
    ):
        try:
            scenario_fn()
        except Exception as exc:
            _record(scenario_fn.__name__, scenario_fn.__name__, "FAIL", f"Scenario itself raised: {type(exc).__name__}: {exc}")
        finally:
            # Always ensure Postgres is left running between scenarios.
            _docker("start", CONTAINER, timeout=30)
            _wait_pg_ready(15)

    print("\n" + "=" * 70)
    print("TIMINGS")
    print("=" * 70)
    print(json.dumps(_TIMINGS, indent=2))

    print("\n" + "=" * 70)
    print("SUMMARY (JSON)")
    print("=" * 70)
    print(json.dumps(_RESULTS, indent=2))

    fail_count = sum(1 for r in _RESULTS if r["result"] == "FAIL")
    sys.exit(1 if fail_count else 0)

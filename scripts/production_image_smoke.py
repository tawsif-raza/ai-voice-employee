"""
Production-image smoke test (docs/MASTER_PROJECT_PLAN.md H4).

Runs INSIDE the built production image (CI job `production-image`):

    docker run --rm --network host -e PERSISTENCE_MODE=production -e DATABASE_URL=... \
        ai-voice-agent:ci python scripts/production_image_smoke.py

1. Checks the configuration actually baked into the image (RAG disabled by
   the Dockerfile, clinical guard still built).
2. Starts the image's real entrypoint (`scripts/run_pipeline.sh --serve`)
   with APP_ENV=production and real OIDC settings pointing at a JWKS
   endpoint this script serves, then exercises the running server over
   HTTP/WebSocket: auth (real RS256 tokens), job ownership, Twilio
   signature enforcement, rate limiting, secret hygiene in logs, and --
   with PERSISTENCE_MODE=production -- readiness against PostgreSQL and
   audit events landing in the database.
3. Stops the server with SIGTERM and expects a clean exit.

No external network access is needed: LLM keys are sentinels (no turn in
this script reaches an LLM), and the JWKS endpoint is local.
Exit code 0 only if every check passes.
"""

import argparse
import asyncio
import base64
import http.server
import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import jwt
import requests
from cryptography.hazmat.primitives.asymmetric import rsa

ROOT = Path(__file__).resolve().parents[1]
for sub in ("src/agent", "src/inference", "src/api"):
    sys.path.insert(0, str(ROOT / sub))

SENTINEL_GEMINI = "AIzaSMOKE_SENTINEL_KEY_do_not_log"
SENTINEL_GROQ = "gsk_SMOKE_SENTINEL_KEY_do_not_log"
TWILIO_TOKEN = "smoke-twilio-auth-token-not-real"
CLINICAL = {"message": "What dose should I take of ibuprofen with my warfarin?"}

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok or not detail else f"  -- {detail}"), flush=True)
    if not ok:
        failures.append(name)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ── 1. configuration baked into the image ───────────────────────────────────
def check_image_configuration() -> None:
    import yaml

    rag = (yaml.safe_load((ROOT / "configs" / "config.yaml").read_text(encoding="utf-8")) or {}).get("rag", {})
    check("image config: rag.enabled is false", rag.get("enabled") is False, repr(rag.get("enabled")))

    from conversation_manager import build_conversation_manager
    from llm_provider import BaseLLMProvider

    class _NoLLM(BaseLLMProvider):
        provider_name = "none"

        def generate_stream(self, messages, **kwargs):
            raise AssertionError("not called")

    manager = build_conversation_manager(llm_provider=_NoLLM(), persistence_enabled=False)
    check("image config: retriever disabled", manager.retriever is None)
    check("image config: clinical guard built", manager.clinical_guard is not None)
    match = manager.clinical_guard.score(CLINICAL["message"])
    check("image config: guard uses clinical triggers", match.is_handoff)


# ── 2. local JWKS + real RS256 tokens ───────────────────────────────────────
def _b64(n: int) -> str:
    return base64.urlsafe_b64encode(n.to_bytes((n.bit_length() + 7) // 8, "big")).rstrip(b"=").decode()


class JWKS:
    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        nums = self.key.public_key().public_numbers()
        body = json.dumps(
            {
                "keys": [
                    {"kty": "RSA", "kid": "smoke", "use": "sig", "alg": "RS256", "n": _b64(nums.n), "e": _b64(nums.e)}
                ]
            }
        ).encode()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.port = free_port()
        self.httpd = http.server.HTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.issuer = f"http://127.0.0.1:{self.port}/"
        self.audience = "ai-voice-agent"

    def token(self, sub: str, key=None, **claims) -> str:
        payload = {
            "sub": sub,
            "iss": self.issuer,
            "aud": self.audience,
            "exp": int(time.time()) + 600,
            "roles": ["user"],
        }
        payload.update(claims)
        return jwt.encode(payload, key or self.key, algorithm="RS256", headers={"kid": "smoke"})


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── 3. running server ───────────────────────────────────────────────────────
def start_server(server_cmd: str, jwks: JWKS, port: int, log_file) -> subprocess.Popen:
    env = dict(os.environ)
    env.update(
        {
            "APP_ENV": "production",
            "AUTH_MODE": "production",
            "OIDC_ISSUER_URL": jwks.issuer,
            "OIDC_AUDIENCE": jwks.audience,
            "OIDC_JWKS_URL": jwks.issuer + "jwks.json",
            "PORT": str(port),
            "LLM_PROVIDER": "free_fallback",
            "GEMINI_API_KEY": SENTINEL_GEMINI,
            "GROQ_API_KEY": SENTINEL_GROQ,
            "LLM_TIMEOUT_SECONDS": "5",
            "TWILIO_AUTH_TOKEN": TWILIO_TOKEN,
            # One token per minute: refill cannot hide the 429 on a slow runner.
            "RATE_LIMIT_REQUESTS_PER_MINUTE": "1",
            "RATE_LIMIT_BURST": "5",
            "TRACING_ENABLED": "false",
        }
    )
    for name in ("DEV_AUTH_ENABLED", "VOICE_MOCK_SERVICES", "TELEPHONY_MOCK_PIN"):
        env.pop(name, None)
    env.setdefault("PERSISTENCE_MODE", "dev")
    return subprocess.Popen(shlex.split(server_cmd), cwd=ROOT, env=env, stdout=log_file, stderr=subprocess.STDOUT)


def wait_ready(base: str, proc: subprocess.Popen, timeout: float = 90.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        try:
            if requests.get(base + "/ready", timeout=2).status_code == 200:
                return True
        except requests.RequestException:
            pass
        time.sleep(0.5)
    return False


def check_running_server(base: str, port: int, jwks: JWKS) -> None:
    from twilio_signature import compute_signature

    alice, bob, carol = jwks.token("alice"), jwks.token("bob"), jwks.token("carol")

    check("GET /health -> 200", requests.get(base + "/health", timeout=5).status_code == 200)
    check("GET /ready -> 200", requests.get(base + "/ready", timeout=5).status_code == 200)

    def gen(headers=None):
        return requests.post(base + "/generate", json=CLINICAL, headers=headers or {}, timeout=30)

    check("anonymous /generate -> 401", gen().status_code == 401)
    check("dev token /generate -> 401", gen(bearer("test-admin-token")).status_code == 401)
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    check("JWT signed by another key -> 401", gen(bearer(jwks.token("eve", key=other_key))).status_code == 401)
    check("expired JWT -> 401", gen(bearer(jwks.token("alice", exp=int(time.time()) - 3600))).status_code == 401)
    check("wrong-audience JWT -> 401", gen(bearer(jwks.token("alice", aud="someone-else"))).status_code == 401)
    r = gen(bearer(alice))
    check(
        "valid JWT -> 200 + clinical handoff",
        r.status_code == 200 and r.json().get("is_handoff") is True,
        f"{r.status_code} {r.text[:200]}",
    )

    sub = requests.post(base + "/jobs/generate", json=CLINICAL, headers=bearer(alice), timeout=10)
    check("job submit -> 202", sub.status_code == 202, sub.text[:200])
    job_id = sub.json().get("job_id", "") if sub.status_code == 202 else ""
    status = {}
    for _ in range(100):
        status = requests.get(f"{base}/jobs/{job_id}", headers=bearer(alice), timeout=5).json()
        if status.get("status") in ("completed", "failed"):
            break
        time.sleep(0.1)
    check("job owner can read result", status.get("status") == "completed", str(status)[:200])
    check(
        "other user cannot read job (404)",
        requests.get(f"{base}/jobs/{job_id}", headers=bearer(bob), timeout=5).status_code == 404,
    )
    check("anonymous cannot read job (401)", requests.get(f"{base}/jobs/{job_id}", timeout=5).status_code == 401)

    twiml_url = f"http://127.0.0.1:{port}/twiml/inbound-call"
    params = {"CallSid": "CAsmoke", "From": "+15550000000"}
    check("unsigned Twilio webhook -> 403", requests.post(twiml_url, data=params, timeout=5).status_code == 403)
    signed = requests.post(
        twiml_url,
        data=params,
        headers={"X-Twilio-Signature": compute_signature(TWILIO_TOKEN, twiml_url, params)},
        timeout=5,
    )
    check(
        "signed Twilio webhook -> 200 TwiML",
        signed.status_code == 200 and "<Stream url=" in signed.text,
        f"{signed.status_code}",
    )

    async def _ws_unsigned_rejected() -> bool:
        import websockets

        try:
            async with websockets.connect(f"ws://127.0.0.1:{port}/ws/call", open_timeout=5) as ws:
                await asyncio.wait_for(ws.recv(), timeout=5)
        except Exception:
            return True
        return False

    check("unsigned media-stream WebSocket rejected", asyncio.run(_ws_unsigned_rejected()))

    codes = [gen(bearer(carol)).status_code for _ in range(6)]
    check("rate limit: burst of 5 then 429", codes == [200] * 5 + [429], str(codes))

    if os.environ.get("PERSISTENCE_MODE", "").lower() in ("production", "postgres", "postgresql"):
        import sqlalchemy

        engine = sqlalchemy.create_engine(os.environ["DATABASE_URL"])
        with engine.connect() as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    sqlalchemy.text("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
                )
            }
            audit_rows = conn.execute(sqlalchemy.text("SELECT count(*) FROM audit_events")).scalar()
        engine.dispose()
        expected = {
            "alembic_version",
            "sessions",
            "memory_records",
            "audit_events",
            "security_events",
            "idempotency_records",
        }
        check("database: migrations applied", expected <= tables, str(sorted(tables)))
        check("database: audit events persisted by the live server", (audit_rows or 0) > 0, f"rows={audit_rows}")
    else:
        print("SKIP  database checks (PERSISTENCE_MODE is not production)")


def _persistence_is_production() -> bool:
    return os.environ.get("PERSISTENCE_MODE", "").lower() in ("production", "postgres", "postgresql")


def _count_audit_events(event_type=None) -> int:
    import sqlalchemy

    engine = sqlalchemy.create_engine(os.environ["DATABASE_URL"])
    try:
        with engine.connect() as conn:
            if event_type is None:
                return conn.execute(sqlalchemy.text("SELECT count(*) FROM audit_events")).scalar() or 0
            return (
                conn.execute(
                    sqlalchemy.text("SELECT count(*) FROM audit_events WHERE event_type = :t"), {"t": event_type}
                ).scalar()
                or 0
            )
    finally:
        engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-cmd", default="scripts/run_pipeline.sh --serve")
    args = parser.parse_args()

    check_image_configuration()

    jwks = JWKS()
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    with tempfile.TemporaryFile(mode="w+") as log:
        proc = start_server(args.server_cmd, jwks, port, log)
        try:
            ready = wait_ready(base, proc)
            check("server became ready under APP_ENV=production", ready)
            if ready:
                check_running_server(base, port, jwks)
        finally:
            sigterm_code = None
            if proc.poll() is None:
                if os.name == "posix":
                    proc.send_signal(signal.SIGTERM)
                    try:
                        sigterm_code = proc.wait(timeout=40)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        check("SIGTERM -> process exits within 40s", False, "still running; killed")
                else:
                    proc.terminate()
                    proc.wait(timeout=20)
            log.seek(0)
            output = log.read()
        if sigterm_code is not None:
            # uvicorn shuts down gracefully on SIGTERM and then re-raises the
            # signal (uvicorn/server.py capture_signals -> signal.raise_signal),
            # so a clean stop exits with -SIGTERM (143 in a shell), or 0 if the
            # signal arrives after its handlers are restored. The exit status
            # alone cannot tell a graceful stop from a kill, so gracefulness is
            # proven by uvicorn's lifespan-shutdown log line and, with a
            # database, by the application's own GRACEFUL_SHUTDOWN audit event.
            check(
                "SIGTERM -> exits with 0 or -SIGTERM",
                sigterm_code in (0, -signal.SIGTERM),
                f"exit={sigterm_code}",
            )
            check("SIGTERM -> lifespan shutdown completed", "Application shutdown complete." in output)
            if _persistence_is_production():
                check(
                    "SIGTERM -> GRACEFUL_SHUTDOWN audit event persisted", _count_audit_events("GRACEFUL_SHUTDOWN") > 0
                )
        leaked = [s for s in (SENTINEL_GEMINI, SENTINEL_GROQ, TWILIO_TOKEN) if s in output]
        check("no credentials in server logs", not leaked, f"found {len(leaked)}")
        if failures:
            print("---- server log (tail) ----")
            print(output[-4000:])

    jwks.httpd.shutdown()
    print(f"RESULT: {'ALL PASS' if not failures else f'{len(failures)} FAILED: {failures}'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

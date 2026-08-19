"""
Phase 9: Inference API
Goal: Serve the exported voice assistant over HTTP so a Text-to-Speech
      layer (or any other client) can consume plain-text responses
      without embedding the model-loading code itself.

Extraction note (docs/IMPLEMENTATION_ROADMAP.md M2): this route handler
now calls ConversationManager.handle_turn() directly rather than going
through the VoiceAssistantInference facade — FastAPI -> ConversationManager
-> {Safety, RAG, LLM, Handoff} is the live request path, matching
docs/MODULES.md §2. VoiceAssistantInference (src/inference/predict.py)
still exists as a backward-compatible wrapper around the same
ConversationManager for non-API callers (src/eval/evaluate.py, the CLI).

Authentication note (Phase 7; plan.md Steps 7.3, 7.10): FastAPI ->
AuthenticationProvider -> AuthContext -> ConversationManager. An
`Authorization: Bearer <token>` header, when present, is resolved through
an AuthenticationProvider into a trusted AuthContext. Backward
compatibility is deliberate: a request with NO Authorization header
still succeeds, using ANONYMOUS_CONTEXT (today's pre-Phase-7 behavior,
and what every existing client — src/voice/client_tts.py, the full
pre-Phase-7 test suite — already does). A request that DOES supply an
Authorization header but an invalid/unrecognized token fails closed with
401 — trying and failing to authenticate is treated differently from not
trying at all, which is the correct security posture (silently falling
back to anonymous on a bad token would mask a broken client integration
as "it worked, just anonymously").

Production authentication note (Phase 9; plan.md Steps 9.11, 9.12): which
AuthenticationProvider implementation is constructed is controlled by the
AUTH_MODE environment variable:

  AUTH_MODE unset or "dev" (default) -> identity.DevelopmentAuthenticationProvider,
  a deterministic, TEST-ONLY provider (see identity.py's module docstring)
  gated additionally by DEV_AUTH_ENABLED.

  AUTH_MODE="production" or "oidc" -> oidc_provider.OIDCAuthenticationProvider,
  a real JWT/OIDC validator (signature, issuer, audience, expiry, JWKS —
  see oidc_provider.py's module docstring) built from configs/auth.yaml +
  OIDC_ISSUER_URL/OIDC_AUDIENCE/OIDC_JWKS_URL environment variables.

There is no automatic, silent fallback between the two in either
direction: if AUTH_MODE requests production/oidc mode and the required
OIDC configuration is missing or invalid, constructing this module raises
AuthConfigurationError, which is left unhandled here — the process fails
to start rather than serving traffic with broken or absent authentication
(plan.md Principle 32/33).
"""

import json
import logging
import os
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator
import uvicorn

# This codebase avoids package-relative imports (no __init__.py anywhere),
# so the sibling directory is added to sys.path explicitly rather than
# importing across src/ subpackages.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))
from action_models import ANONYMOUS_CONTEXT, AuthContext  # noqa: E402
from audit import AuditLogger, SecurityEventDetector  # noqa: E402
from conversation_manager import ConversationManager, build_conversation_manager  # noqa: E402
from identity import AuthenticationError, DevelopmentAuthenticationProvider  # noqa: E402
from metrics import MetricsRegistry  # noqa: E402
from observability_models import EventType, new_request_id  # noqa: E402
from oidc_provider import OIDCAuthenticationProvider, load_oidc_config  # noqa: E402
from reliability_config import load_reliability_config  # noqa: E402

_error_logger = logging.getLogger("ai_voice_agent.errors")

# Where to load the model from. Prefer an already-merged export (produced by
# src/export/merge_and_convert.py) — no adapter attach step needed. Falls
# back to base model + auto-resolved LoRA adapter if no merged model exists
# yet (e.g. before Phase 8 export has been run).
MERGED_MODEL_DIR = os.environ.get("MERGED_MODEL_DIR", "outputs/merged_model")
BASE_MODEL_NAME = os.environ.get("BASE_MODEL_NAME", "Qwen/Qwen2.5-0.5B-Instruct")

# The development/test authentication provider can be disabled via
# environment configuration (plan.md Step 7.4: "Production configuration
# should be able to disable it") — set DEV_AUTH_ENABLED=false to ensure it
# can never become an accidental production authentication mechanism.
_DEV_AUTH_ENABLED = os.environ.get("DEV_AUTH_ENABLED", "true").strip().lower() != "false"

# Phase 9: which AuthenticationProvider is constructed below. "dev" (or
# unset) preserves every pre-Phase-9 behavior exactly. "production"/"oidc"
# switches to a real JWT/OIDC validator and, per plan.md Principle 32/33,
# never silently falls back to the development provider if OIDC
# configuration is missing -- see AUTH_MODE's docstring above.
AUTH_MODE = os.environ.get("AUTH_MODE", "dev").strip().lower()
_PRODUCTION_AUTH_MODES = {"production", "oidc"}

# Phase 8: one shared AuditLogger/MetricsRegistry/SecurityEventDetector
# for the whole process -- constructed here (not inside
# build_conversation_manager()) so the authentication boundary below,
# which lives outside ConversationManager entirely, emits into the same
# audit trail as everything ConversationManager wires internally, rather
# than a second, disconnected one.
_audit_logger = AuditLogger()
_metrics = MetricsRegistry()
_security_detector = SecurityEventDetector(_audit_logger)

if AUTH_MODE in _PRODUCTION_AUTH_MODES:
    # Fails closed by raising AuthConfigurationError, uncaught, if
    # required OIDC configuration is missing/invalid -- the process must
    # not start with broken authentication rather than serve requests
    # under it (plan.md Principle 32).
    _authentication_provider = OIDCAuthenticationProvider(
        load_oidc_config(), audit_logger=_audit_logger, security_detector=_security_detector,
    )
else:
    _authentication_provider = DevelopmentAuthenticationProvider(
        enabled=_DEV_AUTH_ENABLED, audit_logger=_audit_logger, security_detector=_security_detector,
    )

_conversation_manager: Optional[ConversationManager] = None


def resolve_identity(request: Request, authorization: Optional[str] = Header(default=None)) -> AuthContext:
    """
    FastAPI dependency: Authorization header -> AuthContext. No header at
    all -> ANONYMOUS_CONTEXT (backward compatible). A header that fails to
    authenticate -> 401, never a silent fallback to anonymous. Never
    reads identity from the request body, query parameters, or anything
    else a client fully controls without this dedicated header — see
    tests/test_server_api.py's identity-spoofing tests.

    `client_identifier` (Phase 8) is the caller's network address only —
    a safe, non-secret reference used solely for repeated-failure
    security-event detection (identity.py's SecurityEventDetector), never
    treated as an identity claim.
    """
    client_identifier = request.client.host if request.client else "unknown"
    if authorization is None:
        return ANONYMOUS_CONTEXT
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Invalid or missing credentials.")
    try:
        return _authentication_provider.authenticate({"token": token}, client_identifier=client_identifier)
    except AuthenticationError:
        # Deliberately the same generic message as identity.py's own
        # exception -- never echoes the submitted token, matching
        # plan.md Step 7.12's "do not log authentication secrets or
        # tokens."
        raise HTTPException(status_code=401, detail="Invalid or missing credentials.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _conversation_manager
    merged_dir = Path(MERGED_MODEL_DIR)
    if merged_dir.exists():
        print(f"Loading merged model from {merged_dir}...")
        _conversation_manager = build_conversation_manager(
            base_model_name=str(merged_dir), auto_resolve_adapter=False,
            audit_logger=_audit_logger, metrics=_metrics, security_detector=_security_detector,
        )
    else:
        print(
            f"No merged model at {merged_dir} — loading base model "
            "with auto-resolved LoRA adapter instead."
        )
        _conversation_manager = build_conversation_manager(
            base_model_name=BASE_MODEL_NAME,
            audit_logger=_audit_logger, metrics=_metrics, security_detector=_security_detector,
        )
    yield
    # Phase 10 (plan.md Step 10.18): graceful shutdown. This process
    # holds no external connection pool/persistent client that needs an
    # explicit close (no database, no long-lived HTTP session kept open
    # across requests) -- the meaningful shutdown action is stopping
    # traffic acceptance (uvicorn's own responsibility, bounded via
    # `timeout_graceful_shutdown` below, never an indefinite wait) and
    # recording that a shutdown happened. `_conversation_manager` is
    # intentionally left non-None here: an in-flight request already
    # past the readiness check should be allowed to finish rather than
    # be told "not ready" mid-response.
    _audit_logger.record(EventType.GRACEFUL_SHUTDOWN, outcome="success", reason="Server shutdown initiated.")


app = FastAPI(title="AI Voice Employee — Inference API", lifespan=lifespan)


@app.middleware("http")
async def _correlation_id_middleware(request: Request, call_next):
    """
    Phase 8, plan.md Step 8.3: every request gets a correlation ID —
    reused from an incoming `X-Request-ID` header when the caller already
    has one (e.g. an upstream gateway), otherwise generated here. Stored
    on `request.state` for handlers to read and thread into
    ConversationManager.handle_turn(), and echoed back in the response
    header so a client can correlate its own logs with this service's
    audit trail.
    """
    request_id = request.headers.get("x-request-id") or new_request_id()
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


@app.exception_handler(Exception)
async def _safe_exception_handler(request: Request, exc: Exception):
    """
    Phase 8, plan.md Step 8.15: an unhandled exception anywhere in a
    route never reaches the client as a raw traceback/exception message
    (the same "never expose internal failure detail" posture
    ConversationManager.handle_turn() already documents for its own
    "error" field). The internal audit log records the real exception
    type and correlation ID; the client gets only a generic, safe body.
    """
    request_id = getattr(request.state, "request_id", None) or new_request_id()
    error_id = new_request_id()
    _error_logger.error(
        "unhandled_exception error_id=%s request_id=%s exception_type=%s",
        error_id, request_id, type(exc).__name__,
    )
    _audit_logger.record(
        EventType.SYSTEM_ERROR, outcome="internal_error", request_id=request_id,
        reason=f"Unhandled {type(exc).__name__}", metadata={"error_id": error_id, "exception_type": type(exc).__name__},
    )
    return JSONResponse(
        status_code=500,
        content={"request_id": request_id, "error_id": error_id, "error_code": "internal_error",
                 "message": "An unexpected error occurred. Please try again."},
    )


@app.exception_handler(RequestValidationError)
async def _request_validation_exception_handler(request: Request, exc: RequestValidationError):
    """
    Phase 10 (plan.md Step 10.16/10.20): a request rejected by
    ChatRequest's resource-limit validation (message/history too long)
    is still a normal, safe 422 -- this handler only adds a metrics
    counter on top of FastAPI's own default response body, never changes
    what the client receives.
    """
    if _metrics is not None:
        _metrics.increment("request_rejections_total")
    return await request_validation_exception_handler(request, exc)


_RELIABILITY = load_reliability_config()


class ChatRequest(BaseModel):
    """
    Resource limits (Phase 10, plan.md Step 10.16) are sourced from
    configs/reliability.yaml (`_RELIABILITY.request_limits`) so tuning
    them doesn't require a code change. A request outside these bounds
    is rejected by FastAPI's normal Pydantic validation (422, a generic
    structured error) before it ever reaches ConversationManager — the
    same "reject obviously abusive input before consuming resources"
    posture already used for auth/malformed-JSON handling.
    """

    message: str = Field(max_length=_RELIABILITY.request_limits.max_message_length)
    history: list[dict] = Field(default_factory=list, max_length=_RELIABILITY.request_limits.max_history_turns)
    # When True, /generate returns newline-delimited JSON (NDJSON) chunks
    # instead of blocking for the full reply — see generate() below.
    stream: bool = False
    # Phase 5/7: client-supplied session identifier only — NOT an
    # identity claim. Session *ownership* is still verified against the
    # trusted AuthContext resolved from the Authorization header (see
    # resolve_identity()), never against anything else in this request
    # body. A client cannot use this field to read/act on another user's
    # session; SessionManager.get_session()'s existing user_id check
    # (Phase 5) still applies unchanged.
    session_id: Optional[str] = None

    @field_validator("history")
    @classmethod
    def _bound_history_turn_length(cls, history: list[dict]) -> list[dict]:
        limit = _RELIABILITY.request_limits.max_history_turn_length
        for turn in history:
            content = turn.get("content") if isinstance(turn, dict) else None
            if isinstance(content, str) and len(content) > limit:
                raise ValueError(f"history turn content exceeds max_history_turn_length ({limit})")
        return history


class ChatResponse(BaseModel):
    response: str
    is_handoff: bool
    latency_ms: float


@app.get("/health")
def health() -> dict:
    """Liveness only — always "ok" once the process is up, unchanged from pre-Phase-8 behavior."""
    return {"status": "ok", "model_loaded": _conversation_manager is not None}


@app.get("/ready")
def ready() -> JSONResponse:
    """
    Readiness (Phase 8, plan.md Step 8.14) — distinct from /health:
    reports whether this instance can actually serve a /generate request
    right now (the model finished loading), without exposing which
    internal dependency is missing or any other implementation detail.
    """
    is_ready = _conversation_manager is not None
    return JSONResponse(status_code=200 if is_ready else 503, content={"ready": is_ready})


@app.post("/generate", response_model=ChatResponse)
def generate(req: ChatRequest, request: Request, identity: AuthContext = Depends(resolve_identity)):
    """
    Generate a response to req.message via ConversationManager.handle_turn().

    req.stream == False (default): blocks until generation finishes and
    returns a single ChatResponse JSON body.

    req.stream == True: returns NDJSON — one {"token": "..."} line per
    generated chunk, followed by a final
    {"done": true, "response": ..., "is_handoff": ..., "latency_ms": ...}
    line. Lets a client (e.g. src/voice/client_tts.py) start speaking the
    first sentence before the rest of the reply has finished generating.

    Privacy note (Phase 6, plan.md Step 6.12): both response shapes are
    built from explicitly named fields, never by spreading
    ConversationManager's full internal result dict — that dict also
    carries "policy"/"intent" (Phase 3/2 routing/policy reasoning) and
    "degraded"/"error" (internal failure-path metadata), none of which is
    client-facing information. Client-relevant tool-execution status
    (Phase 4) is surfaced narrowly, without the raw tool `result` payload
    or its internal `metadata`.

    Identity note (Phase 7): `identity` is resolved exclusively from the
    Authorization header by resolve_identity() — never from req.message,
    req.session_id, or anything else the client fully controls. It is
    passed straight through to ConversationManager.handle_turn(), which
    itself never authenticates or assigns roles (plan.md Step 7.13); it
    only uses `identity.user_id` for session/memory ownership scoping
    (Phase 5, unchanged) and tool authorization (Phase 4/7, unchanged).
    """
    if _conversation_manager is None:
        raise HTTPException(status_code=503, detail="Model not loaded yet.")

    request_id = getattr(request.state, "request_id", None)

    if not req.stream:
        result = None
        for item in _conversation_manager.handle_turn(
            req.message, history=req.history, auth=identity, session_id=req.session_id, request_id=request_id,
        ):
            if not isinstance(item, str):
                result = item
        return ChatResponse(
            response=result["response"],
            is_handoff=result["is_handoff"],
            latency_ms=result["latency_ms"],
        )

    def _client_safe_done_event(result: dict) -> dict:
        event = {
            "done": True,
            "response": result["response"],
            "is_handoff": result["is_handoff"],
            "latency_ms": result["latency_ms"],
        }
        tool = result.get("tool")
        if tool is not None:
            event["tool_status"] = tool.get("status")
        return event

    def ndjson():
        for item in _conversation_manager.handle_turn(
            req.message, history=req.history, auth=identity, session_id=req.session_id, request_id=request_id,
        ):
            if isinstance(item, str):
                yield json.dumps({"token": item}) + "\n"
            else:
                yield json.dumps(_client_safe_done_event(item)) + "\n"

    return StreamingResponse(ndjson(), media_type="application/x-ndjson")


if __name__ == "__main__":
    uvicorn.run(
        app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)),
        # Phase 10 (plan.md Step 10.18): bounded graceful-shutdown window
        # -- never wait indefinitely for in-flight requests to finish.
        timeout_graceful_shutdown=int(_RELIABILITY.graceful_shutdown_timeout_seconds),
    )

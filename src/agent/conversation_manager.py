"""
Conversation Manager — the orchestration layer for a single conversational
turn (docs/MODULES.md §2; docs/ARCHITECTURE.md Core Principle 1;
docs/adr/ADR-001; docs/IMPLEMENTATION_ROADMAP.md milestone M2).

Before this module existed, the entire per-turn sequence — clinical check,
retrieval, prompt assembly, generation, handoff check — lived fused inside
VoiceAssistantInference (src/inference/predict.py), alongside model loading
itself. That fusion is exactly what docs/MODULES.md's "Known Implementation
Debt" section and docs/ARCHITECTURE_REVIEW.md finding 3.1 (High) describe.
This module is the extraction: it owns *only* call order and fallback
policy. It does not implement clinical detection, handoff detection,
retrieval, or generation itself — those algorithms stay exactly where they
already were (HandoffDetector, Retriever, LLMService), unmodified, and are
handed to ConversationManager as constructor-injected collaborators so the
orchestration sequence can be exercised with all of them mocked (the
"Unit Test Boundary" docs/MODULES.md §2 already specifies).

Clinical guard vs. handoff detector: per ADR-005, these are two
*configurations* of the same HandoffDetector class (one YAML file each),
not two independently-implemented components — deliberate reuse, not a
design gap. ConversationManager holds two HandoffDetector instances for
exactly that reason; it is not a missing "SafetyGuard" abstraction.

Phase 2 adds intent classification (IntentEngine, src/agent/intent_engine.py)
as a new step strictly *after* the clinical safety check and *before*
retrieval/generation — see handle_turn()'s sequencing below. This ordering
is not a convention, it's structural: the clinical guard short-circuits
and returns before IntentEngine is ever constructed or called for that
turn, so no intent classification result — however confident, however
"FAQ"-labeled — can route a clinically-flagged message to normal
generation. IntentEngine has no authority to grant that path; it isn't
consulted at all when the clinical guard has already fired.

Phase 4 adds an optional ToolOrchestrator for the TOOL_ORCHESTRATOR route
(appointment/order actions). The LLM is never in this execution loop at
all: ConversationManager extracts only what's conservatively, verbatim
extractable from the current message (an existing record's ID, never a
guessed date/doctor/time — see _extract_action_parameters()), builds an
untrusted ActionProposal, and hands it to ToolOrchestrator, which is the
only thing that validates/authorizes/executes it. Missing information
never gets guessed — it produces a clarification-style response asking
for exactly what's missing, per plan.md Phase 4 Step 4.9.

Phase 5 adds optional SessionManager/MemoryManager integration. A pending
tool confirmation is tracked in trusted, server-side SessionState — never
inferred from model output. When a session has a pending confirmation,
whether the *current* message counts as "yes"/"no" is decided by
_is_affirmative()/_is_negative() below: a small, deterministic,
regex-based classifier that is this application's own code, exactly the
same posture as HandoffDetector's phrase matching — not the LLM's
interpretation of the message, and not something ConversationManager
takes the LLM's word for. See handle_turn()'s "2.5b" step. A session past
its expiry can never have its pending action picked back up (enforced by
SessionManager.get_session() itself — see session_manager.py).
"""

import contextlib
import logging
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import yaml
from opentelemetry import trace

_INFERENCE_DIR = str(Path(__file__).resolve().parents[1] / "inference")
if _INFERENCE_DIR not in sys.path:
    sys.path.insert(0, _INFERENCE_DIR)
from action_models import ANONYMOUS_CONTEXT, ActionProposal, AuthContext, ToolRequest  # noqa: E402
from handoff_detector import HandoffDetector, HandoffMatch  # noqa: E402
from intent_engine import IntentEngine, IntentResult, Route, RoutingDecision  # noqa: E402
from memory_manager import MemoryManager  # noqa: E402
from policy_engine import Action, PolicyDecision, PolicyEngine  # noqa: E402
from privacy_logging import get_privacy_aware_logger, log_event  # noqa: E402
from privacy_service import PrivacyService  # noqa: E402
from session_manager import SessionManager  # noqa: E402
from tool_orchestrator import ToolOrchestrator, ToolValidationError  # noqa: E402
from tracing import SpanAttributes, get_tracer  # noqa: E402  (Phase 14)

_CONFIG_PATH = Path(__file__).resolve().parents[2] / "configs" / "config.yaml"
_CLINICAL_CONFIG_PATH = Path(__file__).resolve().parents[2] / "configs" / "clinical_triggers.yaml"

logger = logging.getLogger("ai_voice_agent.conversation")

# Phase 14: module-level tracer singleton. Safe under both the real SDK and
# tracing.py's NoOpTracerProvider fallback (TRACING_ENABLED=false) -- every
# span created below becomes a zero-cost no-op in the disabled case.
_tracer = get_tracer("ai-voice-agent.conversation")

# Stability fix (Phase 16.1, closing the Phase 15.1 gap): max_concurrent_
# generations=1 exists to serialize calls against a single, non-thread-
# verified LOCAL model instance (plan.md Step 10.16/10.17). Phase 15.1
# raised this for remote providers, but only inside
# build_conversation_manager()'s own env-var-driven branch -- ANY other
# construction path (direct construction, tests, or even
# build_conversation_manager()'s own caller-injected-provider branch)
# fell back to the conservative default. Moved here, into the
# constructor itself (see ConversationManager.__init__ below), so the
# decision is based on the actual injected llm_service's type -- a
# property true regardless of *how* it was constructed -- rather than
# which code path constructed it. Applies universally now: direct
# construction, the factory, and any future caller all get the same,
# correct default.
_REMOTE_PROVIDER_DEFAULT_MAX_CONCURRENT_GENERATIONS = 10


def _llm_service_is_safe_for_concurrent_generation(llm_service) -> bool:
    """
    True only for an llm_service verified to hold no shared-mutable-state
    hazard under concurrent generate_stream() calls.

    Deliberately NOT a blanket `isinstance(llm_service, BaseLLMProvider)`
    check: src/inference/llm_provider.py's LocalLLMProvider ALSO
    implements BaseLLMProvider while wrapping the exact single torch-
    based LLMService instance this semaphore exists to protect --
    build_llm_provider() returns one for LLM_PROVIDER=local (or any
    unrecognized value), so a caller who invokes it directly (bypassing
    build_conversation_manager()'s own LLM_PROVIDER-based guard) could
    hand ConversationManager a LocalLLMProvider. This function returns
    False for that case (and for anything wrapping it, e.g. a
    FallbackLLMProvider whose primary/fallback is itself Local), True
    only for the verified-stateless ClaudeLLMProvider/GeminiLLMProvider
    (directly, or as a FallbackLLMProvider's primary/fallback), and
    False (conservative) for the plain local LLMService itself or any
    caller-injected/test-double object of unknown type.
    """
    from llm_provider import ClaudeLLMProvider, FallbackLLMProvider, GeminiLLMProvider, GroqLLMProvider, LocalLLMProvider

    if isinstance(llm_service, LocalLLMProvider):
        return False
    if isinstance(llm_service, FallbackLLMProvider):
        return _llm_service_is_safe_for_concurrent_generation(
            llm_service.primary
        ) and _llm_service_is_safe_for_concurrent_generation(llm_service.fallback)
    return isinstance(llm_service, (ClaudeLLMProvider, GeminiLLMProvider, GroqLLMProvider))


def _resolve_max_concurrent_generations(configured_value: Optional[int], llm_service) -> int:
    """
    Pure decision, directly unit-testable. configured_value is the
    caller's EXPLICIT choice (e.g. configs/reliability.yaml's value,
    threaded through by build_conversation_manager()) -- always
    respected exactly when not None, for either provider type; None
    means "auto-detect from llm_service's type," used both by
    ConversationManager's own default and by
    build_conversation_manager() when reliability tuning is disabled.
    """
    if configured_value is not None:
        return configured_value
    if _llm_service_is_safe_for_concurrent_generation(llm_service):
        effective = _REMOTE_PROVIDER_DEFAULT_MAX_CONCURRENT_GENERATIONS
        logger.info(
            "max_concurrent_generations auto-detected as %s: llm_service is a verified "
            "stateless remote provider (%s), which has no shared-mutable-state hazard "
            "requiring serialization against a single local model instance.",
            effective,
            type(llm_service).__name__,
        )
        return effective
    return 1


@contextlib.contextmanager
def _traced(span_name: str):
    """
    Resilient span helper (Step 14.3.8/14.9): every `with` block below
    uses this instead of calling `_tracer.start_as_current_span()`
    directly, so a tracer failure -- even one that replaces
    `_tracer.start_as_current_span` itself, not just an internal SDK
    error `_tracer` (tracing.py's `_SafeTracer`) already guards against --
    can never propagate into and break a turn. Yields a real span on
    success, `opentelemetry.trace.INVALID_SPAN` (a documented no-op)
    otherwise.
    """
    try:
        _cm = _tracer.start_as_current_span(span_name)
    except Exception:
        yield trace.INVALID_SPAN
        return
    with _cm as _span:
        yield _span


class ConversationManager:
    """
    Coordinates one conversational turn: input validation, clinical safety
    check, safe-path retrieval, prompt assembly, LLM inference, handoff
    detection, and response metadata — with centralized fallback handling
    for each downstream dependency, per ARCHITECTURE.md Core Principle 5
    ("fail safe, not silent") and docs/MODULES.md §2 Error Handling.

    Every collaborator is injected, never constructed internally, so this
    class can be exercised in tests with all of them mocked/faked (see
    tests/test_conversation_manager.py) — no model load required.
    """

    # Must match the system prompt src/data/preprocess.py trained the model
    # against — drifting from it here changes model behavior, not just
    # orchestration. Unchanged from the pre-extraction VoiceAssistantInference.
    SYSTEM_PROMPT = (
        "You are a helpful, professional customer support voice assistant. "
        "Keep your responses brief, clear, and conversational. "
        "Never use bullet points or numbered lists. "
        "Speak naturally as if on a phone call. "
        "If you cannot help, offer to connect the customer to a human agent."
    )

    # User-facing text for each fail-safe fallback path. Kept as class
    # constants so tests can assert against them without hardcoding strings
    # in two places.
    CLINICAL_HANDOFF_RESPONSE = (
        "That's a question our pharmacist needs to answer directly for your safety — let me connect you with one now."
    )
    EMPTY_INPUT_RESPONSE = "I didn't catch that — could you say that again?"
    LLM_FAILURE_RESPONSE = "I'm sorry — I'm having trouble responding right now. Let me connect you with a human agent."
    # Phase 2: pre-generation routes that don't reach the LLM at all.
    CLARIFICATION_RESPONSE = (
        "I want to make sure I connect you with the right help — could you tell me a bit more about what you need?"
    )
    # Phase 4: templated (never LLM-generated) responses for tool-backed
    # routes — see _handle_tool_action().
    TOOL_CONFIRMATION_RESPONSE = "Before I do that, can you confirm you'd like me to proceed?"
    TOOL_UNAVAILABLE_RESPONSE = (
        "I'm not able to complete that action for you right now — let me connect you with a human agent."
    )
    TOOL_FAILURE_RESPONSE = "I wasn't able to complete that action. Let me connect you with a human agent."

    # Maps a confidently-classified intent to the ToolOrchestrator action
    # it corresponds to (src/agent/mock_tools.py's build_default_tool_registry()
    # registers exactly these four names). Deterministic, code-level —
    # never inferred from model output.
    _INTENT_TO_TOOL_ACTION = {
        "APPOINTMENT_BOOKING": "BOOK_APPOINTMENT",
        "APPOINTMENT_CANCEL": "CANCEL_APPOINTMENT",
        "APPOINTMENT_RESCHEDULE": "RESCHEDULE_APPOINTMENT",
        "ORDER_STATUS": "ORDER_LOOKUP",
    }
    # Matches this system's own mock-tool ID format exactly
    # (src/agent/mock_tools.py's MockAppointmentStore/_new_id, and the
    # seeded MockOrderStore records) — the one piece of information
    # reliably present verbatim in a user's message when they reference
    # an existing record. Never used to invent an ID that wasn't typed.
    _RECORD_ID_PATTERN = re.compile(r"\b(appt_\d+|order_\d+)\b")
    # Deterministic, application-owned reply classifiers for a pending
    # confirmation — never the LLM's interpretation (see module docstring).
    _AFFIRMATIVE_PATTERN = re.compile(
        r"^\s*(yes|yeah|yep|yup|confirm(ed)?|go ahead|do it|proceed|"
        r"please (do|proceed|confirm)|sounds good|that'?s right)\b",
        re.IGNORECASE,
    )
    _NEGATIVE_PATTERN = re.compile(
        r"^\s*(no|nope|nah|never\s*mind|cancel that|don'?t|stop|wait)\b",
        re.IGNORECASE,
    )

    def __init__(
        self,
        llm_service,
        retriever=None,
        clinical_guard: Optional[HandoffDetector] = None,
        handoff_detector: Optional[HandoffDetector] = None,
        intent_engine: Optional[IntentEngine] = None,
        policy_engine: Optional[PolicyEngine] = None,
        tool_orchestrator: Optional[ToolOrchestrator] = None,
        session_manager: Optional[SessionManager] = None,
        memory_manager: Optional[MemoryManager] = None,
        privacy_service: Optional[PrivacyService] = None,
        audit_logger=None,
        metrics=None,
        rag_top_k: int = 3,
        rag_score_threshold: float = 0.35,
        system_prompt: Optional[str] = None,
        rag_retry_policy=None,
        rag_circuit_breaker=None,
        llm_retry_policy=None,
        llm_circuit_breaker=None,
        max_concurrent_generations: Optional[int] = None,
        sleep_fn=None,
        database=None,
        caller_pin: Optional[str] = None,
    ):
        """
        Args:
            llm_service:      Anything exposing
                               generate_stream(messages) -> Iterator[str | {"text", "latency_ms"}],
                               e.g. src/inference/llm_service.py's LLMService.
                               Pure text-in/text-out — see module docstring.
            retriever:        Anything exposing
                               retrieve(query, top_k) -> list[chunk-with-.score/.title/.content/.to_dict()],
                               e.g. src/rag/retriever.py's Retriever. None
                               disables retrieval entirely.
            clinical_guard:   A HandoffDetector configured to detect
                               clinical questions (dosage, interactions,
                               diagnosis, ...) pre-generation. None disables
                               the clinical short-circuit.
            handoff_detector: A HandoffDetector configured to detect
                               handoff intent in the model's *output*,
                               post-generation. Required — unlike the
                               clinical guard, every turn's response is
                               checked.
            intent_engine:     An IntentEngine (src/agent/intent_engine.py)
                               used to classify and route a turn *after*
                               the clinical guard has cleared it. Defaults
                               to a real IntentEngine() if not given —
                               unlike the clinical guard, intent
                               classification always runs (there's no
                               "disabled" mode), matching handoff_detector's
                               default-construct pattern above.
            policy_engine:     A PolicyEngine (src/agent/policy_engine.py)
                               — the deterministic enforcement boundary
                               (Phase 3) this method routes its clinical
                               and generation/clarification decisions
                               through. Defaults to a real PolicyEngine()
                               if not given. PolicyEngine never
                               re-implements clinical trigger matching —
                               it only interprets clinical_guard's and
                               intent_engine's already-computed results.
            tool_orchestrator: A ToolOrchestrator (src/agent/tool_orchestrator.py).
                               None (the default) disables tool execution
                               entirely — a TOOL_ORCHESTRATOR-routed turn
                               then behaves exactly as it did in Phase 2/3
                               (falls through to normal generation,
                               metadata only, nothing executes). This
                               keeps every existing caller that doesn't
                               pass this argument unaffected.
            session_manager:   A SessionManager (src/agent/session_manager.py).
                               None (the default) disables session-backed
                               confirmation entirely — a tool action
                               requiring confirmation is asked for and
                               confirmed via handle_turn()'s own
                               `confirmed` parameter only, per-call, with
                               no cross-turn memory of the pending action
                               (Phase 4 behavior, unchanged).
            memory_manager:    A MemoryManager (src/agent/memory_manager.py).
                               None (the default) disables durable-memory
                               context injection entirely.
            rag_top_k / rag_score_threshold: Retrieval tuning, same
                               semantics as configs/config.yaml's `rag:`
                               section (see build_conversation_manager()).
            system_prompt:     Override for SYSTEM_PROMPT, mainly for tests.
            audit_logger:      An AuditLogger (src/agent/audit.py, Phase 8).
                               None (the default) disables turn-level audit
                               emission entirely -- see handle_turn()'s "2."/
                               "2.5."/"6." steps, each of which emits from a
                               PolicyDecision it already computed for its own
                               control-flow purposes, never a separate check.
            metrics:           A MetricsRegistry (src/agent/metrics.py,
                               Phase 8). None disables metrics recording.
            rag_retry_policy / rag_circuit_breaker: Phase 10 reliability
                               for RAG retrieval (src/agent/reliability.py).
                               Retrieval is always treated as read-only/
                               safe-to-retry (no idempotency question).
                               None/None preserves exact pre-Phase-10
                               behavior (a single attempt, degrade on
                               failure -- unchanged fallback).
            llm_retry_policy / llm_circuit_breaker: Phase 10 reliability
                               for LLM generation. A retry is only ever
                               attempted before any chunk of the current
                               attempt has been yielded to the caller —
                               once streaming has begun, a failure always
                               falls through to LLM_FAILURE_RESPONSE
                               unchanged, since already-sent output can't
                               be un-sent. None/None preserves exact
                               pre-Phase-10 behavior.
            max_concurrent_generations: Bounds concurrent generate_stream()
                               calls via an internal semaphore (plan.md
                               Step 10.16/10.17). None (the default) auto-
                               detects a safe value from llm_service's
                               type -- see _resolve_max_concurrent_
                               generations()/_llm_service_is_safe_for_
                               concurrent_generation() above: 1 (serialize)
                               for the local model or any object of
                               unverified type, a higher bound for a
                               verified-stateless remote provider
                               (Claude/Gemini/their Fallback wrapper). An
                               explicit int always overrides auto-
                               detection exactly, for either case.
            sleep_fn:          Injectable delay function for retry backoff
                               (plan.md Step 10.5 — tests must not really
                               sleep). Defaults to time.sleep.
            caller_pin:        Phase 18 fix. An operator-configured value
                               compared against what an unauthenticated
                               telephony caller speaks during the
                               AWAITING_AUTHENTICATION step (see
                               handle_turn()'s "2.4b"). This is NOT real
                               per-caller identity verification -- it is a
                               single shared secret, at most suitable for
                               a controlled canary/demo deployment where
                               every caller is known and trusted out of
                               band. None (the default, and the only safe
                               value for any real deployment) disables the
                               mock PIN entirely: every AWAITING_AUTHENTICATION
                               attempt fails closed to human handoff
                               regardless of what is spoken, rather than
                               silently accepting a hardcoded literal.
        """
        self.llm_service = llm_service
        self.retriever = retriever
        self.clinical_guard = clinical_guard
        self.handoff_detector = handoff_detector or HandoffDetector()
        self.intent_engine = intent_engine or IntentEngine()
        self.policy_engine = policy_engine or PolicyEngine()
        self.tool_orchestrator = tool_orchestrator
        self.session_manager = session_manager
        self.memory_manager = memory_manager
        self.privacy_service = privacy_service
        self.audit_logger = audit_logger
        self.metrics = metrics
        self.database = database
        self.caller_pin = caller_pin
        self.rag_top_k = rag_top_k
        self.rag_score_threshold = rag_score_threshold
        self.system_prompt = system_prompt or self.SYSTEM_PROMPT
        # Phase 10 (plan.md Steps 10.4-10.10). Default RetryPolicy(max_attempts=1)
        # is a no-op (never retries) -- omitting these preserves exact
        # pre-Phase-10 behavior, same convention as ToolOrchestrator's
        # own reliability wiring. build_conversation_manager() constructs
        # real multi-attempt policies by default for the live system.
        from reliability import RetryPolicy as _RetryPolicy

        self.rag_retry_policy = rag_retry_policy or _RetryPolicy(max_attempts=1)
        self.rag_circuit_breaker = rag_circuit_breaker
        self.llm_retry_policy = llm_retry_policy or _RetryPolicy(max_attempts=1)
        self.llm_circuit_breaker = llm_circuit_breaker
        self._sleep_fn = sleep_fn or time.sleep
        _effective_max_concurrent_generations = _resolve_max_concurrent_generations(
            max_concurrent_generations, llm_service
        )
        self._generation_semaphore = threading.Semaphore(max(1, _effective_max_concurrent_generations))

    # ── Turn orchestration ───────────────────────────────────────────────────

    def handle_turn(
        self,
        user_input,
        history: Optional[list] = None,
        auth: Optional[AuthContext] = None,
        confirmed: bool = False,
        session_id: Optional[str] = None,
        request_id: Optional[str] = None,
    ):
        """
        Run one conversational turn end to end, yielding text chunks as
        they're produced, followed by a final metadata dict.

        Args:
            user_input: The latest user turn. Anything other than a
                        non-blank string is treated as invalid input (see
                        Input validation below) rather than passed to
                        downstream components.
            history:    Prior turns as [{"role": ..., "content": ...}, ...].
                        Malformed entries are dropped rather than raised on
                        (see _normalize_history) — a defensively-parsed
                        caller input, not a trusted internal data
                        structure, consistent with ARCHITECTURE.md Core
                        Principle 5.
            auth:       Trusted AuthContext for a tool-backed turn (Phase 4).
                        MUST come from the caller's own trusted
                        application/session state — never derived from
                        user_input or any model output. Defaults to
                        ANONYMOUS_CONTEXT (unauthenticated) when omitted,
                        since no authentication system exists in this
                        repository yet (docs/ARCHITECTURE.md §9's
                        documented gap) — tool actions therefore fail
                        closed by default rather than silently succeeding.
            confirmed:  Trusted confirmation flag for a tool-backed turn
                        requiring confirmation (Phase 4). MUST come from
                        the caller's own trusted state (e.g. the user
                        explicitly confirming in a prior turn) — never
                        parsed from user_input or model output. Defaults
                        to False (fail closed).
            session_id: Optional session identifier (Phase 5). Only used
                        when session_manager is configured; None disables
                        session-backed confirmation tracking and memory-
                        context injection for this call, falling back to
                        Phase 4's per-call-only confirmation behavior.
            request_id: Optional correlation ID (Phase 8) shared by every
                        audit event this turn emits directly (the events
                        below in "2."/"2.5."/"6."). Normally supplied by
                        src/api/server.py's request-ID middleware; a real
                        one is generated here if audit_logger is
                        configured and none was given, so a turn invoked
                        directly (e.g. from tests or predict.py) still
                        gets one. Does not replace ToolRequest.request_id
                        (Phase 4's separate per-tool-call idempotency key)
                        -- SessionManager/ToolOrchestrator's own audit
                        events continue to use their own identifiers;
                        threading this same correlation ID into those
                        subsystems is deferred (see PHASE_8 report's
                        Limitations).

        Yields:
            str chunks as they're generated, then a final
            {"response": str, "is_handoff": bool, "handoff_confidence": float,
             "latency_ms": float, "retrieved_chunks": list[dict],
             "clinical_guard_triggered": bool, "degraded": bool,
             "error": Optional[str]}
            dict as the last item. The first six keys are the exact
            contract the pre-extraction VoiceAssistantInference produced;
            "degraded", "error", and "intent" are additive metadata for
            callers that want it (existing consumers ignore unknown keys).
            "error", when set, is always a short generic code — never a raw
            exception message — so internal failure detail is never
            exposed to clients (see LLM inference below). "intent" is the
            IntentEngine RoutingDecision.to_dict() for this turn
            ({intent, confidence, route, reason}) — None on any path where
            IntentEngine never ran (empty input, clinical short-circuit).
        """
        if request_id is None and self.audit_logger is not None:
            from observability_models import new_request_id

            request_id = new_request_id()

        # Phase 14: root span for the whole turn. Every span created by
        # _handle_turn_body() below (clinical/intent/policy/RAG/LLM/handoff)
        # becomes a child of this one. Tracing is purely observational —
        # a span-creation failure here must never affect the turn itself,
        # so attribute-setting and status-recording are wrapped defensively
        # and the generator's actual output/exceptions pass through
        # unchanged (plan.md Step 14.3.2/14.3.8).
        with _traced("conversation.handle_turn") as _turn_span:
            try:
                _turn_span.set_attribute(SpanAttributes.REQUEST_ID, request_id or "")
                _turn_span.set_attribute(SpanAttributes.SESSION_ID, session_id or "")
            except Exception:
                pass
            try:
                yield from self._handle_turn_body(
                    user_input,
                    history=history,
                    auth=auth,
                    confirmed=confirmed,
                    session_id=session_id,
                    request_id=request_id,
                )
            except Exception as exc:
                try:
                    from opentelemetry.trace import StatusCode

                    _turn_span.set_status(StatusCode.ERROR, type(exc).__name__)
                    _turn_span.record_exception(exc, attributes={"exception.type": type(exc).__name__})
                except Exception:
                    pass
                raise

    def _handle_turn_body(
        self,
        user_input,
        history: Optional[list] = None,
        auth: Optional[AuthContext] = None,
        confirmed: bool = False,
        session_id: Optional[str] = None,
        request_id: Optional[str] = None,
    ):
        """
        The actual turn-handling logic, unchanged from pre-Phase-14
        handle_turn() -- extracted verbatim into its own method so
        handle_turn() itself can wrap it in a root tracing span (Step
        14.3.2) without touching a single line of this body's control
        flow. request_id is already resolved by handle_turn() by the time
        this runs. See handle_turn()'s docstring for the full contract.
        """
        # 1. Input validation — fail safe rather than handing a malformed
        # or empty turn to the clinical guard / retriever / LLM.
        if not isinstance(user_input, str) or not user_input.strip():
            yield self.EMPTY_INPUT_RESPONSE
            yield self._final(
                self.EMPTY_INPUT_RESPONSE,
                is_handoff=False,
                confidence=0.0,
                latency_ms=0.0,
                retrieved_chunks=[],
                clinical_guard_triggered=False,
            )
            return

        if self.metrics is not None:
            self.metrics.increment("requests_total")

        normalized_history = self._normalize_history(history or [])
        turn_actor = auth.user_id if auth is not None and auth.authenticated else None

        # 2. Clinical safety check — deterministic, runs before generation,
        # and can short-circuit the turn entirely (ARCHITECTURE.md
        # Communication Rule 4). On an internal error, fail closed
        # (treat as triggered) per ADR-005 — a safety check that silently
        # no-ops on error is worse than one that over-escalates. The
        # *decision* to block is routed through PolicyEngine
        # (evaluate_clinical), which is the deterministic enforcement
        # boundary (Phase 3) — but PolicyEngine only interprets
        # clinical_guard's already-computed HandoffMatch, it never
        # re-implements clinical trigger matching itself (see
        # policy_engine.py's module docstring).
        if self.clinical_guard is not None:
            # Phase 14 (Step 14.3.3): observational only -- never gates the
            # decision below, which is computed exactly as before.
            with _traced("conversation.clinical_safety_check") as _span:
                try:
                    clinical_match = self.clinical_guard.score(user_input)
                except Exception:
                    clinical_match = HandoffMatch(is_handoff=True, confidence=1.0)
                # Phase 10 (plan.md Step 10.12): PolicyEngine itself failing
                # internally must deny/block the same way an unavailable
                # safety component does -- never fall through to normal
                # generation just because the *interpretation* step raised.
                try:
                    clinical_policy = self.policy_engine.evaluate_clinical(clinical_match)
                except Exception:
                    clinical_policy = PolicyDecision(
                        allowed=False,
                        policy="clinical",
                        rule="POLICY_ENGINE_UNAVAILABLE",
                        action=Action.HANDOFF,
                        reason="Clinical policy evaluation failed internally -- failing closed.",
                    )
                try:
                    _span.set_attribute(SpanAttributes.CLINICAL_TRIGGERED, not clinical_policy.allowed)
                except Exception:
                    pass
            if not clinical_policy.allowed:
                if self.audit_logger is not None:
                    from observability_models import EventType

                    self.audit_logger.record(
                        EventType.SAFETY_BLOCK,
                        outcome="blocked",
                        actor=turn_actor,
                        request_id=request_id,
                        policy=clinical_policy.policy,
                        reason=clinical_policy.reason,
                        metadata={"confidence": clinical_match.confidence},
                    )
                if self.metrics is not None:
                    self.metrics.increment("policy_denials_total")
                    self.metrics.increment("handoffs_total")
                yield self.CLINICAL_HANDOFF_RESPONSE
                yield self._final(
                    self.CLINICAL_HANDOFF_RESPONSE,
                    is_handoff=True,
                    confidence=clinical_match.confidence,
                    latency_ms=0.0,
                    retrieved_chunks=[],
                    clinical_guard_triggered=True,
                    policy=clinical_policy.to_dict(),
                )
                return

        # 2.4. Session lookup + pending-confirmation interception (Phase 5).
        # Runs after the clinical guard (never before — a "yes" reply is
        # still subject to clinical safety like any other message) and
        # before intent classification, because a reply to a pending
        # confirmation prompt ("yes" / "no") would otherwise score as
        # zero-signal in IntentEngine and be lost. Whether this message
        # counts as an affirmative/negative reply is decided by this
        # application's own deterministic regex classifiers
        # (_AFFIRMATIVE_PATTERN/_NEGATIVE_PATTERN) — never by asking the
        # LLM and never by trusting anything the model might have said.
        # A session whose pending action has expired can never be picked
        # back up here: SessionManager.get_session() itself returns None
        # for an expired session (see session_manager.py), so `session`
        # below is simply absent and this block is skipped entirely.
        session = None
        if self.session_manager is not None and session_id:
            user_id = auth.user_id if auth is not None else None
            session = self.session_manager.get_session(session_id, user_id=user_id)
            if session is None:
                session = self.session_manager.create_session(session_id=session_id, user_id=user_id)

            if (
                session.workflow_state == "AWAITING_CONFIRMATION"
                and session.pending_action
                and self.tool_orchestrator is not None
            ):
                if confirmed or self._AFFIRMATIVE_PATTERN.match(user_input):
                    reply, tool_metadata = self._execute_pending_action(session, auth)
                    yield reply
                    yield self._final(
                        reply,
                        is_handoff=False,
                        confidence=0.0,
                        latency_ms=0.0,
                        retrieved_chunks=[],
                        clinical_guard_triggered=False,
                        tool=tool_metadata,
                    )
                    return
                if self._NEGATIVE_PATTERN.match(user_input):
                    self.session_manager.update_session(
                        session_id,
                        workflow_state=None,
                        pending_action=None,
                        pending_parameters={},
                    )
                    cancelled_reply = "No problem — I won't go ahead with that."
                    yield cancelled_reply
                    yield self._final(
                        cancelled_reply,
                        is_handoff=False,
                        confidence=0.0,
                        latency_ms=0.0,
                        retrieved_chunks=[],
                        clinical_guard_triggered=False,
                    )
                    return
                # Ambiguous reply while a confirmation is pending — fall
                # through to normal turn handling rather than guessing;
                # the pending action/session state is left untouched so a
                # later clear "yes"/"no" can still resolve it (until it
                # expires).

            if (
                session.workflow_state == "AWAITING_AUTHENTICATION"
                and session.pending_action
                and self.tool_orchestrator is not None
            ):
                if self.caller_pin is None:
                    # Phase 18 security-gate fix: this used to accept a
                    # hardcoded literal ("1234") from any caller as valid
                    # identity verification -- see PHASE_18 report. There
                    # is no real per-caller PIN store, so the only safe
                    # default is fail-closed: never accept any spoken
                    # value, always hand off to a human. An operator may
                    # opt into the old (still not real-identity) mock
                    # behavior by explicitly configuring caller_pin, e.g.
                    # for a controlled canary where every caller is known
                    # out of band.
                    reply = (
                        "For your security, I'm not able to verify your identity "
                        "automatically right now — let me connect you with a human agent."
                    )
                    self.session_manager.update_session(
                        session_id,
                        workflow_state=None,
                        pending_action=None,
                        pending_parameters={},
                    )
                    yield reply
                    yield self._final(
                        reply,
                        is_handoff=True,
                        confidence=1.0,
                        latency_ms=0.0,
                        retrieved_chunks=[],
                        clinical_guard_triggered=False,
                    )
                    return
                pin_match = re.search(r"\b\d{4}\b", user_input)
                if pin_match:
                    pin = pin_match.group(0)
                    if pin == self.caller_pin:
                        new_metadata = dict(session.metadata)
                        new_metadata["authenticated_caller"] = True
                        self.session_manager.update_session(session_id, metadata=new_metadata)
                        # Re-execute with newly authenticated identity.
                        # Phase 18 fix: this previously set roles=["caller"],
                        # a string with no entry in identity.py's
                        # ROLE_PERMISSIONS table and no `permissions` set
                        # either -- AuthContext.has_permission() is a pure
                        # membership check against `permissions` (never
                        # derived from `roles`), so the resulting context
                        # could pass PolicyEngine's `authenticated` check
                        # but would then fail every actual permission
                        # check, silently turning "successful" PIN entry
                        # into an unusable identity for every real action
                        # (ORDER_LOOKUP, BOOK_APPOINTMENT, ...). Grant the
                        # same least-privilege Role.USER permission set
                        # DevelopmentAuthenticationProvider grants an
                        # ordinary authenticated user.
                        from action_models import AuthContext
                        from identity import Role, permissions_for_roles

                        new_auth = AuthContext(
                            user_id=session.user_id or "telephony_caller",
                            authenticated=True,
                            roles=(Role.USER.value,),
                            permissions=permissions_for_roles((Role.USER,)),
                            authentication_method="telephony_pin",
                        )
                        reply, tool_metadata = self._execute_pending_authentication(session, new_auth)
                        yield reply
                        yield self._final(
                            reply,
                            is_handoff=False,
                            confidence=0.0,
                            latency_ms=0.0,
                            retrieved_chunks=[],
                            clinical_guard_triggered=False,
                            tool=tool_metadata,
                        )
                        return
                    else:
                        reply = "That PIN doesn't seem to match. Let me connect you with a human agent."
                        self.session_manager.update_session(
                            session_id,
                            workflow_state=None,
                            pending_action=None,
                            pending_parameters={},
                        )
                        yield reply
                        yield self._final(
                            reply,
                            is_handoff=True,
                            confidence=1.0,
                            latency_ms=0.0,
                            retrieved_chunks=[],
                            clinical_guard_triggered=False,
                        )
                        return
                else:
                    reply = "I didn't hear a 4-digit PIN. Could you please say your PIN?"
                    yield reply
                    yield self._final(
                        reply,
                        is_handoff=False,
                        confidence=0.0,
                        latency_ms=0.0,
                        retrieved_chunks=[],
                        clinical_guard_triggered=False,
                    )
                    return

        # 2.5. Intent classification and routing — runs strictly after
        # the clinical guard above (never before, never in parallel), so
        # a clinical message has already exited this function by the
        # time IntentEngine would run. The LLM has no authority over this
        # decision (deterministic, config-driven — same posture as the
        # safety checks). On an internal error, degrade to RAG_LLM (the
        # existing, already-safety-net-backed generation path below)
        # rather than blocking the turn — IntentEngine is a routing/UX
        # classifier, not a safety gate, so an error here should not by
        # itself deny service.
        with _traced("conversation.intent_classify") as _span:
            try:
                routing = self.intent_engine.classify(user_input, history=normalized_history)
            except Exception:
                routing = RoutingDecision(
                    intent_result=IntentResult(intent=IntentEngine.UNKNOWN_INTENT, confidence=0.0),
                    route=Route.RAG_LLM,
                    reason="intent_engine_error",
                )
            try:
                _span.set_attribute(SpanAttributes.INTENT_NAME, routing.intent or "")
            except Exception:
                pass

        # The *decision* of whether this routing decision permits normal
        # generation is routed through PolicyEngine.evaluate_generation()
        # — the deterministic enforcement boundary — rather than checking
        # routing.route directly here. Only CLARIFICATION changes control
        # flow in Phase 2/3. HUMAN_HANDOFF/COMPLAINT, TOOL_ORCHESTRATOR
        # (appointment/order actions), and BILLING_WORKFLOW are classified
        # and carried as metadata (`routing.to_dict()` on the final
        # result) but fall through to the unchanged generation path below
        # UNLESS a ToolOrchestrator is configured and the route is
        # TOOL_ORCHESTRATOR (Phase 4, see below) — a billing workflow
        # still doesn't exist (out of scope for Phase 4), so BILLING_WORKFLOW
        # always falls through, and handoff outcomes for complaint/human-
        # handoff phrasing continue to be decided the same way they
        # already are — by the model's own response plus the existing,
        # separately-tested post-generation handoff_detector below —
        # rather than by a second, differently-tuned pre-generation
        # mechanism that risks diverging from it (the exact shared-fate
        # risk ADR-005 already flags for the two existing detectors).
        # Phase 10 (plan.md Step 10.12): an internal PolicyEngine failure
        # here fails toward CLARIFY -- the safe, already-existing
        # non-generating fallback (no LLM call, no tool execution, no
        # information disclosure) -- rather than defaulting to ALLOW or
        # raising an uncaught exception out of this generator.
        with _traced("conversation.policy_evaluate") as _span:
            try:
                generation_policy = self.policy_engine.evaluate_generation(routing)
            except Exception:
                generation_policy = PolicyDecision(
                    allowed=False,
                    policy="generation",
                    rule="POLICY_ENGINE_UNAVAILABLE",
                    action=Action.CLARIFY,
                    reason="Generation policy evaluation failed internally -- failing closed to clarification.",
                )
            try:
                _span.set_attribute(
                    SpanAttributes.POLICY_OUTCOME,
                    generation_policy.action.value
                    if hasattr(generation_policy.action, "value")
                    else str(generation_policy.action),
                )
                _span.set_attribute(SpanAttributes.POLICY_NAME, generation_policy.policy or "")
            except Exception:
                pass
        if generation_policy.action == Action.CLARIFY:
            if self.audit_logger is not None:
                from observability_models import EventType

                self.audit_logger.record(
                    EventType.POLICY_DENY,
                    outcome="denied",
                    actor=turn_actor,
                    request_id=request_id,
                    policy=generation_policy.policy,
                    reason=generation_policy.reason,
                    metadata={"intent": routing.intent},
                )
            if self.metrics is not None:
                self.metrics.increment("policy_denials_total")
            yield self.CLARIFICATION_RESPONSE
            yield self._final(
                self.CLARIFICATION_RESPONSE,
                is_handoff=False,
                confidence=0.0,
                latency_ms=0.0,
                retrieved_chunks=[],
                clinical_guard_triggered=False,
                intent=routing.to_dict(),
                policy=generation_policy.to_dict(),
            )
            return

        # 2.6. Tool-backed action (Phase 4) — only when a ToolOrchestrator
        # is configured (None preserves exact Phase 2/3 behavior) and the
        # classified intent maps to a registered tool action. The LLM is
        # never called anywhere in this branch: parameters are extracted
        # conservatively from the raw message (never guessed — see
        # _extract_action_parameters), wrapped in an untrusted
        # ActionProposal, and handed to ToolOrchestrator, which owns
        # every subsequent validation/policy/confirmation/execution
        # decision. Responses here are fixed templates, not generated
        # text, so nothing downstream of ToolOrchestrator's typed result
        # is ever reinterpreted through the model.
        if routing.route == Route.TOOL_ORCHESTRATOR and self.tool_orchestrator is not None:
            action_name = self._INTENT_TO_TOOL_ACTION.get(routing.intent)
            if action_name is not None:
                reply, tool_metadata = self._handle_tool_action(action_name, user_input, auth, confirmed, session)
                yield reply
                yield self._final(
                    reply,
                    is_handoff=False,
                    confidence=0.0,
                    latency_ms=0.0,
                    retrieved_chunks=[],
                    clinical_guard_triggered=False,
                    intent=routing.to_dict(),
                    tool=tool_metadata,
                )
                return

        # 3. Safe-path retrieval — a retrieval failure degrades to an
        # ungrounded turn rather than failing it (ARCHITECTURE.md §5
        # RAG Engine Error Handling: "no grounding available" is a
        # degraded-but-valid state, not a hard failure). Phase 10 (plan.md
        # Step 10.10) adds a bounded retry + circuit breaker in FRONT of
        # this same, unchanged fallback -- retrieval is always read-only,
        # so retrying it is always safe; the fallback itself (proceed
        # ungrounded) is never replaced with anything new, e.g. never
        # "ask the LLM to invent an answer instead."
        retrieved_chunks, degraded = self._retrieve_with_reliability(user_input, turn_actor, request_id)
        context_message = None
        if retrieved_chunks:
            relevant = [c for c in retrieved_chunks if c.score >= self.rag_score_threshold]
            if relevant:
                context_lines = "\n".join(f"- {c.title}: {c.content}" for c in relevant)
                context_message = {
                    "role": "system",
                    "content": (
                        "Reference information that may help answer the "
                        "customer's question, if relevant. Use it naturally "
                        "without mentioning that you looked anything up:\n" + context_lines
                    ),
                }

        # 4. Prompt/context assembly — the LLM Service never sees anything
        # but this already-assembled messages list (ARCHITECTURE.md Core
        # Principle 2, Communication Rule 5). Durable-memory context
        # (Phase 5, if configured) is injected as its OWN system message,
        # deliberately separate from RAG's context_message above — never
        # merged with public knowledge-base content (plan.md Step 5.9:
        # "do not mix private user memory with public RAG documents").
        # MemoryManager.get_allowed_context() is already policy-filtered
        # and user-scoped; nothing here re-checks or re-fetches raw data.
        messages = [{"role": "system", "content": self.system_prompt}]
        if context_message is not None:
            messages.append(context_message)
        if self.memory_manager is not None:
            memory_user_id = (auth.user_id if auth is not None else None) or (
                session.user_id if session is not None else None
            )
            if memory_user_id:
                memory_records = self.memory_manager.get_allowed_context(memory_user_id)
                # Phase 6 (only when configured): a second, content-level
                # pass over each record's *value* before it enters the
                # prompt, context "LLM_CONTEXT" — independent of
                # MemoryManager's own key-based/value-based filtering at
                # write time (Phase 5/6), since a record could have been
                # written before privacy_service was configured, or the
                # write-time and read-time policies could simply differ.
                # A BLOCK-worthy record is dropped entirely; a REDACT/
                # RESTRICT-worthy one is included with its value redacted.
                lines = []
                for r in memory_records:
                    value = r.value
                    if self.privacy_service is not None:
                        pii_decision = self.privacy_service.decide(value, context="LLM_CONTEXT")
                        if not pii_decision.allowed:
                            continue
                        if pii_decision.action in ("REDACT", "RESTRICT") and pii_decision.findings:
                            value = self.privacy_service.redact(value, list(pii_decision.findings))
                    lines.append(f"- {r.key}: {value}")
                if lines:
                    messages.append(
                        {
                            "role": "system",
                            "content": "Known preferences for this customer (not from the knowledge base):\n"
                            + "\n".join(lines),
                        }
                    )
        messages += normalized_history + [{"role": "user", "content": user_input}]

        # 5. LLM inference — a generation failure degrades to a fixed
        # apology-and-handoff response rather than a raw exception
        # reaching the client (docs/MODULES.md §2 Error Handling: "LLM
        # Service failure → a fixed apology-and-handoff response, never a
        # raw exception surfaced to the client"). Note: any text chunks
        # already yielded to the caller before the failure can't be
        # un-sent for streaming callers — the final dict still carries the
        # safe fallback response as the authoritative result.
        #
        # Phase 10 (plan.md Steps 10.7/10.9/10.16/10.17) adds three
        # things around this same, unchanged fallback:
        #   - a semaphore bounding concurrent generate_stream() calls
        #     against the single loaded model instance;
        #   - a circuit breaker that, when open, skips straight to the
        #     unchanged LLM_FAILURE_RESPONSE fallback without attempting
        #     generation at all;
        #   - a bounded retry, but ONLY before any chunk of the CURRENT
        #     attempt has been yielded to the caller -- once streaming has
        #     actually begun, a failure always falls through to the
        #     unchanged fallback below, since already-sent output can't be
        #     un-sent (retrying would duplicate/confuse a partial reply).
        chunks: list[str] = []
        final_llm = None

        # Phase 14 (Step 14.3.6): observational only -- every yield/return
        # below is unchanged from pre-Phase-14 behavior. The LLM response
        # text itself is NEVER recorded as a span attribute (Step 14.3.6
        # privacy requirement) -- only provider/model/latency/failover
        # metadata, all already-existing fields on `final_llm`.
        with _traced("conversation.llm_generate") as _llm_span:
            with self._generation_semaphore:
                if self.llm_circuit_breaker is not None and not self.llm_circuit_breaker.allow_request():
                    if self.audit_logger is not None:
                        from observability_models import EventType

                        self.audit_logger.record(
                            EventType.DEPENDENCY_FAILURE,
                            outcome="denied",
                            actor=turn_actor,
                            action="llm_generate",
                            request_id=request_id,
                            reason="Circuit breaker open for LLM generation.",
                        )
                    if self.metrics is not None:
                        self.metrics.increment("dependency_failures_total")
                        self.metrics.increment("requests_failed")
                    try:
                        _llm_span.set_attribute(
                            SpanAttributes.CIRCUIT_BREAKER_STATE, str(self.llm_circuit_breaker.state)
                        )
                        from opentelemetry.trace import StatusCode

                        _llm_span.set_status(StatusCode.ERROR, "DependencyUnavailableError")
                    except Exception:
                        pass
                    yield self.LLM_FAILURE_RESPONSE
                    yield self._final(
                        self.LLM_FAILURE_RESPONSE,
                        is_handoff=True,
                        confidence=1.0,
                        latency_ms=0.0,
                        retrieved_chunks=[c.to_dict() for c in retrieved_chunks],
                        clinical_guard_triggered=False,
                        degraded=True,
                        error="llm_generation_failed",
                    )
                    return

                attempt = 1
                while True:
                    stream_started = False
                    try:
                        for item in self.llm_service.generate_stream(messages):
                            stream_started = True
                            if isinstance(item, str):
                                chunks.append(item)
                                yield item
                            else:
                                final_llm = item
                        if self.llm_circuit_breaker is not None:
                            self.llm_circuit_breaker.record_success()
                        break
                    except Exception as _llm_exc:
                        if self.audit_logger is not None:
                            from observability_models import EventType

                            self.audit_logger.record(
                                EventType.DEPENDENCY_FAILURE,
                                outcome="failed",
                                actor=turn_actor,
                                action="llm_generate",
                                request_id=request_id,
                                reason="LLM generation raised an exception.",
                            )
                        if self.metrics is not None:
                            self.metrics.increment("dependency_failures_total")
                        if self.llm_circuit_breaker is not None:
                            self.llm_circuit_breaker.record_failure()

                        if stream_started:
                            # Already sent partial output this attempt --
                            # never safe to retry (would duplicate/confuse
                            # what the caller already received).
                            if self.metrics is not None:
                                self.metrics.increment("requests_failed")
                            try:
                                from opentelemetry.trace import StatusCode

                                _llm_span.set_status(StatusCode.ERROR, type(_llm_exc).__name__)
                                _llm_span.record_exception(
                                    _llm_exc, attributes={"exception.type": type(_llm_exc).__name__}
                                )
                            except Exception:
                                pass
                            yield self.LLM_FAILURE_RESPONSE
                            yield self._final(
                                self.LLM_FAILURE_RESPONSE,
                                is_handoff=True,
                                confidence=1.0,
                                latency_ms=0.0,
                                retrieved_chunks=[c.to_dict() for c in retrieved_chunks],
                                clinical_guard_triggered=False,
                                degraded=True,
                                error="llm_generation_failed",
                            )
                            return

                        decision = self.llm_retry_policy.decide(attempt=attempt, retryable=True)
                        if not decision.retryable:
                            if self.metrics is not None:
                                self.metrics.increment("requests_failed")
                            try:
                                _llm_span.set_attribute(SpanAttributes.RETRY_ATTEMPT, attempt)
                                from opentelemetry.trace import StatusCode

                                _llm_span.set_status(StatusCode.ERROR, type(_llm_exc).__name__)
                                _llm_span.record_exception(
                                    _llm_exc, attributes={"exception.type": type(_llm_exc).__name__}
                                )
                            except Exception:
                                pass
                            yield self.LLM_FAILURE_RESPONSE
                            yield self._final(
                                self.LLM_FAILURE_RESPONSE,
                                is_handoff=True,
                                confidence=1.0,
                                latency_ms=0.0,
                                retrieved_chunks=[c.to_dict() for c in retrieved_chunks],
                                clinical_guard_triggered=False,
                                degraded=True,
                                error="llm_generation_failed",
                            )
                            return

                        if self.audit_logger is not None:
                            from observability_models import EventType

                            self.audit_logger.record(
                                EventType.RETRY_ATTEMPT,
                                outcome="retrying",
                                actor=turn_actor,
                                action="llm_generate",
                                request_id=request_id,
                                metadata={"attempt": attempt + 1, "max_attempts": decision.max_attempts},
                            )
                        if self.metrics is not None:
                            self.metrics.increment("retries_total")
                        self._sleep_fn(decision.delay_seconds)
                        attempt += 1
                        continue

                # Reached only via the `break` above (successful generation).
                try:
                    _final_llm = final_llm or {}
                    _llm_span.set_attribute(SpanAttributes.LLM_PROVIDER, _final_llm.get("provider", "") or "")
                    _llm_span.set_attribute(SpanAttributes.LLM_MODEL, _final_llm.get("model", "") or "")
                    _llm_span.set_attribute(SpanAttributes.LLM_FAILOVER, bool(_final_llm.get("fallback_used", False)))
                    _llm_span.set_attribute(SpanAttributes.LATENCY_MS, float(_final_llm.get("latency_ms", 0.0) or 0.0))
                    if attempt > 1:
                        _llm_span.set_attribute(SpanAttributes.RETRY_ATTEMPT, attempt - 1)
                except Exception:
                    pass

        response_text = (final_llm or {}).get("text", "".join(chunks).strip())
        latency_ms = (final_llm or {}).get("latency_ms", 0.0)

        # 6. Handoff detection — deterministic, runs on the model's output
        # (ARCHITECTURE.md Communication Rule 4). Fails closed on an
        # internal error, same rationale as the clinical guard above.
        with _traced("conversation.handoff_detect") as _span:
            try:
                handoff_match = self.handoff_detector.score(response_text)
            except Exception:
                handoff_match = HandoffMatch(is_handoff=True, confidence=1.0)
            try:
                _span.set_attribute(SpanAttributes.HANDOFF_TRIGGERED, handoff_match.is_handoff)
                _span.set_attribute(SpanAttributes.HANDOFF_CONFIDENCE, handoff_match.confidence)
            except Exception:
                pass

        # PolicyEngine.evaluate_handoff() aggregates this turn's signals
        # (intent routing + the post-generation handoff_detector result)
        # into one auditable decision. Observability only here — it does
        # NOT gate is_handoff below, which still comes directly from
        # handoff_match exactly as before Phase 3, so this cannot weaken
        # or change existing handoff behavior. Phase 10: an internal
        # failure here therefore only degrades the recorded metadata, not
        # the turn's actual outcome.
        try:
            handoff_policy = self.policy_engine.evaluate_handoff(
                intent_routing=routing,
                post_generation_handoff=handoff_match,
            )
        except Exception:
            handoff_policy = PolicyDecision(
                allowed=not handoff_match.is_handoff,
                policy="handoff",
                rule="POLICY_ENGINE_UNAVAILABLE",
                action=Action.HANDOFF if handoff_match.is_handoff else Action.ALLOW,
                reason="Handoff policy evaluation failed internally.",
            )

        if self.audit_logger is not None:
            from observability_models import EventType

            if handoff_match.is_handoff:
                self.audit_logger.record(
                    EventType.SAFETY_HANDOFF,
                    outcome="handoff",
                    actor=turn_actor,
                    request_id=request_id,
                    policy=handoff_policy.policy,
                    reason=handoff_policy.reason,
                    metadata={"confidence": handoff_match.confidence},
                )
            else:
                self.audit_logger.record(
                    EventType.POLICY_ALLOW,
                    outcome="allowed",
                    actor=turn_actor,
                    request_id=request_id,
                    policy=handoff_policy.policy,
                )
        if self.metrics is not None:
            self.metrics.observe("generation_latency_ms", latency_ms)
            if handoff_match.is_handoff:
                self.metrics.increment("handoffs_total")

        # Demonstrative privacy-aware logging (Phase 6, only when
        # configured) — the one real structured log call this
        # application's live request path makes, proving the
        # PrivacySanitizingFilter boundary actually works end to end
        # (plan.md Step 6.13: "test the actual logging boundary," not
        # just the redact() function in isolation). See
        # privacy_logging.py's module docstring for why this codebase had
        # no logging framework to retrofit before this phase.
        if self.privacy_service is not None:
            log_event(
                get_privacy_aware_logger(self.privacy_service),
                "conversation_turn_completed",
                {"user_input": user_input, "response": response_text},
            )

        # 7 + 8. Response metadata.
        yield self._final(
            response_text,
            is_handoff=handoff_match.is_handoff,
            confidence=handoff_match.confidence,
            latency_ms=latency_ms,
            retrieved_chunks=[c.to_dict() for c in retrieved_chunks],
            clinical_guard_triggered=False,
            degraded=degraded,
            intent=routing.to_dict(),
            policy=handoff_policy.to_dict(),
        )

    # ── Tool-backed actions (Phase 4) ───────────────────────────────────────

    def _extract_action_parameters(self, action_name: str, user_input: str) -> dict:
        """
        Deterministic, conservative extraction from the current message
        only — never guesses. The only thing reliably present verbatim in
        text is an existing record's ID (matching this system's own mock-
        tool ID format); free-form fields a *new* booking needs (doctor,
        date, time) are never inferred from text, since this repository
        has no NLU/slot-filling component. BOOK_APPOINTMENT therefore
        always requires clarification in Phase 4 — an honest reflection
        of capability, not a bug (see PHASE_4 report's technical debt).
        """
        if action_name == "BOOK_APPOINTMENT":
            return {}
        match = self._RECORD_ID_PATTERN.search(user_input)
        if not match:
            return {}
        id_field = "order_id" if action_name == "ORDER_LOOKUP" else "appointment_id"
        return {id_field: match.group(0)}

    def _missing_info_response(self, action_name: str, spec) -> str:
        if action_name == "BOOK_APPOINTMENT":
            return "I'd be happy to help book that — could you tell me the doctor, date, and time you'd like?"
        required = list(spec.required_params) if spec is not None else []
        if required:
            friendly = required[0].replace("_", " ")
            return f"Could you give me the {friendly} so I can help with that?"
        return self.CLARIFICATION_RESPONSE

    @staticmethod
    def _tool_success_response(action_name: str, result_data: Optional[dict]) -> str:
        data = result_data or {}
        if action_name == "BOOK_APPOINTMENT":
            return f"You're all set — your appointment is booked for {data.get('date')} at {data.get('time')}."
        if action_name == "CANCEL_APPOINTMENT":
            return "Your appointment has been cancelled."
        if action_name == "RESCHEDULE_APPOINTMENT":
            return f"Your appointment has been rescheduled to {data.get('date')} at {data.get('time')}."
        if action_name == "ORDER_LOOKUP":
            return f"Your order is currently {data.get('status')}."
        return "That action completed successfully."

    def _handle_tool_action(
        self,
        action_name: str,
        user_input: str,
        auth: Optional[AuthContext],
        confirmed: bool,
        session=None,
    ) -> tuple[str, dict]:
        """
        Untrusted proposal -> ToolOrchestrator.validate_proposal() ->
        ToolOrchestrator.invoke(). Returns (reply_text, result_metadata).
        `auth`/`confirmed` are passed straight through from handle_turn()'s
        own trusted parameters — never derived from `user_input` here.

        When `session` (Phase 5, a trusted SessionState) is given: a
        confirmation_required outcome persists the pending action/params
        to that session (workflow_state="AWAITING_CONFIRMATION") so a
        later turn's "yes" can resolve it via _execute_pending_action();
        any other outcome clears whatever pending state existed.
        """
        params = self._extract_action_parameters(action_name, user_input)
        proposal = ActionProposal(action=action_name, parameters=params)

        try:
            tool_request = self.tool_orchestrator.validate_proposal(proposal)
        except ToolValidationError:
            spec = self.tool_orchestrator.get_action_spec(action_name)
            return self._missing_info_response(action_name, spec), {
                "status": "missing_information",
                "action": action_name,
            }

        # Re-issue with the caller's trusted confirmation state -- never
        # the proposal's own state, which validate_proposal() always sets
        # to confirmed=False by construction. `owner_user_id` (Phase 7) is
        # injected here, AFTER validate_proposal() already ran, from the
        # trusted `auth` context — never from the untrusted proposal's own
        # parameters, which is why it isn't part of BOOK_APPOINTMENT's
        # public params_schema (an untrusted proposal could otherwise
        # claim ownership on another user's behalf). Only meaningful for
        # BOOK_APPOINTMENT: whoever books trivially owns what they create,
        # no lookup needed. CANCEL/RESCHEDULE's resource_owner_user_id is
        # left unset here — see PHASE_7 report's technical debt for why
        # automatic ownership lookup for those isn't wired end-to-end yet.
        trusted_params = dict(tool_request.params)
        if action_name == "BOOK_APPOINTMENT" and auth is not None:
            trusted_params["owner_user_id"] = auth.user_id
        trusted_request = ToolRequest(
            action=tool_request.action,
            params=trusted_params,
            session_id=tool_request.session_id,
            confirmed=bool(confirmed),
            request_id=tool_request.request_id,
        )
        result = self.tool_orchestrator.invoke(trusted_request, auth=auth or ANONYMOUS_CONTEXT)

        if session is not None and self.session_manager is not None:
            if result.status == "confirmation_required":
                self.session_manager.update_session(
                    session.session_id,
                    workflow_state="AWAITING_CONFIRMATION",
                    pending_action=action_name,
                    pending_parameters=tool_request.params,
                )
            elif result.error == "AUTHENTICATION_REQUIRED":
                self.session_manager.update_session(
                    session.session_id,
                    workflow_state="AWAITING_AUTHENTICATION",
                    pending_action=action_name,
                    pending_parameters=tool_request.params,
                )
            else:
                self.session_manager.update_session(
                    session.session_id,
                    workflow_state=None,
                    pending_action=None,
                    pending_parameters={},
                )

        if result.status == "confirmation_required":
            return self.TOOL_CONFIRMATION_RESPONSE, result.to_dict()
        if result.error == "AUTHENTICATION_REQUIRED":
            return "For your security, could you please tell me your 4-digit PIN?", result.to_dict()
        if result.status in ("policy_denied",) or result.error in ("INSUFFICIENT_PERMISSIONS",):
            return self.TOOL_UNAVAILABLE_RESPONSE, result.to_dict()
        if result.success:
            return self._tool_success_response(action_name, result.result), result.to_dict()
        return self.TOOL_FAILURE_RESPONSE, result.to_dict()

    def _execute_pending_action(self, session, auth: Optional[AuthContext]) -> tuple[str, dict]:
        """
        Re-invokes a session's pending tool action with confirmed=True —
        called only from handle_turn()'s "2.4" step, only after this
        application's own deterministic _AFFIRMATIVE_PATTERN matched the
        current message (or the caller passed confirmed=True explicitly).
        Never re-runs validate_proposal() (the parameters were already
        validated when the pending action was first proposed) — but still
        goes through the full policy/auth/confirmation gate sequence in
        invoke(), since PolicyEngine remains authoritative regardless of
        how confirmation was established.

        Phase 11 fix (plan.md Step 11.13/11.22): the pending action is
        read AND cleared via SessionManager.try_consume_pending_confirmation()
        -- a single atomic operation -- rather than a separate get/update
        pair. Two concurrent "yes" replies for the same session can no
        longer both observe and execute the same pending action; the
        loser of the race gets `None` here and must not execute anything.
        """
        user_id = auth.user_id if auth is not None else None
        consumed = self.session_manager.try_consume_pending_confirmation(session.session_id, user_id=user_id)
        if consumed is None:
            # Already consumed by a concurrent request, or the session
            # changed underneath us -- never re-execute; treat this as
            # "there is nothing left to confirm," the safe default.
            return self.TOOL_UNAVAILABLE_RESPONSE, {"status": "already_consumed"}

        action_name, params = consumed
        trusted_request = ToolRequest(action=action_name, params=params, session_id=session.session_id, confirmed=True)
        result = self.tool_orchestrator.invoke(trusted_request, auth=auth or ANONYMOUS_CONTEXT)

        if result.success:
            return self._tool_success_response(action_name, result.result), result.to_dict()
        if result.status in ("policy_denied",) or result.error in (
            "AUTHENTICATION_REQUIRED",
            "INSUFFICIENT_PERMISSIONS",
        ):
            return self.TOOL_UNAVAILABLE_RESPONSE, result.to_dict()
        return self.TOOL_FAILURE_RESPONSE, result.to_dict()

    def _execute_pending_authentication(self, session, auth: Optional[AuthContext]) -> tuple[str, dict]:
        """
        Re-invokes a session's pending tool action with a newly authenticated context.
        """
        user_id = auth.user_id if auth is not None else None
        consumed = self.session_manager.try_consume_pending_authentication(session.session_id, user_id=user_id)
        if consumed is None:
            return self.TOOL_UNAVAILABLE_RESPONSE, {"status": "already_consumed"}

        action_name, params = consumed

        # When a user authenticates, they shouldn't automatically confirm the action.
        # But wait, if they were asked to authenticate to do an action, do we ask for confirmation immediately?
        # A tool might still require confirmation. We invoke it with confirmed=False first,
        # so it can return confirmation_required if it's destructive.
        trusted_request = ToolRequest(action=action_name, params=params, session_id=session.session_id, confirmed=False)
        result = self.tool_orchestrator.invoke(trusted_request, auth=auth or ANONYMOUS_CONTEXT)

        if session is not None and self.session_manager is not None:
            if result.status == "confirmation_required":
                self.session_manager.update_session(
                    session.session_id,
                    workflow_state="AWAITING_CONFIRMATION",
                    pending_action=action_name,
                    pending_parameters=trusted_request.params,
                )
            else:
                self.session_manager.update_session(
                    session.session_id,
                    workflow_state=None,
                    pending_action=None,
                    pending_parameters={},
                )

        if result.status == "confirmation_required":
            return self.TOOL_CONFIRMATION_RESPONSE, result.to_dict()
        if result.status in ("policy_denied",) or result.error in (
            "AUTHENTICATION_REQUIRED",
            "INSUFFICIENT_PERMISSIONS",
        ):
            return self.TOOL_UNAVAILABLE_RESPONSE, result.to_dict()
        if result.success:
            return self._tool_success_response(action_name, result.result), result.to_dict()
        return self.TOOL_FAILURE_RESPONSE, result.to_dict()

    # ── Reliability (Phase 10) ──────────────────────────────────────────────

    def _retrieve_with_reliability(
        self, user_input: str, actor: Optional[str], request_id: Optional[str]
    ) -> tuple[list, bool]:
        """
        Returns (retrieved_chunks, degraded). Retrieval is always
        read-only, so a bounded retry is always safe -- this never
        changes the existing degraded-fallback contract (empty chunks,
        ungrounded generation continues), only adds a retry/circuit-
        breaker gate in front of the exact same fallback.
        """
        if self.retriever is None:
            return [], False

        # Phase 14 (Step 14.3.5): observational only -- every return below
        # is unchanged from pre-Phase-14 behavior, this only annotates the
        # span each exit path already takes.
        with _traced("conversation.rag_retrieve") as _span:

            def _record(chunks_len: int, degraded: bool, retry_attempt: int = 0) -> None:
                try:
                    _span.set_attribute(SpanAttributes.RAG_CHUNKS_RETRIEVED, chunks_len)
                    _span.set_attribute(SpanAttributes.RAG_DEGRADED, degraded)
                    if retry_attempt > 0:
                        _span.set_attribute(SpanAttributes.RETRY_ATTEMPT, retry_attempt)
                    if self.rag_circuit_breaker is not None:
                        _span.set_attribute(SpanAttributes.CIRCUIT_BREAKER_STATE, str(self.rag_circuit_breaker.state))
                except Exception:
                    pass

            if self.rag_circuit_breaker is not None and not self.rag_circuit_breaker.allow_request():
                if self.audit_logger is not None:
                    from observability_models import EventType

                    self.audit_logger.record(
                        EventType.DEPENDENCY_FAILURE,
                        outcome="denied",
                        actor=actor,
                        action="rag_retrieve",
                        request_id=request_id,
                        reason="Circuit breaker open for RAG retrieval.",
                    )
                if self.metrics is not None:
                    self.metrics.increment("dependency_failures_total")
                _record(0, True)
                return [], True

            attempt = 1
            while True:
                try:
                    chunks = self.retriever.retrieve(user_input, top_k=self.rag_top_k)
                except Exception:
                    if self.audit_logger is not None:
                        from observability_models import EventType

                        self.audit_logger.record(
                            EventType.DEPENDENCY_FAILURE,
                            outcome="failed",
                            actor=actor,
                            action="rag_retrieve",
                            request_id=request_id,
                            reason="RAG retrieval raised an exception.",
                        )
                    if self.metrics is not None:
                        self.metrics.increment("dependency_failures_total")
                    decision = self.rag_retry_policy.decide(attempt=attempt, retryable=True)
                    if not decision.retryable:
                        if self.rag_circuit_breaker is not None:
                            self.rag_circuit_breaker.record_failure()
                        _record(0, True, retry_attempt=attempt - 1)
                        return [], True
                    if self.audit_logger is not None:
                        from observability_models import EventType

                        self.audit_logger.record(
                            EventType.RETRY_ATTEMPT,
                            outcome="retrying",
                            actor=actor,
                            action="rag_retrieve",
                            request_id=request_id,
                            metadata={"attempt": attempt + 1, "max_attempts": decision.max_attempts},
                        )
                    if self.metrics is not None:
                        self.metrics.increment("retries_total")
                    self._sleep_fn(decision.delay_seconds)
                    attempt += 1
                    continue
                else:
                    if self.rag_circuit_breaker is not None:
                        self.rag_circuit_breaker.record_success()
                    _record(len(chunks), False, retry_attempt=attempt - 1)
                    return chunks, False

    # ── Helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _normalize_history(history) -> list[dict]:
        """
        Defensively filter conversation history to well-formed
        {"role": str, "content": str} turns, so a malformed entry from a
        caller (wrong type, missing key) degrades gracefully instead of
        crashing prompt assembly / tokenizer.apply_chat_template. Silently
        drops anything that doesn't match — this is caller input, not a
        trusted internal structure.
        """
        normalized = []
        try:
            iterator = iter(history)
        except TypeError:
            return []
        for turn in iterator:
            if isinstance(turn, dict) and isinstance(turn.get("role"), str) and isinstance(turn.get("content"), str):
                normalized.append({"role": turn["role"], "content": turn["content"]})
        return normalized

    @staticmethod
    def _final(
        response: str,
        is_handoff: bool,
        confidence: float,
        latency_ms: float,
        retrieved_chunks: list,
        clinical_guard_triggered: bool,
        *,
        degraded: bool = False,
        error: Optional[str] = None,
        intent: Optional[dict] = None,
        policy: Optional[dict] = None,
        tool: Optional[dict] = None,
    ) -> dict:
        return {
            "response": response,
            "is_handoff": is_handoff,
            "handoff_confidence": confidence,
            "latency_ms": latency_ms,
            "retrieved_chunks": retrieved_chunks,
            "clinical_guard_triggered": clinical_guard_triggered,
            "degraded": degraded,
            "error": error,
            "intent": intent,
            "tool": tool,
            "policy": policy,
        }


# ── Factory ──────────────────────────────────────────────────────────────────


def _load_rag_config() -> dict:
    if not _CONFIG_PATH.exists():
        return {}
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("rag", {}) or {}


class PersistenceRepositories:
    """
    Return value of resolve_persistence_repositories() (Phase 12.10).
    `database` is None unless PERSISTENCE_MODE requested production
    persistence; the four repository attributes are each either a real
    Postgres-backed repository (production, database reachable) or None
    (dev/test) — None is exactly what SessionManager/MemoryManager/
    AuditLogger/ToolOrchestrator already treat as "use my in-memory
    default," so passing these straight through as constructor
    `repository=`/`idempotency_repository=` arguments requires no change
    to any of those classes.
    """

    def __init__(self, database=None, session=None, memory=None, audit=None, idempotency=None):
        self.database = database
        self.session = session
        self.memory = memory
        self.audit = audit
        self.idempotency = idempotency


def resolve_persistence_repositories(persistence_enabled: bool = True) -> PersistenceRepositories:
    """
    Resolves which repositories build_conversation_manager()'s managers
    should be constructed with (plan.md Step 12.10's integration target:
    ConversationManager -> Service -> Repository Interface -> PostgreSQL).

    Mode boundary mirrors src/api/server.py's AUTH_MODE exactly:

    PERSISTENCE_MODE unset/"dev" (default) -> db.load_database_config()
        resolves to the in-memory-equivalent SQLite default and
        .is_production() is False, so every attribute on the returned
        PersistenceRepositories stays None and every manager falls
        through to its existing in-memory repository default -- zero
        behavior change for every caller that doesn't set
        PERSISTENCE_MODE, which is every one of this repository's 662+
        pre-Phase-12.10 tests (none of which call build_conversation_manager()
        at all -- it requires a real LLM/torch -- but this function itself
        has no such dependency and is unit-tested directly).

    PERSISTENCE_MODE=production (or "postgres"/"postgresql") ->
        Database.health_check() is called HERE, synchronously, before
        this function returns. A failure raises DatabaseUnavailableError
        and propagates -- this function does NOT catch it and fall back
        to in-memory repositories. Per Step 12.10's explicit requirement:
        "If PostgreSQL is unavailable -> safe failure, NOT silently
        switch to in-memory storage." A production deployment that can't
        reach its database must fail to start, not quietly run
        unpersisted.

    `persistence_enabled=False` (test-only escape hatch, mirrors every
    other `_enabled` flag this factory already has) skips this
    resolution entirely regardless of PERSISTENCE_MODE, returning an
    all-None PersistenceRepositories — "tests may continue using
    in-memory fakes where appropriate" (this step's own instruction);
    nothing forces a unit test to require PostgreSQL.
    """
    if not persistence_enabled:
        return PersistenceRepositories()

    from db import load_database_config  # noqa: E402

    db_config = load_database_config()
    if not db_config.is_production():
        return PersistenceRepositories()

    from db import Database  # noqa: E402

    database = Database(db_config)
    database.health_check()  # raises DatabaseUnavailableError -- deliberately not caught here

    from audit_repository_postgres import PostgresAuditRepository  # noqa: E402
    from idempotency_repository_postgres import PostgresIdempotencyRepository  # noqa: E402
    from memory_repository_postgres import PostgresMemoryRepository  # noqa: E402
    from session_repository_postgres import PostgresSessionRepository  # noqa: E402

    return PersistenceRepositories(
        database=database,
        session=PostgresSessionRepository(database),
        memory=PostgresMemoryRepository(database),
        audit=PostgresAuditRepository(database),
        idempotency=PostgresIdempotencyRepository(database),
    )


def build_conversation_manager(
    base_model_name: str = "Qwen/Qwen2.5-0.5B-Instruct",
    adapter_path: Optional[str] = None,
    merge_weights: bool = True,
    max_new_tokens: int = 200,
    temperature: float = 0.7,
    top_p: float = 0.9,
    repetition_penalty: float = 1.1,
    auto_resolve_adapter: bool = True,
    handoff_config_path: Optional[str] = None,
    rag_enabled: bool = True,
    clinical_config_path: Optional[str] = None,
    intent_config_path: Optional[str] = None,
    tool_orchestrator_enabled: bool = True,
    session_enabled: bool = True,
    memory_enabled: bool = True,
    privacy_enabled: bool = True,
    observability_enabled: bool = True,
    audit_logger=None,
    metrics=None,
    security_detector=None,
    reliability_enabled: bool = True,
    persistence_enabled: bool = True,
    llm_provider=None,
    caller_pin: Optional[str] = None,
) -> ConversationManager:
    """
    Build a fully-wired ConversationManager: resolve LLMService/LLMProvider
    (either an injected BaseLLMProvider, an auto-resolved Claude/Gemini
    fallback provider, or the local model + adapter) and, if RAG is enabled,
    a Retriever plus a second HandoffDetector instance configured as the
    clinical guard (see ConversationManager's module docstring for why that's
    the same class as the handoff detector, not a separate one — ADR-005).

    This is the single construction path both the live API
    (src/api/server.py) and the backward-compatible VoiceAssistantInference
    facade (src/inference/predict.py) use, so there is exactly one place
    that wires collaborators together. Deferred imports (LLMService,
    Retriever) mean importing this module does not require torch/faiss/
    sentence-transformers unless this function is actually called — same
    convention the pre-extraction predict.py used for the RAG imports.
    """
    if _INFERENCE_DIR not in sys.path:
        sys.path.insert(0, _INFERENCE_DIR)

    # Phase 18: an explicit argument always wins; otherwise resolve from
    # TELEPHONY_MOCK_PIN. Unset (the safe default for any real
    # deployment) leaves caller_pin=None, which ConversationManager
    # treats as "fail closed to human handoff" -- see its own docstring
    # and handle_turn()'s AWAITING_AUTHENTICATION step.
    if caller_pin is None:
        caller_pin = os.environ.get("TELEPHONY_MOCK_PIN") or None

    # Note: which max_concurrent_generations default applies (below, via
    # _resolve_max_concurrent_generations()) is decided by llm_service's
    # actual type, not by which of these branches constructed it -- see
    # _llm_service_is_safe_for_concurrent_generation()'s docstring. A
    # caller-injected llm_provider therefore gets the correct default
    # for whatever it actually is, including a real Claude/Gemini
    # provider injected directly.
    if llm_provider is not None:
        llm_service = llm_provider
    elif os.environ.get("LLM_PROVIDER") in ("fallback", "free_fallback", "claude", "gemini", "groq") or (
        os.environ.get("LLM_PROVIDER") != "local"
        and (
            os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GROQ_API_KEY")
        )
    ):
        from llm_provider import build_llm_provider

        llm_service = build_llm_provider(audit_logger=audit_logger, metrics=metrics)
    else:
        from llm_service import LLMService  # noqa: E402

        llm_service = LLMService(
            base_model_name=base_model_name,
            adapter_path=adapter_path,
            merge_weights=merge_weights,
            max_new_tokens=max_new_tokens,
            temperature=temperature,
            top_p=top_p,
            repetition_penalty=repetition_penalty,
            auto_resolve_adapter=auto_resolve_adapter,
        )

    handoff_detector = HandoffDetector(config_path=handoff_config_path)
    intent_engine = IntentEngine(config_path=intent_config_path)
    policy_engine = PolicyEngine()

    privacy_service = PrivacyService(policy_engine) if privacy_enabled else None

    # Phase 12.10: resolve PostgreSQL-backed repositories BEFORE
    # AuditLogger/SessionManager/MemoryManager/ToolOrchestrator are
    # constructed below, so each can be handed its persisted repository
    # at construction time -- the same "single wiring point" discipline
    # this factory already uses for policy_engine/privacy_service.
    # Extracted to a module-level function (see its own docstring) so it
    # can be unit-tested directly -- this factory as a whole requires a
    # real LLM/torch and cannot be invoked in this offline test
    # environment, but the persistence-wiring decision itself has nothing
    # to do with the LLM and does not need one to verify.
    persistence = resolve_persistence_repositories(persistence_enabled)

    # Phase 8: AuditLogger/MetricsRegistry/SecurityEventDetector are
    # constructed once here (unless the caller already has instances to
    # share, e.g. src/api/server.py's DevelopmentAuthenticationProvider,
    # which must emit into the same audit trail as everything else) and
    # threaded into every collaborator that accepts them -- same "single
    # wiring point" convention this factory already uses for
    # policy_engine/privacy_service above.
    if observability_enabled and audit_logger is None:
        from audit import AuditLogger  # noqa: E402

        audit_logger = AuditLogger(privacy_service=privacy_service, repository=persistence.audit)
    if observability_enabled and metrics is None:
        from metrics import MetricsRegistry  # noqa: E402

        metrics = MetricsRegistry()
    if observability_enabled and security_detector is None:
        from audit import SecurityEventDetector  # noqa: E402

        security_detector = SecurityEventDetector(audit_logger)

    # Phase 10 (plan.md Step 10.2): real, bounded RetryPolicy/CircuitBreaker
    # instances for the live system -- reliability_enabled=False (or
    # constructing ConversationManager/ToolOrchestrator directly, as every
    # test in this repository does) preserves exact pre-Phase-10 behavior
    # (no retries, no circuit breaking), same "additive, optional,
    # enabled-by-default-in-the-factory-only" convention Phase 6/7/8 used.
    rag_retry_policy = rag_circuit_breaker = None
    llm_retry_policy = llm_circuit_breaker = None
    tool_retry_policy = tool_circuit_breaker = None
    # None (not 1) when reliability tuning is off entirely, or when
    # reliability.yaml's value is still at its own default (1) --
    # either way, "no explicit choice was made," so
    # ConversationManager's own _resolve_max_concurrent_generations()
    # auto-detects the effective value from llm_service's type instead
    # of this factory guessing at it. An operator who has deliberately
    # configured something other than 1 always gets exactly that value.
    max_concurrent_generations = None
    if reliability_enabled:
        from reliability import (
            CircuitBreaker,  # noqa: E402
            RetryPolicy,  # noqa: E402
        )
        from reliability_config import load_reliability_config  # noqa: E402

        rel = load_reliability_config()
        rag_retry_policy = RetryPolicy(
            max_attempts=rel.rag.max_retries + 1,
            base_delay_seconds=rel.rag.base_delay_seconds,
            max_delay_seconds=rel.rag.max_delay_seconds,
        )
        rag_circuit_breaker = CircuitBreaker(
            failure_threshold=rel.rag.circuit_failure_threshold,
            recovery_timeout_seconds=rel.rag.circuit_recovery_timeout_seconds,
        )
        llm_retry_policy = RetryPolicy(
            max_attempts=rel.llm.max_retries + 1,
            base_delay_seconds=rel.llm.base_delay_seconds,
            max_delay_seconds=rel.llm.max_delay_seconds,
        )
        llm_circuit_breaker = CircuitBreaker(
            failure_threshold=rel.llm.circuit_failure_threshold,
            recovery_timeout_seconds=rel.llm.circuit_recovery_timeout_seconds,
        )
        tool_retry_policy = RetryPolicy(
            max_attempts=rel.tools.max_retries + 1,
            base_delay_seconds=rel.tools.base_delay_seconds,
            max_delay_seconds=rel.tools.max_delay_seconds,
        )
        tool_circuit_breaker = CircuitBreaker(
            failure_threshold=rel.tools.circuit_failure_threshold,
            recovery_timeout_seconds=rel.tools.circuit_recovery_timeout_seconds,
        )
        # 1 is reliability_config.py's own unconfigured default -- treat
        # it the same as "not set" (None) so the constructor auto-
        # detects; anything else is an explicit operator choice, passed
        # straight through.
        max_concurrent_generations = None if rel.max_concurrent_generations == 1 else rel.max_concurrent_generations

    tool_orchestrator = None
    if tool_orchestrator_enabled:
        # Same-directory import (src/agent/) -- see the module-level
        # intent_engine/policy_engine imports above for the same pattern.
        from mock_tools import build_default_tool_registry  # noqa: E402

        tool_orchestrator = ToolOrchestrator(
            build_default_tool_registry(),
            policy_engine,
            privacy_service=privacy_service,
            audit_logger=audit_logger,
            security_detector=security_detector,
            retry_policy=tool_retry_policy,
            circuit_breaker=tool_circuit_breaker,
            metrics=metrics,
            idempotency_repository=persistence.idempotency,
        )

    # session_id is caller-supplied (e.g. a future API layer's session
    # header) — handle_turn() simply no-ops session-backed behavior when
    # no session_id is passed, so enabling these by default is harmless
    # for every existing caller (src/api/server.py doesn't pass one yet).
    session_manager = (
        SessionManager(repository=persistence.session, audit_logger=audit_logger, security_detector=security_detector)
        if session_enabled
        else None
    )
    memory_manager = (
        MemoryManager(
            policy_engine,
            repository=persistence.memory,
            privacy_service=privacy_service,
            audit_logger=audit_logger,
            security_detector=security_detector,
        )
        if memory_enabled
        else None
    )

    rag_config = _load_rag_config()
    retriever = None
    clinical_guard = None
    if rag_enabled and bool(rag_config.get("enabled", True)):
        rag_dir = str(Path(__file__).resolve().parents[1] / "rag")
        if rag_dir not in sys.path:
            sys.path.insert(0, rag_dir)
        from retriever import Retriever  # noqa: E402

        retriever = Retriever(
            knowledge_dir=rag_config.get("knowledge_dir"),
            index_dir=rag_config.get("index_dir"),
            embedding_model=rag_config.get("embedding_model", "sentence-transformers/all-MiniLM-L6-v2"),
        )
        clinical_guard = HandoffDetector(
            config_path=clinical_config_path or rag_config.get("clinical_triggers_path") or str(_CLINICAL_CONFIG_PATH)
        )

    return ConversationManager(
        llm_service=llm_service,
        retriever=retriever,
        clinical_guard=clinical_guard,
        handoff_detector=handoff_detector,
        intent_engine=intent_engine,
        policy_engine=policy_engine,
        tool_orchestrator=tool_orchestrator,
        session_manager=session_manager,
        memory_manager=memory_manager,
        privacy_service=privacy_service,
        audit_logger=audit_logger,
        metrics=metrics,
        rag_top_k=int(rag_config.get("top_k", 3)),
        rag_score_threshold=float(rag_config.get("score_threshold", 0.35)),
        rag_retry_policy=rag_retry_policy,
        rag_circuit_breaker=rag_circuit_breaker,
        llm_retry_policy=llm_retry_policy,
        llm_circuit_breaker=llm_circuit_breaker,
        max_concurrent_generations=max_concurrent_generations,
        database=persistence.database,
        caller_pin=caller_pin,
    )

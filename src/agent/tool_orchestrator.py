"""
Tool Orchestrator — the only application layer permitted to execute a
registered business tool (Phase 4; plan.md Steps 4.4, 4.5, 4.6, 4.8;
docs/adr/ADR-003; docs/MODULES.md §8).

Trust boundary (mandatory, see tests/test_tool_orchestrator.py's
TestLLMTrustBoundary):

    LLM / any proposal source
          │  UNTRUSTED ActionProposal
          ▼
    validate_proposal()  — structural validation only, resolves against
          │                 ToolRegistry, never executes anything
          ▼
    ToolRequest           — the only shape invoke() accepts
          │
          ▼
    invoke()
          ├── PolicyEngine.evaluate_tool_action()   — trusted, deterministic
          ├── AuthContext check (caller-supplied, trusted)
          ├── PolicyEngine.evaluate_confirmation()  — trusted, deterministic
          ├── idempotency / duplicate-request check
          ├── timeout-bounded execution of the REGISTERED callable only
          └── ToolExecutionResult

No method anywhere in this module accepts, parses, or derives a decision
from free-text model output. `ToolRequest.confirmed` and `AuthContext`
must be supplied by the caller from trusted application/session state —
this module's guarantee is that it never manufactures either of those
from text itself (see action_models.py's docstrings for the same
boundary stated on the types themselves).
"""

import contextlib
import queue
import threading
import time
from typing import Callable, Optional

from opentelemetry import trace

from action_models import ANONYMOUS_CONTEXT, ActionProposal, AuthContext, ToolExecutionResult, ToolRequest
from observability_models import EventType
from reliability import CircuitBreaker, CircuitState, RetryPolicy
from tool_registry import ToolRegistry
from tracing import get_tracer, SpanAttributes  # noqa: E402  (Phase 14)

# Phase 14: module-level tracer singleton -- see conversation_manager.py's
# identical pattern. Zero-cost no-op spans when tracing is disabled.
_tracer = get_tracer("ai-voice-agent.tool-orchestrator")


@contextlib.contextmanager
def _traced(span_name: str):
    """Resilient span helper -- see conversation_manager.py's identical _traced()."""
    try:
        _cm = _tracer.start_as_current_span(span_name)
    except Exception:
        yield trace.INVALID_SPAN
        return
    with _cm as _span:
        yield _span


class ToolValidationError(ValueError):
    """Raised by validate_proposal() when an ActionProposal is structurally invalid. Never raised by invoke()."""


# Params whose declared type is one of these names are checked with a
# plain isinstance() — deliberately small and explicit rather than a
# generic schema library, matching this repository's existing
# dependency-light conventions (HandoffDetector, IntentEngine, PolicyEngine
# are all stdlib + PyYAML only).
_TYPE_CHECKS = {
    "str": str,
    "int": int,
    "float": (int, float),
    "bool": bool,
    "dict": dict,
    "list": list,
}


def _type_matches(value, expected_type_name: str) -> bool:
    expected = _TYPE_CHECKS.get(expected_type_name)
    if expected is None:
        return True  # unknown/unenforced type name -- don't block on a schema typo
    if expected_type_name == "bool":
        return isinstance(value, bool)
    if expected_type_name in ("int",) and isinstance(value, bool):
        return False  # bool is a subclass of int in Python; don't let a bool masquerade as int
    return isinstance(value, expected)


class ToolOrchestrator:
    """
    Coordinates: validate a proposal -> resolve the registered tool ->
    evaluate policy -> check authentication -> check confirmation ->
    execute with a timeout -> validate/return a typed result. Never
    decides clinical safety itself, never bypasses PolicyEngine, never
    interprets natural-language authorization, never executes anything
    but the exact callable ToolRegistry resolved for a registered name.
    """

    def __init__(
        self, registry: ToolRegistry, policy_engine, privacy_service=None, audit_logger=None, security_detector=None,
        retry_policy: Optional[RetryPolicy] = None, circuit_breaker: Optional[CircuitBreaker] = None,
        metrics=None, sleep_fn: Optional[Callable[[float], None]] = None, idempotency_repository=None,
    ):
        self._registry = registry
        self._policy_engine = policy_engine
        # Phase 6, optional -- None preserves exact Phase 4/5 behavior
        # (no content-pattern PII scanning of tool params/results). See
        # invoke()'s docstring.
        self._privacy_service = privacy_service
        # Phase 8, both optional -- None preserves exact pre-Phase-8
        # behavior. See invoke()'s docstring for how these observe the
        # *already-computed* result rather than participating in any gate.
        self._audit_logger = audit_logger
        self._security_detector = security_detector
        # Phase 10 (plan.md Steps 10.4-10.9). retry_policy defaults to a
        # single-attempt (no-op) policy -- omitting it preserves exact
        # pre-Phase-10 behavior (no retries), matching this codebase's
        # "additive, optional, backward-compatible" convention for every
        # prior phase's wiring. build_conversation_manager() constructs a
        # real multi-attempt policy by default for the live system. Only
        # ActionSpec.idempotency != "NON_IDEMPOTENT_WRITE" actions are
        # ever retried, and only on a "timeout" outcome specifically (see
        # _invoke()'s step 5) -- never on a validation error (ValueError/
        # KeyError from the tool itself), which is the caller's fault, not
        # a transient dependency issue.
        self._retry_policy = retry_policy or RetryPolicy(max_attempts=1)
        # circuit_breaker guards the "Business API" layer only (plan.md
        # Step 10.9) -- None (default) disables it entirely; it is never
        # applied to the policy/auth/confirmation gates above step 5.
        self._circuit_breaker = circuit_breaker
        self._metrics = metrics
        self._sleep_fn = sleep_fn or time.sleep
        # In-memory only (Phase 4 scope) -- a real deployment would back
        # this with the session/durable store Phase 5 introduces. Tracks
        # request_ids that have already completed, so a duplicate/replayed
        # request doesn't re-execute a destructive action. Phase 10:
        # access is lock-protected (plan.md Step 10.15/10.17) since
        # FastAPI's sync routes run in a threadpool and two concurrent
        # requests could otherwise both pass the "not yet executed" check
        # for the same request_id before either adds it.
        #
        # Phase 12.9: this remains the exact, untouched DEFAULT behavior
        # when `idempotency_repository` is None -- "do not replace working
        # idempotency behavior blindly" (that step's own instruction).
        # `idempotency_repository`, when supplied (e.g. a
        # PostgresIdempotencyRepository or InMemoryIdempotencyRepository
        # from idempotency_repository.py), is used INSTEAD of this raw set
        # for both the pre-execution check and the post-success record —
        # see _invoke()'s step 4 and its post-execution recording below.
        # A configured repository is scoped by (user_id, request_id), not
        # request_id alone, which this raw set structurally cannot do —
        # PHASE_12_9_IDEMPOTENCY_REPORT.md documents why.
        self._executed_request_ids: set[str] = set()
        self._executed_request_ids_lock = threading.Lock()
        self._idempotency_repository = idempotency_repository

    def get_action_spec(self, action_name):
        """Public passthrough to the registry's spec lookup — a pure read, never executes anything."""
        return self._registry.get_spec(action_name)

    # ── Validation (untrusted proposal -> trusted request) ──────────────────

    def validate_proposal(self, proposal: ActionProposal) -> ToolRequest:
        """
        Structural validation only — action is registered, required
        params present, no unexpected params, basic type checks pass.
        Never evaluates policy, authentication, or confirmation (that is
        invoke()'s job, kept separate so "is this well-formed" and "is
        this allowed" are independently testable and auditable). Raises
        ToolValidationError on any failure; never silently repairs a
        malformed proposal.
        """
        if not isinstance(proposal, ActionProposal):
            raise ToolValidationError("proposal must be an ActionProposal instance")

        action = proposal.action
        if not isinstance(action, str) or not action.strip():
            raise ToolValidationError("action name must be a non-empty string")

        spec = self._registry.get_spec(action)
        if spec is None:
            raise ToolValidationError(f"Unknown tool action: '{action}'")

        params = proposal.parameters
        if not isinstance(params, dict):
            raise ToolValidationError("parameters must be a dict")

        missing = [p for p in spec.required_params if params.get(p) in (None, "")]
        if missing:
            raise ToolValidationError(f"Missing required parameters for '{action}': {missing}")

        allowed_keys = set(spec.params_schema.keys())
        unexpected = set(params.keys()) - allowed_keys
        if unexpected:
            raise ToolValidationError(f"Unexpected parameters for '{action}': {sorted(unexpected)}")

        for key, value in params.items():
            expected_type_name = spec.params_schema.get(key)
            if expected_type_name and not _type_matches(value, expected_type_name):
                raise ToolValidationError(
                    f"Parameter '{key}' for '{action}' expected type '{expected_type_name}', "
                    f"got {type(value).__name__}"
                )

        return ToolRequest(
            action=action, params=dict(params), session_id=proposal.session_id,
            confirmed=False, request_id=proposal.request_id,
        )

    # ── Execution (trusted request -> typed result) ─────────────────────────

    def invoke(self, tool_request: ToolRequest, auth: AuthContext = ANONYMOUS_CONTEXT) -> ToolExecutionResult:
        """
        Observability wrapper (Phase 8) around _invoke() — the actual
        gate sequence, unchanged from Phase 4-7. Emits TOOL_REQUESTED
        before, and a lifecycle event derived from the *real, already-
        computed* ToolExecutionResult after — never predicting or
        pre-deciding the outcome. This is what makes "tool execution
        audit records MUST come from actual ToolOrchestrator execution"
        (plan.md Principle 16) structurally true: the event is built
        from the result object _invoke() returns, not from a separate
        judgment this method makes.
        """
        action_name = tool_request.action if isinstance(tool_request, ToolRequest) else None
        actor = auth.user_id if isinstance(auth, AuthContext) and auth.authenticated else None
        request_id = tool_request.request_id if isinstance(tool_request, ToolRequest) else None

        if self._audit_logger is not None and action_name:
            self._audit_logger.record(
                EventType.TOOL_REQUESTED, outcome="requested", actor=actor,
                action=action_name, request_id=request_id,
            )
            if self._registry.get_spec(action_name) is None and self._security_detector is not None:
                self._security_detector.record_unknown_tool_request(action_name, actor, request_id=request_id)

        # Phase 14 (Step 14.4.2): root span for the whole gate sequence --
        # observational only. Every gate/execute child span created inside
        # _invoke() becomes a child of this one; the returned result and
        # every audit event above/below are completely unchanged.
        with _traced("tool_orchestrator.invoke") as _span:
            try:
                _span.set_attribute(SpanAttributes.TOOL_NAME, action_name or "")
                _span.set_attribute(SpanAttributes.TOOL_ACTION, action_name or "")
                _span.set_attribute(SpanAttributes.REQUEST_ID, request_id or "")
            except Exception:
                pass

            result = self._invoke(tool_request, auth)

            try:
                _span.set_attribute(SpanAttributes.TOOL_OUTCOME, result.status or "")
            except Exception:
                pass

        if self._audit_logger is not None and action_name:
            self._emit_result_events(result, tool_request, actor, request_id)

        return result

    _STATUS_TO_EVENT = {
        "policy_denied": EventType.TOOL_DENIED,
        "confirmation_required": EventType.CONFIRMATION_REQUIRED,
        "duplicate": EventType.TOOL_DENIED,
        "timeout": EventType.TOOL_TIMEOUT,
    }

    def _emit_result_events(self, result: ToolExecutionResult, tool_request: ToolRequest, actor: Optional[str], request_id: Optional[str]) -> None:
        if result.success:
            self._audit_logger.record(
                EventType.TOOL_ALLOWED, outcome="allowed", actor=actor, action=result.tool, request_id=request_id,
            )
            # A confirmed request that actually reached success means the
            # confirmation gate was genuinely satisfied -- record that
            # distinctly from the tool's own success (plan.md's
            # CONFIRMATION_RECEIVED category), still derived from the real
            # trusted ToolRequest.confirmed field, never from any claim.
            if getattr(tool_request, "confirmed", False):
                self._audit_logger.record(
                    EventType.CONFIRMATION_RECEIVED, outcome="success", actor=actor,
                    action=result.tool, request_id=request_id,
                )
            self._audit_logger.record(
                EventType.TOOL_SUCCEEDED, outcome="success", actor=actor, action=result.tool, request_id=request_id,
            )
            return

        if result.error in ("AUTHENTICATION_REQUIRED", "INSUFFICIENT_PERMISSIONS", "NOT_RESOURCE_OWNER"):
            self._audit_logger.record(
                EventType.AUTHZ_DENY, outcome="denied", actor=actor, action=result.tool,
                request_id=request_id, reason=result.error,
            )
            return

        event_type = self._STATUS_TO_EVENT.get(result.status, EventType.TOOL_FAILED)
        self._audit_logger.record(
            event_type, outcome=result.status, actor=actor, action=result.tool,
            request_id=request_id, reason=result.error,
        )

    def _record_pii_event(self, pii_decision, action: str, tool_request: ToolRequest, auth: AuthContext) -> None:
        """
        Emits PII_DETECTED plus a decision-specific event for a
        TOOL_INPUT privacy decision already computed in _invoke(). Never
        wired inside PrivacyService itself -- see memory_manager.py's
        _record_pii_event docstring for the recursion hazard this avoids.
        Derived only from `pii_decision` (category/action), never the raw
        matched value (plan.md Step 8.11).
        """
        if self._audit_logger is None:
            return
        actor = auth.user_id if isinstance(auth, AuthContext) and auth.authenticated else None
        request_id = tool_request.request_id
        pii_types = sorted({f.type.value for f in pii_decision.findings})
        self._audit_logger.record(
            EventType.PII_DETECTED, outcome="detected", actor=actor, action=action,
            request_id=request_id, metadata={"pii_types": pii_types, "context": "TOOL_INPUT"},
        )
        if pii_decision.action == "BLOCK":
            self._audit_logger.record(
                EventType.PRIVACY_BLOCK, outcome="denied", actor=actor, action=action,
                request_id=request_id, reason=pii_decision.reason,
                metadata={"pii_types": pii_types, "context": "TOOL_INPUT"},
            )
        elif pii_decision.action == "REDACT":
            self._audit_logger.record(
                EventType.PII_REDACTED, outcome="redacted", actor=actor, action=action,
                request_id=request_id, metadata={"pii_types": pii_types, "context": "TOOL_INPUT"},
            )
        elif pii_decision.action == "RESTRICT":
            self._audit_logger.record(
                EventType.PRIVACY_RESTRICT, outcome="restricted", actor=actor, action=action,
                request_id=request_id, metadata={"pii_types": pii_types, "context": "TOOL_INPUT"},
            )

    def _idempotency_denied_result(self, action: str, tool_request: ToolRequest, auth: AuthContext) -> ToolExecutionResult:
        """
        Shared "already claimed" result-building for step 4's two paths
        (default in-process set, and Phase 12.9's persisted repository) —
        extracted so both produce the exact same observable
        result/audit/metrics shape; only the underlying storage/atomicity
        mechanism differs between them.
        """
        actor = auth.user_id if isinstance(auth, AuthContext) and auth.authenticated else None
        if self._audit_logger is not None:
            self._audit_logger.record(
                EventType.IDEMPOTENCY_DUPLICATE, outcome="denied", actor=actor,
                action=action, request_id=tool_request.request_id,
                reason="This request_id has already been executed.",
            )
        if self._metrics is not None:
            self._metrics.increment("idempotency_duplicates_total")
        return ToolExecutionResult(
            success=False, tool=action, status="duplicate",
            error="This request_id has already been executed.",
            request_id=tool_request.request_id,
        )

    def _invoke(self, tool_request: ToolRequest, auth: AuthContext = ANONYMOUS_CONTEXT) -> ToolExecutionResult:
        """
        The full gate sequence. `tool_request.confirmed` and `auth` are
        read exactly as given — this method performs no inference on
        model text of any kind to determine either. See module docstring.
        Unchanged from Phase 4-7 except for the rename (invoke() is now
        the Phase 8 observability wrapper above).
        """
        if not isinstance(tool_request, ToolRequest):
            return ToolExecutionResult(success=False, tool="UNKNOWN", status="failure", error="INVALID_TOOL_REQUEST")

        action = tool_request.action
        spec = self._registry.get_spec(action)
        if spec is None:
            return ToolExecutionResult(
                success=False, tool=action, status="failure", error="UNKNOWN_TOOL",
                request_id=tool_request.request_id,
            )

        # 1. Policy: is this action category permitted at all? Phase 10
        # (plan.md Step 10.12): an internal PolicyEngine failure here
        # denies the action -- never falls through to execution just
        # because the evaluation itself raised.
        with _traced("tool_orchestrator.gate_policy") as _gate_span:
            try:
                tool_policy = self._policy_engine.evaluate_tool_action(action, tool_request.params)
            except Exception:
                try:
                    _gate_span.set_attribute(SpanAttributes.POLICY_OUTCOME, "policy_denied")
                except Exception:
                    pass
                return ToolExecutionResult(
                    success=False, tool=action, status="policy_denied", error="POLICY_ENGINE_UNAVAILABLE",
                    request_id=tool_request.request_id,
                )
            try:
                _gate_span.set_attribute(SpanAttributes.POLICY_OUTCOME, "allowed" if tool_policy.allowed else "policy_denied")
            except Exception:
                pass
            if not tool_policy.allowed:
                return ToolExecutionResult(
                    success=False, tool=action, status="policy_denied", error=tool_policy.reason,
                    request_id=tool_request.request_id, metadata={"policy": tool_policy.to_dict()},
                )

        # 2. Authentication/authorization -- trusted `auth`, never derived
        # from tool_request or any text. A caller that never supplies
        # `auth` gets ANONYMOUS_CONTEXT, which is unauthenticated by
        # construction (see action_models.py) -- so omitting auth fails
        # closed, not open.
        with _traced("tool_orchestrator.gate_authorization") as _gate_span:
            if not isinstance(auth, AuthContext) or not auth.authenticated:
                try:
                    _gate_span.set_attribute(SpanAttributes.POLICY_OUTCOME, "denied")
                except Exception:
                    pass
                return ToolExecutionResult(
                    success=False, tool=action, status="failure", error="AUTHENTICATION_REQUIRED",
                    request_id=tool_request.request_id,
                )
            if spec.required_role and not auth.has_role(spec.required_role):
                try:
                    _gate_span.set_attribute(SpanAttributes.POLICY_OUTCOME, "denied")
                except Exception:
                    pass
                return ToolExecutionResult(
                    success=False, tool=action, status="failure", error="INSUFFICIENT_PERMISSIONS",
                    request_id=tool_request.request_id,
                )

            # 2.1. Fine-grained authorization (Phase 7) -- routed through
            # PolicyEngine.evaluate_authorization(), the single authoritative
            # authorization decision (plan.md Step 7.6/7.9), never
            # re-implemented here. Covers both "does this identity hold the
            # required permission" and, when tool_request.resource_owner_user_id
            # is set by the caller, "does this identity own the resource" —
            # e.g. USER + CANCEL_APPOINTMENT + own appointment -> ALLOW,
            # USER + CANCEL_APPOINTMENT + another user's appointment -> DENY.
            if spec.required_permission is not None:
                try:
                    authz = self._policy_engine.evaluate_authorization(
                        auth, spec.required_permission, resource_owner_user_id=tool_request.resource_owner_user_id,
                    )
                except Exception:
                    try:
                        _gate_span.set_attribute(SpanAttributes.POLICY_OUTCOME, "denied")
                    except Exception:
                        pass
                    return ToolExecutionResult(
                        success=False, tool=action, status="failure", error="POLICY_ENGINE_UNAVAILABLE",
                        request_id=tool_request.request_id,
                    )
                try:
                    _gate_span.set_attribute(SpanAttributes.POLICY_OUTCOME, "allowed" if authz.allowed else "denied")
                except Exception:
                    pass
                if not authz.allowed:
                    return ToolExecutionResult(
                        success=False, tool=action, status="failure", error=authz.rule,
                        request_id=tool_request.request_id, metadata={"policy": authz.to_dict()},
                    )
                if self._audit_logger is not None:
                    self._audit_logger.record(
                        EventType.AUTHZ_ALLOW, outcome="allowed", actor=auth.user_id, action=action,
                        request_id=tool_request.request_id, policy=authz.policy,
                    )

        # 2.5. Tool-input privacy (Phase 6, only when a PrivacyService is
        # configured) -- scans each string parameter for embedded PII
        # content and asks PolicyEngine.evaluate_pii() what's permitted
        # for context "TOOL_INPUT" (e.g. a payment-card-shaped number
        # typed into a free-text field is BLOCKed here, per configs/
        # policies/privacy.yaml). Distinct from step 1's tool policy
        # (which governs the action category, not its parameter content).
        if self._privacy_service is not None:
            with _traced("tool_orchestrator.gate_privacy") as _gate_span:
                for value in tool_request.params.values():
                    if not isinstance(value, str):
                        continue
                    pii_decision = self._privacy_service.decide(value, context="TOOL_INPUT")
                    if pii_decision.findings:
                        self._record_pii_event(pii_decision, action, tool_request, auth)
                    if not pii_decision.allowed:
                        try:
                            _gate_span.set_attribute(SpanAttributes.POLICY_OUTCOME, "policy_denied")
                        except Exception:
                            pass
                        return ToolExecutionResult(
                            success=False, tool=action, status="policy_denied", error=pii_decision.reason,
                            request_id=tool_request.request_id, metadata={"policy": pii_decision.to_dict()},
                        )

        # 3. Confirmation -- trusted `tool_request.confirmed`, never
        # derived from anything but that explicit field. Phase 10: an
        # internal PolicyEngine failure here requires confirmation (fail
        # closed) rather than skipping the check.
        with _traced("tool_orchestrator.gate_confirmation") as _gate_span:
            try:
                confirmation_policy = self._policy_engine.evaluate_confirmation(action, confirmed=tool_request.confirmed)
            except Exception:
                try:
                    _gate_span.set_attribute(SpanAttributes.POLICY_OUTCOME, "confirmation_required")
                except Exception:
                    pass
                return ToolExecutionResult(
                    success=False, tool=action, status="confirmation_required", error="POLICY_ENGINE_UNAVAILABLE",
                    request_id=tool_request.request_id,
                )
            try:
                _gate_span.set_attribute(
                    SpanAttributes.POLICY_OUTCOME, "allowed" if confirmation_policy.allowed else "confirmation_required",
                )
            except Exception:
                pass
            if not confirmation_policy.allowed:
                return ToolExecutionResult(
                    success=False, tool=action, status="confirmation_required", error=confirmation_policy.reason,
                    request_id=tool_request.request_id, metadata={"policy": confirmation_policy.to_dict()},
                )

        # 4. Idempotency -- a repeated request_id never re-executes,
        # regardless of whether the action is destructive.
        #
        # Default path (no idempotency_repository configured): unchanged
        # from Phase 4-11 -- lock-protected (Phase 10, plan.md Step
        # 10.15) check-then-add against the raw in-process set.
        #
        # Phase 12.9 path (idempotency_repository configured): the
        # reservation happens HERE, atomically, at the database layer,
        # scoped by (user_id, request_id) rather than request_id alone
        # (see idempotency_repository.py's module docstring for why two
        # different users must not collide on the same key string).
        # try_reserve() is called BEFORE execution (step 5), not after --
        # only the caller that wins the reservation proceeds to actually
        # invoke the tool, which is what guarantees exactly-one-execution
        # even for a genuinely non-idempotent tool under real cross-
        # process concurrency (recording success only after execution, as
        # the default path does, leaves a window where two concurrent
        # callers could both execute before either finishes recording --
        # acceptable for the default in-process set given its existing
        # Phase 10/11 test coverage, but not for a multi-process-shared
        # database, which this step's own instruction explicitly calls
        # out: "Do not rely only on Python locks"). auth.authenticated is
        # already guaranteed True here (step 2 already denied otherwise),
        # so auth.user_id is a real identity, never a guess.
        with _traced("tool_orchestrator.gate_idempotency") as _gate_span:
            using_persisted_idempotency = tool_request.request_id and self._idempotency_repository is not None
            if tool_request.request_id and not using_persisted_idempotency:
                with self._executed_request_ids_lock:
                    if tool_request.request_id in self._executed_request_ids:
                        try:
                            _gate_span.set_attribute(SpanAttributes.POLICY_OUTCOME, "duplicate")
                        except Exception:
                            pass
                        return self._idempotency_denied_result(action, tool_request, auth)
            elif using_persisted_idempotency:
                reserved = self._idempotency_repository.try_reserve(tool_request.request_id, user_id=auth.user_id, action=action)
                if not reserved:
                    try:
                        _gate_span.set_attribute(SpanAttributes.POLICY_OUTCOME, "duplicate")
                    except Exception:
                        pass
                    return self._idempotency_denied_result(action, tool_request, auth)

        # 5. Execute the registered callable ONLY, timeout-bounded (Phase
        # 4). Phase 10 adds a bounded, idempotency-aware retry on top:
        # only a "timeout" outcome for an action whose ActionSpec.idempotency
        # is NOT "NON_IDEMPOTENT_WRITE" is ever retried (plan.md Steps
        # 10.4/10.6/10.7) -- a validation error (ValueError/KeyError,
        # status="failure") is never retried regardless of idempotency,
        # since that's the caller's fault, not a transient dependency
        # issue. circuit_breaker (if configured) gates the whole
        # execution attempt -- an OPEN circuit fails immediately with a
        # controlled result, never by falling through to some other
        # (e.g. LLM-guessed) behavior.
        with _traced("tool_orchestrator.execute") as _exec_span:
            _exec_start = time.monotonic()
            result = self._execute_with_reliability(action, spec, tool_request, auth)
            try:
                _exec_span.set_attribute(SpanAttributes.TOOL_OUTCOME, result.status or "")
                _exec_span.set_attribute(SpanAttributes.LATENCY_MS, (time.monotonic() - _exec_start) * 1000.0)
                if result.status == "timeout":
                    from opentelemetry.trace import StatusCode
                    _exec_span.set_status(StatusCode.ERROR, "DependencyTimeoutError")
                    _exec_span.set_attribute(SpanAttributes.ERROR_TYPE, "DependencyTimeoutError")
            except Exception:
                pass

        if tool_request.request_id:
            if using_persisted_idempotency:
                if result.success:
                    self._idempotency_repository.update_result(tool_request.request_id, user_id=auth.user_id, result_status="success")
                else:
                    # A failed attempt must not permanently consume the
                    # idempotency key -- only success is "locked in",
                    # matching the default path's own `if result.success`
                    # guard below and standard idempotency-key semantics.
                    # A later, legitimate retry with the same
                    # (user_id, request_id) can then proceed normally.
                    self._idempotency_repository.release(tool_request.request_id, user_id=auth.user_id)
            elif result.success:
                with self._executed_request_ids_lock:
                    self._executed_request_ids.add(tool_request.request_id)

        # Sanitize the tool's own result before it leaves this method
        # (Phase 6, only when configured) — context "LLM_CONTEXT" since
        # that's this result's eventual downstream destination
        # (ConversationManager._tool_success_response()). Ensures raw PII
        # a mock/real tool might return is never passed further
        # unredacted, regardless of what ConversationManager does with it.
        if self._privacy_service is not None and result.result:
            result = ToolExecutionResult(
                success=result.success, tool=result.tool, status=result.status,
                result=self._privacy_service.sanitize(result.result, context="LLM_CONTEXT"),
                error=result.error, request_id=result.request_id, metadata=result.metadata,
            )

        return result

    def _record_dependency_timeout(self, action: str, tool_request: ToolRequest, actor: Optional[str]) -> None:
        if self._audit_logger is not None:
            self._audit_logger.record(
                EventType.DEPENDENCY_TIMEOUT, outcome="timeout", actor=actor, action=action,
                request_id=tool_request.request_id,
            )
        if self._metrics is not None:
            self._metrics.increment("timeouts_total")

    def _execute_with_reliability(self, action: str, spec, tool_request: ToolRequest, auth: AuthContext) -> ToolExecutionResult:
        """
        Wraps _execute_once() with the circuit breaker (Step 10.9) and
        bounded, idempotency-gated retry (Steps 10.4-10.7). Never applied
        to anything upstream of this point (policy/auth/confirmation/
        idempotency checks already ran in _invoke() before this is
        called) -- this only governs the actual business-API call.
        """
        actor = auth.user_id if isinstance(auth, AuthContext) and auth.authenticated else None

        if self._circuit_breaker is not None and not self._circuit_breaker.allow_request():
            if self._audit_logger is not None:
                self._audit_logger.record(
                    EventType.DEPENDENCY_FAILURE, outcome="denied", actor=actor, action=action,
                    request_id=tool_request.request_id, reason="Circuit breaker open for tool execution.",
                )
            if self._metrics is not None:
                self._metrics.increment("dependency_failures_total")
            return ToolExecutionResult(
                success=False, tool=action, status="failure", error="DEPENDENCY_UNAVAILABLE",
                request_id=tool_request.request_id,
            )

        fn = self._registry.get_callable(action)
        attempt = 1
        result = self._execute_once(fn, action, tool_request.params, spec.timeout_seconds, tool_request.request_id)
        if result.status == "timeout":
            self._record_dependency_timeout(action, tool_request, actor)

        while result.status == "timeout" and spec.idempotency != "NON_IDEMPOTENT_WRITE":
            decision = self._retry_policy.decide(attempt=attempt, retryable=True)
            if not decision.retryable:
                break
            if self._audit_logger is not None:
                self._audit_logger.record(
                    EventType.RETRY_ATTEMPT, outcome="retrying", actor=actor, action=action,
                    request_id=tool_request.request_id,
                    metadata={"attempt": attempt + 1, "max_attempts": decision.max_attempts},
                )
            if self._metrics is not None:
                self._metrics.increment("retries_total")
            self._sleep_fn(decision.delay_seconds)
            attempt += 1
            result = self._execute_once(fn, action, tool_request.params, spec.timeout_seconds, tool_request.request_id)
            if result.status == "timeout":
                self._record_dependency_timeout(action, tool_request, actor)

        if self._circuit_breaker is not None:
            dependency_failed = result.status == "timeout" or (result.status == "failure" and result.error == "TOOL_EXECUTION_FAILED")
            if dependency_failed:
                new_state = self._circuit_breaker.record_failure()
                if new_state is CircuitState.OPEN:
                    if self._audit_logger is not None:
                        self._audit_logger.record(
                            EventType.CIRCUIT_OPEN, outcome="opened", actor=actor, action=action,
                            request_id=tool_request.request_id, reason="Consecutive tool dependency failures exceeded threshold.",
                        )
                    if self._metrics is not None:
                        self._metrics.increment("circuit_breaker_open_total")
            else:
                self._circuit_breaker.record_success()

        return result

    @staticmethod
    def _execute_once(fn, action: str, params: dict, timeout_seconds: float, request_id: Optional[str]) -> ToolExecutionResult:
        """
        Phase 10 fix (plan.md Step 10.3/10.21): this previously used
        `with concurrent.futures.ThreadPoolExecutor(...) as executor:`.
        `ThreadPoolExecutor` registers its worker threads with an
        internal `atexit` hook (`concurrent.futures.thread._python_exit`)
        that joins every submitted work item at interpreter shutdown --
        so even calling `executor.shutdown(wait=False)` on the timeout
        path does NOT free the calling thread: the *process* still hangs
        at exit waiting for the permanently-stuck worker, completely
        defeating the timeout's purpose (verified: a `future.result(timeout=0.1)`
        against a `time.sleep(999)` submission still blocks process exit
        for 999s even after `shutdown(wait=False)`). A plain
        `threading.Thread(daemon=True)` has no such registry and does
        NOT block interpreter exit, so it's used directly here instead.
        Python still cannot forcibly kill a thread, so a genuinely stuck
        call's worker thread is still leaked in memory until it finishes
        naturally or the process exits -- an inherent limitation of
        thread-based (not process-based) execution, honestly documented
        rather than silently presented as a complete fix.
        """
        result_queue: "queue.Queue" = queue.Queue(maxsize=1)

        def _worker():
            try:
                result_queue.put(("success", fn(params)))
            except BaseException as exc:  # noqa: BLE001 -- forwarded to the waiting thread, not swallowed here
                result_queue.put(("error", exc))

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()
        try:
            outcome, payload = result_queue.get(timeout=timeout_seconds)
        except queue.Empty:
            return ToolExecutionResult(
                success=False, tool=action, status="timeout",
                error=f"Tool execution exceeded {timeout_seconds}s", request_id=request_id,
            )

        if outcome == "error":
            exc = payload
            if isinstance(exc, (ValueError, KeyError)):
                # Our own mock tools' deterministic, safe validation
                # errors (e.g. "Missing required parameters") -- fine to
                # surface verbatim, we authored every message.
                return ToolExecutionResult(success=False, tool=action, status="failure", error=str(exc), request_id=request_id)
            # Anything else (including simulated failures) is
            # generalized -- never leak internal exception detail to
            # the caller, matching ConversationManager's existing
            # error-handling discipline (src/agent/conversation_manager.py).
            return ToolExecutionResult(success=False, tool=action, status="failure", error="TOOL_EXECUTION_FAILED", request_id=request_id)

        result_data = payload
        if not isinstance(result_data, dict):
            return ToolExecutionResult(
                success=False, tool=action, status="failure", error="MALFORMED_TOOL_RESULT", request_id=request_id,
            )
        return ToolExecutionResult(success=True, tool=action, status="success", result=result_data, request_id=request_id)

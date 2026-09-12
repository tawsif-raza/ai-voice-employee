"""
Typed correlation and audit models (Phase 8; plan.md Steps 8.2, 8.6, 8.7).

Observability records what actually happened — it never decides what
happens. Every type here is a passive record, never itself evaluated by
any decision-making code (PolicyEngine, ToolOrchestrator, ConversationManager
all remain unaware these types exist for the purposes of their own
decisions — see audit.py's module docstring for how events are emitted
strictly *after* a real decision is already made).
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class EventType(str, Enum):
    """
    Explicit event taxonomy (plan.md Step 8.7) — only events this
    implementation actually emits, no speculative categories.
    """

    # Authentication
    AUTH_SUCCESS = "AUTH_SUCCESS"
    AUTH_FAILURE = "AUTH_FAILURE"
    # Authorization
    AUTHZ_ALLOW = "AUTHZ_ALLOW"
    AUTHZ_DENY = "AUTHZ_DENY"
    # Policy (generic — clinical/generation/handoff policy decisions)
    POLICY_ALLOW = "POLICY_ALLOW"
    POLICY_DENY = "POLICY_DENY"
    # Safety
    SAFETY_BLOCK = "SAFETY_BLOCK"
    SAFETY_HANDOFF = "SAFETY_HANDOFF"
    # Tool
    TOOL_REQUESTED = "TOOL_REQUESTED"
    TOOL_ALLOWED = "TOOL_ALLOWED"
    TOOL_DENIED = "TOOL_DENIED"
    TOOL_STARTED = "TOOL_STARTED"
    TOOL_SUCCEEDED = "TOOL_SUCCEEDED"
    TOOL_FAILED = "TOOL_FAILED"
    TOOL_TIMEOUT = "TOOL_TIMEOUT"
    # Confirmation
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
    CONFIRMATION_RECEIVED = "CONFIRMATION_RECEIVED"
    CONFIRMATION_REJECTED = "CONFIRMATION_REJECTED"
    CONFIRMATION_EXPIRED = "CONFIRMATION_EXPIRED"
    # Privacy
    PII_DETECTED = "PII_DETECTED"
    PII_REDACTED = "PII_REDACTED"
    PRIVACY_BLOCK = "PRIVACY_BLOCK"
    PRIVACY_RESTRICT = "PRIVACY_RESTRICT"
    # Session
    SESSION_CREATED = "SESSION_CREATED"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    SESSION_INVALID_TRANSITION = "SESSION_INVALID_TRANSITION"
    # System — an unhandled exception at the API boundary (plan.md Step
    # 8.15). Distinct from TOOL_FAILED/POLICY_DENY: this fires when a
    # route crashed outright, not when a component made a real deny/fail
    # decision.
    SYSTEM_ERROR = "SYSTEM_ERROR"
    # Reliability (Phase 10) — dependency timeout/failure/retry/circuit-
    # breaker signals for the LLM, RAG, and tool "Business API" layers
    # only (never PolicyEngine/ClinicalSafetyGuard/Authentication/
    # PrivacyService -- see reliability.py's module docstring).
    DEPENDENCY_TIMEOUT = "DEPENDENCY_TIMEOUT"
    DEPENDENCY_FAILURE = "DEPENDENCY_FAILURE"
    RETRY_ATTEMPT = "RETRY_ATTEMPT"
    CIRCUIT_OPEN = "CIRCUIT_OPEN"
    CIRCUIT_HALF_OPEN = "CIRCUIT_HALF_OPEN"
    CIRCUIT_CLOSED = "CIRCUIT_CLOSED"
    IDEMPOTENCY_DUPLICATE = "IDEMPOTENCY_DUPLICATE"
    GRACEFUL_SHUTDOWN = "GRACEFUL_SHUTDOWN"


class Severity(str, Enum):
    INFO = "INFO"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True)
class CorrelationContext:
    """
    Minimum-necessary request/turn correlation (plan.md Step 8.2).
    Deliberately excludes tokens, passwords, API keys, raw PII, and full
    conversation text — only opaque identifiers. `user_id` here is the
    same trusted value AuthContext already carries (Phase 7) — this type
    does not introduce a second identity representation, it threads the
    existing one through for correlation purposes only.
    """

    request_id: str
    conversation_id: Optional[str] = None
    session_id: Optional[str] = None
    user_id: Optional[str] = None
    turn_id: Optional[str] = None
    trace_id: Optional[str] = None  # Phase 14: hex string from OpenTelemetry
    span_id: Optional[str] = None  # Phase 14: hex string from OpenTelemetry

    def to_dict(self) -> dict:
        d = {
            "request_id": self.request_id,
            "conversation_id": self.conversation_id,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "turn_id": self.turn_id,
        }
        if self.trace_id is not None:
            d["trace_id"] = self.trace_id
        if self.span_id is not None:
            d["span_id"] = self.span_id
        return d


@dataclass(frozen=True)
class AuditEvent:
    """
    A structured, immutable record of something that actually happened —
    never model-generated, never influencing the decision it describes
    (see audit.py's module docstring for the "observe after the fact"
    discipline every emission site follows).
    """

    event_id: str
    timestamp: datetime
    event_type: EventType
    request_id: Optional[str]
    conversation_id: Optional[str]
    session_id: Optional[str]
    actor: Optional[str]  # a safe identifier (user_id), never a token/credential
    action: Optional[str]
    resource: Optional[str]
    outcome: str  # "success" | "denied" | "failure" | "timeout" | ...
    policy: Optional[str] = None
    reason: Optional[str] = None
    metadata: dict = field(default_factory=dict)
    trace_id: Optional[str] = None  # Phase 14: OpenTelemetry trace correlation
    span_id: Optional[str] = None  # Phase 14: OpenTelemetry span correlation

    def to_dict(self) -> dict:
        d = {
            "event_id": self.event_id,
            "timestamp": self.timestamp.isoformat(),
            "event_type": self.event_type.value,
            "request_id": self.request_id,
            "conversation_id": self.conversation_id,
            "session_id": self.session_id,
            "actor": self.actor,
            "action": self.action,
            "resource": self.resource,
            "outcome": self.outcome,
            "policy": self.policy,
            "reason": self.reason,
            "metadata": self.metadata,
        }
        if self.trace_id is not None:
            d["trace_id"] = self.trace_id
        if self.span_id is not None:
            d["span_id"] = self.span_id
        return d


@dataclass(frozen=True)
class SecurityEvent:
    """Structured security event (plan.md Step 8.17) — a distinguishable subtype of observability output, not a full SIEM."""

    event_id: str
    timestamp: datetime
    type: str
    severity: Severity
    request_id: Optional[str]
    actor: Optional[str]
    resource: Optional[str]
    outcome: str
    reason: str

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "timestamp": self.timestamp.isoformat(),
            "type": self.type,
            "severity": self.severity.value,
            "request_id": self.request_id,
            "actor": self.actor,
            "resource": self.resource,
            "outcome": self.outcome,
            "reason": self.reason,
        }


def new_event_id() -> str:
    return f"evt_{uuid.uuid4().hex[:16]}"


def new_request_id() -> str:
    return f"req_{uuid.uuid4().hex[:16]}"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)

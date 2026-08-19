"""
Typed session state (Phase 5; plan.md Step 5.2).

Naming reconciliation (same discipline as Phases 2-4): docs/DOMAIN_MODEL.md
Group 1 already freezes `ConversationSession` — {session_id, status
(active|closed), created_at} required, {user_id, last_activity_at,
retention_expires_at} optional — owned by Memory (§4). That entity
predates Tool Orchestrator and confirmation workflows (both didn't exist
when v1.0 froze), so it has no field for workflow/confirmation state.

`SessionState` here is the Phase 5 operational representation: its core
identity fields match ConversationSession's frozen contract exactly
(`session_id`, `status`, `created_at`, optional `user_id`), and every
workflow-specific field (`workflow_state`, `pending_action`,
`pending_parameters`, `confirmation_state`) is an ADDITIVE extension —
explicitly permitted by DOMAIN_MODEL.md's own Global Versioning
Convention ("entities version additively within v1.0 — new optional
fields may be added without a version bump"). `SessionStatus` is a richer
enum than ConversationSession's plain active/closed for the same reason;
ACTIVE/WAITING_FOR_INPUT/WAITING_FOR_CONFIRMATION all conceptually map
onto "active", and COMPLETED/EXPIRED/FAILED all map onto "closed".
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional


class SessionStatus(str, Enum):
    ACTIVE = "ACTIVE"
    WAITING_FOR_INPUT = "WAITING_FOR_INPUT"
    WAITING_FOR_CONFIRMATION = "WAITING_FOR_CONFIRMATION"
    COMPLETED = "COMPLETED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"


# Explicit, deterministic transition table — never inferred, never
# accepting an arbitrary string. Terminal states (COMPLETED, EXPIRED,
# FAILED) have no outgoing transitions: "never reopened once closed"
# (docs/DOMAIN_MODEL.md's ConversationSession State Invariant), extended
# to this richer enum.
ALLOWED_TRANSITIONS: dict[SessionStatus, frozenset] = {
    SessionStatus.ACTIVE: frozenset({
        SessionStatus.WAITING_FOR_INPUT, SessionStatus.WAITING_FOR_CONFIRMATION,
        SessionStatus.COMPLETED, SessionStatus.EXPIRED, SessionStatus.FAILED,
    }),
    SessionStatus.WAITING_FOR_INPUT: frozenset({
        SessionStatus.ACTIVE, SessionStatus.EXPIRED, SessionStatus.FAILED,
    }),
    SessionStatus.WAITING_FOR_CONFIRMATION: frozenset({
        SessionStatus.ACTIVE, SessionStatus.EXPIRED, SessionStatus.FAILED,
    }),
    SessionStatus.COMPLETED: frozenset(),
    SessionStatus.EXPIRED: frozenset(),
    SessionStatus.FAILED: frozenset(),
}

DEFAULT_SESSION_TTL = timedelta(minutes=30)


@dataclass
class SessionState:
    """
    Mutable session record (unlike this repository's other domain types,
    which are frozen — a session's whole purpose is to change over time
    under SessionManager's control). Never constructed or mutated
    directly by anything except SessionManager.
    """

    session_id: str
    user_id: Optional[str] = None
    status: SessionStatus = SessionStatus.ACTIVE
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc) + DEFAULT_SESSION_TTL)
    current_intent: Optional[str] = None
    workflow_state: Optional[str] = None
    pending_action: Optional[str] = None
    pending_parameters: dict = field(default_factory=dict)
    confirmation_state: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        current = now or datetime.now(timezone.utc)
        return current >= self.expires_at

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "current_intent": self.current_intent,
            "workflow_state": self.workflow_state,
            "pending_action": self.pending_action,
            "pending_parameters": dict(self.pending_parameters),
            "metadata": dict(self.metadata),
        }


def new_session_id() -> str:
    return f"sess_{uuid.uuid4().hex[:16]}"

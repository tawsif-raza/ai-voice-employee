"""
Typed action/tool models for the Tool Orchestrator (Phase 4; plan.md
Steps 4.2, 4.3, 4.8).

Naming reconciliation (documented, not silent — same discipline as
intent_engine.py's IntentResult/RoutingDecision split in Phase 2):
docs/DOMAIN_MODEL.md Group 4 already freezes `ToolRequest`
(action, params, session_id, confirmed) and `ToolResponse`
(status, data, error) as the *validated, trusted* structures Tool
Orchestrator accepts and returns — matching docs/MODULES.md §8's
`ActionRequest`/`ActionResult`. This module keeps those names and shapes
exactly, and adds two things Phase 4's plan explicitly asks for that
DOMAIN_MODEL.md does not yet define:

- `ActionProposal` — the UNTRUSTED, pre-validation shape (this is new;
  DOMAIN_MODEL.md has no entity for "a proposal that hasn't been
  validated yet" because Tool Orchestrator didn't exist when it was
  written). It never implies approval or execution.
- `ToolExecutionResult` — extends DOMAIN_MODEL's `ToolResponse` shape
  additively (`tool`, `request_id`, `metadata`, and a convenience
  `success` bool alongside `status`) per DOMAIN_MODEL.md's own Global
  Versioning Convention ("entities version additively... new optional
  fields may be added without a version bump").

`ActionSpec` reuses docs/MODULES.md §8's exact name and required fields
(`name`, `description`, `params_schema`, `requires_confirmation`),
extended additively with `destructive` and `timeout` (plan.md Step 4.3's
`ToolDefinition` fields), since a fresh `ToolDefinition` type would be a
needless duplicate of an already-frozen concept with the same shape.

Every type here is a plain, immutable dataclass — matching this
repository's existing convention (HandoffMatch, RetrievedChunk,
IntentResult, RoutingDecision, PolicyDecision are all frozen dataclasses;
Pydantic is reserved for the FastAPI request/response boundary only).
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class ActionSpec:
    """
    A registered tool's contract (docs/MODULES.md §8's ActionSpec,
    extended additively). ToolRegistry is the only place these are
    constructed — never from model output.
    """

    name: str
    description: str
    params_schema: dict  # {param_name: type_name}; required_params below lists which are mandatory
    required_params: tuple[str, ...] = ()
    requires_confirmation: bool = False
    destructive: bool = False
    timeout_seconds: float = 5.0
    required_role: Optional[str] = None  # None = any authenticated caller may invoke
    required_permission: Optional[str] = None  # Phase 7 — see identity.py's Permission enum. None = no permission check beyond authentication.
    # Phase 10 (plan.md Step 10.6) — one of reliability.IdempotencyClass's
    # values ("READ_ONLY" | "IDEMPOTENT_WRITE" | "NON_IDEMPOTENT_WRITE"),
    # kept as a plain str here (not importing reliability.py's enum) so
    # this module stays free of a Phase 10 dependency the same way it
    # stayed free of a Phase 6/7/8 one — ToolOrchestrator is the only
    # place that interprets this value. Fail-closed default: an action
    # that doesn't explicitly declare itself idempotent is NEVER
    # automatically retried after a transient failure.
    idempotency: str = "NON_IDEMPOTENT_WRITE"


@dataclass(frozen=True)
class ActionProposal:
    """
    An UNTRUSTED, proposed action — plan.md Step 4.2's shape exactly.
    This object existing does NOT mean approved=true or execute=true; it
    is not accepted anywhere by ToolOrchestrator.invoke() until it has
    been validated into a ToolRequest (see validate_proposal() in
    tool_orchestrator.py). Whether it originated from parsed model output
    or from Conversation Manager's own intent-driven logic is irrelevant
    to this type — it is untrusted either way.
    """

    action: str
    parameters: dict = field(default_factory=dict)
    request_id: Optional[str] = None
    session_id: Optional[str] = None
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ToolRequest:
    """
    Exactly docs/DOMAIN_MODEL.md's frozen ToolRequest / docs/MODULES.md
    §8's ActionRequest: the ONLY shape ToolOrchestrator.invoke() accepts.
    Constructed exclusively by validate_proposal() after structural
    validation — never constructed directly from an ActionProposal
    without going through validation, and never carries raw model text.
    """

    action: str
    params: dict
    session_id: Optional[str] = None
    confirmed: bool = False
    request_id: Optional[str] = None
    # Phase 7 — who owns the resource this action targets (e.g. the
    # user_id that originally booked the appointment being cancelled).
    # None means "no ownership check for this action" (e.g. ORDER_LOOKUP
    # against a caller's own order isn't currently ownership-tracked by
    # the mock store — see PHASE_7 report's technical debt). Set by the
    # caller (ConversationManager or a test) from a trusted lookup —
    # never derived from tool params or model output.
    resource_owner_user_id: Optional[str] = None


@dataclass(frozen=True)
class ToolExecutionResult:
    """
    docs/DOMAIN_MODEL.md's ToolResponse (status/data/error), extended
    additively with `tool`, `request_id`, `metadata`, and a convenience
    `success` bool — plan.md Step 4.8's requested shape. Never constructed
    from raw, unvalidated tool output; ToolOrchestrator is the only
    producer.
    """

    success: bool
    tool: str
    status: str  # "success" | "failure" | "confirmation_required" | "policy_denied" | "duplicate" | "timeout"
    result: Optional[dict] = None
    error: Optional[str] = None
    request_id: Optional[str] = None
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "tool": self.tool,
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "request_id": self.request_id,
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class AuthContext:
    """
    Trusted authentication/authorization context (plan.md Step 4.6).
    MUST be constructed by trusted application code — server-side session
    lookup, an auth middleware, or (today, since no production identity
    provider exists in this repository — see docs/ARCHITECTURE.md §9's
    documented gap and identity.py's DevelopmentAuthenticationProvider) a
    caller-supplied default. Never constructed from parsing model output;
    there is no code path anywhere in this module, identity.py, or
    tool_orchestrator.py that builds an AuthContext from text.

    Reconciliation note (Phase 7; same discipline as every prior phase's
    naming reconciliation): plan.md Phase 7 Step 7.2 asks for an
    "IdentityContext" with {user_id, authenticated, roles, permissions,
    authentication_method, metadata}. Rather than introduce a second,
    competing type, this class — already used pervasively since Phase 4
    — is extended additively with exactly those missing fields
    (`permissions`, `authentication_method`, `metadata`). This *is*
    Phase 7's IdentityContext; the name AuthContext is kept for backward
    compatibility with every existing caller (ToolOrchestrator,
    ConversationManager, ~40+ existing tests).
    """

    user_id: str
    authenticated: bool = False
    roles: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    authentication_method: str = "none"
    metadata: dict = field(default_factory=dict)

    def has_role(self, role: str) -> bool:
        return role in self.roles

    def has_permission(self, permission: str) -> bool:
        return permission in self.permissions


# A context representing "not authenticated" — used as ToolOrchestrator's
# default so a caller that never supplies identity fails closed rather
# than silently bypassing the authentication check. This default context
# is *not* elevated — ANONYMOUS_CONTEXT.authenticated is False and it has
# no roles/permissions, so any ActionSpec that requires a role or
# permission still denies it.
ANONYMOUS_CONTEXT = AuthContext(user_id="anonymous", authenticated=False, roles=(), permissions=(), authentication_method="none")

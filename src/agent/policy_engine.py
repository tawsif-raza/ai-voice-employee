"""
Policy Engine — a deterministic enforcement boundary between requests and
protected application behavior (Phase 3; docs/IMPLEMENTATION_ROADMAP.md-
style milestone, plan.md Phase 3).

This is NOT a rewrite of the existing Clinical Safety Guard or Handoff
Detector (src/inference/handoff_detector.py) or Intent Engine
(src/agent/intent_engine.py) -- it is a thin, deterministic aggregation
layer on top of them, matching this repository's established pattern
(config-driven, offline-testable, no model call, fail-safe on error).
PolicyEngine never re-implements clinical trigger matching or handoff
phrase matching; it accepts their already-computed results (HandoffMatch,
RoutingDecision) as inputs and turns them into one uniform PolicyDecision.

Security posture (non-negotiable, see tests/test_policy_engine.py's LLM
trust-boundary tests): every evaluate_* method takes plain, typed
parameters that the CALLER controls -- there is no code path anywhere in
this module that parses or trusts a field out of LLM-generated text
(e.g. a model response containing `{"approved": true}`). A caller could
misuse this API by passing model text through as if it were trusted
input, but that misuse is impossible to make structurally safe from
inside PolicyEngine alone; the guarantee this module provides is that IT
never does that itself, and every public method's parameter types make
"pass the model's own claim about a boolean" the only way to fool it --
which is a caller bug, not a PolicyEngine trust boundary failure. Phase 4
(Tool Orchestrator) is the place that must not commit that caller bug.
"""

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

_INFERENCE_DIR = str(Path(__file__).resolve().parents[1] / "inference")
if _INFERENCE_DIR not in sys.path:
    sys.path.insert(0, _INFERENCE_DIR)
from handoff_detector import HandoffMatch  # noqa: E402

from intent_engine import Route, RoutingDecision  # noqa: E402

_POLICIES_DIR = Path(__file__).resolve().parents[2] / "configs" / "policies"


class Action:
    """Valid PolicyDecision.action values. A closed set -- not a free-form string."""

    ALLOW = "ALLOW"
    BLOCK = "BLOCK"
    HANDOFF = "HANDOFF"
    CLARIFY = "CLARIFY"
    REQUEST_CONFIRMATION = "REQUEST_CONFIRMATION"
    # Phase 6 (PII/privacy) additions.
    REDACT = "REDACT"
    RESTRICT = "RESTRICT"


@dataclass(frozen=True)
class PolicyDecision:
    """
    The single typed result every PolicyEngine evaluate_* method returns.
    Matches plan.md's requested shape exactly:
    {allowed, policy, rule, action, reason}.
    """

    allowed: bool
    policy: str
    rule: str
    action: str
    reason: str

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "policy": self.policy,
            "rule": self.rule,
            "action": self.action,
            "reason": self.reason,
        }


# Fallback configs used when a policy YAML file is missing, unreadable, or
# malformed -- deliberately conservative (fail-closed), never permissive,
# per plan.md's "malformed or ambiguous policy configuration MUST NOT
# silently result in permissive behavior."
_BUILTIN_GENERATION_CONFIG = {
    "default_rule": "SAFE_GENERAL_INFORMATION",
    "default_action": "ALLOW",
    "rules": [
        {"match_route": "CLARIFICATION", "rule": "UNKNOWN_REQUEST", "allowed": False, "action": "CLARIFY",
         "reason": "Request intent is unclear."},
    ],
}
_BUILTIN_TOOLS_CONFIG = {
    "default_action": "BLOCK",
    "default_rule": "TOOL_NOT_REGISTERED",
    "rules": [],
}
_BUILTIN_CONFIRMATION_CONFIG = {
    "default_requires_confirmation": True,
    "actions": {},
}
_BUILTIN_HANDOFF_CONFIG = {
    "default_action": "ALLOW",
    "default_rule": "NO_HANDOFF_SIGNAL",
    "rules": [
        {"signal": "clinical_triggered", "rule": "CLINICAL_RISK", "action": "HANDOFF", "reason": "Clinical guard fired."},
    ],
}
_BUILTIN_PRIVACY_CONFIG = {
    "default_action": "ALLOW",
    "default_rule": "NOT_RESTRICTED",
    "restricted_fields": {},
    # Conservative fallback for evaluate_pii() if privacy.yaml is
    # missing/malformed -- never fully permissive for the two highest-risk
    # detectable types, regardless of context.
    "pii_policies": {
        "default_action": "REDACT",
        "contexts": {},
    },
}


def _load_yaml(path: Path, builtin_default: dict) -> dict:
    """Shared fail-safe loader, mirroring HandoffDetector._load_config's resilience pattern."""
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f)
            if loaded:
                return loaded
        except (OSError, yaml.YAMLError):
            pass
    return builtin_default


# Explicit, documented precedence order -- highest authority first. When
# multiple policy categories produce decisions for the same request,
# resolve() picks the most restrictive one from the highest-precedence
# category, never an implicit dict/insertion order. See resolve()'s
# docstring.
PRECEDENCE: tuple[str, ...] = ("clinical", "authorization", "handoff", "confirmation", "tool", "privacy", "generation")


class PolicyEngine:
    """
    Deterministic policy evaluation. Every method here is a pure function
    of its explicit arguments and loaded configuration -- no network
    call, no model call, no hidden global state.
    """

    def __init__(
        self,
        generation_config_path: Optional[str] = None,
        tools_config_path: Optional[str] = None,
        confirmation_config_path: Optional[str] = None,
        handoff_config_path: Optional[str] = None,
        privacy_config_path: Optional[str] = None,
    ):
        self._generation = _load_yaml(
            Path(generation_config_path) if generation_config_path else _POLICIES_DIR / "generation.yaml",
            _BUILTIN_GENERATION_CONFIG,
        )
        self._tools = _load_yaml(
            Path(tools_config_path) if tools_config_path else _POLICIES_DIR / "tools.yaml",
            _BUILTIN_TOOLS_CONFIG,
        )
        self._confirmation = _load_yaml(
            Path(confirmation_config_path) if confirmation_config_path else _POLICIES_DIR / "confirmation.yaml",
            _BUILTIN_CONFIRMATION_CONFIG,
        )
        self._handoff = _load_yaml(
            Path(handoff_config_path) if handoff_config_path else _POLICIES_DIR / "handoff.yaml",
            _BUILTIN_HANDOFF_CONFIG,
        )
        self._privacy = _load_yaml(
            Path(privacy_config_path) if privacy_config_path else _POLICIES_DIR / "privacy.yaml",
            _BUILTIN_PRIVACY_CONFIG,
        )

    # ── Clinical (highest precedence; never re-derives, only wraps) ────────

    def evaluate_clinical(self, clinical_result: Optional[HandoffMatch]) -> PolicyDecision:
        """
        Wraps the existing ClinicalSafetyGuard's result (a HandoffMatch
        from src/inference/handoff_detector.py) as a PolicyDecision.
        Never re-implements clinical trigger matching -- `clinical_result`
        is assumed to already be the guard's authoritative output.
        """
        if clinical_result is not None and clinical_result.is_handoff:
            return PolicyDecision(
                allowed=False, policy="clinical", rule="MEDICAL_DOSAGE",
                action=Action.HANDOFF,
                reason="Clinical dosage/safety requests require human review.",
            )
        return PolicyDecision(
            allowed=True, policy="clinical", rule="NO_CLINICAL_RISK", action=Action.ALLOW,
            reason="No clinical safety trigger detected.",
        )

    # ── Generation ───────────────────────────────────────────────────────

    def evaluate_generation(self, intent_routing: Optional[RoutingDecision] = None) -> PolicyDecision:
        """
        Determines whether normal generation may proceed, based on the
        Intent Engine's routing decision. Does not consider clinical
        safety -- call evaluate_clinical() separately and combine via
        resolve() (clinical always outranks generation in PRECEDENCE).
        """
        route = intent_routing.route if intent_routing is not None else None
        intent = intent_routing.intent if intent_routing is not None else None

        for rule in self._generation.get("rules", []):
            if "match_route" in rule and rule["match_route"] == route:
                return self._decision_from_rule("generation", rule)
            if "match_intent" in rule and rule["match_intent"] == intent:
                return self._decision_from_rule("generation", rule)

        return PolicyDecision(
            allowed=(self._generation.get("default_action", "ALLOW") == "ALLOW"),
            policy="generation",
            rule=self._generation.get("default_rule", "SAFE_GENERAL_INFORMATION"),
            action=self._generation.get("default_action", "ALLOW"),
            reason="No specific generation rule matched; default policy applies.",
        )

    @staticmethod
    def _decision_from_rule(policy_name: str, rule: dict) -> PolicyDecision:
        return PolicyDecision(
            allowed=bool(rule.get("allowed", True)),
            policy=policy_name,
            rule=rule.get("rule", "UNNAMED_RULE"),
            action=rule.get("action", Action.ALLOW),
            reason=rule.get("reason", ""),
        )

    # ── Tool (permission lookup only -- never executes anything) ───────────

    def evaluate_tool_action(self, action_name, params: Optional[dict] = None) -> PolicyDecision:
        """
        Answers "would this hypothetical action be permitted?" — a pure
        lookup against configs/policies/tools.yaml. Never calls, imports,
        or references any actual tool implementation; Tool Orchestrator
        does not exist yet (Phase 4). `params` is accepted for interface
        completeness (future per-parameter rules) but unused in Phase 3.
        """
        if not isinstance(action_name, str) or not action_name.strip():
            return PolicyDecision(
                allowed=False, policy="tool", rule="INVALID_ACTION_NAME", action=Action.BLOCK,
                reason="Action name must be a non-empty string.",
            )

        for rule in self._tools.get("rules", []):
            if rule.get("action") == action_name:
                decision = self._decision_from_rule("tool", rule)
                # rules in tools.yaml don't set `action` (policy_action) explicitly;
                # default to ALLOW/BLOCK based on `allowed`.
                inferred_action = Action.ALLOW if decision.allowed else Action.BLOCK
                return PolicyDecision(
                    allowed=decision.allowed, policy="tool", rule=decision.rule,
                    action=rule.get("action_result", inferred_action), reason=decision.reason,
                )

        return PolicyDecision(
            allowed=(self._tools.get("default_action", "BLOCK") == "ALLOW"),
            policy="tool",
            rule=self._tools.get("default_rule", "TOOL_NOT_REGISTERED"),
            action=self._tools.get("default_action", "BLOCK"),
            reason=f"'{action_name}' is not a registered tool action.",
        )

    # ── Handoff (aggregates existing signals; never re-matches phrases) ────

    def evaluate_handoff(
        self,
        clinical_triggered: bool = False,
        intent_routing: Optional[RoutingDecision] = None,
        post_generation_handoff: Optional[HandoffMatch] = None,
    ) -> PolicyDecision:
        """
        Aggregates already-computed signals (clinical guard result,
        IntentEngine's RoutingDecision, and/or the existing post-
        generation HandoffDetector's result) into one deterministic
        handoff decision. Never runs its own phrase/pattern matching.
        """
        signals = {
            "clinical_triggered": bool(clinical_triggered),
            "intent_human_handoff": bool(intent_routing is not None and intent_routing.intent == "HUMAN_HANDOFF"),
            "intent_complaint": bool(intent_routing is not None and intent_routing.intent == "COMPLAINT"),
            "post_generation_handoff": bool(post_generation_handoff is not None and post_generation_handoff.is_handoff),
            "low_confidence": bool(intent_routing is not None and intent_routing.route == Route.CLARIFICATION),
            "unsupported_request": False,
        }

        for rule in self._handoff.get("rules", []):
            signal_name = rule.get("signal")
            if signal_name and signals.get(signal_name):
                return PolicyDecision(
                    allowed=(rule.get("action", "HANDOFF") != "HANDOFF"),
                    policy="handoff", rule=rule.get("rule", "UNNAMED_RULE"),
                    action=rule.get("action", Action.HANDOFF), reason=rule.get("reason", ""),
                )

        return PolicyDecision(
            allowed=(self._handoff.get("default_action", "ALLOW") == "ALLOW"),
            policy="handoff",
            rule=self._handoff.get("default_rule", "NO_HANDOFF_SIGNAL"),
            action=self._handoff.get("default_action", "ALLOW"),
            reason="No handoff signal present.",
        )

    # ── Privacy (boundary only, not a full PII system) ─────────────────────

    def evaluate_privacy(self, field_name, operation: str = "log") -> PolicyDecision:
        """
        Answers "is this named field permitted for this operation?"
        (operation in {"log", "persist", "expose_downstream",
        "include_in_metadata", "store_in_session"}). Does not scan free
        text for PII -- a named-field boundary only, per Phase 3 scope.
        """
        if not isinstance(field_name, str) or not field_name.strip():
            return PolicyDecision(
                allowed=False, policy="privacy", rule="INVALID_FIELD_NAME", action=Action.BLOCK,
                reason="Field name must be a non-empty string.",
            )

        restricted = self._privacy.get("restricted_fields", {}).get(operation, [])
        if field_name in restricted:
            return PolicyDecision(
                allowed=False, policy="privacy", rule="RESTRICTED_FIELD", action=Action.BLOCK,
                reason=f"'{field_name}' is restricted for operation '{operation}'.",
            )

        return PolicyDecision(
            allowed=(self._privacy.get("default_action", "ALLOW") == "ALLOW"),
            policy="privacy",
            rule=self._privacy.get("default_rule", "NOT_RESTRICTED"),
            action=self._privacy.get("default_action", "ALLOW"),
            reason=f"'{field_name}' is not restricted for operation '{operation}'.",
        )

    def evaluate_pii(self, pii_types, context: str) -> PolicyDecision:
        """
        Content-pattern PII policy (Phase 6) -- complementary to
        evaluate_privacy() above. `pii_types` is an iterable of PIIType
        values (or their .value strings) already found by a detector
        (e.g. pii_detector.PIIDetector) — this method never scans text
        itself, it only decides what to DO given what was already found,
        keeping detection and policy decision as separate, independently
        testable concerns (same separation as evaluate_clinical()
        wrapping HandoffDetector's output rather than re-matching).

        `context` is one of LOGGING, MEMORY, SESSION, LLM_CONTEXT,
        TOOL_INPUT, API_RESPONSE, TELEMETRY. If multiple pii_types are
        given, the single MOST RESTRICTIVE action among them wins
        (BLOCK > RESTRICT > REDACT > ALLOW) — deterministic, not
        first-match order, since here there's no meaningful "first" to
        anchor on (an unordered set of findings).
        """
        _SEVERITY = {"BLOCK": 3, "RESTRICT": 2, "REDACT": 1, "ALLOW": 0}
        pii_config = self._privacy.get("pii_policies", {})
        context_config = pii_config.get("contexts", {}).get(context, {})
        default_action = pii_config.get("default_action", "REDACT")

        type_values = [t.value if hasattr(t, "value") else str(t) for t in (pii_types or [])]
        if not type_values:
            return PolicyDecision(
                allowed=True, policy="privacy", rule="NO_PII_DETECTED", action=Action.ALLOW,
                reason=f"No PII detected for context '{context}'.",
            )

        worst_action = "ALLOW"
        worst_type = None
        for type_value in type_values:
            action = context_config.get(type_value, default_action)
            if _SEVERITY.get(action, _SEVERITY["REDACT"]) > _SEVERITY[worst_action]:
                worst_action = action
                worst_type = type_value

        return PolicyDecision(
            allowed=(worst_action == "ALLOW"),
            policy="privacy",
            rule=f"PII_{worst_type}_{context}" if worst_type else "NO_PII_DETECTED",
            action=worst_action,
            reason=f"{worst_type} detected for context '{context}' -> {worst_action}." if worst_type else "No PII detected.",
        )

    # ── Authorization (Phase 7) ─────────────────────────────────────────────

    def evaluate_authorization(self, identity, permission: str, resource_owner_user_id: Optional[str] = None) -> PolicyDecision:
        """
        The single authoritative authorization decision (plan.md Step
        7.6): does `identity` (a trusted AuthContext — see
        identity.py/action_models.py) have `permission`, and — if
        `resource_owner_user_id` is given — does `identity` own the
        resource being acted on (or hold ADMIN_OPERATIONS, which grants
        cross-resource access)?

        This method never authenticates anyone and never derives
        `identity` itself — it only evaluates an already-resolved,
        trusted AuthContext against a requested permission/resource.
        Never re-implemented per call site: ToolOrchestrator and any
        future permission-gated code path call this one method rather
        than hand-rolling their own role/permission comparison (plan.md:
        "Do not create a competing authorization engine").
        """
        if identity is None or not getattr(identity, "authenticated", False):
            return PolicyDecision(
                allowed=False, policy="authorization", rule="AUTHENTICATION_REQUIRED", action=Action.BLOCK,
                reason="Request is not authenticated.",
            )

        if permission and not identity.has_permission(permission):
            return PolicyDecision(
                allowed=False, policy="authorization", rule="INSUFFICIENT_PERMISSIONS", action=Action.BLOCK,
                reason=f"Identity lacks permission '{permission}'.",
            )

        if (
            resource_owner_user_id is not None
            and resource_owner_user_id != identity.user_id
            and not identity.has_permission("ADMIN_OPERATIONS")
        ):
            return PolicyDecision(
                allowed=False, policy="authorization", rule="NOT_RESOURCE_OWNER", action=Action.BLOCK,
                reason="Identity does not own this resource.",
            )

        return PolicyDecision(
            allowed=True, policy="authorization", rule="AUTHORIZED", action=Action.ALLOW,
            reason="Identity is authenticated, holds the required permission, and owns (or is exempt from owning) the resource.",
        )

    # ── Confirmation ─────────────────────────────────────────────────────

    def evaluate_confirmation(self, action_name, confirmed: bool = False) -> PolicyDecision:
        """
        Determines whether `action_name` may proceed given `confirmed`.

        SECURITY: `confirmed` MUST be sourced from trusted application/
        session state by the caller -- this method has no way to inspect
        where its boolean came from, and deliberately does not accept a
        message string, a model response object, or anything resembling
        raw LLM output. Passing a model's own textual claim
        ("I have confirmed this") through as this argument would be a
        caller bug this method cannot detect or prevent — the boundary
        this module guarantees is that IT never derives `confirmed` from
        text itself. See tests/test_policy_engine.py's trust-boundary
        tests for the explicit regression coverage.
        """
        if not isinstance(action_name, str) or not action_name.strip():
            return PolicyDecision(
                allowed=False, policy="confirmation", rule="INVALID_ACTION_NAME", action=Action.BLOCK,
                reason="Action name must be a non-empty string.",
            )

        entry = self._confirmation.get("actions", {}).get(action_name)
        if entry is None:
            requires_confirmation = bool(self._confirmation.get("default_requires_confirmation", True))
            rule_name = "CONFIRMATION_DEFAULT_REQUIRED" if requires_confirmation else "CONFIRMATION_DEFAULT_NOT_REQUIRED"
            reason = f"'{action_name}' has no explicit confirmation rule; defaulting to requires_confirmation={requires_confirmation}."
        else:
            requires_confirmation = bool(entry.get("requires_confirmation", True))
            rule_name = entry.get("rule", "UNNAMED_RULE")
            reason = entry.get("reason", "")

        if requires_confirmation and not bool(confirmed):
            return PolicyDecision(
                allowed=False, policy="confirmation", rule=rule_name, action=Action.REQUEST_CONFIRMATION,
                reason=reason or "Explicit user confirmation is required before executing this action.",
            )

        return PolicyDecision(
            allowed=True, policy="confirmation",
            rule=rule_name if requires_confirmation else "NO_CONFIRMATION_REQUIRED",
            action=Action.ALLOW,
            reason="Confirmation satisfied or not required." if requires_confirmation else reason,
        )

    # ── Precedence resolution ────────────────────────────────────────────

    @staticmethod
    def _precedence_index(policy_name: str) -> int:
        try:
            return PRECEDENCE.index(policy_name)
        except ValueError:
            # An unrecognized policy category sorts last (lowest
            # authority) -- deterministic, not an error, so a future
            # policy category added without updating PRECEDENCE degrades
            # safely instead of raising.
            return len(PRECEDENCE)

    def resolve(self, decisions: list) -> PolicyDecision:
        """
        Given several already-computed PolicyDecisions (e.g. from calling
        evaluate_clinical(), evaluate_handoff(), evaluate_confirmation(),
        evaluate_tool_action() for the same request), return the single
        governing decision using the explicit PRECEDENCE order — never
        Python dict/list insertion order or YAML file order by accident.

        Rule: among all decisions with allowed=False, the one whose
        `policy` is highest in PRECEDENCE wins (e.g. a clinical denial
        always outranks a confirmation requirement, which outranks a
        plain tool-permission denial). If every decision allows, the
        highest-precedence *allowed* decision is returned (so, e.g., a
        clinical ALLOW is returned over a generation ALLOW when both were
        evaluated), which lets a caller show the most authoritative
        "why this was allowed" reason.
        """
        if not decisions:
            return PolicyDecision(
                allowed=True, policy="generation", rule="NO_POLICY_EVALUATED", action=Action.ALLOW,
                reason="No policy categories were evaluated; defaulting to allow.",
            )

        denied = [d for d in decisions if not d.allowed]
        pool = denied if denied else list(decisions)
        pool.sort(key=lambda d: self._precedence_index(d.policy))
        return pool[0]

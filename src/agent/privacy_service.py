"""
PrivacyService — the single reusable boundary every component routes
through for PII inspection, policy decisions, and redaction (Phase 6;
plan.md Step 6.6).

Does not duplicate PII detection logic into every component
(plan.md's explicit instruction): logging, memory, session, LLM-context,
tool, and API-response call sites all share this one class. Does not
create a second, competing policy engine: `decide()` always delegates the
actual ALLOW/REDACT/BLOCK/RESTRICT decision to Phase 3's PolicyEngine
(`evaluate_pii()`) — PrivacyService's own code only detects and
sanitizes, it never decides policy itself.
"""

from typing import Any, Optional

from pii_detector import PIIDetector
from privacy_models import PIIFinding, PrivacyDecision


class PrivacyService:
    def __init__(self, policy_engine, detector: Optional[PIIDetector] = None):
        self._policy_engine = policy_engine
        self._detector = detector or PIIDetector()

    def inspect(self, text: str) -> list[PIIFinding]:
        """Detection only — never decides policy. See module docstring."""
        return self._detector.detect(text)

    def decide(self, text: str, context: str) -> PrivacyDecision:
        """
        Inspects `text`, then asks PolicyEngine.evaluate_pii() (the
        authoritative decision-maker — see module docstring) what to do
        about it for `context` (LOGGING, MEMORY, SESSION, LLM_CONTEXT,
        TOOL_INPUT, API_RESPONSE, TELEMETRY).
        """
        findings = self.inspect(text)
        types = [f.type for f in findings]
        decision = self._policy_engine.evaluate_pii(types, context)
        return PrivacyDecision(
            allowed=decision.allowed,
            action=decision.action,
            reason=decision.reason,
            policy=decision.policy,
            findings=tuple(findings),
        )

    def validate_destination(self, text: str, destination: str) -> PrivacyDecision:
        """Alias for decide() — plan.md Step 6.6's requested method name for the same operation."""
        return self.decide(text, destination)

    def redact(self, text: str, findings: Optional[list[PIIFinding]] = None) -> str:
        """
        Replaces every finding's span with `[REDACTED_<TYPE>]`. Processes
        spans in reverse position order so earlier indices stay valid
        while later ones are rewritten. Does not itself decide whether
        redaction is warranted — callers use decide()'s action to decide
        whether to call this at all (or sanitize() below does it for
        them, gated by decide()).
        """
        if not isinstance(text, str) or not text:
            return text
        findings = findings if findings is not None else self.inspect(text)
        if not findings:
            return text
        result = text
        for finding in sorted(findings, key=lambda f: f.start, reverse=True):
            result = result[: finding.start] + f"[REDACTED_{finding.type.value}]" + result[finding.end :]
        return result

    def sanitize(self, value: Any, context: str = "LOGGING") -> Any:
        """
        Recursively sanitizes strings, dicts, lists, and tuples (plan.md
        Step 6.7's nested-structure requirement) — a structured log
        event, a tool payload, or arbitrary metadata can be handed to
        this directly. Every string leaf is inspected and, if
        decide(context) says REDACT or BLOCK, replaced:
        REDACT -> the string with PII spans redacted in place;
        BLOCK   -> the literal placeholder "[BLOCKED_CONTENT]" (the whole
                   string, since a partial redaction would still leak
                   context BLOCK is meant to fully withhold);
        ALLOW/RESTRICT -> RESTRICT is treated as REDACT here (sanitize()
                   has no separate concept of "usable but limited" beyond
                   redaction — see PHASE_6 report's Limitations); ALLOW
                   passes the string through unchanged.
        Non-string leaves (int/float/bool/None/etc.) pass through as-is.
        """
        if isinstance(value, str):
            decision = self.decide(value, context)
            if decision.action == "BLOCK":
                return "[BLOCKED_CONTENT]" if decision.findings else value
            if decision.action in ("REDACT", "RESTRICT"):
                return self.redact(value, list(decision.findings)) if decision.findings else value
            return value
        if isinstance(value, dict):
            return {k: self.sanitize(v, context) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            sanitized = [self.sanitize(v, context) for v in value]
            return type(value)(sanitized)
        return value

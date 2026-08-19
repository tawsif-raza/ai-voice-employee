"""
Typed PII/privacy models (Phase 6; plan.md Steps 6.2).

Taxonomy note (plan.md: "Do not blindly use this exact taxonomy if the
repository already has a domain taxonomy"): configs/policies/privacy.yaml
already has a privacy-relevant taxonomy from Phase 3/5 — but it's a
different *dimension*: `restricted_fields` governs named record KEYS
(e.g. a MemoryRecord whose `key` is literally "medical_condition").
`PIIType` here governs DETECTED CONTENT PATTERNS found by scanning free
text (e.g. an email address embedded anywhere in a message), regardless
of which field it appears in. Both are real, complementary, and both feed
the same PolicyEngine (see policy_engine.py's new evaluate_pii()) — this
is not a competing taxonomy, it's the other half of a two-dimensional one.

Honesty note (plan.md Step 6.3: "Do not pretend regex can perfectly
detect all PII"): NAME, ADDRESS, and DATE_OF_BIRTH are declared here as
taxonomy values because plan.md's illustrative list includes them, but
pii_detector.py does NOT attempt to detect them. Free-text names have no
reliable regex signature; addresses are unbounded free text; and a bare
date pattern cannot be distinguished from, say, an appointment date
without semantic understanding this deterministic detector deliberately
does not attempt (ML-based classification is explicitly out of scope for
this phase). Detecting these three types is a known, documented gap — see
PHASE_6 report's Limitations section — not a silent omission.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class PIIType(str, Enum):
    EMAIL = "EMAIL"
    PHONE = "PHONE"
    ADDRESS = "ADDRESS"  # not detected — see module docstring
    DATE_OF_BIRTH = "DATE_OF_BIRTH"  # not detected — see module docstring
    IDENTIFIER = "IDENTIFIER"  # this app's own record IDs (appt_<n>, order_<n>)
    PAYMENT_INFORMATION = "PAYMENT_INFORMATION"  # credit-card-shaped digit sequences
    NAME = "NAME"  # not detected — see module docstring
    OTHER = "OTHER"


class PrivacyAction(str, Enum):
    ALLOW = "ALLOW"
    REDACT = "REDACT"
    BLOCK = "BLOCK"
    RESTRICT = "RESTRICT"


@dataclass(frozen=True)
class PIIFinding:
    type: PIIType
    start: int
    end: int
    confidence: float
    value: str  # the raw matched substring -- used internally for redaction; callers should not log/expose this field itself

    def to_dict(self) -> dict:
        return {"type": self.type.value, "start": self.start, "end": self.end, "confidence": self.confidence}


@dataclass(frozen=True)
class PrivacyDecision:
    """
    Wraps PolicyEngine.evaluate_pii()'s PolicyDecision (Phase 3's uniform
    {allowed, policy, rule, action, reason} shape) with the PIIFinding
    list that produced it — plan.md Step 6.2's requested shape. Same
    reconciliation pattern as RoutingDecision (Phase 2): PolicyDecision
    itself is never extended with a findings field; this is a separate,
    Phase-6-specific wrapper.
    """

    allowed: bool
    action: str
    reason: str
    policy: str
    findings: tuple[PIIFinding, ...] = ()

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "action": self.action,
            "reason": self.reason,
            "policy": self.policy,
            "findings": [f.to_dict() for f in self.findings],
        }

"""
Deterministic PII detector (Phase 6; plan.md Step 6.3).

Regex/pattern-based only — no ML classification, per plan.md's explicit
instruction ("Avoid unnecessary ML-based PII classification in this
phase"). Only detects structured, reliably-patterned content: email,
phone, payment-shaped digit sequences, and this application's own record
identifiers. See privacy_models.py's module docstring for which PIIType
values are deliberately NOT detected here, and why.

Designed so a stronger detector can be swapped in later without changing
PrivacyService/PolicyEngine consumers: everything downstream only depends
on this class producing `list[PIIFinding]` from `detect()`.
"""

import re

from privacy_models import PIIFinding, PIIType

# Ordered by specificity — a higher-priority pattern claims its span
# first, and lower-priority detectors skip already-claimed spans, so a
# credit-card-shaped run of digits inside what could also loosely match
# a phone pattern isn't double-reported.
_EMAIL_PATTERN = re.compile(r"\b[\w.+-]+@[\w-]+\.[a-zA-Z]{2,}\b")
_PAYMENT_PATTERN = re.compile(r"\b(?:\d[ -]?){13,16}\b")
_PHONE_PATTERN = re.compile(r"(?<!\d)(?:\+?\d{1,2}[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)")
_IDENTIFIER_PATTERN = re.compile(r"\b(?:appt|order)_\d+\b")


def _non_overlapping_matches(pattern: re.Pattern, text: str, claimed: list[tuple[int, int]]) -> list[re.Match]:
    matches = []
    for match in pattern.finditer(text):
        start, end = match.start(), match.end()
        if any(start < c_end and end > c_start for c_start, c_end in claimed):
            continue
        matches.append(match)
        claimed.append((start, end))
    return matches


class PIIDetector:
    def detect_email(self, text: str, claimed: list[tuple[int, int]] = None) -> list[PIIFinding]:
        claimed = claimed if claimed is not None else []
        return [
            PIIFinding(type=PIIType.EMAIL, start=m.start(), end=m.end(), confidence=0.95, value=m.group(0))
            for m in _non_overlapping_matches(_EMAIL_PATTERN, text, claimed)
        ]

    def detect_payment(self, text: str, claimed: list[tuple[int, int]] = None) -> list[PIIFinding]:
        claimed = claimed if claimed is not None else []
        findings = []
        for m in _non_overlapping_matches(_PAYMENT_PATTERN, text, claimed):
            digits = re.sub(r"[ -]", "", m.group(0))
            if len(digits) in (13, 14, 15, 16):
                findings.append(
                    PIIFinding(
                        type=PIIType.PAYMENT_INFORMATION, start=m.start(), end=m.end(), confidence=0.7, value=m.group(0)
                    )
                )
        return findings

    def detect_phone(self, text: str, claimed: list[tuple[int, int]] = None) -> list[PIIFinding]:
        claimed = claimed if claimed is not None else []
        return [
            PIIFinding(type=PIIType.PHONE, start=m.start(), end=m.end(), confidence=0.8, value=m.group(0))
            for m in _non_overlapping_matches(_PHONE_PATTERN, text, claimed)
        ]

    def detect_identifier(self, text: str, claimed: list[tuple[int, int]] = None) -> list[PIIFinding]:
        claimed = claimed if claimed is not None else []
        return [
            PIIFinding(type=PIIType.IDENTIFIER, start=m.start(), end=m.end(), confidence=0.99, value=m.group(0))
            for m in _non_overlapping_matches(_IDENTIFIER_PATTERN, text, claimed)
        ]

    def detect(self, text: str) -> list[PIIFinding]:
        """Runs every sub-detector in priority order (email > payment > phone > identifier), non-overlapping, and returns findings sorted by position."""
        if not isinstance(text, str) or not text:
            return []
        claimed: list[tuple[int, int]] = []
        findings: list[PIIFinding] = []
        findings += self.detect_email(text, claimed)
        findings += self.detect_payment(text, claimed)
        findings += self.detect_phone(text, claimed)
        findings += self.detect_identifier(text, claimed)
        return sorted(findings, key=lambda f: f.start)

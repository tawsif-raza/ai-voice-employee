"""
Known clinical-guard gaps, found while auditing for the Decision/Routing
Layer (docs/DECISION_ROUTING.md, "Pre-existing safety finding").

These medication questions are NOT caught by configs/clinical_triggers.yaml
today, so they reach the LLM instead of the pharmacist handoff. The router
never shortcuts them (tests/test_decision_router.py), but it does not fix
the guard either: changing the clinical safety authority is a separate,
owner-approved change. Strict xfail -- when the guard is fixed these start
passing, fail as XPASS, and the marker must be removed.
"""

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src" / "inference"))

from handoff_detector import HandoffDetector  # noqa: E402

GAPS = [
    "Can I take this medicine twice?",
    "Can I take this medicine twice a day?",
    "Can I take this with another medicine?",
    "hello, can I take ibuprofen with warfarin",
    # Confirmed in the 2026-10-09 final router review (same root cause:
    # dosage / frequency / stopping questions without a trigger keyword).
    "Is this dosage safe?",
    "Should I increase my dose?",
    "Can I stop taking this medicine?",
    "What happens if I take two tablets?",
]


@pytest.mark.xfail(strict=True, reason="pre-existing clinical-guard gap; fix needs owner approval")
@pytest.mark.parametrize("text", GAPS)
def test_clinical_guard_catches_frequency_and_interaction_questions(text):
    assert HandoffDetector(_ROOT / "configs" / "clinical_triggers.yaml").score(text).is_handoff

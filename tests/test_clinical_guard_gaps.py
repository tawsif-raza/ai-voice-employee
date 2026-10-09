"""
Clinical-guard gaps found while auditing for the Decision/Routing Layer
(docs/DECISION_ROUTING.md) and fixed by the clinical safety hardening
(docs/CLINICAL_SAFETY.md, configs/clinical_triggers.yaml).

These medication questions used to score 0 (or below threshold) and reach
the LLM instead of the clinical safety response. They were pinned here as
strict xfail; they now pass as ordinary regression tests. The end-to-end
proof that they never reach Gemini or Groq is in
tests/test_clinical_safety_hardening.py.
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


@pytest.mark.parametrize("text", GAPS)
def test_clinical_guard_catches_frequency_and_interaction_questions(text):
    assert HandoffDetector(_ROOT / "configs" / "clinical_triggers.yaml").score(text).is_handoff

"""
Intent Engine — deterministic, config-driven intent classification and
routing decisions (docs/MODULES.md §3; Phase 2).

The LLM has no role in this module and no authority over routing: this is
a rule-based classifier (normalize -> exact phrase -> regex -> keyword
overlap layers), the same pattern already established by
src/inference/handoff_detector.py's HandoffDetector for clinical/handoff
detection -- config-driven, offline-testable, no model call. It
deliberately reuses that module's normalize() rather than reimplementing
text normalization a third time, closing the exact gap
docs/MODULES_REVIEW.md finding 4.2 predicted Intent Engine would hit.

IMPORTANT — frozen-contract note: docs/DOMAIN_MODEL.md's IntentResult
entity (Group 2) is frozen with EXACTLY two fields, `intent` and
`confidence`, and states "Optional Fields: None" explicitly. The Phase 2
task asked for a result shaped like {intent, confidence, route, reason}.
Rather than silently extending the frozen IntentResult contract with two
new fields, this module keeps IntentResult exactly as frozen and
introduces a separate, new entity — RoutingDecision — that wraps an
IntentResult and adds `route`/`reason` as routing metadata. RoutingDecision
is not itself part of docs/DOMAIN_MODEL.md (Intent Engine is listed there
as "not yet implemented"); its `.to_dict()` produces the exact
{intent, confidence, route, reason} shape requested. This reconciliation
is reported explicitly rather than resolved by quietly changing the
frozen document — see the Phase 2 completion report.
"""

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

_INFERENCE_DIR = str(Path(__file__).resolve().parents[1] / "inference")
if _INFERENCE_DIR not in sys.path:
    sys.path.insert(0, _INFERENCE_DIR)
from handoff_detector import normalize  # noqa: E402 - reused, not reimplemented (MODULES_REVIEW.md 4.2)

_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "configs" / "intent_taxonomy.yaml"


class Route:
    """
    Where a classified intent sends ConversationManager. Not all routes
    have a real backing implementation yet — TOOL_ORCHESTRATOR and
    BILLING_WORKFLOW currently fall through to the existing RAG -> LLM
    generation path (see conversation_manager.py's handle_turn()): per
    ADR-003, Tool Orchestrator does not exist yet, and this phase is
    explicitly scoped to routing decisions only, not execution.
    """

    RAG_LLM = "RAG_LLM"
    TOOL_ORCHESTRATOR = "TOOL_ORCHESTRATOR"
    BILLING_WORKFLOW = "BILLING_WORKFLOW"
    HUMAN_HANDOFF = "HUMAN_HANDOFF"
    CLARIFICATION = "CLARIFICATION"


@dataclass(frozen=True)
class IntentResult:
    """
    Exactly docs/DOMAIN_MODEL.md's frozen IntentResult contract (Group 2):
    intent (string, from the configured taxonomy) + confidence (float,
    0.0-1.0). No other fields — see this module's docstring.
    """

    intent: str
    confidence: float


@dataclass(frozen=True)
class RoutingDecision:
    """
    Wraps a frozen-contract IntentResult with routing metadata. See this
    module's docstring for why `route`/`reason` live here rather than on
    IntentResult itself.
    """

    intent_result: IntentResult
    route: str
    reason: str

    @property
    def intent(self) -> str:
        return self.intent_result.intent

    @property
    def confidence(self) -> float:
        return self.intent_result.confidence

    def to_dict(self) -> dict:
        return {
            "intent": self.intent,
            "confidence": self.confidence,
            "route": self.route,
            "reason": self.reason,
        }


# Mirrors configs/intent_taxonomy.yaml. Used if that file is missing,
# unreadable, or malformed, so classification never silently goes empty —
# same resilience guarantee HandoffDetector's _BUILTIN_DEFAULT_CONFIG
# provides. Deliberately minimal (not a full copy of the YAML) since its
# only job is "keep working, conservatively," not "match the tuned
# taxonomy exactly."
_BUILTIN_DEFAULT_CONFIG: dict = {
    "decision_threshold": 0.5,
    "ambiguity_margin": 0.08,
    "intents": {
        "FAQ": {
            "route": Route.RAG_LLM,
            "exact_weight": 0.9,
            "exact_phrases": ["business hours", "return policy", "shipping time"],
        },
        "HUMAN_HANDOFF": {
            "route": Route.HUMAN_HANDOFF,
            "exact_weight": 0.92,
            "exact_phrases": ["speak to a human", "talk to a person", "connect me to an agent"],
        },
    },
}


class IntentEngine:
    """
    Deterministic intent classifier and router (docs/MODULES.md §3).

    classify() runs every configured intent's matcher and returns the
    highest-scoring RoutingDecision, or an UNKNOWN/CLARIFICATION decision
    if nothing scores above `decision_threshold`, or if the top two
    candidates are too close to call (`ambiguity_margin`) — "route to
    clarification rather than hallucinating intent," per the Phase 2
    task. Unclassifiable/malformed input degrades to the same UNKNOWN
    result rather than raising, matching MODULES.md §3 Error Handling
    ("Unclassifiable input defaults to a conservative fallback intent...
    rather than raising").
    """

    DEFAULT_CONFIG_PATH = _DEFAULT_CONFIG_PATH
    UNKNOWN_INTENT = "UNKNOWN"

    def __init__(self, config_path: Optional[str] = None):
        path = Path(config_path) if config_path else self.DEFAULT_CONFIG_PATH
        self._apply_config(self._load_config(path))

    @classmethod
    def from_config(cls, config: dict) -> "IntentEngine":
        """Build an IntentEngine directly from a config dict, bypassing YAML file I/O. Mainly for tests."""
        self = cls.__new__(cls)
        self._apply_config(config)
        return self

    @staticmethod
    def _load_config(path: Path) -> dict:
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    loaded = yaml.safe_load(f)
                if loaded:
                    return loaded
            except (OSError, yaml.YAMLError):
                pass
        return _BUILTIN_DEFAULT_CONFIG

    def _apply_config(self, config: dict) -> None:
        self.decision_threshold = float(config.get("decision_threshold", 0.5))
        self.ambiguity_margin = float(config.get("ambiguity_margin", 0.08))

        self._intents: dict[str, dict] = {}
        for name, spec in (config.get("intents") or {}).items():
            spec = spec or {}
            self._intents[name] = {
                "route": spec.get("route", Route.CLARIFICATION),
                "exact_weight": float(spec.get("exact_weight", 0.9)),
                "exact_phrases": [p.lower() for p in spec.get("exact_phrases", [])],
                "regex_weight": float(spec.get("regex_weight", 0.85)),
                "regex_patterns": [re.compile(p, re.IGNORECASE) for p in spec.get("regex_patterns", [])],
                "keyword_weight": float(spec.get("keyword_weight", 0.6)),
                "keywords": [k.lower() for k in spec.get("keywords", [])],
            }

    # ── Scoring ──────────────────────────────────────────────────────────────

    @staticmethod
    def _score_one(normalized_text: str, spec: dict) -> tuple[float, str]:
        """Highest-confidence layer match for a single intent's config. Returns (0.0, '') if nothing matched."""
        best_score, best_evidence = 0.0, ""

        for phrase in spec["exact_phrases"]:
            if phrase in normalized_text and spec["exact_weight"] > best_score:
                best_score, best_evidence = spec["exact_weight"], f"matched phrase '{phrase}'"

        for pattern in spec["regex_patterns"]:
            match = pattern.search(normalized_text)
            if match and spec["regex_weight"] > best_score:
                best_score, best_evidence = spec["regex_weight"], f"matched pattern '{match.group(0)}'"

        if spec["keywords"]:
            hits = [kw for kw in spec["keywords"] if kw in normalized_text]
            # Require at least two corroborating keyword hits before the
            # keyword layer contributes anything. A single generic word
            # (e.g. "hours" alone, "human" alone) is too weak a signal on
            # its own and previously caused unrelated messages to score
            # just enough to be misrouted to clarification instead of
            # falling through to normal generation — exact_phrases/regex
            # remain the layer responsible for single-signal confidence.
            if len(hits) >= 2:
                fraction = len(hits) / len(spec["keywords"])
                keyword_score = spec["keyword_weight"] * min(1.0, 0.5 + fraction)
                if keyword_score > best_score:
                    best_score, best_evidence = keyword_score, f"matched keyword(s) {hits}"

        return best_score, best_evidence

    # ── Public API ───────────────────────────────────────────────────────────

    def classify(self, message, history: Optional[list] = None) -> RoutingDecision:
        """
        Classify `message` and return a routing decision. `history` is
        accepted for interface compatibility with docs/MODULES.md §3's
        classify(message, history) signature but is not used by this
        deterministic, single-turn classifier in Phase 2 — a documented
        limitation, not an oversight (see Phase 2 completion report).

        Two distinct UNKNOWN outcomes, deliberately routed differently:

        - Zero signal (no configured intent matched anything at all) —
          nothing in the business taxonomy applies, so this routes to
          RAG_LLM (falls through to normal generation), the same way a
          general-knowledge or chit-chat message is handled today. This
          matters for backward compatibility: the existing evaluation
          benchmark's out-of-domain cases ("tell me a joke", "what's the
          capital of France") must keep reaching the model normally, not
          get blocked because they don't match a business intent.
        - Weak or ambiguous signal (something matched, but below
          `decision_threshold`, or the top two candidates are within
          `ambiguity_margin` of each other) — routes to CLARIFICATION.
          This is the "route to clarification rather than hallucinating
          intent" behavior the Phase 2 task specifies for genuinely
          unclear requests (e.g. "I need something tomorrow.").
        """
        if not isinstance(message, str) or not message.strip():
            return self._fallback(confidence=0.0, reason="empty or non-string input")

        normalized = normalize(message)

        scored: list[tuple[str, float, str]] = []
        for name, spec in self._intents.items():
            score, evidence = self._score_one(normalized, spec)
            if score > 0.0:
                scored.append((name, score, evidence))

        if not scored:
            return self._fallback(confidence=0.0, reason="no configured intent matched")

        scored.sort(key=lambda item: item[1], reverse=True)
        top_name, top_score, top_evidence = scored[0]

        ambiguous = False
        second_name = second_score = None
        if len(scored) > 1:
            second_name, second_score, _ = scored[1]
            ambiguous = (top_score - second_score) < self.ambiguity_margin

        if top_score < self.decision_threshold or ambiguous:
            reason = (
                f"ambiguous between '{top_name}' ({top_score:.2f}) and '{second_name}' ({second_score:.2f})"
                if ambiguous
                else f"best match '{top_name}' scored {top_score:.2f}, below threshold {self.decision_threshold}"
            )
            return RoutingDecision(
                intent_result=IntentResult(intent=self.UNKNOWN_INTENT, confidence=round(top_score, 4)),
                route=Route.CLARIFICATION,
                reason=reason,
            )

        confidence = max(0.0, min(1.0, top_score))
        return RoutingDecision(
            intent_result=IntentResult(intent=top_name, confidence=round(confidence, 4)),
            route=self._intents[top_name]["route"],
            reason=top_evidence,
        )

    def _fallback(self, confidence: float, reason: str) -> RoutingDecision:
        """Zero-signal default: classified UNKNOWN but routed to RAG_LLM (not blocked) — see classify()'s docstring."""
        return RoutingDecision(
            intent_result=IntentResult(intent=self.UNKNOWN_INTENT, confidence=confidence),
            route=Route.RAG_LLM,
            reason=reason,
        )

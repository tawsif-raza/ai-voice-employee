"""
Layered handoff-intent detector.

Replaces the old exact-substring-only check in predict.py. The eval
benchmark (outputs/evaluation/failure_analysis.md, cases 12 & 14) showed
the model already agreeing to hand off ("connect you with one of our
experienced agents", "transfer you to one of our dedicated managers")
while the old fixed HANDOFF_PHRASES list missed both paraphrases —
that was a detector bug, not a model failure, and this module is the fix.

Four layers run over every response, cheapest/most-precise first, fuzziest
last. Each layer that fires reports a confidence; the highest wins:

  1. normalize()        - lowercase, strip punctuation, collapse whitespace.
  2. exact_phrases       - the original substring list (fast path, highest
                           confidence, kept for backward compatibility).
  3. regex_patterns      - tolerates inserted words and a wider vocabulary
                           (manager, supervisor, specialist, ...).
  4. synonyms            - fires when an "action" word (connect, transfer,
                           escalate, ...) and a "target" word (agent, rep,
                           manager, ...) both appear within a small word
                           window, in any order/structure.
  5. semantic_examples   - lightweight lexical-similarity match (token
                           overlap + sequence ratio) against canonical
                           example sentences. Deliberately not an
                           embedding model, to keep this dependency-free
                           and fast on CPU.

All thresholds, phrases, and examples live in configs/handoff_phrases.yaml
so new paraphrases can be added without touching code.
"""

import difflib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

import yaml

# ── Text normalization ──────────────────────────────────────────────────────

_PUNCT_RE = re.compile(r"[^\w\s]")
_WS_RE = re.compile(r"\s+")
_CLAUSE_SPLIT_RE = re.compile(r"[.!?;\n]+")


def normalize(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    text = text.lower()
    text = _PUNCT_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    return text


def _split_clauses(text: str) -> list[str]:
    """
    Split on sentence/clause boundaries, before punctuation is stripped.
    Used by the semantic layer so a short canonical example is compared
    against comparably-sized chunks of a longer response, not the whole
    multi-sentence reply at once.
    """
    parts = [p.strip() for p in _CLAUSE_SPLIT_RE.split(text.lower()) if p.strip()]
    return parts or ([text.strip().lower()] if text.strip() else [])


def _similarity(a: str, b: str) -> float:
    """Average of word-set Jaccard overlap and difflib sequence ratio."""
    a_words, b_words = set(a.split()), set(b.split())
    if not a_words or not b_words:
        return 0.0
    jaccard = len(a_words & b_words) / len(a_words | b_words)
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    return (jaccard + ratio) / 2


def _word_matches(text_word: str, phrase_word: str) -> bool:
    """Exact match, or a naive singular/plural match ("agents" <-> "agent") in either direction."""
    if text_word == phrase_word:
        return True
    return text_word == phrase_word + "s" or phrase_word == text_word + "s"


def _phrase_matches_at(words: list[str], start: int, phrase_words: list[str]) -> bool:
    return all(_word_matches(words[start + j], phrase_words[j]) for j in range(len(phrase_words)))


def _find_all_positions(words: list[str], phrases: list[str]) -> list[tuple[int, str]]:
    """Word-index positions where each (possibly multi-word) phrase occurs as a contiguous subsequence of `words` (plural-tolerant)."""
    results: list[tuple[int, str]] = []
    n = len(words)
    for phrase in phrases:
        p_words = phrase.split()
        m = len(p_words)
        if m == 0 or m > n:
            continue
        for i in range(n - m + 1):
            if _phrase_matches_at(words, i, p_words):
                results.append((i, phrase))
    return results


def _contains_phrase(words: list[str], phrase: str) -> bool:
    p_words = phrase.split()
    m = len(p_words)
    n = len(words)
    if m == 0 or m > n:
        return False
    return any(_phrase_matches_at(words, i, p_words) for i in range(n - m + 1))


# ── Config ───────────────────────────────────────────────────────────────────

_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "configs" / "handoff_phrases.yaml"

# Mirrors configs/handoff_phrases.yaml. Used if that file is missing,
# unreadable, or malformed, so detection never silently goes empty —
# same resilience guarantee the old hard-coded HANDOFF_PHRASES list had.
_BUILTIN_DEFAULT_CONFIG: dict = {
    "decision_threshold": 0.6,
    "exact_phrases": {
        "weight": 0.95,
        "phrases": [
            "connect you to a human",
            "connect you with a human",
            "connect you to an agent",
            "connect you with an agent",
            "connect you to a representative",
            "connect you with a representative",
            "connect you to someone",
            "transfer you to a human",
            "transfer you to an agent",
            "transfer you to a representative",
            "transfer your call",
            "transfer this call",
            "speak with a human",
            "speak to a human",
            "speak with a representative",
            "speak to a representative",
            "speak with an agent",
            "speak to an agent",
            "talk to a human",
            "talk to a representative",
            "talk to an agent",
            "human agent",
            "live agent",
            "customer service representative",
            "escalate this",
            "escalate you",
            "escalate your",
            "get you a human",
            "get a human",
            "reach a representative",
            "reach a human agent",
        ],
    },
    "regex_patterns": {
        "weight": 0.9,
        "patterns": [
            r"\b(connect|transfer|get|put|hand|loop|forward|route)\s+(you|me)?\s*(over|through)?\s*(with|to)?\s+"
            r"(a|an|one of (our|the)|your|my)?\s*(experienced |dedicated |senior |live )?"
            r"(human|agent|reps?|representatives?|specialists?|managers?|supervisors?|team\s*members?|someone|person)",
            r"\b(speak|talk|chat)\s+(with|to)\s+(a|an|one of (our|the))?\s*"
            r"(human|agent|reps?|representatives?|specialists?|managers?|supervisors?|someone|person)",
            r"\bescalat\w*\b",
            r"\bhuman\s+assistance\b",
            r"\bcustomer\s+care\b",
        ],
    },
    "synonyms": {
        "weight": 0.8,
        "proximity_window": 8,
        "actions": [
            "connect",
            "transfer",
            "escalate",
            "forward",
            "reach",
            "put you through",
            "get you",
            "hand you off",
            "loop in",
            "route you",
            "bring in",
        ],
        "targets": [
            "human",
            "agent",
            "representative",
            "rep",
            "specialist",
            "manager",
            "supervisor",
            "team member",
            "someone",
            "person",
            "live person",
            "customer care",
            "support representative",
            "human assistance",
        ],
        "standalone_targets": [
            "human assistance",
            "customer care team",
            "support representative",
            "live agent",
            "human agent",
        ],
    },
    "semantic_examples": {
        "weight": 0.7,
        "similarity_threshold": 0.55,
        "examples": [
            "let me connect you with someone who can help",
            "I will transfer this call to a specialist",
            "our team member will assist you shortly",
            "you will be speaking with a live agent",
            "I can get you human assistance right away",
            "allow me to bring in a support representative",
            "I am connecting you to our customer care team",
        ],
    },
}


# ── Result type ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class HandoffMatch:
    """Result of scoring one response. `bool(match)` behaves like `is_handoff` for backward compatibility."""

    is_handoff: bool
    confidence: float
    layer: Optional[str] = None
    evidence: Optional[str] = None

    def __bool__(self) -> bool:
        return self.is_handoff


# ── Detector ─────────────────────────────────────────────────────────────────

class HandoffDetector:
    """Layered handoff-intent detector. See module docstring for the layer order."""

    DEFAULT_CONFIG_PATH = _DEFAULT_CONFIG_PATH
    # Backward-compat: VoiceAssistantInference.HANDOFF_PHRASES used to be
    # the sole source of truth. It now just points at this list.
    DEFAULT_EXACT_PHRASES = _BUILTIN_DEFAULT_CONFIG["exact_phrases"]["phrases"]

    def __init__(self, config_path: Optional[Union[str, Path]] = None):
        path = Path(config_path) if config_path else self.DEFAULT_CONFIG_PATH
        self._apply_config(self._load_config(path))

    @classmethod
    def from_config(cls, config: dict) -> "HandoffDetector":
        """Build a detector directly from a config dict, bypassing YAML file I/O. Mainly for tests and programmatic construction."""
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
        self.decision_threshold = float(config.get("decision_threshold", 0.6))

        exact_cfg = config.get("exact_phrases") or {}
        self._exact_weight = float(exact_cfg.get("weight", 0.95))
        self._exact_phrases = [p.lower() for p in exact_cfg.get("phrases", [])]

        regex_cfg = config.get("regex_patterns") or {}
        self._regex_weight = float(regex_cfg.get("weight", 0.9))
        self._regex_patterns = [re.compile(p, re.IGNORECASE) for p in regex_cfg.get("patterns", [])]

        syn_cfg = config.get("synonyms") or {}
        self._syn_weight = float(syn_cfg.get("weight", 0.8))
        self._proximity_window = int(syn_cfg.get("proximity_window", 8))
        self._action_phrases = [p.lower() for p in syn_cfg.get("actions", [])]
        self._target_phrases = [p.lower() for p in syn_cfg.get("targets", [])]
        self._standalone_targets = [p.lower() for p in syn_cfg.get("standalone_targets", [])]

        sem_cfg = config.get("semantic_examples") or {}
        self._semantic_weight = float(sem_cfg.get("weight", 0.7))
        self._semantic_threshold = float(sem_cfg.get("similarity_threshold", 0.55))
        self._semantic_examples = [(normalize(ex), ex) for ex in sem_cfg.get("examples", [])]

    # ── Layers ───────────────────────────────────────────────────────────────

    def _match_exact(self, normalized_text: str) -> Optional[tuple[float, str]]:
        for phrase in self._exact_phrases:
            if phrase in normalized_text:
                return self._exact_weight, phrase
        return None

    def _match_regex(self, normalized_text: str) -> Optional[tuple[float, str]]:
        for pattern in self._regex_patterns:
            m = pattern.search(normalized_text)
            if m:
                return self._regex_weight, m.group(0)
        return None

    def _match_synonyms(self, words: list[str]) -> Optional[tuple[float, str]]:
        for target in self._standalone_targets:
            if _contains_phrase(words, target):
                return self._syn_weight * 0.95, f"standalone:{target}"

        action_positions = _find_all_positions(words, self._action_phrases)
        target_positions = _find_all_positions(words, self._target_phrases)
        if not action_positions or not target_positions:
            return None

        best: Optional[tuple[float, str]] = None
        best_distance = None
        for a_start, a_phrase in action_positions:
            for t_start, t_phrase in target_positions:
                distance = abs(a_start - t_start)
                if distance <= self._proximity_window and (best_distance is None or distance < best_distance):
                    best_distance = distance
                    best = (self._syn_weight, f'"{a_phrase}" + "{t_phrase}" (word distance={distance})')
        return best

    def _match_semantic(self, raw_text: str) -> Optional[tuple[float, str]]:
        if not self._semantic_examples:
            return None
        best_sim = 0.0
        best_example = None
        for clause in _split_clauses(raw_text):
            norm_clause = normalize(clause)
            if not norm_clause:
                continue
            for example_norm, example_raw in self._semantic_examples:
                sim = _similarity(norm_clause, example_norm)
                if sim > best_sim:
                    best_sim = sim
                    best_example = example_raw
        if best_example is not None and best_sim >= self._semantic_threshold:
            return self._semantic_weight * best_sim, f'~"{best_example}" (similarity={best_sim:.2f})'
        return None

    # ── Public API ───────────────────────────────────────────────────────────

    def score(self, text: str) -> HandoffMatch:
        """Run every layer and keep the highest-confidence match."""
        if not text or not text.strip():
            return HandoffMatch(is_handoff=False, confidence=0.0)

        normalized = normalize(text)
        words = normalized.split()

        candidates: list[tuple[str, float, str]] = []
        for layer_name, match in (
            ("exact", self._match_exact(normalized)),
            ("regex", self._match_regex(normalized)),
            ("synonym", self._match_synonyms(words)),
            ("semantic", self._match_semantic(text)),
        ):
            if match is not None:
                confidence, evidence = match
                candidates.append((layer_name, confidence, evidence))

        if not candidates:
            return HandoffMatch(is_handoff=False, confidence=0.0)

        layer, confidence, evidence = max(candidates, key=lambda c: c[1])
        confidence = max(0.0, min(1.0, confidence))
        return HandoffMatch(
            is_handoff=confidence >= self.decision_threshold,
            confidence=round(confidence, 4),
            layer=layer,
            evidence=evidence,
        )

    def detect(self, text: str) -> bool:
        """Backward-compatible boolean check. Equivalent to `self.score(text).is_handoff`."""
        return self.score(text).is_handoff


# ── Module-level convenience (default config, lazily built) ─────────────────

_default_detector: Optional[HandoffDetector] = None


def get_default_detector() -> HandoffDetector:
    global _default_detector
    if _default_detector is None:
        _default_detector = HandoffDetector()
    return _default_detector


def detect_handoff(text: str) -> bool:
    """Module-level convenience wrapper using the default detector/config."""
    return get_default_detector().detect(text)

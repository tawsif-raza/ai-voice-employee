"""
Decision/Routing Layer -- picks the cheapest safe way to answer a turn.

An optimization layer, not a safety layer. ConversationManager calls
DecisionRouter.decide() once per turn, strictly AFTER the clinical guard,
the session/pending-workflow checks, IntentEngine and PolicyEngine have
run. Those stay authoritative:

- The clinical block, clarification and tool execution keep their own
  existing conditions in conversation_manager.py; none of them reads the
  router's decision. The TOOL / CLARIFICATION / RAG / LLM routes below are
  labels for metrics and metadata only.
- The one thing the router can change is the RAG -> LLM generation path:
  for a short utterance that FULL-matches a configured greeting / goodbye
  / thanks (DETERMINISTIC) or a verified FAQ question (CACHE), the turn is
  answered with fixed text and no retrieval or LLM call. That shortcut is
  only eligible when the clinical guard ran and scored exactly 0 (no
  clinical signal at all, not merely below its threshold), generation
  policy allowed the turn, IntentEngine routed it to RAG_LLM as UNKNOWN or
  FAQ, and no workflow (confirmation / PIN) is pending on the session.

Routing is internal: the user's text is only ever compared against fixed,
operator-configured patterns. Nothing in it can name a route, a tool or an
instruction, and no request writes to the answer table -- it is built once
at construction (templates from configs/decision_routing.yaml, FAQ answers
from the knowledge base by id) and is read-only, so it cannot be poisoned
or leak one user's data to another. It holds no per-user data at all.

No model, no network, no new dependency: a handful of precompiled regexes
over at most `max_utterance_chars` characters. Any load problem disables
the router (every turn takes the existing path); decide() errors are
caught by the caller, which then takes the existing path too.

Config: configs/decision_routing.yaml. Design: docs/DECISION_ROUTING.md.
"""

import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Optional

import yaml

_INFERENCE_DIR = str(Path(__file__).resolve().parents[1] / "inference")
if _INFERENCE_DIR not in sys.path:
    sys.path.insert(0, _INFERENCE_DIR)
from handoff_detector import normalize  # noqa: E402 - same normalization as IntentEngine/clinical guard

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_CONFIG_PATH = _REPO_ROOT / "configs" / "decision_routing.yaml"

# IntentEngine values the router depends on (intent_engine.Route.RAG_LLM,
# IntentEngine.UNKNOWN_INTENT). Plain strings so this module has no import
# cycle with intent_engine; tests pin them to the real constants.
_RAG_LLM_ROUTE = "RAG_LLM"
_SHORTCUT_INTENTS = frozenset({"UNKNOWN", "FAQ"})
# Intents whose downstream path is sensitive enough that the router labels
# them ELEVATED risk (observational; never changes the path).
_ELEVATED_INTENTS = frozenset({"MEDICATION_QUESTION", "COMPLAINT", "HUMAN_HANDOFF", "BILLING"})
_APOSTROPHES_RE = re.compile(r"['’`]")


class DecisionRoute:
    """Where a turn is answered. Only DETERMINISTIC and CACHE change control flow."""

    DETERMINISTIC = "DETERMINISTIC"
    CACHE = "CACHE"
    TOOL = "TOOL"
    RAG = "RAG"
    LLM = "LLM"
    CLARIFICATION = "CLARIFICATION"
    SAFETY = "SAFETY"
    FALLBACK = "FALLBACK"

    SHORTCUTS = frozenset({DETERMINISTIC, CACHE})


class Complexity:
    SIMPLE = "SIMPLE"
    MODERATE = "MODERATE"
    COMPLEX = "COMPLEX"
    UNKNOWN = "UNKNOWN"


class Risk:
    LOW = "LOW"
    ELEVATED = "ELEVATED"
    UNKNOWN = "UNKNOWN"


class ModelChoice:
    """The router never picks a provider: generation always uses the existing Gemini -> Groq chain."""

    NONE = "NONE"
    DEFAULT_CHAIN = "DEFAULT_CHAIN"


# A turn with at least this many words is labelled COMPLEX (observational).
_COMPLEX_WORD_COUNT = 20


@dataclass(frozen=True)
class Decision:
    intent: str
    route: str
    complexity: str
    risk: str
    cacheable: bool
    model: str
    reason: str
    confidence: float = 0.0
    # Template name or knowledge-base id of a shortcut answer (never user text).
    answer_key: Optional[str] = None
    latency_ms: float = 0.0
    # The fixed shortcut answer; excluded from to_dict() (it is the response itself).
    response: Optional[str] = field(default=None, repr=False)

    def to_dict(self) -> dict:
        return {
            "intent": self.intent,
            "route": self.route,
            "complexity": self.complexity,
            "risk": self.risk,
            "cacheable": self.cacheable,
            "model": self.model,
            "reason": self.reason,
            "confidence": self.confidence,
            "answer_key": self.answer_key,
            "latency_ms": self.latency_ms,
        }


def fallback_decision(intent: str = "UNKNOWN", reason: str = "decision_router_error") -> Decision:
    """What the caller records when decide() itself failed: the existing path, unchanged."""
    return Decision(
        intent=intent,
        route=DecisionRoute.FALLBACK,
        complexity=Complexity.UNKNOWN,
        risk=Risk.UNKNOWN,
        cacheable=False,
        model=ModelChoice.DEFAULT_CHAIN,
        reason=reason,
    )


@dataclass(frozen=True)
class _Shortcut:
    route: str
    key: str
    response: str
    patterns: tuple


class DecisionRouter:
    def __init__(self, config: Optional[dict] = None, *, repo_root: Path = _REPO_ROOT):
        self.enabled = False
        self.load_error: Optional[str] = None
        self.skipped_answers: tuple = ()
        self.max_utterance_chars = 0
        self._prefixes: tuple = ()
        self._suffixes: tuple = ()
        self._shortcuts: tuple = ()
        try:
            self._apply_config(config or {}, repo_root)
        except Exception as exc:
            # Fail safe: a broken config disables the optimization, never the turn.
            self.enabled = False
            self._shortcuts = ()
            self.load_error = f"{type(exc).__name__}: {exc}"
            logger.warning("Decision router disabled -- invalid configuration (%s)", self.load_error)

    @classmethod
    def from_config_file(cls, path: Optional[Path] = None) -> "DecisionRouter":
        path = Path(path) if path else _DEFAULT_CONFIG_PATH
        try:
            with open(path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}
        except Exception as exc:
            router = cls({"enabled": False})
            router.load_error = f"{type(exc).__name__}: {exc}"
            logger.warning("Decision router disabled -- cannot read %s (%s)", path.name, router.load_error)
            return router
        return cls(config)

    def _apply_config(self, config: dict, repo_root: Path) -> None:
        if not isinstance(config, dict):
            raise ValueError("config must be a mapping")
        max_chars = int(config.get("max_utterance_chars", 120))
        if max_chars <= 0:
            raise ValueError("max_utterance_chars must be positive")

        def compile_all(items) -> tuple:
            return tuple(re.compile(str(p)) for p in (items or []))

        # Anchored so a courtesy word is only ever stripped as a whole leading
        # / trailing phrase, and never when it is the whole utterance.
        self._prefixes = tuple(re.compile(rf"^(?:{p})\s+(?=\S)") for p in config.get("courtesy_prefixes") or [])
        self._suffixes = tuple(re.compile(rf"(?<=\S)\s+(?:{p})$") for p in config.get("courtesy_suffixes") or [])

        shortcuts = []
        for name, spec in (config.get("templates") or {}).items():
            response = (spec or {}).get("response")
            if not isinstance(response, str) or not response.strip():
                raise ValueError(f"template '{name}' has no response")
            shortcuts.append(
                _Shortcut(DecisionRoute.DETERMINISTIC, str(name), response.strip(), compile_all(spec.get("patterns")))
            )

        faqs = config.get("faqs") or []
        skipped = []
        if faqs:
            answers = self._load_knowledge(repo_root / str(config.get("knowledge_file", "")))
            for entry in faqs:
                answer_id = str(entry.get("answer_id", ""))
                answer = answers.get(answer_id)
                if not answer:
                    skipped.append(answer_id)
                    continue
                shortcuts.append(_Shortcut(DecisionRoute.CACHE, answer_id, answer, compile_all(entry.get("patterns"))))
        if skipped:
            logger.warning("Decision router: %d FAQ answer(s) not found in the knowledge base", len(skipped))

        self.max_utterance_chars = max_chars
        self.skipped_answers = tuple(skipped)
        self._shortcuts = tuple(shortcuts)
        self.enabled = bool(config.get("enabled", False))

    @staticmethod
    def _load_knowledge(path: Path) -> Mapping[str, str]:
        """{id: content} from a knowledge-base JSON list. Missing file -> no FAQ answers (logged), not an error."""
        try:
            with open(path, "r", encoding="utf-8") as f:
                entries = json.load(f)
        except FileNotFoundError:
            logger.warning("Decision router: knowledge file %s not found -- FAQ answers disabled", path.name)
            return {}
        return {
            str(e["id"]): str(e["content"]).strip()
            for e in entries
            if isinstance(e, dict) and e.get("id") and isinstance(e.get("content"), str)
        }

    @property
    def answer_table(self) -> Mapping[str, str]:
        """Read-only {key: response} view (for tests and docs). There is no write API."""
        return MappingProxyType({s.key: s.response for s in self._shortcuts})

    # ── Matching ─────────────────────────────────────────────────────────────

    def _candidates(self, text: str) -> list:
        """The utterance, then with up to two leading and one trailing courtesy phrase removed."""
        canonical = normalize(_APOSTROPHES_RE.sub("", text))
        forms = [canonical]
        current = canonical
        for _ in range(2):
            for pattern in self._prefixes:
                stripped = pattern.sub("", current, count=1)
                if stripped != current:
                    current = stripped
                    forms.append(current)
                    break
            else:
                break
        for form in list(forms):
            for pattern in self._suffixes:
                stripped = pattern.sub("", form, count=1)
                if stripped != form:
                    forms.append(stripped)
                    break
        return [f for f in forms if f]

    def _match(self, text: str) -> Optional[_Shortcut]:
        for candidate in self._candidates(text):
            for shortcut in self._shortcuts:
                for pattern in shortcut.patterns:
                    if pattern.fullmatch(candidate):
                        return shortcut
        return None

    # ── Public API ───────────────────────────────────────────────────────────

    def decide(
        self,
        user_input: str,
        routing,
        *,
        generation_action: str,
        clinical_confidence: Optional[float],
        tool_action: Optional[str],
        retriever_available: bool,
        workflow_pending: bool = False,
    ) -> Decision:
        """
        Args:
            user_input:          The validated turn text.
            routing:             IntentEngine's RoutingDecision for this turn.
            generation_action:   PolicyEngine.evaluate_generation()'s action value ("ALLOW", "CLARIFY", ...).
            clinical_confidence: The clinical guard's score for this turn, or None if no guard ran.
                                 Only exactly 0.0 makes a shortcut eligible.
            tool_action:         The tool action ConversationManager's own mapping gives this intent,
                                 if a tool orchestrator is configured (label only).
            retriever_available: Whether the generation path will run retrieval (label only).
            workflow_pending:    A confirmation/PIN step is pending on the session: no shortcut.
        """
        started = time.perf_counter()
        intent = routing.intent
        confidence = float(routing.confidence or 0.0)
        clinical_signal = clinical_confidence is None or clinical_confidence > 0.0
        risk = Risk.UNKNOWN if clinical_confidence is None else Risk.LOW
        if (clinical_confidence is not None and clinical_confidence > 0.0) or intent in _ELEVATED_INTENTS:
            risk = Risk.ELEVATED

        def done(route, *, reason, complexity, model, shortcut=None, risk_override=None) -> Decision:
            return Decision(
                intent=intent,
                route=route,
                complexity=complexity,
                risk=risk_override or risk,
                cacheable=route in DecisionRoute.SHORTCUTS,
                model=model,
                reason=reason,
                confidence=confidence,
                answer_key=shortcut.key if shortcut else None,
                latency_ms=round((time.perf_counter() - started) * 1000.0, 3),
                response=shortcut.response if shortcut else None,
            )

        if generation_action == "CLARIFY":
            return done(
                DecisionRoute.CLARIFICATION,
                reason="generation policy requires clarification",
                complexity=Complexity.SIMPLE,
                model=ModelChoice.NONE,
            )
        if tool_action is not None:
            return done(
                DecisionRoute.TOOL,
                reason=f"intent maps to tool action {tool_action}",
                complexity=Complexity.SIMPLE,
                model=ModelChoice.NONE,
                risk_override=Risk.ELEVATED,
            )

        blocker = None
        if not self.enabled:
            blocker = "router disabled"
        elif workflow_pending:
            blocker = "workflow pending on session"
        elif clinical_signal:
            blocker = "clinical guard did not run" if clinical_confidence is None else "clinical signal present"
        elif generation_action != "ALLOW":
            blocker = f"generation policy action {generation_action}"
        elif routing.route != _RAG_LLM_ROUTE or intent not in _SHORTCUT_INTENTS:
            blocker = f"intent {intent} routes to {routing.route}"
        elif len(user_input) > self.max_utterance_chars:
            blocker = "utterance too long for a shortcut"

        if blocker is None:
            shortcut = self._match(user_input)
            if shortcut is not None:
                return done(
                    shortcut.route,
                    reason=f"full match: {shortcut.key}",
                    complexity=Complexity.SIMPLE,
                    model=ModelChoice.NONE,
                    shortcut=shortcut,
                )
            blocker = "no shortcut matched"

        words = len(user_input.split())
        if words >= _COMPLEX_WORD_COUNT:
            complexity = Complexity.COMPLEX
        elif intent == "UNKNOWN":
            complexity = Complexity.UNKNOWN
        else:
            complexity = Complexity.MODERATE
        return done(
            DecisionRoute.RAG if retriever_available else DecisionRoute.LLM,
            reason=blocker,
            complexity=complexity,
            model=ModelChoice.DEFAULT_CHAIN,
        )


# ── Metrics (fixed counter names, registered in metrics.py) ─────────────────

_ROUTE_COUNTERS = {
    DecisionRoute.DETERMINISTIC: "decision_route_deterministic_total",
    DecisionRoute.CACHE: "decision_route_cache_total",
    DecisionRoute.TOOL: "decision_route_tool_total",
    DecisionRoute.RAG: "decision_route_rag_total",
    DecisionRoute.LLM: "decision_route_llm_total",
    DecisionRoute.CLARIFICATION: "decision_route_clarification_total",
    DecisionRoute.SAFETY: "decision_route_safety_total",
    DecisionRoute.FALLBACK: "decision_route_fallback_total",
}
_PROVIDER_COUNTERS = {"gemini": "llm_provider_gemini_total", "groq": "llm_provider_groq_total"}


def record_decision(metrics, decision: Decision) -> None:
    """Never raises: metrics are observational. An unknown route is counted as a fallback."""
    if metrics is None:
        return
    try:
        metrics.increment("decisions_total")
        metrics.increment(_ROUTE_COUNTERS.get(decision.route, "decision_route_fallback_total"))
        if decision.route in DecisionRoute.SHORTCUTS:
            # Before this layer, every shortcut turn ran the generation path.
            metrics.increment("llm_calls_avoided_total")
        metrics.observe("decision_latency_ms", decision.latency_ms)
    except Exception:
        logger.debug("decision metrics not recorded", exc_info=True)


def record_safety_block(metrics) -> None:
    if metrics is None:
        return
    try:
        metrics.increment("decision_route_safety_total")
    except Exception:
        logger.debug("decision metrics not recorded", exc_info=True)


def record_llm_provider(metrics, final_llm: Optional[dict]) -> None:
    """Which provider actually generated the answer (the existing chain decided, not the router)."""
    if metrics is None or not final_llm:
        return
    try:
        provider = str(final_llm.get("provider") or "").lower()
        metrics.increment(_PROVIDER_COUNTERS.get(provider, "llm_provider_other_total"))
    except Exception:
        logger.debug("provider metric not recorded", exc_info=True)

"""
Regression tests for the production wiring of the clinical safety guard
(docs/MASTER_PROJECT_PLAN.md finding F-01).

docker/Dockerfile.production disables RAG (it rewrites configs/config.yaml's
`rag.enabled` to false because the lean image ships no FAISS/embedding
stack). build_conversation_manager() used to construct the clinical guard
only inside its RAG branch, so the production image ran with
`clinical_guard=None`: a dosage/drug-interaction question went straight to
the LLM instead of being handed to a pharmacist.

These tests build the manager exactly the way production does (RAG off,
remote-provider-style injected LLM) and assert that the safety guard is
present, uses the clinical trigger set, and short-circuits before the LLM.
They exercise both ways RAG can be off -- the factory flag and the config
file the production image patches -- and a working directory other than the
repo root, because HandoffDetector silently falls back to its generic
handoff phrases when its config path does not resolve.
"""

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src" / "agent"))
sys.path.insert(0, str(_ROOT / "src" / "inference"))

import conversation_manager  # noqa: E402
from conversation_manager import build_conversation_manager  # noqa: E402
from llm_provider import BaseLLMProvider  # noqa: E402

CLINICAL_QUESTION = "What dose should I take of ibuprofen with my warfarin?"
# The production image's rag section after Dockerfile.production's sed rewrite.
PRODUCTION_RAG_CONFIG = {
    "enabled": False,
    "knowledge_dir": "data/knowledge",
    "index_dir": "outputs/rag_index",
    "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
    "top_k": 3,
    "score_threshold": 0.35,
    "clinical_triggers_path": "configs/clinical_triggers.yaml",
}


class RecordingLLM(BaseLLMProvider):
    provider_name = "recording"

    def __init__(self):
        self.calls: list[list[dict]] = []

    def generate_stream(self, messages, **kwargs):
        self.calls.append(messages)
        yield "Take 800mg twice a day."
        yield {"text": "Take 800mg twice a day.", "latency_ms": 1.0}


def _final(manager, message, **kwargs) -> dict:
    *_, final = list(manager.handle_turn(message, **kwargs))
    return final


def _build(llm, **kwargs):
    return build_conversation_manager(llm_provider=llm, persistence_enabled=False, **kwargs)


def test_clinical_guard_present_when_rag_disabled_by_flag():
    manager = _build(RecordingLLM(), rag_enabled=False)
    assert manager.clinical_guard is not None


def test_clinical_guard_present_when_rag_disabled_by_config(monkeypatch):
    # server.py always calls the factory with rag_enabled=True; the
    # production image turns RAG off through configs/config.yaml instead.
    monkeypatch.setattr(conversation_manager, "_load_rag_config", lambda: dict(PRODUCTION_RAG_CONFIG))
    manager = _build(RecordingLLM())
    assert manager.retriever is None
    assert manager.clinical_guard is not None


def test_clinical_question_is_handed_off_before_llm_when_rag_disabled(monkeypatch):
    monkeypatch.setattr(conversation_manager, "_load_rag_config", lambda: dict(PRODUCTION_RAG_CONFIG))
    llm = RecordingLLM()
    manager = _build(llm)

    final = _final(manager, CLINICAL_QUESTION)

    assert final["clinical_guard_triggered"] is True
    assert final["is_handoff"] is True
    assert final["response"] == manager.CLINICAL_HANDOFF_RESPONSE
    assert llm.calls == [], "a clinical question must never reach the LLM"


def test_clinical_guard_loads_clinical_triggers_from_any_working_directory(monkeypatch, tmp_path):
    # The configured path is repo-relative. From another working directory
    # it must still resolve to configs/clinical_triggers.yaml -- otherwise
    # HandoffDetector quietly uses its generic handoff phrases and the
    # dosage question below is not caught.
    monkeypatch.setattr(conversation_manager, "_load_rag_config", lambda: dict(PRODUCTION_RAG_CONFIG))
    monkeypatch.chdir(tmp_path)
    llm = RecordingLLM()
    manager = _build(llm)

    final = _final(manager, CLINICAL_QUESTION)

    assert final["clinical_guard_triggered"] is True
    assert llm.calls == []


def test_missing_clinical_trigger_file_fails_startup(monkeypatch, tmp_path):
    # Running with HandoffDetector's generic fallback phrases would look
    # like a working guard while missing every clinical trigger.
    config = dict(PRODUCTION_RAG_CONFIG, clinical_triggers_path=str(tmp_path / "missing.yaml"))
    monkeypatch.setattr(conversation_manager, "_load_rag_config", lambda: config)

    with pytest.raises(FileNotFoundError):
        _build(RecordingLLM())


def test_ordinary_question_still_reaches_llm_when_rag_disabled(monkeypatch):
    # Control: the guard must not block normal traffic.
    monkeypatch.setattr(conversation_manager, "_load_rag_config", lambda: dict(PRODUCTION_RAG_CONFIG))
    llm = RecordingLLM()
    manager = _build(llm)

    final = _final(manager, "What are your opening hours on Saturday?")

    assert final["clinical_guard_triggered"] is False
    assert len(llm.calls) == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

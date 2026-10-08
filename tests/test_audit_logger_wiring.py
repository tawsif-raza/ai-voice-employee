"""
Regression tests for the live server's audit trail
(docs/MASTER_PROJECT_PLAN.md finding F-08).

src/api/server.py constructs one process-wide `AuditLogger()` (no
repository, no privacy service) so its authentication boundary and
ConversationManager share a trail, and hands it to
build_conversation_manager(). The factory only built the PostgreSQL-backed,
privacy-sanitizing logger when no logger was passed in -- so in the live
server:
  * audit events never reached PostgresAuditRepository, even with
    PERSISTENCE_MODE=production;
  * audit metadata was not PII-sanitized;
  * events accumulated in an unbounded in-memory list for the life of the
    process (one or more per turn).
"""

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src" / "agent"))
sys.path.insert(0, str(_ROOT / "src" / "inference"))

import conversation_manager  # noqa: E402
from audit import AuditLogger, AuditRepository  # noqa: E402
from conversation_manager import PersistenceRepositories, build_conversation_manager  # noqa: E402
from llm_provider import BaseLLMProvider  # noqa: E402
from observability_models import EventType  # noqa: E402

F08 = pytest.mark.xfail(strict=True, reason="F-08: server audit logger unpersisted/unsanitized/unbounded; fixed in H1")


class StaticLLM(BaseLLMProvider):
    provider_name = "static"

    def generate_stream(self, messages, **kwargs):
        yield "Hello."
        yield {"text": "Hello.", "latency_ms": 1.0}


class FakePersistedAuditRepository(AuditRepository):
    """Stands in for PostgresAuditRepository (same append/list interface)."""


@F08
def test_in_memory_audit_repository_is_bounded():
    repo = AuditRepository()
    logger = AuditLogger(repository=repo)
    for i in range(repo.max_events + 50):
        logger.record(EventType.POLICY_ALLOW, outcome="allowed", request_id=f"req_{i}")

    events = repo.list_events()
    assert len(events) == repo.max_events
    assert events[-1].request_id == f"req_{repo.max_events + 49}", "oldest events are evicted first"


@F08
def test_in_memory_security_events_are_bounded():
    repo = AuditRepository(max_events=10)
    logger = AuditLogger(repository=repo)
    from audit import SecurityEventDetector

    detector = SecurityEventDetector(logger)
    for i in range(25):
        detector.record_cross_user_access_attempt("session", f"user_{i}")

    assert len(repo.list_security_events()) == 10


@F08
def test_injected_audit_logger_uses_persisted_repository(monkeypatch):
    persisted = FakePersistedAuditRepository()
    monkeypatch.setattr(
        conversation_manager,
        "resolve_persistence_repositories",
        lambda persistence_enabled=True: PersistenceRepositories(audit=persisted),
    )
    shared_logger = AuditLogger()  # exactly what server.py constructs

    manager = build_conversation_manager(llm_provider=StaticLLM(), rag_enabled=False, audit_logger=shared_logger)
    list(manager.handle_turn("What are your opening hours?"))

    assert manager.audit_logger is shared_logger, "server and manager must keep sharing one trail"
    assert persisted.list_events(), "turn audit events must reach the persisted repository"


@F08
def test_injected_audit_logger_sanitizes_pii_in_metadata():
    shared_logger = AuditLogger()
    build_conversation_manager(
        llm_provider=StaticLLM(), rag_enabled=False, persistence_enabled=False, audit_logger=shared_logger
    )

    event = shared_logger.record(
        EventType.POLICY_DENY, outcome="denied", metadata={"note": "caller email is jane.doe@example.com"}
    )

    assert event is not None
    assert "jane.doe@example.com" not in str(event.metadata)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

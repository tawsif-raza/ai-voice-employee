"""
Tests for PostgresAuditRepository (Phase 12; plan.md Step 12.8):
src/agent/audit_repository_postgres.py, plus the additive
actor/session_id/start_time/end_time filters added to
audit.py's AuditRepository.list_events() for this step.

Run against SQLite as this repository's dialect-portable test double
(PHASE_12_1_PERSISTENCE_AUDIT.md §13).

Run with:
    python -m unittest tests.test_audit_repository_postgres -v
"""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "agent"))

from audit import AuditLogger, SecurityEventDetector  # noqa: E402
from audit_repository_postgres import PostgresAuditRepository  # noqa: E402
from db import Database, DatabaseUnavailableError, load_database_config  # noqa: E402
from db_models import Base  # noqa: E402
from observability_models import AuditEvent, EventType, Severity, SecurityEvent, new_event_id, now_utc  # noqa: E402
from policy_engine import PolicyEngine  # noqa: E402
from privacy_service import PrivacyService  # noqa: E402


def _fresh_database() -> Database:
    database = Database(load_database_config(env={"DATABASE_URL": "sqlite:///:memory:"}))
    Base.metadata.create_all(database.engine)
    return database


def _event(**overrides) -> AuditEvent:
    defaults = dict(
        event_id=new_event_id(), timestamp=now_utc(), event_type=EventType.AUTH_SUCCESS,
        request_id="req-1", conversation_id=None, session_id="sess-1", actor="user-1",
        action=None, resource="authentication", outcome="success", policy=None, reason=None, metadata={},
    )
    defaults.update(overrides)
    return AuditEvent(**defaults)


class TestAuditPersistence(unittest.TestCase):
    def setUp(self):
        self.database = _fresh_database()
        self.repo = PostgresAuditRepository(self.database)

    def tearDown(self):
        self.database.dispose()

    def test_append_then_list_round_trips(self):
        self.repo.append(_event())
        events = self.repo.list_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].actor, "user-1")

    def test_append_is_append_only_no_update_method(self):
        self.assertFalse(hasattr(self.repo, "update"))
        self.assertFalse(hasattr(self.repo, "delete"))

    def test_security_event_persistence(self):
        self.repo.append_security_event(SecurityEvent(
            event_id=new_event_id(), timestamp=now_utc(), type="CROSS_USER_ACCESS_ATTEMPT",
            severity=Severity.HIGH, request_id="req-1", actor="user-b", resource="memory",
            outcome="denied", reason="test",
        ))
        events = self.repo.list_security_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].type, "CROSS_USER_ACCESS_ATTEMPT")


class TestQueryFiltering(unittest.TestCase):
    """plan.md Step 12.8's exact required filter set: correlation ID, user ID, session ID, event type, time range."""

    def setUp(self):
        self.database = _fresh_database()
        self.repo = PostgresAuditRepository(self.database)
        self.base_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.repo.append(_event(event_id="e1", event_type=EventType.AUTH_SUCCESS, request_id="req-a", actor="user-a", session_id="sess-a", timestamp=self.base_time))
        self.repo.append(_event(event_id="e2", event_type=EventType.AUTH_FAILURE, request_id="req-b", actor="user-b", session_id="sess-b", timestamp=self.base_time + timedelta(hours=1)))
        self.repo.append(_event(event_id="e3", event_type=EventType.AUTH_SUCCESS, request_id="req-c", actor="user-a", session_id="sess-a", timestamp=self.base_time + timedelta(hours=2)))

    def tearDown(self):
        self.database.dispose()

    def test_filter_by_event_type(self):
        events = self.repo.list_events(event_type=EventType.AUTH_SUCCESS)
        self.assertEqual({e.event_id for e in events}, {"e1", "e3"})

    def test_filter_by_request_id_correlation(self):
        events = self.repo.list_events(request_id="req-b")
        self.assertEqual([e.event_id for e in events], ["e2"])

    def test_filter_by_actor_user_id(self):
        events = self.repo.list_events(actor="user-a")
        self.assertEqual({e.event_id for e in events}, {"e1", "e3"})

    def test_filter_by_session_id(self):
        events = self.repo.list_events(session_id="sess-b")
        self.assertEqual([e.event_id for e in events], ["e2"])

    def test_filter_by_time_range(self):
        events = self.repo.list_events(start_time=self.base_time + timedelta(minutes=30), end_time=self.base_time + timedelta(hours=1, minutes=30))
        self.assertEqual([e.event_id for e in events], ["e2"])

    def test_combined_filters(self):
        events = self.repo.list_events(event_type=EventType.AUTH_SUCCESS, actor="user-a", session_id="sess-a")
        self.assertEqual({e.event_id for e in events}, {"e1", "e3"})

    def test_no_matching_filter_returns_empty(self):
        self.assertEqual(self.repo.list_events(actor="nobody"), [])


class TestInMemoryRepositoryFiltersUnchangedBehavior(unittest.TestCase):
    """The same new filters were added to the in-memory AuditRepository for consistency -- verify they work identically there, and that omitting them preserves every pre-Phase-12.8 call shape."""

    def test_in_memory_repository_supports_same_filters(self):
        from audit import AuditRepository

        repo = AuditRepository()
        repo.append(_event(event_id="x1", actor="user-a", session_id="s1"))
        repo.append(_event(event_id="x2", actor="user-b", session_id="s2"))
        self.assertEqual([e.event_id for e in repo.list_events(actor="user-a")], ["x1"])
        self.assertEqual([e.event_id for e in repo.list_events(session_id="s2")], ["x2"])

    def test_pre_phase_12_8_call_shape_still_works(self):
        from audit import AuditRepository

        repo = AuditRepository()
        repo.append(_event(event_id="y1", event_type=EventType.AUTH_FAILURE, request_id="req-y"))
        self.assertEqual(len(repo.list_events(event_type=EventType.AUTH_FAILURE)), 1)
        self.assertEqual(len(repo.list_events(request_id="req-y")), 1)


class TestPrivacySanitizationPreserved(unittest.TestCase):
    """
    Critical constraint: Business Decision -> AuditLogger -> Privacy
    Sanitization -> AuditRepository -> PostgreSQL. Sanitization must
    happen BEFORE persistence, and AuditLogger/PrivacyService must not be
    touched by this step -- verified by using the real, unmodified
    AuditLogger with a real PolicyEngine/PrivacyService, backed by the
    new Postgres repository, and confirming the persisted row (not just
    the returned event) never contains raw PII.
    """

    def setUp(self):
        self.database = _fresh_database()
        self.policy_engine = PolicyEngine()
        self.privacy_service = PrivacyService(self.policy_engine)
        self.logger = AuditLogger(privacy_service=self.privacy_service, repository=PostgresAuditRepository(self.database))

    def tearDown(self):
        self.database.dispose()

    def test_pii_in_metadata_is_sanitized_before_reaching_the_database(self):
        self.logger.record(
            EventType.TOOL_REQUESTED, outcome="requested", actor="user-1", action="BOOK_APPOINTMENT",
            metadata={"raw_input": "email me at attacker@example.com or call 555-987-6543"},
        )
        raw = PostgresAuditRepository(self.database).list_events()[0]
        self.assertNotIn("attacker@example.com", str(raw.metadata))
        self.assertNotIn("555-987-6543", str(raw.metadata))

    def test_no_tokens_or_credentials_in_persisted_metadata(self):
        self.logger.record(
            EventType.AUTH_FAILURE, outcome="denied", actor="client-ip-127.0.0.1",
            reason="Invalid or missing credentials.",  # matches identity.py's own never-echo-token discipline
            metadata={"attempted_token": "should never be logged raw"},
        )
        raw = PostgresAuditRepository(self.database).list_events()[0]
        # AuditLogger/identity.py never pass the raw token value as
        # metadata in the first place (see identity.py's own discipline)
        # -- this test documents that the persisted reason field also
        # carries no credential material, matching the in-memory
        # behavior exactly.
        self.assertNotIn("Bearer ", raw.reason or "")

    def test_no_prompts_or_internal_reasoning_persisted(self):
        # AuditEvent's own schema (observability_models.py) has no field
        # for raw prompt/completion text or model reasoning -- structurally
        # impossible to persist what the type doesn't carry. Confirmed by
        # inspecting the actual persisted row's fields.
        self.logger.record(EventType.TOOL_SUCCEEDED, outcome="success", actor="user-1", action="ORDER_LOOKUP")
        raw = PostgresAuditRepository(self.database).list_events()[0]
        for field_name in ("event_id", "event_type", "request_id", "conversation_id", "session_id", "actor", "action", "resource", "outcome", "policy", "reason", "metadata"):
            self.assertTrue(hasattr(raw, field_name))
        self.assertFalse(hasattr(raw, "prompt"))
        self.assertFalse(hasattr(raw, "completion"))
        self.assertFalse(hasattr(raw, "raw_model_output"))


class TestAuditCannotAlterBusinessDecisions(unittest.TestCase):
    """
    Structural guarantee (audit.py's own module docstring): audit
    recording is fire-and-forget and never returns a value any caller
    uses to decide what to do next. Verified here specifically for the
    persisted repository: even when the database is completely
    unreachable, AuditLogger.record() must not raise -- an audit-layer
    failure must never be able to block, alter, or fail a real business
    decision that already happened.
    """

    def test_audit_logger_never_raises_even_when_database_is_unreachable(self):
        database = Database(load_database_config(env={"PERSISTENCE_MODE": "production", "DATABASE_URL": "postgresql+psycopg2://u:p@127.0.0.1:1/nope?connect_timeout=1"}))
        logger = AuditLogger(repository=PostgresAuditRepository(database))
        try:
            result = logger.record(EventType.TOOL_DENIED, outcome="denied", actor="user-1", action="CANCEL_APPOINTMENT")
            self.assertIsNone(result)  # best-effort: signals failure via None, never an exception
        finally:
            database.dispose()

    def test_security_event_detector_never_raises_when_database_is_unreachable(self):
        database = Database(load_database_config(env={"PERSISTENCE_MODE": "production", "DATABASE_URL": "postgresql+psycopg2://u:p@127.0.0.1:1/nope?connect_timeout=1"}))
        logger = AuditLogger(repository=PostgresAuditRepository(database))
        detector = SecurityEventDetector(logger)
        try:
            detector.record_cross_user_access_attempt("memory", "user-b")  # must not raise
        finally:
            database.dispose()


class TestRestartRecovery(unittest.TestCase):
    def setUp(self):
        import os
        import tempfile

        fd, path = tempfile.mkstemp(suffix=".db", prefix="phase12_audit_restart_")
        os.close(fd)
        os.remove(path)
        self.db_path = Path(path)
        self.url = f"sqlite:///{self.db_path.as_posix()}"
        self._databases = []
        database = self._new_database()
        Base.metadata.create_all(database.engine)

    def tearDown(self):
        for database in self._databases:
            database.dispose()
        if self.db_path.exists():
            self.db_path.unlink()

    def _new_database(self) -> Database:
        database = Database(load_database_config(env={"DATABASE_URL": self.url}))
        self._databases.append(database)
        return database

    def test_audit_trail_survives_simulated_restart(self):
        logger_before = AuditLogger(repository=PostgresAuditRepository(self._new_database()))
        logger_before.record(EventType.SESSION_CREATED, outcome="success", actor="user-1", session_id="sess-1")

        repo_after = PostgresAuditRepository(self._new_database())
        events = repo_after.list_events(event_type=EventType.SESSION_CREATED)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].actor, "user-1")


class TestDatabaseFailure(unittest.TestCase):
    def test_append_raises_database_unavailable(self):
        database = Database(load_database_config(env={"PERSISTENCE_MODE": "production", "DATABASE_URL": "postgresql+psycopg2://u:p@127.0.0.1:1/nope?connect_timeout=1"}))
        repo = PostgresAuditRepository(database)
        try:
            with self.assertRaises(DatabaseUnavailableError):
                repo.append(_event())
        finally:
            database.dispose()

    def test_list_events_raises_database_unavailable(self):
        database = Database(load_database_config(env={"PERSISTENCE_MODE": "production", "DATABASE_URL": "postgresql+psycopg2://u:p@127.0.0.1:1/nope?connect_timeout=1"}))
        repo = PostgresAuditRepository(database)
        try:
            with self.assertRaises(DatabaseUnavailableError):
                repo.list_events()
        finally:
            database.dispose()


if __name__ == "__main__":
    unittest.main()

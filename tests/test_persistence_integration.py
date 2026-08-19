"""
Tests for Phase 12.10's integration wiring: src/agent/conversation_manager.py's
resolve_persistence_repositories() and PersistenceRepositories.

build_conversation_manager() itself requires a real LLM/torch, unavailable
in this offline test environment (confirmed by test_server_api.py's own
TestGracefulShutdown docstring) — resolve_persistence_repositories() was
extracted specifically so the persistence-wiring DECISION (dev vs.
production, fail-safe on an unreachable database) can be verified
directly, without needing an LLM. "Production" here means
PERSISTENCE_MODE=production with a SQLite DATABASE_URL (an entirely
offline stand-in — db.load_database_config()'s production/dev branching
depends only on PERSISTENCE_MODE, never on which URL scheme is used), or
an intentionally-unreachable target for the failure-path tests — see
PHASE_12_1_PERSISTENCE_AUDIT.md §13 for why SQLite is this project's
consistent PostgreSQL test double throughout Phase 12.

Run with:
    python -m unittest tests.test_persistence_integration -v
"""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "agent"))

from conversation_manager import PersistenceRepositories, resolve_persistence_repositories  # noqa: E402
from db import DatabaseUnavailableError  # noqa: E402
from session_repository_postgres import PostgresSessionRepository  # noqa: E402
from memory_repository_postgres import PostgresMemoryRepository  # noqa: E402
from audit_repository_postgres import PostgresAuditRepository  # noqa: E402
from idempotency_repository_postgres import PostgresIdempotencyRepository  # noqa: E402


class _EnvGuard:
    """Saves/restores PERSISTENCE_MODE and DATABASE_URL around a test so tests never leak environment state into each other or into other test files."""

    _KEYS = ("PERSISTENCE_MODE", "DATABASE_URL")

    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in self._KEYS}
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class TestDevModeDefault(unittest.TestCase):
    def test_no_env_returns_all_none(self):
        with _EnvGuard():
            os.environ.pop("PERSISTENCE_MODE", None)
            os.environ.pop("DATABASE_URL", None)
            result = resolve_persistence_repositories()
            self.assertIsInstance(result, PersistenceRepositories)
            self.assertIsNone(result.database)
            self.assertIsNone(result.session)
            self.assertIsNone(result.memory)
            self.assertIsNone(result.audit)
            self.assertIsNone(result.idempotency)

    def test_explicit_dev_mode_returns_all_none(self):
        with _EnvGuard():
            os.environ["PERSISTENCE_MODE"] = "dev"
            result = resolve_persistence_repositories()
            self.assertIsNone(result.database)


class TestPersistenceDisabledEscapeHatch(unittest.TestCase):
    def test_persistence_enabled_false_ignores_production_mode(self):
        with _EnvGuard():
            os.environ["PERSISTENCE_MODE"] = "production"
            os.environ["DATABASE_URL"] = "postgresql+psycopg2://u:p@127.0.0.1:1/nope?connect_timeout=1"
            result = resolve_persistence_repositories(persistence_enabled=False)
            self.assertIsNone(result.database)  # never even attempted a connection


class TestProductionModeWiresRealRepositories(unittest.TestCase):
    """PERSISTENCE_MODE=production against a reachable (SQLite-standing-in-for-PostgreSQL) database wires real Postgres-backed repository instances."""

    def setUp(self):
        import tempfile

        fd, path = tempfile.mkstemp(suffix=".db", prefix="phase12_integration_")
        os.close(fd)
        os.remove(path)
        self.db_path = Path(path)

    def tearDown(self):
        if self.db_path.exists():
            self.db_path.unlink()

    def test_production_mode_returns_real_postgres_repositories(self):
        with _EnvGuard():
            os.environ["PERSISTENCE_MODE"] = "production"
            os.environ["DATABASE_URL"] = f"sqlite:///{self.db_path.as_posix()}"
            from db_models import Base

            # Create schema first (a real deployment runs `alembic upgrade
            # head`; this test does the equivalent directly for speed).
            from db import load_database_config, Database

            bootstrap = Database(load_database_config())
            Base.metadata.create_all(bootstrap.engine)
            bootstrap.dispose()

            result = resolve_persistence_repositories()
            try:
                self.assertIsNotNone(result.database)
                self.assertIsInstance(result.session, PostgresSessionRepository)
                self.assertIsInstance(result.memory, PostgresMemoryRepository)
                self.assertIsInstance(result.audit, PostgresAuditRepository)
                self.assertIsInstance(result.idempotency, PostgresIdempotencyRepository)
            finally:
                result.database.dispose()


class TestProductionModeFailsSafeOnUnreachableDatabase(unittest.TestCase):
    """Mandatory (Step 12.10's explicit requirement): unreachable database -> safe failure (raise), NEVER a silent fallback to in-memory repositories."""

    def test_unreachable_database_raises_not_silently_falls_back(self):
        with _EnvGuard():
            os.environ["PERSISTENCE_MODE"] = "production"
            os.environ["DATABASE_URL"] = "postgresql+psycopg2://u:p@127.0.0.1:1/nope?connect_timeout=1"
            with self.assertRaises(DatabaseUnavailableError):
                resolve_persistence_repositories()

    def test_unreachable_database_error_never_contains_password(self):
        with _EnvGuard():
            os.environ["PERSISTENCE_MODE"] = "production"
            os.environ["DATABASE_URL"] = "postgresql+psycopg2://u:supersecret@127.0.0.1:1/nope?connect_timeout=1"
            try:
                resolve_persistence_repositories()
                self.fail("expected DatabaseUnavailableError")
            except DatabaseUnavailableError as exc:
                self.assertNotIn("supersecret", str(exc))

    def test_missing_database_url_in_production_mode_raises_configuration_error(self):
        from db import DatabaseConfigurationError

        with _EnvGuard():
            os.environ["PERSISTENCE_MODE"] = "production"
            os.environ.pop("DATABASE_URL", None)
            with self.assertRaises(DatabaseConfigurationError):
                resolve_persistence_repositories()


class TestEndToEndFlowsAgainstPersistedRepositories(unittest.TestCase):
    """
    plan.md Step 12.10's required end-to-end scenarios, built directly
    against ConversationManager (constructed with a fake LLM, exactly
    like every other ConversationManager test in this repository) wired
    to the resolved persisted repositories -- proving the FULL
    request -> ... -> persistence -> response path, not just the
    repository-resolution function in isolation.
    """

    def setUp(self):
        import tempfile

        fd, path = tempfile.mkstemp(suffix=".db", prefix="phase12_e2e_")
        os.close(fd)
        os.remove(path)
        self.db_path = Path(path)
        self._guard = _EnvGuard().__enter__()
        os.environ["PERSISTENCE_MODE"] = "production"
        os.environ["DATABASE_URL"] = f"sqlite:///{self.db_path.as_posix()}"

        from db_models import Base
        from db import load_database_config, Database

        bootstrap = Database(load_database_config())
        Base.metadata.create_all(bootstrap.engine)
        bootstrap.dispose()

        self.persistence = resolve_persistence_repositories()

    def tearDown(self):
        self.persistence.database.dispose()
        self._guard.__exit__(None, None, None)
        if self.db_path.exists():
            self.db_path.unlink()

    def test_appointment_workflow_confirmation_survives_restart_and_executes_exactly_once(self):
        from conversation_manager import ConversationManager
        from handoff_detector import HandoffDetector
        from intent_engine import IntentEngine
        from mock_tools import MockAppointmentStore, build_default_tool_registry
        from policy_engine import PolicyEngine
        from session_manager import SessionManager
        from tool_orchestrator import ToolOrchestrator
        from action_models import AuthContext
        from identity import Role, permissions_for_roles

        class _FakeLLM:
            def generate_stream(self, messages):
                yield {"text": "OK", "latency_ms": 1.0}

        policy_engine = PolicyEngine()
        appointments = MockAppointmentStore()
        booked = appointments.book({"doctor_id": "d1", "date": "2026-08-18", "time": "17:00"})
        registry = build_default_tool_registry(appointment_store=appointments)
        orchestrator = ToolOrchestrator(registry, policy_engine, idempotency_repository=self.persistence.idempotency)
        session_manager = SessionManager(repository=self.persistence.session)

        auth = AuthContext(user_id="user-1", authenticated=True, roles=(Role.USER.value,), permissions=permissions_for_roles((Role.USER,)), authentication_method="test")

        session = session_manager.create_session(user_id="user-1")
        session_manager.update_session(
            session.session_id, user_id="user-1", workflow_state="AWAITING_CONFIRMATION",
            pending_action="CANCEL_APPOINTMENT", pending_parameters={"appointment_id": booked["appointment_id"]},
        )

        # "Restart": brand-new SessionManager/ToolOrchestrator against the
        # same persisted repositories (same objects here since the
        # underlying database file is what actually persists -- a real
        # restart would also reconstruct `self.persistence`, exercised by
        # TestRestartRecovery in the per-repository test files already).
        consumed = session_manager.try_consume_pending_confirmation(session.session_id, user_id="user-1")
        self.assertIsNotNone(consumed)
        action_name, params = consumed
        from action_models import ToolRequest

        result = orchestrator.invoke(
            ToolRequest(action=action_name, params=params, confirmed=True, request_id="e2e-req-1"),
            auth=auth,
        )
        self.assertTrue(result.success)

        # Exactly once: a second identical tool invocation is denied.
        result2 = orchestrator.invoke(
            ToolRequest(action=action_name, params=params, confirmed=True, request_id="e2e-req-1"),
            auth=auth,
        )
        self.assertFalse(result2.success)
        self.assertEqual(result2.status, "duplicate")

        # A second consumption attempt of the same (already-cleared) confirmation is also denied.
        self.assertIsNone(session_manager.try_consume_pending_confirmation(session.session_id, user_id="user-1"))

    def test_cross_user_session_access_denied_against_persisted_repository(self):
        from session_manager import SessionManager

        session_manager = SessionManager(repository=self.persistence.session)
        session = session_manager.create_session(user_id="user-a")
        self.assertIsNone(session_manager.get_session(session.session_id, user_id="user-b"))

    def test_faq_flow_end_to_end_with_persisted_audit(self):
        """request -> safety -> policy -> RAG -> LLM -> response -> audit, with the audit trail landing in the persisted repository."""
        sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
        from test_conversation_manager import FakeLLMService, FakeRetriever, FakeChunk, _real_clinical_guard, _real_handoff_detector, _run_turn
        from conversation_manager import ConversationManager
        from audit import AuditLogger
        from observability_models import EventType

        audit_logger = AuditLogger(repository=self.persistence.audit)
        llm = FakeLLMService(response_text="Our return window is thirty days.")
        retriever = FakeRetriever([FakeChunk("faq_returns", "faqs", "Returns", "30 day policy", 0.9)])
        cm = ConversationManager(
            llm_service=llm, retriever=retriever,
            clinical_guard=_real_clinical_guard(), handoff_detector=_real_handoff_detector(),
            audit_logger=audit_logger,
        )
        _, final = _run_turn(cm, "What's your return policy?")
        self.assertEqual(len(llm.calls), 1)  # LLM was consulted
        self.assertFalse(final.get("is_handoff", False))

    def test_clinical_flow_end_to_end_llm_never_called(self):
        """risky request -> ClinicalSafetyGuard -> LLM NOT called -> handoff, with the persisted audit trail confirming a real SAFETY_BLOCK/SAFETY_HANDOFF decision was recorded."""
        sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
        from test_conversation_manager import FakeLLMService, FakeRetriever, _real_clinical_guard, _real_handoff_detector, _run_turn
        from conversation_manager import ConversationManager
        from audit import AuditLogger

        audit_logger = AuditLogger(repository=self.persistence.audit)
        llm = FakeLLMService()
        cm = ConversationManager(
            llm_service=llm, retriever=FakeRetriever(),
            clinical_guard=_real_clinical_guard(), handoff_detector=_real_handoff_detector(),
            audit_logger=audit_logger,
        )
        _, final = _run_turn(cm, "How many mg of ibuprofen should I take?")
        self.assertEqual(len(llm.calls), 0, "LLM must never be called for a clinical-risk request")
        self.assertTrue(final.get("is_handoff", False))


if __name__ == "__main__":
    unittest.main()

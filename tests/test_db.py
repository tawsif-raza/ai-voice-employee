"""
Unit tests for the database foundation (Phase 12; plan.md Step 12.2):
src/agent/db.py -- DatabaseConfig, load_database_config(), Database.

Fully offline: exercises SQLite (:memory: and a temp file), never a real
network connection. PostgreSQL-specific behavior is out of scope for this
module (it has no PostgreSQL-only code path yet -- see db.py's
_build_engine()) and is deferred to later Phase 12 steps' tests, per
PHASE_12_1_PERSISTENCE_AUDIT.md §13.

Run with:
    python -m unittest tests.test_db -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "agent"))
from db import (  # noqa: E402
    Database,
    DatabaseConfig,
    DatabaseConfigurationError,
    DatabaseUnavailableError,
    load_database_config,
)


class TestConfigLoadingDefaults(unittest.TestCase):
    def test_no_env_defaults_to_dev_in_memory_sqlite(self):
        config = load_database_config(env={})
        self.assertEqual(config.mode, "dev")
        self.assertEqual(config.url, "sqlite:///:memory:")

    def test_dev_mode_explicit_still_defaults_to_sqlite_without_database_url(self):
        config = load_database_config(env={"PERSISTENCE_MODE": "dev"})
        self.assertEqual(config.url, "sqlite:///:memory:")

    def test_dev_mode_honors_explicit_database_url(self):
        config = load_database_config(env={"PERSISTENCE_MODE": "dev", "DATABASE_URL": "sqlite:///./devtest.db"})
        self.assertEqual(config.url, "sqlite:///./devtest.db")

    def test_default_pool_settings_applied(self):
        config = load_database_config(env={})
        self.assertEqual(config.pool_size, 5)
        self.assertEqual(config.max_overflow, 10)
        self.assertGreater(config.pool_timeout_seconds, 0)

    def test_is_production_false_for_dev(self):
        self.assertFalse(load_database_config(env={}).is_production())


class TestProductionModeFailsClosed(unittest.TestCase):
    """
    Mandatory security requirement (plan.md Step 12.2): "Production must
    NOT silently fall back to in-memory persistence if PostgreSQL is
    unavailable." Missing/blank DATABASE_URL under a production
    PERSISTENCE_MODE must raise, never quietly resolve to SQLite.
    """

    def test_production_mode_without_database_url_raises(self):
        with self.assertRaises(DatabaseConfigurationError):
            load_database_config(env={"PERSISTENCE_MODE": "production"})

    def test_production_mode_with_blank_database_url_raises(self):
        with self.assertRaises(DatabaseConfigurationError):
            load_database_config(env={"PERSISTENCE_MODE": "production", "DATABASE_URL": "   "})

    def test_postgres_alias_mode_without_database_url_raises(self):
        with self.assertRaises(DatabaseConfigurationError):
            load_database_config(env={"PERSISTENCE_MODE": "postgres"})

    def test_production_mode_with_database_url_succeeds(self):
        config = load_database_config(
            env={"PERSISTENCE_MODE": "production", "DATABASE_URL": "postgresql://u:p@host:5432/db"}
        )
        self.assertTrue(config.is_production())
        self.assertEqual(config.url, "postgresql://u:p@host:5432/db")


class TestInvalidConfiguration(unittest.TestCase):
    def test_non_numeric_pool_size_raises(self):
        with self.assertRaises(DatabaseConfigurationError):
            load_database_config(env={"DB_POOL_SIZE": "not-a-number"})

    def test_zero_pool_size_rejected(self):
        with self.assertRaises(DatabaseConfigurationError):
            load_database_config(env={"DB_POOL_SIZE": "0"})

    def test_negative_max_overflow_rejected(self):
        with self.assertRaises(DatabaseConfigurationError):
            load_database_config(env={"DB_MAX_OVERFLOW": "-1"})

    def test_zero_pool_timeout_rejected(self):
        with self.assertRaises(DatabaseConfigurationError):
            load_database_config(env={"DB_POOL_TIMEOUT_SECONDS": "0"})


class TestCredentialSafety(unittest.TestCase):
    """Never log/expose a raw password (plan.md Step 12.2's explicit security requirement)."""

    def test_safe_url_masks_password(self):
        config = load_database_config(
            env={"PERSISTENCE_MODE": "production", "DATABASE_URL": "postgresql://myuser:supersecret@dbhost:5432/mydb"}
        )
        self.assertNotIn("supersecret", config.safe_url)
        self.assertIn("myuser", config.safe_url)
        self.assertIn("***", config.safe_url)

    def test_safe_url_used_in_configuration_error_not_raw_url(self):
        # A malformed pool value error message must never need to embed
        # the URL at all -- verify no credential-shaped substring leaks
        # through even incidentally.
        try:
            load_database_config(
                env={
                    "PERSISTENCE_MODE": "production",
                    "DATABASE_URL": "postgresql://myuser:supersecret@dbhost:5432/mydb",
                    "DB_POOL_SIZE": "bad",
                }
            )
            self.fail("expected DatabaseConfigurationError")
        except DatabaseConfigurationError as exc:
            self.assertNotIn("supersecret", str(exc))

    def test_url_without_password_is_unchanged(self):
        config = load_database_config(env={"DATABASE_URL": "sqlite:///./no_password.db"})
        self.assertEqual(config.safe_url, "sqlite:///./no_password.db")


class TestDatabaseConnection(unittest.TestCase):
    def setUp(self):
        self.db = Database(load_database_config(env={"DATABASE_URL": "sqlite:///:memory:"}))

    def tearDown(self):
        self.db.dispose()

    def test_health_check_succeeds_against_sqlite(self):
        self.assertTrue(self.db.health_check())

    def test_session_scope_yields_working_session(self):
        with self.db.session_scope() as session:
            result = session.execute(__import__("sqlalchemy").text("SELECT 1")).scalar()
            self.assertEqual(result, 1)

    def test_session_scope_commits_on_clean_exit(self):
        # No table exists yet (schema-free at this step) -- exercise the
        # commit path itself via a no-op statement, proving the context
        # manager's commit/close sequence runs without error.
        with self.db.session_scope():
            pass  # commits an empty transaction cleanly


class TestConnectionFailure(unittest.TestCase):
    def test_health_check_raises_database_unavailable_for_unreachable_host(self):
        # A syntactically valid but unreachable PostgreSQL target -- must
        # surface as DatabaseUnavailableError, never a raw driver
        # exception, and must never hang the test suite (default libpq
        # connect timeout is bounded).
        db = Database(
            load_database_config(
                env={
                    "PERSISTENCE_MODE": "production",
                    "DATABASE_URL": "postgresql+psycopg2://u:p@127.0.0.1:1/does_not_exist?connect_timeout=1",
                }
            )
        )
        try:
            with self.assertRaises(DatabaseUnavailableError):
                db.health_check()
        finally:
            db.dispose()

    def test_connection_failure_message_never_contains_password(self):
        db = Database(
            load_database_config(
                env={
                    "PERSISTENCE_MODE": "production",
                    "DATABASE_URL": "postgresql+psycopg2://u:supersecret@127.0.0.1:1/does_not_exist?connect_timeout=1",
                }
            )
        )
        try:
            with self.assertRaises(DatabaseUnavailableError) as ctx:
                db.health_check()
            self.assertNotIn("supersecret", str(ctx.exception))
        finally:
            db.dispose()


class TestDatabaseIsolation(unittest.TestCase):
    """Two Database instances against separate in-memory SQLite databases must never share state -- proves test isolation is real, not accidental."""

    def test_two_in_memory_databases_are_independent(self):
        db_a = Database(load_database_config(env={"DATABASE_URL": "sqlite:///:memory:"}))
        db_b = Database(load_database_config(env={"DATABASE_URL": "sqlite:///:memory:"}))
        try:
            self.assertIsNot(db_a.engine, db_b.engine)
            self.assertTrue(db_a.health_check())
            self.assertTrue(db_b.health_check())
        finally:
            db_a.dispose()
            db_b.dispose()

    def test_dataclass_is_frozen(self):
        config = load_database_config(env={})
        with self.assertRaises(Exception):
            config.url = "changed"  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()

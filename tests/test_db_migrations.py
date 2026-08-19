"""
Migration tests (Phase 12; plan.md Step 12.3): verifies alembic/'s initial
migration against a clean, isolated SQLite database file per test.

Schema-only scope, matching this step's boundary — these tests prove the
migration system itself works (clean DB -> migrate -> expected schema ->
constraints reject bad data -> downgrade -> clean state again); they do
NOT exercise any repository or manager code (that's Steps 12.5+).

Not "fully offline, stdlib only" like most of this repo's tests (Alembic
itself is required, already added in this step) but still fully local:
no network, no real PostgreSQL server, one throwaway SQLite file per test
case, always removed in tearDown.

Run with:
    python -m unittest tests.test_db_migrations -v
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from alembic import command
from alembic.config import Config

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ALEMBIC_INI = _REPO_ROOT / "alembic.ini"

sys.path.insert(0, str(_REPO_ROOT / "src" / "agent"))


def _alembic_config_for(sqlite_path: Path) -> Config:
    """
    alembic/env.py resolves its URL from db.load_database_config(), which
    reads DATABASE_URL from the real process environment (see env.py's
    Phase 12 customization) -- so isolating each test to its own SQLite
    file means temporarily setting DATABASE_URL, not just passing a Config
    option (which env.py's override would clobber anyway). Callers set
    os.environ["DATABASE_URL"] themselves before invoking a command with
    this Config; this function only builds the Config object.
    """
    return Config(str(_ALEMBIC_INI))


class _IsolatedSQLiteMigration(unittest.TestCase):
    """Base class: gives each test case its own throwaway SQLite file and a correctly-scoped DATABASE_URL, cleaned up unconditionally."""

    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db", prefix="phase12_migration_test_")
        os.close(fd)
        os.remove(path)  # alembic/sqlite should create it fresh -- start from "file does not exist"
        self.db_path = Path(path)
        self._prior_database_url = os.environ.get("DATABASE_URL")
        self._prior_persistence_mode = os.environ.get("PERSISTENCE_MODE")
        os.environ["DATABASE_URL"] = f"sqlite:///{self.db_path.as_posix()}"
        os.environ.pop("PERSISTENCE_MODE", None)
        self.config = _alembic_config_for(self.db_path)

    def tearDown(self):
        if self._prior_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = self._prior_database_url
        if self._prior_persistence_mode is None:
            os.environ.pop("PERSISTENCE_MODE", None)
        else:
            os.environ["PERSISTENCE_MODE"] = self._prior_persistence_mode
        if self.db_path.exists():
            self.db_path.unlink()

    def _tables(self) -> set:
        conn = sqlite3.connect(str(self.db_path))
        try:
            cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            return {row[0] for row in cur.fetchall()}
        finally:
            conn.close()

    def _indexes(self) -> set:
        conn = sqlite3.connect(str(self.db_path))
        try:
            cur = conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
            return {row[0] for row in cur.fetchall()}
        finally:
            conn.close()


class TestCleanDatabaseMigration(_IsolatedSQLiteMigration):
    def test_migration_succeeds_against_clean_database(self):
        self.assertFalse(self.db_path.exists())
        command.upgrade(self.config, "head")
        self.assertTrue(self.db_path.exists())

    def test_expected_tables_exist_after_migration(self):
        command.upgrade(self.config, "head")
        tables = self._tables()
        for expected in ("sessions", "memory_records", "audit_events", "security_events", "idempotency_records"):
            self.assertIn(expected, tables)

    def test_expected_indexes_exist_after_migration(self):
        command.upgrade(self.config, "head")
        indexes = self._indexes()
        for expected in (
            "ix_sessions_user_id", "ix_sessions_expires_at",
            "ix_memory_records_user_id",
            "ix_audit_events_event_type", "ix_audit_events_request_id",
            "ix_security_events_type",
        ):
            self.assertIn(expected, indexes)


class TestConstraintsRejectInvalidData(_IsolatedSQLiteMigration):
    def setUp(self):
        super().setUp()
        command.upgrade(self.config, "head")
        self.conn = sqlite3.connect(str(self.db_path))

    def tearDown(self):
        self.conn.close()
        super().tearDown()

    def test_invalid_session_status_rejected(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO sessions (session_id, status, created_at, updated_at, expires_at, "
                "pending_parameters, confirmation_state, metadata) VALUES "
                "('s1','NOT_A_STATUS','2026-01-01','2026-01-01','2026-01-01','{}','{}','{}')"
            )
            self.conn.commit()

    def test_invalid_memory_category_rejected(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO memory_records (id, user_id, category, key, value, source, "
                "created_at, updated_at, metadata) VALUES "
                "('m1','u1','NOT_A_CATEGORY','k','v','src','2026-01-01','2026-01-01','{}')"
            )
            self.conn.commit()

    def test_invalid_security_event_severity_rejected(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO security_events (event_id, timestamp, type, severity, outcome, reason) "
                "VALUES ('e1','2026-01-01','X','NOT_A_SEVERITY','denied','r')"
            )
            self.conn.commit()

    def test_duplicate_primary_key_rejected(self):
        self.conn.execute(
            "INSERT INTO sessions (session_id, status, created_at, updated_at, expires_at, "
            "pending_parameters, confirmation_state, metadata) VALUES "
            "('dup','ACTIVE','2026-01-01','2026-01-01','2026-01-01','{}','{}','{}')"
        )
        self.conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO sessions (session_id, status, created_at, updated_at, expires_at, "
                "pending_parameters, confirmation_state, metadata) VALUES "
                "('dup','ACTIVE','2026-01-01','2026-01-01','2026-01-01','{}','{}','{}')"
            )
            self.conn.commit()

    def test_duplicate_idempotency_request_id_rejected(self):
        # Phase 12.9 (plan.md: "same key + different user -> isolated")
        # re-scoped this table's primary key to (user_id, request_id) --
        # same user_id + same request_id is the actual duplicate case.
        self.conn.execute(
            "INSERT INTO idempotency_records (user_id, request_id, action, result_status, executed_at, expires_at) "
            "VALUES ('user-1','req-1','CANCEL_APPOINTMENT','success','2026-01-01','2026-01-02')"
        )
        self.conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO idempotency_records (user_id, request_id, action, result_status, executed_at, expires_at) "
                "VALUES ('user-1','req-1','CANCEL_APPOINTMENT','success','2026-01-01','2026-01-02')"
            )
            self.conn.commit()

    def test_same_request_id_different_user_is_not_a_duplicate(self):
        # The other half of the same requirement: two different users
        # reusing the identical request_id string must NOT collide at
        # the database layer -- each is an independent primary key.
        self.conn.execute(
            "INSERT INTO idempotency_records (user_id, request_id, action, result_status, executed_at, expires_at) "
            "VALUES ('user-1','req-shared','CANCEL_APPOINTMENT','success','2026-01-01','2026-01-02')"
        )
        self.conn.execute(
            "INSERT INTO idempotency_records (user_id, request_id, action, result_status, executed_at, expires_at) "
            "VALUES ('user-2','req-shared','CANCEL_APPOINTMENT','success','2026-01-01','2026-01-02')"
        )
        self.conn.commit()  # no exception

    def test_valid_row_accepted_for_every_table(self):
        # Sanity check that the constraints above reject only *invalid*
        # data, not everything -- each table accepts a well-formed row.
        self.conn.execute(
            "INSERT INTO sessions (session_id, status, created_at, updated_at, expires_at, "
            "pending_parameters, confirmation_state, metadata) VALUES "
            "('ok','ACTIVE','2026-01-01','2026-01-01','2026-01-01','{}','{}','{}')"
        )
        self.conn.execute(
            "INSERT INTO memory_records (id, user_id, category, key, value, source, "
            "created_at, updated_at, metadata) VALUES "
            "('ok','u1','PREFERENCE','k','v','src','2026-01-01','2026-01-01','{}')"
        )
        self.conn.execute(
            "INSERT INTO security_events (event_id, timestamp, type, severity, outcome, reason) "
            "VALUES ('ok','2026-01-01','X','HIGH','denied','r')"
        )
        self.conn.commit()  # no exception


class TestRollback(_IsolatedSQLiteMigration):
    def test_downgrade_to_base_removes_all_application_tables(self):
        command.upgrade(self.config, "head")
        self.assertIn("sessions", self._tables())
        command.downgrade(self.config, "base")
        remaining = self._tables()
        self.assertNotIn("sessions", remaining)
        self.assertNotIn("memory_records", remaining)
        self.assertNotIn("audit_events", remaining)
        self.assertNotIn("security_events", remaining)
        self.assertNotIn("idempotency_records", remaining)

    def test_downgrade_then_upgrade_again_is_clean(self):
        command.upgrade(self.config, "head")
        command.downgrade(self.config, "base")
        command.upgrade(self.config, "head")
        tables = self._tables()
        for expected in ("sessions", "memory_records", "audit_events", "security_events", "idempotency_records"):
            self.assertIn(expected, tables)


if __name__ == "__main__":
    unittest.main()

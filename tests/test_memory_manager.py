"""
Unit tests for MemoryManager/MemoryRepository/MemoryRecord (Phase 5).

Fully offline -- only needs PyYAML (via PolicyEngine's config loading).

Run with:
    python -m unittest tests.test_memory_manager -v
"""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "agent"))
from memory_manager import MemoryManager, MemoryPolicyDeniedError, MemoryValidationError  # noqa: E402
from memory_models import MemoryCategory, MemoryRecord  # noqa: E402
from policy_engine import PolicyEngine  # noqa: E402


def _manager() -> MemoryManager:
    return MemoryManager(PolicyEngine())


class TestValidMemory(unittest.TestCase):
    def test_propose_and_persist_allowed_memory(self):
        manager = _manager()
        record = manager.propose_memory(
            user_id="user-1",
            category=MemoryCategory.PREFERENCE,
            key="preferred_language",
            value="English",
            source="user_explicit",
        )
        persisted = manager.persist_memory(record)
        self.assertIs(persisted, record)
        self.assertIn(record, manager.list_allowed_memory("user-1"))

    def test_proposing_does_not_persist(self):
        manager = _manager()
        manager.propose_memory(user_id="user-1", category=MemoryCategory.PREFERENCE, key="k", value="v", source="s")
        self.assertEqual(manager.list_allowed_memory("user-1"), [])


class TestInvalidMemory(unittest.TestCase):
    def test_invalid_user_id_rejected(self):
        manager = _manager()
        with self.assertRaises(MemoryValidationError):
            manager.propose_memory(user_id="", category=MemoryCategory.PREFERENCE, key="k", value="v", source="s")

    def test_invalid_category_rejected(self):
        manager = _manager()
        with self.assertRaises(MemoryValidationError):
            manager.propose_memory(
                user_id="u", category="PREFERENCE", key="k", value="v", source="s"
            )  # plain string, not enum

    def test_empty_key_rejected(self):
        manager = _manager()
        with self.assertRaises(MemoryValidationError):
            manager.propose_memory(user_id="u", category=MemoryCategory.PREFERENCE, key="", value="v", source="s")

    def test_non_string_value_rejected(self):
        manager = _manager()
        with self.assertRaises(MemoryValidationError):
            manager.propose_memory(user_id="u", category=MemoryCategory.PREFERENCE, key="k", value=12345, source="s")


class TestRestrictedMemory(unittest.TestCase):
    def test_medical_condition_key_denied_persistence(self):
        manager = _manager()
        record = manager.propose_memory(
            user_id="user-1",
            category=MemoryCategory.PREFERENCE,
            key="medical_condition",
            value="diabetes",
            source="user_explicit",
        )
        decision = manager.validate_memory(record)
        self.assertFalse(decision.allowed)
        with self.assertRaises(MemoryPolicyDeniedError):
            manager.persist_memory(record)
        self.assertEqual(manager.list_allowed_memory("user-1"), [])

    def test_payment_method_key_denied_persistence(self):
        manager = _manager()
        record = manager.propose_memory(
            user_id="user-1",
            category=MemoryCategory.PREFERENCE,
            key="payment_method",
            value="visa-1234",
            source="user_explicit",
        )
        with self.assertRaises(MemoryPolicyDeniedError):
            manager.persist_memory(record)

    def test_model_claimed_remember_flag_does_not_bypass_policy(self):
        """
        A model output shaped like {"remember": true, "memory": {"key":
        "medical_condition", ...}} is never even a parameter anywhere in
        this module -- MemoryManager only ever sees an already-typed
        MemoryRecord, and persist_memory() always re-validates through
        PolicyEngine regardless of provenance.
        """
        manager = _manager()
        record = manager.propose_memory(
            user_id="user-1",
            category=MemoryCategory.PREFERENCE,
            key="medical_condition",
            value="diabetes",
            source="llm_output_parsed_unsafely",
        )
        with self.assertRaises(MemoryPolicyDeniedError):
            manager.persist_memory(record)


class TestMemoryUpdateAndDeletion(unittest.TestCase):
    def test_memory_deletion(self):
        manager = _manager()
        record = manager.propose_memory(
            user_id="user-1", category=MemoryCategory.PREFERENCE, key="k", value="v", source="s"
        )
        manager.persist_memory(record)
        self.assertTrue(manager.remove_memory(record.id, user_id="user-1"))
        self.assertEqual(manager.list_allowed_memory("user-1"), [])

    def test_memory_update_is_propose_and_persist_again(self):
        # There is no in-place mutation -- MemoryRecord is frozen; an
        # "update" is a new propose_memory()/persist_memory() call, same
        # id or not, at the caller's discretion.
        manager = _manager()
        record = manager.propose_memory(
            user_id="user-1", category=MemoryCategory.PREFERENCE, key="k", value="v1", source="s"
        )
        manager.persist_memory(record)
        updated = manager.propose_memory(
            user_id="user-1", category=MemoryCategory.PREFERENCE, key="k", value="v2", source="s"
        )
        manager.persist_memory(updated)
        values = {r.value for r in manager.list_allowed_memory("user-1")}
        self.assertEqual(values, {"v1", "v2"})  # both exist -- caller's responsibility to reuse an id for true update

    def test_expired_memory_excluded_from_listing(self):
        manager = _manager()
        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        record = manager.propose_memory(
            user_id="user-1",
            category=MemoryCategory.WORKFLOW_CONTEXT,
            key="temp_context",
            value="v",
            source="s",
            expires_at=past,
        )
        manager.persist_memory(record)
        self.assertEqual(manager.list_allowed_memory("user-1"), [])

    def test_duplicate_memory_ids_do_not_raise(self):
        # Persisting a record with a reused id (caller-constructed)
        # simply overwrites -- no crash, no silent duplication.
        manager = _manager()
        record = MemoryRecord(
            id="mem_fixed",
            user_id="user-1",
            category=MemoryCategory.PREFERENCE,
            key="k",
            value="first",
            source="s",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        manager.persist_memory(record)
        record2 = MemoryRecord(
            id="mem_fixed",
            user_id="user-1",
            category=MemoryCategory.PREFERENCE,
            key="k",
            value="second",
            source="s",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        manager.persist_memory(record2)
        records = manager.list_allowed_memory("user-1")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].value, "second")

    def test_remove_nonexistent_memory_returns_false_not_raise(self):
        manager = _manager()
        self.assertFalse(manager.remove_memory("does-not-exist", user_id="user-1"))


class TestCrossUserIsolation(unittest.TestCase):
    """Mandatory security regression test — plan.md Step 5.11."""

    def test_user_a_memory_not_visible_to_user_b(self):
        manager = _manager()
        record = manager.propose_memory(
            user_id="user-a",
            category=MemoryCategory.PREFERENCE,
            key="preferred_clinic",
            value="Downtown Clinic",
            source="user_explicit",
        )
        manager.persist_memory(record)

        self.assertIn(record, manager.list_allowed_memory("user-a"))
        self.assertEqual(manager.list_allowed_memory("user-b"), [])
        self.assertEqual(manager.get_allowed_context("user-b"), [])

    def test_user_b_cannot_delete_user_a_memory(self):
        manager = _manager()
        record = manager.propose_memory(
            user_id="user-a",
            category=MemoryCategory.PREFERENCE,
            key="preferred_clinic",
            value="Downtown Clinic",
            source="user_explicit",
        )
        manager.persist_memory(record)

        self.assertFalse(manager.remove_memory(record.id, user_id="user-b"))
        self.assertIn(record, manager.list_allowed_memory("user-a"))  # untouched


class TestControlledContextRetrieval(unittest.TestCase):
    def test_get_allowed_context_scoped_by_category(self):
        manager = _manager()
        manager.persist_memory(
            manager.propose_memory(
                user_id="user-1",
                category=MemoryCategory.PREFERENCE,
                key="preferred_language",
                value="English",
                source="s",
            )
        )
        manager.persist_memory(
            manager.propose_memory(
                user_id="user-1",
                category=MemoryCategory.COMMUNICATION_PREFERENCE,
                key="preferred_contact_channel",
                value="voice",
                source="s",
            )
        )
        prefs_only = manager.get_allowed_context("user-1", category=MemoryCategory.PREFERENCE)
        self.assertEqual([r.key for r in prefs_only], ["preferred_language"])

    def test_context_never_includes_restricted_fields_even_if_somehow_persisted(self):
        # Defense in depth: even a record that (hypothetically) made it
        # into storage with a restricted key is still filtered out at
        # context-retrieval time by the expose_downstream check.
        manager = _manager()
        # Bypass persist_memory()'s own gate to simulate a
        # hypothetically-corrupted store, and confirm get_allowed_context
        # still filters it.
        record = MemoryRecord(
            id="mem_x",
            user_id="user-1",
            category=MemoryCategory.PREFERENCE,
            key="payment_method",
            value="visa",
            source="s",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        manager._repository.save(record)
        self.assertEqual(manager.get_allowed_context("user-1"), [])

    def test_no_raw_database_access_method_exists(self):
        manager = _manager()
        for forbidden in ("query", "get_all", "execute_sql", "raw"):
            self.assertFalse(hasattr(manager, forbidden), f"MemoryManager must not expose {forbidden}()")


if __name__ == "__main__":
    unittest.main()

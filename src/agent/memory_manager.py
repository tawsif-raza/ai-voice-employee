"""
MemoryManager and MemoryRepository — controlled durable memory (Phase 5;
plan.md Steps 5.6, 5.8, 5.9).

No raw storage access is ever exposed — there is no `database.query(...)`-
shaped method anywhere on this class, and every read/write path is
scoped by `user_id` and filtered through Phase 3's PolicyEngine
(`evaluate_privacy()`, reused as-is — no new policy category was added
for this; `persist`/`expose_downstream` already existed as operations in
configs/policies/privacy.yaml).

Trust boundary: `propose_memory()` builds an UNTRUSTED candidate record.
Its `source` field documents provenance for audit only — it is never
read as an authorization signal, so a record whose source happens to be
"llm_output"-shaped is exactly as untrusted as any other; the only thing
that ever decides whether a candidate gets persisted is
PolicyEngine.evaluate_privacy() in validate_memory()/persist_memory().
"""

import dataclasses
import threading
from datetime import datetime, timezone
from typing import Optional

from db import ConcurrentModificationError
from memory_models import MemoryCategory, MemoryRecord, new_memory_id


class MemoryValidationError(ValueError):
    """Raised by propose_memory() for structurally invalid input."""


class MemoryPolicyDeniedError(PermissionError):
    """Raised by persist_memory() when PolicyEngine denies the write."""


class MemoryOwnershipError(PermissionError):
    """
    Raised by persist_memory() when `record.id` already belongs to a
    different user (Phase 12.7; plan.md's explicit security test:
    "User B attempts to modify it -> DENY"). Discovered as a pre-existing
    gap while auditing persist_memory() for Phase 12.7, not something
    persistence itself introduced: `persist_memory()` has always upserted
    by `record.id` with no ownership check (see
    test_duplicate_memory_ids_do_not_raise's *same-user* id-reuse case,
    which this fix does not affect), so a caller able to guess/forge
    another user's record id could previously overwrite their memory
    content simply by calling propose_memory() with their own user_id but
    reusing the other user's id string on the candidate.
    """


class MemoryRepository:
    """
    In-memory storage with optimistic concurrency (Phase 13, Step 13.2).
    Swappable, mirroring session_manager.py's SessionRepository pattern.
    Thread-safe (Phase 10, plan.md Step 10.14/10.17) and version-checked.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._records: dict[str, MemoryRecord] = {}
        self._versions: dict[str, int] = {}

    def get(self, memory_id: str) -> Optional[MemoryRecord]:
        with self._lock:
            rec = self._records.get(memory_id)
            if rec is None:
                return None
            ver = self._versions.get(memory_id, getattr(rec, "version", 1))
            if getattr(rec, "version", 1) != ver:
                return dataclasses.replace(rec, version=ver)
            return rec

    def save(self, record: MemoryRecord) -> None:
        with self._lock:
            mid = record.id
            expected_version = getattr(record, "version", 1) or 1
            if mid in self._records:
                current_ver = self._versions.get(mid, 1)
                if expected_version != current_ver:
                    raise ConcurrentModificationError(
                        f"Concurrent modification detected for memory '{mid}': "
                        f"expected version {expected_version}, current version {current_ver}"
                    )
                new_ver = current_ver + 1
                self._versions[mid] = new_ver
                self._records[mid] = dataclasses.replace(record, version=new_ver)
            else:
                self._versions[mid] = expected_version
                self._records[mid] = dataclasses.replace(record, version=expected_version)

    def delete(self, memory_id: str) -> None:
        with self._lock:
            self._records.pop(memory_id, None)
            self._versions.pop(memory_id, None)

    def list_for_user(self, user_id: str) -> list[MemoryRecord]:
        with self._lock:
            res = []
            for r in self._records.values():
                if r.user_id == user_id:
                    ver = self._versions.get(r.id, getattr(r, "version", 1))
                    if getattr(r, "version", 1) != ver:
                        res.append(dataclasses.replace(r, version=ver))
                    else:
                        res.append(r)
            return res


class MemoryManager:
    def __init__(
        self,
        policy_engine,
        repository: Optional[MemoryRepository] = None,
        privacy_service=None,
        audit_logger=None,
        security_detector=None,
    ):
        self._policy_engine = policy_engine
        self._repository = repository or MemoryRepository()
        # Phase 6, optional -- None preserves exact Phase 5 behavior
        # (named-field policy only, no content-pattern PII scanning of
        # `value`). See persist_memory()'s docstring.
        self._privacy_service = privacy_service
        # Phase 8, both optional -- None preserves exact Phase 5/6
        # behavior. Emitted here (not inside PrivacyService itself) to
        # avoid a recursion hazard: AuditLogger.record() sanitizes its
        # own metadata via PrivacyService.sanitize(), which calls
        # decide() internally -- if PrivacyService emitted audit events
        # from decide() too, that sanitize() call would recurse forever.
        # MemoryManager already consumes the PrivacyService decision for
        # its own real purpose (deciding whether/how to persist), so
        # emitting from here observes a decision already made, same as
        # every other Phase 8 wiring in this codebase.
        self._audit_logger = audit_logger
        self._security_detector = security_detector

    def propose_memory(
        self,
        user_id: str,
        category: MemoryCategory,
        key: str,
        value: str,
        source: str,
        expires_at: Optional[datetime] = None,
        metadata: Optional[dict] = None,
    ) -> MemoryRecord:
        """
        Builds an untrusted candidate MemoryRecord. Constructing this
        object does not persist anything and does not imply the write is
        allowed — see validate_memory()/persist_memory().
        """
        if not isinstance(user_id, str) or not user_id.strip():
            raise MemoryValidationError("user_id must be a non-empty string")
        if not isinstance(category, MemoryCategory):
            raise MemoryValidationError("category must be a MemoryCategory enum member")
        if not isinstance(key, str) or not key.strip():
            raise MemoryValidationError("key must be a non-empty string")
        if not isinstance(value, str):
            raise MemoryValidationError("value must be a string")
        now = datetime.now(timezone.utc)
        return MemoryRecord(
            id=new_memory_id(),
            user_id=user_id,
            category=category,
            key=key,
            value=value,
            source=source,
            created_at=now,
            updated_at=now,
            expires_at=expires_at,
            metadata=metadata or {},
        )

    def validate_memory(self, record: MemoryRecord):
        """Returns PolicyEngine's decision for persisting this record. Never persists anything itself."""
        return self._policy_engine.evaluate_privacy(record.key, operation="persist")

    def persist_memory(self, record: MemoryRecord) -> MemoryRecord:
        """
        Validates then persists. The ONLY path a MemoryRecord reaches
        storage through — nothing else in this module writes to the
        repository. Raises MemoryPolicyDeniedError if PolicyEngine denies
        the write (e.g. a restricted key like "medical_condition" or
        "payment_method" for the "persist" operation, per
        configs/policies/privacy.yaml's restricted_fields).

        If a PrivacyService was configured (Phase 6), the record's
        *value* is additionally scanned for embedded PII content
        (independent of its key) via PolicyEngine.evaluate_pii(), context
        "MEMORY": a BLOCK-worthy finding (e.g. a credit card number typed
        into a free-text preference) denies the write the same way a
        restricted key does; a REDACT/RESTRICT-worthy finding persists a
        redacted copy instead of the raw value — this module never
        persists a value it knows contains such content unredacted.

        Raises MemoryOwnershipError if `record.id` already belongs to a
        different user — see MemoryOwnershipError's docstring. A brand
        new id (the normal propose_memory() -> persist_memory() path) or
        a same-user id reuse (test_duplicate_memory_ids_do_not_raise)
        both pass this check unaffected.
        """
        existing = self._repository.get(record.id)
        if existing is not None and existing.user_id != record.user_id:
            if self._security_detector is not None:
                self._security_detector.record_cross_user_access_attempt("memory", record.user_id)
            raise MemoryOwnershipError("Memory write denied: this id belongs to a different user.")

        decision = self.validate_memory(record)
        if not decision.allowed:
            raise MemoryPolicyDeniedError(f"Memory write denied by policy: {decision.reason}")

        if self._privacy_service is not None:
            pii_decision = self._privacy_service.decide(record.value, context="MEMORY")
            if pii_decision.findings:
                self._record_pii_event(pii_decision, record)
            if not pii_decision.allowed:
                raise MemoryPolicyDeniedError(f"Memory write denied by PII policy: {pii_decision.reason}")
            if pii_decision.action in ("REDACT", "RESTRICT") and pii_decision.findings:
                record = MemoryRecord(
                    id=record.id,
                    user_id=record.user_id,
                    category=record.category,
                    key=record.key,
                    value=self._privacy_service.redact(record.value, list(pii_decision.findings)),
                    source=record.source,
                    created_at=record.created_at,
                    updated_at=record.updated_at,
                    expires_at=record.expires_at,
                    metadata=record.metadata,
                    version=getattr(record, "version", 1),
                )

        self._repository.save(record)
        return record

    def _record_pii_event(self, pii_decision, record: MemoryRecord) -> None:
        """
        Emits PII_DETECTED plus a decision-specific event, derived only
        from `pii_decision` (already computed by PrivacyService.decide())
        -- never the raw value itself. Per plan.md Step 8.11, the audit
        record identifies category/action/outcome, never the matched PII
        content.
        """
        if self._audit_logger is None:
            return
        from observability_models import EventType

        pii_types = sorted({f.type.value for f in pii_decision.findings})
        self._audit_logger.record(
            EventType.PII_DETECTED,
            outcome="detected",
            actor=record.user_id,
            resource="memory",
            action=pii_decision.action,
            metadata={"pii_types": pii_types, "context": "MEMORY"},
        )
        if pii_decision.action == "BLOCK":
            self._audit_logger.record(
                EventType.PRIVACY_BLOCK,
                outcome="denied",
                actor=record.user_id,
                resource="memory",
                reason=pii_decision.reason,
                metadata={"pii_types": pii_types, "context": "MEMORY"},
            )
        elif pii_decision.action == "REDACT":
            self._audit_logger.record(
                EventType.PII_REDACTED,
                outcome="redacted",
                actor=record.user_id,
                resource="memory",
                metadata={"pii_types": pii_types, "context": "MEMORY"},
            )
        elif pii_decision.action == "RESTRICT":
            self._audit_logger.record(
                EventType.PRIVACY_RESTRICT,
                outcome="restricted",
                actor=record.user_id,
                resource="memory",
                metadata={"pii_types": pii_types, "context": "MEMORY"},
            )

    def remove_memory(self, memory_id: str, user_id: str) -> bool:
        """Deletes only if the record belongs to `user_id` — never confirms or denies existence of another user's record via a different return path."""
        record = self._repository.get(memory_id)
        if record is None:
            return False
        if record.user_id != user_id:
            if self._security_detector is not None:
                self._security_detector.record_cross_user_access_attempt("memory", user_id)
            return False
        self._repository.delete(memory_id)
        return True

    def list_allowed_memory(self, user_id: str) -> list[MemoryRecord]:
        """All of this user's own, non-expired records — a management/audit view. Never another user's records."""
        if not isinstance(user_id, str) or not user_id.strip():
            return []
        now = datetime.now(timezone.utc)
        return [r for r in self._repository.list_for_user(user_id) if not r.is_expired(now)]

    def get_allowed_context(self, user_id: str, category: Optional[MemoryCategory] = None) -> list[MemoryRecord]:
        """
        The policy-filtered subset of this user's memory appropriate to
        expose to LLM context assembly — scoped by user_id, optionally by
        category, and by PolicyEngine's `expose_downstream` operation.
        Never raw database records, never another user's data, never
        internal metadata beyond what MemoryRecord itself exposes.
        """
        records = self.list_allowed_memory(user_id)
        if category is not None:
            records = [r for r in records if r.category == category]
        allowed = []
        for record in records:
            decision = self._policy_engine.evaluate_privacy(record.key, operation="expose_downstream")
            if decision.allowed:
                allowed.append(record)
        return allowed

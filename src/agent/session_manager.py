"""
SessionManager and SessionRepository (Phase 5; plan.md Steps 5.3, 5.4, 5.5).

SessionManager owns lifecycle rules (validation, transitions,
expiration, authorization); SessionRepository owns storage only, so
storage can later be swapped without touching SessionManager (plan.md
Step 5.4). No persistence technology exists anywhere in this repository
yet, so SessionRepository's only implementation here is in-memory —
the simplest appropriate abstraction per plan.md's own instruction when
nothing exists to build on.

Security-relevant behavior, all covered by tests/test_session_manager.py:
- `get_session()` never raises and never returns another user's session
  — it returns None for missing, expired, or cross-user access attempts,
  so callers uniformly treat "no usable session" as the safe default.
- A session past its `expires_at` is lazily transitioned to EXPIRED (and
  has its pending workflow state cleared) the moment anything tries to
  read it — a stale WAITING_FOR_CONFIRMATION session can never be picked
  back up by a later "yes" once expired (plan.md Step 5.5's explicit
  example).
"""

import copy
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

from db import ConcurrentModificationError
from session_models import ALLOWED_TRANSITIONS, DEFAULT_SESSION_TTL, SessionState, SessionStatus, new_session_id


class SessionNotFoundError(LookupError):
    """Raised by transition_state()/update_session() when no usable session exists (missing, expired, or unauthorized)."""


class InvalidTransitionError(ValueError):
    """Raised when a requested status transition isn't in ALLOWED_TRANSITIONS for the session's current status."""


class SessionRepository:
    """
    In-memory session storage with optimistic concurrency (Phase 13, Step 13.2).
    Swappable — SessionManager depends only on this interface (get/save/delete).
    Individually thread-safe (Phase 10, plan.md Step 10.14) and version-checked.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._sessions: dict[str, SessionState] = {}
        self._versions: dict[str, int] = {}

    def get(self, session_id: str) -> Optional[SessionState]:
        with self._lock:
            s = self._sessions.get(session_id)
            if s is None:
                return None
            res = copy.copy(s)
            res.version = self._versions.get(session_id, getattr(s, "version", 1))
            return res

    def save(self, session: SessionState) -> None:
        with self._lock:
            sid = session.session_id
            expected_version = getattr(session, "version", 1) or 1
            if sid in self._sessions:
                current_ver = self._versions.get(sid, 1)
                if expected_version != current_ver:
                    raise ConcurrentModificationError(
                        f"Concurrent modification detected for session '{sid}': "
                        f"expected version {expected_version}, current version {current_ver}"
                    )
                new_ver = current_ver + 1
                self._versions[sid] = new_ver
                session.version = new_ver
                self._sessions[sid] = copy.copy(session)
            else:
                self._versions[sid] = expected_version
                self._sessions[sid] = copy.copy(session)

    def delete(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)
            self._versions.pop(session_id, None)

    def delete_expired_before(self, cutoff: datetime) -> int:
        """
        Phase 24 (data lifecycle): deletes every session whose own
        `expires_at` is before `cutoff` -- i.e. sessions the application's
        own existing TTL logic already considers expired (`is_expired()`),
        never a new retention decision. `_expire()` already marks these
        `EXPIRED` on access, but never removed the row (docs/DATABASE.md
        §"Storage Hygiene" already disclosed this). Returns the count
        deleted, for a caller to log/report -- never silent.
        """
        with self._lock:
            expired_ids = [sid for sid, s in self._sessions.items() if s.expires_at < cutoff]
            for sid in expired_ids:
                self._sessions.pop(sid, None)
                self._versions.pop(sid, None)
            return len(expired_ids)


_UPDATABLE_FIELDS = {
    "current_intent",
    "workflow_state",
    "pending_action",
    "pending_parameters",
    "confirmation_state",
    "metadata",
}


class SessionManager:
    def __init__(
        self,
        repository: Optional[SessionRepository] = None,
        ttl: timedelta = DEFAULT_SESSION_TTL,
        audit_logger=None,
        security_detector=None,
    ):
        self._repository = repository or SessionRepository()
        self._ttl = ttl
        # Phase 8, both optional -- None preserves exact Phase 5 behavior.
        # SessionManager remains the sole authority for session events
        # (plan.md: "Keep SessionManager authoritative for session
        # events") -- these are emitted here, from the real state
        # transition already made, never anywhere else.
        self._audit_logger = audit_logger
        self._security_detector = security_detector
        # Phase 10 (plan.md Step 10.14): coarse, instance-level, re-
        # entrant lock covering every public method's full read-modify-
        # write sequence -- SessionRepository's own per-call lock alone
        # is not enough, since a lost-update race lives ABOVE the
        # repository (get a session, mutate the shared object in place,
        # save it back) not inside any single dict operation.
        # Re-entrant because update_session() calls get_session()
        # internally, and both acquire this same lock.
        self._lock = threading.RLock()

    def create_session(self, session_id: Optional[str] = None, user_id: Optional[str] = None) -> SessionState:
        with self._lock:
            sid = session_id or new_session_id()
            if not isinstance(sid, str) or not sid.strip():
                raise ValueError("session_id must be a non-empty string")
            self._repository.delete(sid)
            now = datetime.now(timezone.utc)
            session = SessionState(
                session_id=sid, user_id=user_id, created_at=now, updated_at=now, expires_at=now + self._ttl
            )
            self._repository.save(session)
            if self._audit_logger is not None:
                from observability_models import EventType

                self._audit_logger.record(
                    EventType.SESSION_CREATED,
                    outcome="success",
                    actor=user_id,
                    session_id=sid,
                    resource="session",
                )
            return session

    def get_session(self, session_id, user_id: Optional[str] = None) -> Optional[SessionState]:
        """
        Returns the session if it exists, is not expired/terminal, and
        (when `user_id` is given) belongs to that user — otherwise None.
        Never raises. See module docstring for the security rationale.
        """
        with self._lock:
            if not isinstance(session_id, str) or not session_id.strip():
                return None
            session = self._repository.get(session_id)
            if session is None:
                return None
            if session.status in (SessionStatus.EXPIRED, SessionStatus.FAILED, SessionStatus.COMPLETED):
                return None
            if session.is_expired():
                self._expire(session)
                return None
            if user_id is not None and session.user_id is not None and session.user_id != user_id:
                # Cross-user access attempt — never leak another user's
                # session. Also a real security event (plan.md Step 8.17),
                # derived from this actual denial, not a guess.
                if self._security_detector is not None:
                    self._security_detector.record_cross_user_access_attempt("session", user_id)
                return None
            return session

    def get_or_create_session(self, session_id: str, user_id: Optional[str] = None) -> Optional[SessionState]:
        """
        Resolves a caller-supplied session_id for `user_id`: returns the
        caller's usable session, or creates one if the id is unused (or
        the caller's own session has expired/ended). Returns None -- and
        touches nothing -- when the id belongs to a different owner, so a
        guessed or leaked session_id can never delete, reset, re-own, or
        act on someone else's session (docs/MASTER_PROJECT_PLAN.md F-07).
        A caller with no identity (user_id=None) is never treated as the
        owner of a session that has one.
        """
        with self._lock:
            existing = self._repository.get(session_id) if isinstance(session_id, str) else None
            if existing is not None and existing.user_id is not None and existing.user_id != user_id:
                if self._security_detector is not None:
                    self._security_detector.record_cross_user_access_attempt("session", user_id or "unauthenticated")
                return None
            session = self.get_session(session_id, user_id=user_id)
            if session is None:
                session = self.create_session(session_id=session_id, user_id=user_id)
            return session

    def _expire(self, session: SessionState) -> None:
        """Assumes the caller already holds self._lock (private helper, called only from within a locked method)."""
        session.status = SessionStatus.EXPIRED
        session.pending_action = None
        session.pending_parameters = {}
        session.workflow_state = None
        session.updated_at = datetime.now(timezone.utc)
        self._repository.save(session)
        if self._audit_logger is not None:
            from observability_models import EventType

            self._audit_logger.record(
                EventType.SESSION_EXPIRED,
                outcome="success",
                actor=session.user_id,
                session_id=session.session_id,
                resource="session",
            )

    def _get_for_transition(self, session_id, user_id: Optional[str] = None) -> Optional[SessionState]:
        """
        Like get_session(), but does NOT hide a session already in a
        terminal status (COMPLETED/FAILED/EXPIRED) — transition_state()
        needs to see those to correctly raise InvalidTransitionError
        ("this session exists but can't move from here") rather than the
        less precise SessionNotFoundError a hidden terminal session would
        otherwise produce. A lazily-expiring (not-yet-marked) session is
        still expired here, same as get_session(). Assumes the caller
        already holds self._lock.
        """
        if not isinstance(session_id, str) or not session_id.strip():
            return None
        session = self._repository.get(session_id)
        if session is None:
            return None
        if (
            session.status not in (SessionStatus.EXPIRED, SessionStatus.FAILED, SessionStatus.COMPLETED)
            and session.is_expired()
        ):
            self._expire(session)
        if user_id is not None and session.user_id is not None and session.user_id != user_id:
            return None
        return session

    def transition_state(self, session_id, new_status: SessionStatus, user_id: Optional[str] = None) -> SessionState:
        with self._lock:
            session = self._get_for_transition(session_id, user_id=user_id)
            if session is None:
                raise SessionNotFoundError(f"No usable session: {session_id}")
            if not isinstance(new_status, SessionStatus):
                raise ValueError("new_status must be a SessionStatus enum member — no arbitrary state strings")
            allowed = ALLOWED_TRANSITIONS.get(session.status, frozenset())
            if new_status not in allowed:
                if self._audit_logger is not None:
                    from observability_models import EventType

                    self._audit_logger.record(
                        EventType.SESSION_INVALID_TRANSITION,
                        outcome="denied",
                        actor=session.user_id,
                        session_id=session_id,
                        resource="session",
                        reason=f"{session.status.value} -> {new_status.value} is not an allowed transition.",
                    )
                raise InvalidTransitionError(
                    f"Cannot transition session '{session_id}' from {session.status.value} to {new_status.value}"
                )
            session.status = new_status
            session.updated_at = datetime.now(timezone.utc)
            self._repository.save(session)
            return session

    def update_session(self, session_id, user_id: Optional[str] = None, **fields) -> SessionState:
        with self._lock:
            session = self.get_session(session_id, user_id=user_id)
            if session is None:
                raise SessionNotFoundError(f"No usable session: {session_id}")
            unsupported = set(fields.keys()) - _UPDATABLE_FIELDS
            if unsupported:
                raise ValueError(f"Cannot update unsupported session field(s): {sorted(unsupported)}")
            for key, value in fields.items():
                setattr(session, key, value)
            session.updated_at = datetime.now(timezone.utc)
            self._repository.save(session)
            return session

    def try_consume_pending_confirmation(self, session_id, user_id: Optional[str] = None) -> Optional[tuple[str, dict]]:
        """
        Atomically checks whether `session_id` has a pending
        AWAITING_CONFIRMATION action and, if so, clears it and returns
        `(action_name, parameters)` in one lock-held step. Returns `None`
        if there is nothing usable to consume (no session, wrong user,
        expired, or no pending confirmation).

        Phase 11 fix (plan.md Steps 11.13 Replay Attack / 11.22
        Concurrency Race Attack): ConversationManager._execute_pending_action()
        previously read the pending action via get_session() and cleared
        it via a SEPARATE, later update_session() call -- two concurrent
        "yes" replies for the same session could both observe
        AWAITING_CONFIRMATION before either cleared it, and both invoke
        the tool, executing a non-idempotent business action twice. This
        method closes that window: the read-and-clear is now a single
        atomic operation, so only the first of two racing callers ever
        gets a non-None result -- the second sees the already-cleared
        state and must not re-execute anything.

        Phase 12.6 (plan.md: "Do not rely only on Python locks because
        multiple application processes may exist"): when `self._repository`
        provides its own atomic `try_consume_pending_confirmation()` (a
        DB-backed repository -- see session_repository_postgres.py),
        delegate to it directly instead of the in-process RLock below.
        That method's atomicity is enforced by the database (a single
        guarded UPDATE), which is required once more than one process can
        share the same repository -- an in-process lock alone cannot
        cover that. The in-memory SessionRepository has no such method,
        so hasattr() is False for it and every existing caller/test keeps
        using the exact RLock-based path unchanged below.
        """
        repo_consume = getattr(self._repository, "try_consume_pending_confirmation", None)
        if callable(repo_consume):
            return repo_consume(session_id, user_id=user_id)
        with self._lock:
            session = self.get_session(session_id, user_id=user_id)
            if session is None:
                return None
            if session.workflow_state != "AWAITING_CONFIRMATION" or not session.pending_action:
                return None
            action_name = session.pending_action
            params = dict(session.pending_parameters)
            session.workflow_state = None
            session.pending_action = None
            session.pending_parameters = {}
            session.updated_at = datetime.now(timezone.utc)
            self._repository.save(session)
            return action_name, params

    def try_consume_pending_authentication(
        self, session_id, user_id: Optional[str] = None
    ) -> Optional[tuple[str, dict]]:
        """
        Atomically checks whether `session_id` has a pending
        AWAITING_AUTHENTICATION action and, if so, clears it and returns
        `(action_name, parameters)` in one lock-held step.
        """
        repo_consume = getattr(self._repository, "try_consume_pending_authentication", None)
        if callable(repo_consume):
            return repo_consume(session_id, user_id=user_id)
        with self._lock:
            session = self.get_session(session_id, user_id=user_id)
            if session is None:
                return None
            if session.workflow_state != "AWAITING_AUTHENTICATION" or not session.pending_action:
                return None
            action_name = session.pending_action
            params = dict(session.pending_parameters)
            session.workflow_state = None
            session.pending_action = None
            session.pending_parameters = {}
            session.updated_at = datetime.now(timezone.utc)
            self._repository.save(session)
            return action_name, params

    def expire_session(self, session_id) -> None:
        with self._lock:
            session = self._repository.get(session_id)
            if session is not None and session.status not in (
                SessionStatus.EXPIRED,
                SessionStatus.FAILED,
                SessionStatus.COMPLETED,
            ):
                self._expire(session)

    def delete_session(self, session_id) -> None:
        with self._lock:
            self._repository.delete(session_id)

    def purge_expired_sessions(self, before: Optional[datetime] = None) -> int:
        """
        Phase 24 (data lifecycle): operator-invoked bulk deletion of
        sessions already expired by their own `expires_at` (never a new
        retention decision -- see SessionRepository.delete_expired_before()'s
        docstring). `before` defaults to now; a caller may pass an
        earlier cutoff to purge only sessions expired for at least that
        long. Never called automatically anywhere in this codebase (no
        background scheduler exists) -- see scripts/purge_expired_sessions.py
        for the operator-facing entry point. Emits one DATA_PURGED audit
        event summarizing the count, so a purge run is itself part of the
        audit trail like every other real decision this class makes.
        """
        cutoff = before or datetime.now(timezone.utc)
        with self._lock:
            count = self._repository.delete_expired_before(cutoff)
        if self._audit_logger is not None and count > 0:
            from observability_models import EventType

            self._audit_logger.record(
                EventType.DATA_PURGED,
                outcome="success",
                resource="session",
                metadata={"count": count, "cutoff": cutoff.isoformat()},
            )
        return count

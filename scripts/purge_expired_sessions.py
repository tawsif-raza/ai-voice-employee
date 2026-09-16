"""
Phase 24 (data lifecycle) -- operator-invoked purge of already-expired
session rows from the real database.

This is NOT a new retention policy: it deletes exactly the sessions this
application's own existing TTL logic (`SessionState.is_expired()`) already
considers expired, which currently linger in the database indefinitely
after expiring (docs/DATABASE.md's own disclosed "no scheduled background
cleanup daemon" gap -- SessionManager._expire() only flips a status flag,
it never removes the row). Nothing here changes when a session is
considered expired, only whether an already-expired row is ever removed.

Never runs automatically -- there is no scheduler anywhere in this
repository, and this script deliberately does not add one (that is an
infrastructure/deployment decision, not something to invent here).

Dry-run by default: reports how many rows WOULD be deleted without
deleting anything. Pass --confirm to actually delete.

This script deliberately does NOT purge memory records or audit events:
- Memory records: MemoryRepositoryPostgres has no unscoped/bulk query
  method by deliberate, pre-existing design (see its own module
  docstring) -- a genuine, undecided finding, not something this script
  works around. See PHASE_24_DATA_PRIVACY_REPORT.md.
- Audit events: append-only by design; a real retention period for
  audit/compliance data is a legal/business decision this script does
  not make on anyone's behalf (plan.md Rule 5).

Run with:
    python scripts/purge_expired_sessions.py                 # dry run
    python scripts/purge_expired_sessions.py --confirm        # actually delete
    python scripts/purge_expired_sessions.py --older-than-days 30 --confirm
"""

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "agent"))
from audit import AuditLogger  # noqa: E402
from db import DatabaseUnavailableError, load_database_config  # noqa: E402
from session_repository_postgres import PostgresSessionRepository  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--older-than-days",
        type=float,
        default=0.0,
        help="Only purge sessions expired for at least this many days (default: 0 -- purge anything already expired).",
    )
    parser.add_argument("--confirm", action="store_true", help="Actually delete. Without this flag, dry-run only.")
    args = parser.parse_args()

    try:
        from db import Database

        db_config = load_database_config()
        database = Database(db_config)
    except DatabaseUnavailableError as exc:
        print(f"Database unavailable -- cannot purge: {exc}")
        return 1

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.older_than_days)
    repo = PostgresSessionRepository(database)

    if not args.confirm:
        # Repository has no read-only "count expired" method (by the same
        # least-surface-area discipline as everywhere else in this
        # codebase) -- dry-run reports the cutoff and instructions rather
        # than fabricating a count without actually querying.
        print(f"DRY RUN -- would delete sessions expired before {cutoff.isoformat()}.")
        print("Re-run with --confirm to actually delete. No changes made.")
        return 0

    count = repo.delete_expired_before(cutoff)
    print(f"Deleted {count} expired session(s) (cutoff: {cutoff.isoformat()}).")

    audit_logger = AuditLogger()
    if count > 0:
        from observability_models import EventType

        audit_logger.record(
            EventType.DATA_PURGED,
            outcome="success",
            resource="session",
            metadata={"count": count, "cutoff": cutoff.isoformat(), "source": "scripts/purge_expired_sessions.py"},
        )
        # This script's own AuditLogger is a fresh in-memory instance
        # (it has no access to the running application's shared one) --
        # the event above proves the record() call itself is correct and
        # doesn't raise, but is not written to the application's real
        # audit trail. A future integration should call
        # SessionManager.purge_expired_sessions() (which does emit into
        # the shared, wired-up AuditLogger) from within the running
        # application/an admin task instead, once an operator decides
        # how this should be scheduled.
        print("(Note: this script's audit event is recorded to a standalone AuditLogger, not the running "
              "application's shared one -- see SessionManager.purge_expired_sessions() for the in-process "
              "equivalent that does share it.)")

    database.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())

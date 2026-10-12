"""
Safe, local, in-memory mock tool implementations (Phase 4; plan.md
Step 4.7). Not a real business integration — the purpose is to prove the
ToolOrchestrator architecture end to end (validation -> policy ->
confirmation -> execution -> result validation), not to book real
appointments or look up real orders. No production system is contacted.

Every function here has a uniform signature `fn(params: dict) -> dict`
and:
  - raises ValueError for missing/invalid parameters (caught by
    ToolOrchestrator and turned into a typed failure result, never
    propagated raw),
  - raises KeyError for a reference to a record that doesn't exist,
  - supports two test-only simulation hooks read from `params`:
    `_simulate_failure` (raise immediately) and `_simulate_delay_seconds`
    (sleep, to exercise ToolOrchestrator's timeout enforcement). Both are
    underscore-prefixed so they read unambiguously as test scaffolding,
    never a real business parameter.

State is held per-store-instance (not module-level globals), so tests
constructing a fresh store never see another test's data.
"""

import time
from typing import Optional

# Same-directory import (src/agent/) -- works whenever this module is
# reached via the sys.path.insert(.../src/agent) convention every other
# entry point in this package already uses (see conversation_manager.py).
from action_models import ActionSpec
from identity import Permission
from tool_registry import ToolRegistry


def _maybe_simulate(params: dict) -> None:
    if params.get("_simulate_failure"):
        raise RuntimeError("Simulated tool failure (test hook: _simulate_failure)")
    delay = params.get("_simulate_delay_seconds")
    if delay:
        time.sleep(delay)


class MockAppointmentStore:
    """In-memory appointment records with predictable, incrementing IDs."""

    def __init__(self):
        self._next_id = 1000
        self._appointments: dict[str, dict] = {}

    def _new_id(self) -> str:
        self._next_id += 1
        return f"appt_{self._next_id}"

    def book(self, params: dict) -> dict:
        """
        `params["owner_user_id"]` (Phase 7, optional) records who booked
        the appointment, so a later cancel/reschedule can be authorized
        against it via get_owner() + ToolRequest.resource_owner_user_id.
        This key is NOT part of BOOK_APPOINTMENT's public params_schema
        (see mock_tools.build_default_tool_registry()) — it is never
        settable by an untrusted ActionProposal; only
        ConversationManager._handle_tool_action() injects it into an
        already-validated ToolRequest.params from the trusted `auth`
        context, after validate_proposal() has already run. See
        PHASE_7 report's technical debt for why this exists for
        BOOK_APPOINTMENT only, not CANCEL/RESCHEDULE's read side.
        """
        _maybe_simulate(params)
        required = ("doctor_id", "date", "time")
        missing = [p for p in required if not params.get(p)]
        if missing:
            raise ValueError(f"Missing required parameters: {missing}")
        appointment_id = self._new_id()
        record = {
            "appointment_id": appointment_id,
            "doctor_id": params["doctor_id"],
            "date": params["date"],
            "time": params["time"],
            "status": "booked",
            "owner_user_id": params.get("owner_user_id"),
        }
        self._appointments[appointment_id] = record
        return {k: v for k, v in record.items() if k != "owner_user_id"}

    def get_owner(self, appointment_id: str) -> Optional[str]:
        """Read-only ownership lookup — never used for authorization decisions itself, only to supply resource_owner_user_id to PolicyEngine.evaluate_authorization()."""
        record = self._appointments.get(appointment_id)
        return record.get("owner_user_id") if record else None

    def cancel(self, params: dict) -> dict:
        _maybe_simulate(params)
        appointment_id = params.get("appointment_id")
        if not appointment_id:
            raise ValueError("Missing required parameter: appointment_id")
        record = self._appointments.get(appointment_id)
        if record is None:
            raise KeyError(f"Unknown appointment_id: {appointment_id}")
        if record["status"] == "cancelled":
            raise ValueError(f"Appointment {appointment_id} is already cancelled")
        record["status"] = "cancelled"
        return {k: v for k, v in record.items() if k != "owner_user_id"}

    def reschedule(self, params: dict) -> dict:
        _maybe_simulate(params)
        appointment_id = params.get("appointment_id")
        if not appointment_id:
            raise ValueError("Missing required parameter: appointment_id")
        record = self._appointments.get(appointment_id)
        if record is None:
            raise KeyError(f"Unknown appointment_id: {appointment_id}")
        if record["status"] == "cancelled":
            raise ValueError(f"Cannot reschedule a cancelled appointment: {appointment_id}")
        for key in ("date", "time"):
            if params.get(key):
                record[key] = params[key]
        record["status"] = "rescheduled"
        return {k: v for k, v in record.items() if k != "owner_user_id"}


class MockOrderStore:
    """In-memory, pre-seeded order records — read-only lookups."""

    def __init__(self, seed: Optional[dict] = None):
        self._orders = (
            seed
            if seed is not None
            else {
                "order_1001": {"order_id": "order_1001", "status": "shipped", "eta": "2026-08-20"},
                "order_1002": {"order_id": "order_1002", "status": "processing", "eta": None},
            }
        )

    def lookup(self, params: dict) -> dict:
        _maybe_simulate(params)
        order_id = params.get("order_id")
        if not order_id:
            raise ValueError("Missing required parameter: order_id")
        record = self._orders.get(order_id)
        if record is None:
            raise KeyError(f"Unknown order_id: {order_id}")
        return dict(record)


# ── Registry factory ──────────────────────────────────────────────────────
#
# Action names match configs/policies/tools.yaml and configs/policies/
# confirmation.yaml exactly (BOOK_APPOINTMENT, CANCEL_APPOINTMENT,
# RESCHEDULE_APPOINTMENT, ORDER_LOOKUP) so PolicyEngine's existing Phase 3
# configuration governs these tools with no changes needed on either side.


def build_default_tool_registry(
    appointment_store: Optional[MockAppointmentStore] = None,
    order_store: Optional[MockOrderStore] = None,
) -> ToolRegistry:
    """Builds a ToolRegistry with the four Phase 4 mock tools registered. Fresh stores per call unless explicitly shared."""
    appointments = appointment_store if appointment_store is not None else MockAppointmentStore()
    orders = order_store if order_store is not None else MockOrderStore()

    registry = ToolRegistry()

    registry.register(
        ActionSpec(
            name="BOOK_APPOINTMENT",
            description="Book a new appointment.",
            params_schema={"doctor_id": "str", "date": "str", "time": "str"},
            required_params=("doctor_id", "date", "time"),
            requires_confirmation=False,
            destructive=False,
            timeout_seconds=5.0,
            required_permission=Permission.BOOK_APPOINTMENT.value,
            # Phase 10 (plan.md Step 10.6): creates a NEW resource each
            # call -- retrying a lost/timed-out response would create a
            # second, duplicate appointment. Never auto-retried.
            idempotency="NON_IDEMPOTENT_WRITE",
        ),
        appointments.book,
    )
    registry.register(
        ActionSpec(
            name="CANCEL_APPOINTMENT",
            description="Cancel an existing appointment.",
            params_schema={"appointment_id": "str"},
            required_params=("appointment_id",),
            requires_confirmation=True,
            destructive=True,
            timeout_seconds=5.0,
            required_permission=Permission.CANCEL_APPOINTMENT.value,
            # Phase 10: the end state converges regardless of retry count
            # -- the appointment ends up cancelled either way. The mock
            # implementation (MockAppointmentStore.cancel) raises on a
            # genuine double-cancel rather than silently succeeding
            # twice, so a retry after the first call actually landed
            # produces a harmless failure response, never a second,
            # unsafe side effect -- exactly the retry-safety property
            # this classification is meant to capture.
            idempotency="IDEMPOTENT_WRITE",
        ),
        appointments.cancel,
        owner_lookup=lambda params: appointments.get_owner(params.get("appointment_id")),
    )
    registry.register(
        ActionSpec(
            name="RESCHEDULE_APPOINTMENT",
            description="Reschedule an existing appointment to a new date/time.",
            params_schema={"appointment_id": "str", "date": "str", "time": "str"},
            required_params=("appointment_id",),
            requires_confirmation=True,
            destructive=True,
            timeout_seconds=5.0,
            # Reuses CANCEL_APPOINTMENT's permission rather than a new
            # RESCHEDULE-specific one -- plan.md's permission list has no
            # separate reschedule permission, and "may modify their own
            # appointment" is exactly what CANCEL_APPOINTMENT already
            # represents (least privilege: no new permission was needed).
            required_permission=Permission.CANCEL_APPOINTMENT.value,
            # Phase 10: repeating the SAME (appointment_id, date, time)
            # converges to the same end state -- safe to retry, same
            # reasoning as CANCEL_APPOINTMENT above.
            idempotency="IDEMPOTENT_WRITE",
        ),
        appointments.reschedule,
        owner_lookup=lambda params: appointments.get_owner(params.get("appointment_id")),
    )
    registry.register(
        ActionSpec(
            name="ORDER_LOOKUP",
            description="Look up the status of an existing order.",
            params_schema={"order_id": "str"},
            required_params=("order_id",),
            requires_confirmation=False,
            destructive=False,
            timeout_seconds=5.0,
            required_permission=Permission.READ_ORDER.value,
            idempotency="READ_ONLY",
        ),
        orders.lookup,
    )

    return registry

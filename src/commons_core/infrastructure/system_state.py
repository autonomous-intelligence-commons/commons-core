"""Infrastructure-owned system state (spec §16, §21).

Small typed accessors for the ``system_state`` key/value table. This state is
owned by INFRASTRUCTURE only (emergency stop, worker heartbeat); it is never
writable from agent-generated content. Phase 1 keys:

- ``emergency_stop`` — ``{"active": bool, "set_at": iso, "set_by": agent_id}``.
  Written exclusively by the admin emergency-stop endpoints; read by the
  ``require_not_paused`` API dependency, the worker loop and telemetry.
- ``last_heartbeat`` — ``{"at": iso}``, written by the deterministic
  ``heartbeat`` task handler; surfaced in admin telemetry as
  ``last_worker_heartbeat``.

PRIVACY: values carry operational timestamps and (for ``emergency_stop``) the
admin agent id that toggled it — infrastructure-owned operational metadata,
never collaboration content. Telemetry exposes only ``active`` and the
heartbeat timestamp.
"""

from datetime import datetime

from sqlalchemy.orm import Session

from commons_core.database.enums import SecurityEventCategory
from commons_core.database.models import SystemState
from commons_core.infrastructure.time import utc_now
from commons_core.security.events import record_event

EMERGENCY_STOP_KEY = "emergency_stop"
LAST_HEARTBEAT_KEY = "last_heartbeat"


def read_system_state(session: Session, key: str) -> dict | None:
    """Return the stored value dict for ``key``, or ``None`` when absent."""
    row = session.get(SystemState, key)
    if row is None:
        return None
    return dict(row.value or {})


def write_system_state(session: Session, key: str, value: dict) -> SystemState:
    """Insert or update a system-state row (flush only; caller commits)."""
    row = session.get(SystemState, key)
    if row is None:
        row = SystemState(key=key, value=dict(value))
        session.add(row)
    else:
        row.value = dict(value)
    session.flush()
    return row


def emergency_stop_active(session: Session) -> bool:
    """True while the infrastructure emergency stop is engaged (spec §21)."""
    value = read_system_state(session, EMERGENCY_STOP_KEY)
    return bool((value or {}).get("active"))


class EmergencyStopStateError(Exception):
    """The required emergency-stop transition could not be durably confirmed.

    Internal signal for the admin routes (R04 remediation, 2026-10-30): the
    requested state is NOT verifiably in effect — the required commit failed
    and an authoritative fresh-session re-read did not observe the requested
    state (or the re-read itself failed, leaving the outcome uncertain).
    Routes must translate this into a non-success response; never a false
    success (R04-INV-01/03/04).
    """


def _authoritative_stop_state(session: Session) -> dict | None:
    """Read the persisted emergency-stop value through a FRESH session.

    A new connection bypasses the (possibly broken or rolled-back) request
    transaction, so the value read here is the durable database state —
    used to reconcile uncertain commit outcomes.
    """
    bind = session.get_bind()
    with Session(bind=bind) as fresh:
        return read_system_state(fresh, EMERGENCY_STOP_KEY)


def set_emergency_stop(
    session: Session,
    *,
    active: bool,
    principal: str,
    now: datetime | None = None,
) -> dict:
    """Engage or release the emergency stop, then record the security event.

    R04 durability contract — two stages:

    1. REQUIRED: the state row is written and committed by this function
       (explicit durable-state confirmation). A commit failure is
       reconciled with an authoritative fresh-session re-read: when the
       durable state already matches the request (an uncertain commit that
       actually took effect, or an idempotent repeat) the transition is
       honestly reported as in effect; otherwise
       :class:`EmergencyStopStateError` is raised and the caller must
       respond non-success.
    2. OPTIONAL: the ``emergency_stop`` security event is recorded in its
       own transaction afterwards. Recording failures are swallowed and
       logged by ``record_event`` (category only) — an already-committed
       stop is never undone by an audit failure. AUDIT-LOSS POLICY: a
       failed event is dropped without retry; there is no exactly-once
       audit guarantee (documented non-atomicity, R04-INV-09).

    Both stages commit the session they are given: pass a session without
    unrelated pending writes (the admin emergency-stop routes satisfy
    this — they have no other pending domain writes). The event details
    carry the action only (``set``/``clear``) — never content.
    """
    now = now or utc_now()
    write_system_state(
        session,
        EMERGENCY_STOP_KEY,
        {
            "active": bool(active),
            "set_at": now.isoformat(),
            "set_by": principal,
        },
    )
    try:
        session.commit()
    except Exception:
        try:
            session.rollback()
        except Exception:  # noqa: BLE001 — best-effort cleanup
            pass
        try:
            persisted = _authoritative_stop_state(session)
        except Exception:  # noqa: BLE001 — outcome stays uncertain
            persisted = None
        if bool((persisted or {}).get("active")) != bool(active):
            raise EmergencyStopStateError(
                "emergency-stop state transition not durably confirmed"
            ) from None
        # The durable state already matches the request (uncertain commit
        # that took effect, or an idempotent repeat): honest success.
    record_event(
        session,
        SecurityEventCategory.EMERGENCY_STOP,
        principal,
        {"action": "set" if active else "clear"},
    )
    return read_system_state(session, EMERGENCY_STOP_KEY) or {}

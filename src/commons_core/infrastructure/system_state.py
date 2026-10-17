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


def set_emergency_stop(
    session: Session,
    *,
    active: bool,
    principal: str,
    now: datetime | None = None,
) -> dict:
    """Engage or release the emergency stop and record the security event.

    Returns the stored state value. The event details carry the action only
    (``set``/``clear``) — never content. This function commits (the
    emergency-stop routes have no other pending writes), matching the
    ``record_event`` contract.
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
    record_event(
        session,
        SecurityEventCategory.EMERGENCY_STOP,
        principal,
        {"action": "set" if active else "clear"},
    )
    return read_system_state(session, EMERGENCY_STOP_KEY) or {}

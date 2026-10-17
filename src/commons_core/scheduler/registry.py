"""Task handler registry (spec §16, §17 — DETERMINISTIC SIMULATED HANDLERS ONLY).

Phase 1 registers ONLY deterministic, side-effect-bounded handlers — there
is NO AI model invocation, NO arbitrary code execution, and NO agent
interaction (spec §17: "Phase 1 must not permit arbitrary agent-generated
code execution"). The scheduler OFFERS execution opportunities; handlers are
message-free and never require an agent to produce messages or output
(spec §16 — "It must not require agents to produce messages").

Handler contract: ``handler(session: Session, task: ScheduledTask) -> None``.
A handler signals failure by RAISING; the scheduler service then applies the
retry/backoff policy. Handlers must never include payload content in
exception messages (``last_error`` stores the exception class + message,
truncated — see ``scheduler.service.execute_claimed``).

Registered handlers:
- ``noop`` — succeeds with no effect (the minimal execution opportunity).
- ``heartbeat`` — refreshes the ``system_state`` row ``last_heartbeat``
  (surfaced in admin telemetry as ``last_worker_heartbeat``); proves the
  durable pipeline executes without producing any agent-visible output.
- ``activity_reminder`` — validates that the payload references an activity
  the task OWNER belongs to (joined membership) and then succeeds silently.
  It is a SIMULATED placeholder: it demonstrates payload input validation
  and deliberately does NOT send messages or notifications (spec §16 —
  the scheduler must not require agents to produce messages).

Tests may register additional kinds via :func:`register` (and should clean
up with :func:`unregister`); unknown kinds are rejected at task-CREATE time
with 422 ``UNKNOWN_TASK_KIND`` (the registry is runtime state, so the check
lives in the service, not the pydantic schema).
"""

from typing import Protocol

from sqlalchemy.orm import Session

from commons_core.activities.service import get_activity, get_membership
from commons_core.database.enums import ActivityMemberState
from commons_core.database.models import ScheduledTask
from commons_core.infrastructure.system_state import (
    LAST_HEARTBEAT_KEY,
    write_system_state,
)
from commons_core.infrastructure.time import utc_now


class TaskHandler(Protocol):
    """Deterministic handler signature: raise to fail the attempt."""

    def __call__(self, session: Session, task: ScheduledTask) -> None: ...


#: name -> handler callable.
HANDLERS: dict[str, TaskHandler] = {}


def register(name: str, handler: TaskHandler) -> None:
    """Register (or replace) a handler under ``name`` (tests/operations)."""
    HANDLERS[name] = handler


def unregister(name: str) -> None:
    """Remove a handler registration (no error when absent)."""
    HANDLERS.pop(name, None)


def get_handler(name: str) -> TaskHandler | None:
    """Return the handler registered under ``name``, or ``None``."""
    return HANDLERS.get(name)


def known_kinds() -> tuple[str, ...]:
    """All registered handler names (sorted, machine-readable)."""
    return tuple(sorted(HANDLERS))


def _noop(session: Session, task: ScheduledTask) -> None:
    """Succeed with no effect — the minimal execution opportunity."""
    return None


def _heartbeat(session: Session, task: ScheduledTask) -> None:
    """Refresh ``system_state.last_heartbeat`` (operational liveness signal)."""
    write_system_state(session, LAST_HEARTBEAT_KEY, {"at": utc_now().isoformat()})


def _activity_reminder(session: Session, task: ScheduledTask) -> None:
    """SIMULATED reminder: validate the activity reference, succeed silently.

    Requires the payload to reference (``activity_id``) an activity the task
    OWNER is a JOINED member of. Raises on validation failure (the task then
    follows the normal retry/backoff policy). Produces NO messages — the
    scheduler never requires agents to produce or consume output (spec §16).
    """
    reference = task.payload.get("activity_id")
    if not isinstance(reference, str) or not reference:
        raise ValueError("activity_reminder requires an 'activity_id' payload field")
    activity = get_activity(session, reference)
    if activity is None:
        raise ValueError("referenced activity does not exist")
    membership = get_membership(session, activity.id, task.agent_id)
    if membership is None or membership.state != ActivityMemberState.JOINED:
        raise ValueError("task owner is not a joined member of the referenced activity")
    # Simulated effect: none. A real Phase 2 reminder would offer — never
    # require — an execution opportunity to the owner.


register("noop", _noop)
register("heartbeat", _heartbeat)
register("activity_reminder", _activity_reminder)

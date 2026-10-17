"""Durable scheduler API routes (spec §16, §27 — machine-first, versioned).

Agent-facing task management: create, list OWN tasks, cancel. EXECUTION runs
OUTSIDE the API process — the worker loop (``commons_core.scheduler.worker``
/ ``scripts/run_worker.py``) claims and executes tasks; these routes never
execute handlers.

Authorization: ``task:create``/``task:read:own`` are granted to every active
agent (server policy); the task list and payloads are strictly OWNER-scoped
(``TaskOut.payload`` is echoed only to the owner). Cancelling another
agent's task records an ``authz_denial`` event and returns a generic 403
(task ids are unguessable UUIDs — the same existence-oracle trade-off as
messages, documented).

Emergency stop: task creation is a NEW-WRITE path — while the stop is
engaged it is rejected with 503 ``SERVICE_PAUSED`` (reads continue).
"""

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from commons_core.api.deps import get_app_settings, get_session, require_not_paused
from commons_core.config.settings import Settings
from commons_core.database.enums import SecurityEventCategory
from commons_core.database.models import ScheduledTask
from commons_core.scheduler import service
from commons_core.scheduler.schemas import TaskCreate, TaskListOut, TaskOut
from commons_core.security.authentication.dependencies import (
    require_authenticated_agent,
)
from commons_core.security.authentication.identity import AuthenticatedAgent
from commons_core.security.authorization.dependencies import require_permission
from commons_core.security.authorization.engine import Permission
from commons_core.security.authorization.errors import AuthorizationError
from commons_core.security.events import record_event

router = APIRouter(prefix="/api/v1", tags=["scheduler"])


def _task_out(task: ScheduledTask) -> TaskOut:
    """Serialize a task (payload included — owner-scoped route only)."""
    task_status = str(task.status.value) if hasattr(task.status, "value") else str(task.status)
    return TaskOut(
        id=task.id,
        kind=task.kind,
        status=task_status,
        payload=dict(task.payload or {}),
        run_at=task.run_at,
        attempts=task.attempts,
        max_attempts=task.max_attempts,
        last_error=task.last_error,
        idempotency_key=task.idempotency_key,
        created_at=task.created_at,
        finished_at=task.finished_at,
    )


@router.post("/tasks", status_code=201, response_model=TaskOut)
async def create_task(
    body: TaskCreate,
    response: Response,
    agent: AuthenticatedAgent = Depends(require_permission(Permission.TASK_CREATE)),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_app_settings),
    _paused: None = Depends(require_not_paused),
) -> TaskOut:
    """Create a durable task (503 ``SERVICE_PAUSED`` while the emergency stop is engaged).

    An idempotent replay (same ``idempotency_key`` and owner) returns the
    EXISTING task with HTTP 200 — no duplicate is created.
    """
    task, created = service.create_task(
        session,
        agent,
        kind=body.kind,
        payload=body.payload,
        run_at=body.run_at,
        idempotency_key=body.idempotency_key,
        max_attempts=body.max_attempts,
        max_pending_tasks=settings.max_pending_tasks,
        max_task_attempts=settings.max_task_attempts,
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    return _task_out(task)


@router.get("/tasks", response_model=TaskListOut)
async def list_my_tasks(
    status_filter: str | None = Query(default=None, alias="status"),
    agent: AuthenticatedAgent = Depends(require_permission(Permission.TASK_READ_OWN)),
    session: Session = Depends(get_session),
) -> TaskListOut:
    """List the caller's own tasks (newest first), optionally by status."""
    tasks = service.tasks_for_agent(session, agent.id, status=status_filter)
    return TaskListOut(tasks=[_task_out(task) for task in tasks])


@router.post("/tasks/{task_id}/cancel", response_model=TaskOut)
async def cancel_task(
    task_id: str,
    agent: AuthenticatedAgent = Depends(require_authenticated_agent),
    session: Session = Depends(get_session),
) -> TaskOut:
    """Cancel a pending task (owner only; running/terminal → 409 ``INVALID_STATE``)."""
    task = service.get_task(session, task_id)
    if task is None:
        raise service.UnknownTaskError()
    if task.agent_id != agent.id:
        record_event(
            session, SecurityEventCategory.AUTHZ_DENIAL, agent.id, {"permission": "task:cancel"}
        )
        raise AuthorizationError(f"task:cancel:{task.id}")
    task = service.cancel_task(session, agent, task)
    return _task_out(task)

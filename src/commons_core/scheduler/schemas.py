"""Pydantic schemas for the durable scheduler domain (spec §16).

SECURITY/PRIVACY NOTES:
- ``payload`` is NON-SENSITIVE control data only, stored as plain JSON (it
  is deliberately NOT envelope-encrypted — it must never CONTAIN message,
  memory or governance content). The service validates: size ≤ 4096 JSON
  bytes (413 ``PAYLOAD_TOO_LARGE``) and no forbidden keys at ANY nesting
  depth (``content``, ``secret``, ``token``, ``credentials`` — 422
  ``INVALID_PAYLOAD``).
- ``kind`` is validated against the runtime handler REGISTRY in the service
  (not a static Literal — the registry is runtime state) → 422
  ``UNKNOWN_TASK_KIND``.
- ``TaskOut.payload`` is echoed ONLY to the task's OWNER agent (the only
  route that serializes it is the owner-scoped task list/create/cancel).
- ``max_attempts`` upper bound is the configuration value
  ``max_task_attempts`` (validated in the service → 422, keeping the limit
  out of the schema like the other byte/attempt limits).
- All models reject unknown fields (``extra="forbid"``).
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class TaskCreate(BaseModel):
    """Request body for ``POST /api/v1/tasks``."""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(min_length=1, max_length=64)
    payload: dict = Field(default_factory=dict)
    run_at: datetime | None = Field(
        default=None, description="Earliest execution eligibility (default: now)"
    )
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)
    max_attempts: int | None = Field(default=None, ge=1)


class TaskOut(BaseModel):
    """A scheduled task — ``payload`` is echoed ONLY to the owner agent."""

    id: str
    kind: str
    status: str
    payload: dict
    run_at: datetime
    attempts: int
    max_attempts: int
    last_error: str | None
    idempotency_key: str | None
    created_at: datetime
    finished_at: datetime | None


class TaskListOut(BaseModel):
    """The caller's own tasks (metadata + payload, owner-only)."""

    tasks: list[TaskOut]

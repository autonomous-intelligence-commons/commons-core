"""Pydantic schemas for the activities domain (spec §14).

SECURITY/PRIVACY NOTES:
- ``ActivityCreate.summary`` deliberately has NO ``max_length`` in the schema:
  the byte limit is the shared configuration value
  (``max_memory_entry_bytes``) enforced by the service so violations map to
  HTTP 413 ``PAYLOAD_TOO_LARGE``. The summary is sealed with
  AES-256-GCM (AAD ``activity:{id}:summary``) BEFORE storage.
- ``ActivityDetailOut.summary`` carries the decrypted value — ONLY produced
  for callers with an activity membership row (any state); non-members get
  404 ``UNKNOWN_ACTIVITY`` and never see metadata or content.
- All models reject unknown fields (``extra="forbid"``).
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ActivityCreate(BaseModel):
    """Request body for ``POST /api/v1/activities``."""

    model_config = ConfigDict(extra="forbid")

    handle: str = Field(min_length=1, max_length=64)
    summary: str | None = None
    initial_members: list[str] = Field(default_factory=list)


class ActivityOut(BaseModel):
    """Activity metadata — never contains the (encrypted) summary."""

    id: str
    handle: str
    status: str
    created_by: str
    created_at: datetime
    updated_at: datetime
    channel_id: str | None
    member_count: int


class ActivityListOut(BaseModel):
    """Activities the caller created or holds a membership row in."""

    activities: list[ActivityOut]


class ActivityMembershipOut(BaseModel):
    """The caller's own membership row in an activity."""

    state: str
    joined_at: datetime | None


class ActivityDetailOut(ActivityOut):
    """Activity metadata plus the caller's membership and decrypted summary.

    ``summary`` is decrypted server-side with the AAD check — this model is
    ONLY produced for callers with a membership row (any state) or the
    creator.
    """

    my_membership: ActivityMembershipOut | None
    summary: str | None


class InviteRequest(BaseModel):
    """Request body for ``POST /api/v1/activities/{id}/invite`` (creator)."""

    model_config = ConfigDict(extra="forbid")

    agent_handle: str = Field(min_length=1, max_length=64)


class JoinDecision(BaseModel):
    """Request body for ``POST /api/v1/activities/{id}/respond`` (invitee)."""

    model_config = ConfigDict(extra="forbid")

    state: Literal["joined", "declined"]


class ActivityStatusPatch(BaseModel):
    """Request body for ``PATCH /api/v1/activities/{id}`` (creator only)."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["active", "completed", "abandoned"]

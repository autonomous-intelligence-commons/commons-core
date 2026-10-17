"""Pydantic schemas for the communication domain (spec §9).

SECURITY/PRIVACY NOTES:
- ``MessageCreate.payload`` deliberately has NO ``max_length`` in the schema:
  the byte limit is a *configuration* value (``max_message_bytes``) enforced
  by the service so violations map to HTTP 413 ``PAYLOAD_TOO_LARGE`` instead
  of a generic 422 schema error.
- ``MessageOut`` carries the decrypted payload — it is ONLY ever produced for
  readers that passed channel-membership authorization.
- All models reject unknown fields (``extra="forbid"``) so clients cannot
  smuggle unexpected fields (e.g. anything resembling privileges).
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ContentTypeId = Literal["text/plain", "application/json"]


class ChannelCreate(BaseModel):
    """Request body for ``POST /api/v1/channels``."""

    model_config = ConfigDict(extra="forbid")

    handle: str = Field(min_length=1, max_length=64)
    kind: Literal["direct", "group"]
    description: str | None = None
    initial_members: list[str] = Field(default_factory=list)


class MemberOut(BaseModel):
    """A channel member as returned in channel metadata (handle + role only)."""

    agent_handle: str
    can_write: bool
    joined_at: datetime


class ChannelOut(BaseModel):
    """Channel metadata — never contains message content."""

    id: str
    handle: str
    kind: str
    description: str | None
    created_by_handle: str
    created_at: datetime
    members: list[MemberOut]


class ChannelListOut(BaseModel):
    """Channels the caller participates in (metadata only)."""

    channels: list[ChannelOut]


class MemberAdd(BaseModel):
    """Request body for ``POST /api/v1/channels/{channel_id}/members``."""

    model_config = ConfigDict(extra="forbid")

    agent_handle: str = Field(min_length=1, max_length=64)
    can_write: bool = True


class MessageCreate(BaseModel):
    """Request body for ``POST /api/v1/channels/{channel_id}/messages``."""

    model_config = ConfigDict(extra="forbid")

    content_type: ContentTypeId = "text/plain"
    payload: str = Field(min_length=1)
    correlation_id: str | None = Field(default=None, max_length=128)


class MessageOut(BaseModel):
    """A decrypted message — only for authorized channel members.

    ``sender`` is the sender's handle. ``payload`` is plaintext, decrypted
    server-side for the authorized reader.
    """

    id: str
    channel_id: str
    sender: str
    content_type: str
    payload: str
    correlation_id: str | None
    sequence: int
    created_at: datetime
    size_bytes: int


class MessageListOut(BaseModel):
    """Cursor page of messages (ordered by ``sequence`` ascending)."""

    messages: list[MessageOut]
    has_more: bool


class AckOut(BaseModel):
    """Acknowledgment result (idempotent)."""

    message_id: str
    agent_id: str
    acked_at: datetime
    already_acked: bool

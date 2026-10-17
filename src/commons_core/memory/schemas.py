"""Pydantic schemas for the shared-memory domain (spec §11, §12).

SECURITY/PRIVACY NOTES:
- ``EntryWrite.payload`` has NO ``max_length`` in the schema: the byte limit
  is a configuration value (``max_memory_entry_bytes``) enforced by the
  service so violations map to HTTP 413 ``PAYLOAD_TOO_LARGE``.
- ``EntryOut.payload`` carries the decrypted value — ONLY produced for
  readers with ``memory:read`` authorization.
- ``key`` is validated with the strict ``KEY_PATTERN`` (dot/slash segments).
- All models reject unknown fields (``extra="forbid"``).
- Memory content is UNTRUSTED DATA (spec §12): it is never promoted into
  authority, and none of these schemas feed the authorization engine.
"""

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: Machine-readable entry key: 1–256 chars of [a-zA-Z0-9._/-].
KEY_PATTERN = re.compile(r"^[a-zA-Z0-9._/-]{1,256}$")

#: ROUTING RESERVATION: keys are addressed with the FastAPI ``{key:path}``
#: converter (documented decision — see routes). A key ending in the literal
#: ``/history`` would be shadowed by the history endpoint, so writes of such
#: keys are rejected up front with a validation error.
RESERVED_KEY_SUFFIX = "/history"

ContentTypeId = Literal["text/plain", "application/json"]


class NamespaceCreate(BaseModel):
    """Request body for ``POST /api/v1/memory/namespaces``."""

    model_config = ConfigDict(extra="forbid")

    handle: str = Field(min_length=1, max_length=64)
    description: str | None = None


class NamespacePermissionsOut(BaseModel):
    """The caller's effective permissions on a namespace."""

    is_owner: bool
    can_read: bool
    can_write: bool


class NamespaceOut(BaseModel):
    """Namespace metadata plus the caller's own permissions (no content).

    ``grants`` is populated only when the caller is the owner (permission
    management view); ``None`` otherwise.
    """

    id: str
    handle: str
    description: str | None
    created_by_handle: str
    created_at: datetime
    my_permissions: NamespacePermissionsOut
    grants: list["PermissionOut"] | None = None


class NamespaceListOut(BaseModel):
    """Namespaces the caller owns or holds permissions on."""

    namespaces: list[NamespaceOut]


class PermissionGrant(BaseModel):
    """Request body for ``PUT /api/v1/memory/namespaces/{ns}/permissions``."""

    model_config = ConfigDict(extra="forbid")

    agent_handle: str = Field(min_length=1, max_length=64)
    can_read: bool = False
    can_write: bool = False


class PermissionOut(BaseModel):
    """A granted permission (as seen by the namespace owner)."""

    agent_handle: str
    can_read: bool
    can_write: bool


class EntryWrite(BaseModel):
    """Request body for ``POST /api/v1/memory/namespaces/{ns}/entries``."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(pattern=KEY_PATTERN.pattern)
    content_type: ContentTypeId = "text/plain"
    payload: str = Field(min_length=1)

    @field_validator("key")
    @classmethod
    def _key_not_route_shadowed(cls, value: str) -> str:
        if value.endswith(RESERVED_KEY_SUFFIX):
            raise ValueError("keys ending in '/history' are reserved by the routing scheme")
        return value


class EntryOut(BaseModel):
    """A decrypted memory entry version — only for authorized readers."""

    key: str
    payload: str
    version: int
    author: str
    created_at: datetime
    superseded: bool
    size_bytes: int


class EntryHistoryOut(BaseModel):
    """Full version chain of one key (ascending), payloads decrypted."""

    key: str
    versions: list[EntryOut]


class KeySummaryOut(BaseModel):
    """Key listing entry: key + latest version number — never payloads."""

    key: str
    latest_version: int
    updated_at: datetime


class KeyListOut(BaseModel):
    """Keys present in a namespace (latest versions only, no content)."""

    keys: list[KeySummaryOut]

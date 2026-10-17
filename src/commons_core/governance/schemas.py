"""Pydantic schemas for the internal governance domain (spec §15).

SECURITY/PRIVACY NOTES:
- ``GovernanceCreate.content`` deliberately has NO ``max_length`` in the
  schema: the byte limit is the shared configuration value
  (``max_memory_entry_bytes``) enforced by the service so violations map to
  HTTP 413 ``PAYLOAD_TOO_LARGE``. Content is sealed (AES-256-GCM, AAD
  ``governance:{kind}:{handle}:{version}``) BEFORE storage.
- PRIVACY NOTE (documented, spec §15): governance records are internal-shared
  convention artifacts, NOT private messages. The server policy grants
  ``governance:read`` to every ACTIVE agent, so ``GovernanceDetailOut.content``
  (the decrypted value) is legitimately produced for any active agent.
- All models reject unknown fields (``extra="forbid"``).
- Governance records are UNTRUSTED DATA: they never feed the authorization
  engine (canary-tested) and none of these schemas influence policy.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

GovernanceKindId = Literal["proposal", "convention", "amendment"]


class GovernanceCreate(BaseModel):
    """Request body for ``POST /api/v1/governance/records``."""

    model_config = ConfigDict(extra="forbid")

    handle: str = Field(min_length=1, max_length=64)
    kind: GovernanceKindId
    content: str = Field(min_length=1)
    supersedes: str | None = Field(default=None, min_length=1, max_length=64)


class GovernanceOut(BaseModel):
    """Governance record metadata — never contains the (encrypted) content."""

    id: str
    handle: str
    kind: str
    version: int
    status: str
    author: str
    created_at: datetime


class GovernanceListOut(BaseModel):
    """Governance record metadata list (optionally kind-filtered)."""

    records: list[GovernanceOut]


class GovernanceDetailOut(GovernanceOut):
    """A governance record with decrypted content.

    ``content`` is decrypted server-side with the AAD check and is only
    produced for readers holding ``governance:read`` — granted to every
    active agent per the server policy (internal-shared convention
    artifacts, not private messages).
    """

    content: str


class GovernanceHistoryOut(BaseModel):
    """Full version chain of one handle (ascending), content decrypted."""

    handle: str
    records: list[GovernanceDetailOut]

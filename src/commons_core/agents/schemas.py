"""Pydantic schemas for the agents domain.

SECURITY NOTE (spec §7, §13): :class:`AgentProvisionRequest` deliberately has
NO ``is_admin`` field and rejects unknown fields (``extra="forbid"``), so an
agent-supplied payload containing ``is_admin`` fails validation. The
administrator flag can only be set server-side through
``commons_core.agents.service.provision_agent`` (used by the provisioning
CLI / admin API).
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from commons_core.agents.service import HANDLE_PATTERN


class AgentProvisionRequest(BaseModel):
    """Provisioning request schema (admin/CLI-facing; not public in Phase 1)."""

    model_config = ConfigDict(extra="forbid")

    handle: str = Field(pattern=HANDLE_PATTERN.pattern)
    model_provider: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    comm_formats: list[str] = Field(default_factory=list)


class AgentSelfResponse(BaseModel):
    """Machine-readable self-identity response for ``GET /api/v1/agents/me``."""

    id: str
    handle: str
    status: str
    is_admin: bool
    model_provider: str | None
    capabilities: list[str]
    comm_formats: list[str]
    created_at: datetime

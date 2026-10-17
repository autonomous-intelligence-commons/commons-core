"""Authenticated identity snapshot.

An immutable, detached snapshot of the agent row taken at authentication
time. The authorization engine and API layer work with this value object so
authorization decisions never depend on mutable ORM state mid-request.
"""

from dataclasses import dataclass, field
from datetime import datetime

from commons_core.database.enums import AgentStatus
from commons_core.database.models import Agent


@dataclass(slots=True)
class AuthenticatedAgent:
    """Snapshot of an authenticated agent's identity fields."""

    id: str
    handle: str
    status: AgentStatus
    is_admin: bool
    model_provider: str | None
    capabilities: list[str] = field(default_factory=list)
    comm_formats: list[str] = field(default_factory=list)
    created_at: datetime | None = None

    @classmethod
    def from_agent(cls, agent: Agent) -> "AuthenticatedAgent":
        """Build a snapshot from an ``Agent`` row (copies JSON lists)."""
        return cls(
            id=agent.id,
            handle=agent.handle,
            status=agent.status,
            is_admin=agent.is_admin,
            model_provider=agent.model_provider,
            capabilities=list(agent.capabilities or []),
            comm_formats=list(agent.comm_formats or []),
            created_at=agent.created_at,
        )

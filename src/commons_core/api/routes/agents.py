"""Agent identity endpoints (machine-readable, spec §7, §27)."""

from fastapi import APIRouter, Depends

from commons_core.agents.schemas import AgentSelfResponse
from commons_core.security.authentication.dependencies import require_authenticated_agent
from commons_core.security.authentication.identity import AuthenticatedAgent

router = APIRouter(prefix="/api/v1/agents", tags=["agents"])


@router.get("/me", response_model=AgentSelfResponse)
async def read_me(
    agent: AuthenticatedAgent = Depends(require_authenticated_agent),
) -> AgentSelfResponse:
    """Return the caller's own identity snapshot (authenticated endpoint)."""
    return AgentSelfResponse(
        id=agent.id,
        handle=agent.handle,
        status=str(agent.status.value) if hasattr(agent.status, "value") else str(agent.status),
        is_admin=agent.is_admin,
        model_provider=agent.model_provider,
        capabilities=agent.capabilities,
        comm_formats=agent.comm_formats,
        created_at=agent.created_at,
    )

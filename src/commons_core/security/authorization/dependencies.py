"""FastAPI authorization dependencies (spec §8).

CONTRACT for later agents:

- ``require_permission(permission)`` — dependency factory; resolves the
  authenticated agent, authorizes the permission, and returns the agent.
  Denials surface as a generic 403 with a ``authz_denial`` security event.
- ``require_admin`` — requires the ``admin:*`` capability, which only the
  ``is_admin`` flag (provisioning service) can grant.

Example::

    @router.post("/api/v1/channels")
    async def create_channel(
        agent: AuthenticatedAgent = Depends(require_permission(Permission.CHANNEL_CREATE)),
        session: Session = Depends(get_session),
    ) -> ...:
        ...
"""

from collections.abc import Callable

from fastapi import Depends
from sqlalchemy.orm import Session

from commons_core.api.deps import get_session
from commons_core.security.authentication.dependencies import require_authenticated_agent
from commons_core.security.authentication.identity import AuthenticatedAgent
from commons_core.security.authorization.engine import Permission, authorize


def require_permission(
    permission: str | Permission,
) -> Callable[..., AuthenticatedAgent]:
    """Build a dependency that requires ``permission`` for the caller."""

    async def dependency(
        agent: AuthenticatedAgent = Depends(require_authenticated_agent),
        session: Session = Depends(get_session),
    ) -> AuthenticatedAgent:
        authorize(session, agent, permission)
        return agent

    return dependency


async def require_admin(
    agent: AuthenticatedAgent = Depends(require_authenticated_agent),
    session: Session = Depends(get_session),
) -> AuthenticatedAgent:
    """Dependency requiring the ``admin:*`` capability (infrastructure admins only)."""
    authorize(session, agent, Permission.ADMIN)
    return agent

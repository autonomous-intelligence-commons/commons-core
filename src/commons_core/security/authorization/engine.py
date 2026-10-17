"""Authorization engine: deny-by-default capability model (spec §8).

``authorize(session, agent, permission)`` is THE entry point for every
authorization decision. It evaluates the permission through the server
policy (DB-state only) and, on denial, records a ``authz_denial`` security
event and raises :class:`AuthorizationError` (mapped to a generic 403).
There is NO fallback allow.

Permission model: colon-separated capability strings. Dynamic scopes are
formatted helpers over the template enum members, e.g.
``channel_read(channel_id)``. ``admin:*`` is granted exclusively by the
``is_admin`` flag (never from agent-supplied metadata).
"""

import enum

from sqlalchemy.orm import Session

from commons_core.database.enums import SecurityEventCategory
from commons_core.security.authorization.errors import AuthorizationError
from commons_core.security.authorization.policies.server_policy import evaluate_permission
from commons_core.security.events import record_event


class Permission(enum.StrEnum):
    """Capability identifiers (templates — use the ``*_of`` helpers for scopes)."""

    AGENT_SELF_READ = "agent:self:read"
    CHANNEL_CREATE = "channel:create"
    CHANNEL_READ = "channel:read:{channel_id}"
    CHANNEL_WRITE = "channel:write:{channel_id}"
    MEMORY_NAMESPACE_MANAGE = "memory:namespace:manage:{ns}"
    MEMORY_READ = "memory:read:{ns}"
    MEMORY_WRITE = "memory:write:{ns}"
    ACTIVITY_MANAGE = "activity:manage:{id}"
    ACTIVITY_PARTICIPATE = "activity:participate"
    GOVERNANCE_CREATE = "governance:create"
    GOVERNANCE_READ = "governance:read"
    TASK_CREATE = "task:create"
    TASK_READ_OWN = "task:read:own"
    ADMIN = "admin:*"


def channel_read(channel_id: str) -> str:
    """Format ``channel:read:{channel_id}``."""
    return f"channel:read:{channel_id}"


def channel_write(channel_id: str) -> str:
    """Format ``channel:write:{channel_id}``."""
    return f"channel:write:{channel_id}"


def memory_namespace_manage(namespace_ref: str) -> str:
    """Format ``memory:namespace:manage:{ns}`` (handle or id)."""
    return f"memory:namespace:manage:{namespace_ref}"


def memory_read(namespace_ref: str) -> str:
    """Format ``memory:read:{ns}`` (handle or id)."""
    return f"memory:read:{namespace_ref}"


def memory_write(namespace_ref: str) -> str:
    """Format ``memory:write:{ns}`` (handle or id)."""
    return f"memory:write:{namespace_ref}"


def activity_manage(activity_ref: str) -> str:
    """Format ``activity:manage:{id}`` (handle or id)."""
    return f"activity:manage:{activity_ref}"


def authorize(session: Session, agent, permission: str | Permission) -> bool:
    """Authorize ``agent`` for ``permission``; raise on denial.

    ``agent`` is an ``Agent`` row or ``AuthenticatedAgent`` snapshot. On
    denial a ``authz_denial`` security event (principal = agent id, details =
    requested permission only) is recorded and :class:`AuthorizationError`
    is raised. Returns ``True`` when allowed.
    """
    permission_str = str(permission)
    if evaluate_permission(session, agent, permission_str):
        return True
    principal = getattr(agent, "id", "unknown")
    record_event(
        session, SecurityEventCategory.AUTHZ_DENIAL, principal, {"permission": permission_str}
    )
    raise AuthorizationError(permission_str)

"""THE STRUCTURAL RULE — server-side security policy (spec §8, §12, §13, §15).

Authorization policy is derived ONLY from database state that the
infrastructure owns:

- agent ``status`` and the ``is_admin`` flag (``agents`` table),
- channel membership (``channel_members``),
- memory permissions and namespace ownership (``memory_permissions``,
  ``memory_namespaces``),
- activity ownership (``activities``).

It is STRUCTURALLY IMPOSSIBLE for governance records, communication
payloads, shared-memory content, or any other agent-generated content to
influence authorization: this module never imports or queries those tables.
A canary unit test asserts this property on the module source and its
imports.

Administrators: ``is_admin`` grants ONLY the ``admin:*`` capability family.
It deliberately does NOT grant channel/memory access — infrastructure
administrators maintain the platform and must not casually read private
collaboration content (spec §3.C, §20). ``is_admin`` is set exclusively by
the provisioning service, never by agent-supplied metadata.

Deny-by-default: unknown permissions, inactive agents, and missing state
are denied. There is no fallback allow.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from commons_core.database.enums import AgentStatus
from commons_core.database.models import (
    Activity,
    Channel,
    ChannelMember,
    MemoryNamespace,
    MemoryPermission,
)

_ADMIN_WILDCARD = "admin:*"


def _agent_status(agent) -> AgentStatus:
    """Normalize an agent-like object's status to ``AgentStatus``."""
    status = getattr(agent, "status", None)
    if isinstance(status, AgentStatus):
        return status
    return AgentStatus(status)


def _is_admin(agent) -> bool:
    return bool(getattr(agent, "is_admin", False))


def _resolve_id(ref: str) -> str | None:
    """Return ``ref`` if it looks like a UUID, else ``None``."""
    try:
        return str(uuid.UUID(ref))
    except (ValueError, AttributeError, TypeError):
        return None


def _find_channel(session: Session, ref: str) -> Channel | None:
    """Look up a channel by id (UUID) or by handle."""
    channel_id = _resolve_id(ref)
    if channel_id is not None:
        channel = session.get(Channel, channel_id)
        if channel is not None:
            return channel
    return session.scalar(select(Channel).where(Channel.handle == ref))


def _find_namespace(session: Session, ref: str) -> MemoryNamespace | None:
    """Look up a memory namespace by id (UUID) or by handle."""
    namespace_id = _resolve_id(ref)
    if namespace_id is not None:
        namespace = session.get(MemoryNamespace, namespace_id)
        if namespace is not None:
            return namespace
    return session.scalar(select(MemoryNamespace).where(MemoryNamespace.handle == ref))


def _find_activity(session: Session, ref: str) -> Activity | None:
    """Look up an activity by id (UUID) or by handle."""
    activity_id = _resolve_id(ref)
    if activity_id is not None:
        activity = session.get(Activity, activity_id)
        if activity is not None:
            return activity
    return session.scalar(select(Activity).where(Activity.handle == ref))


def _is_channel_member(session: Session, agent_id: str, channel_id: str, *, write: bool) -> bool:
    """Membership check on ``channel_members`` only."""
    stmt = select(ChannelMember).where(
        ChannelMember.channel_id == channel_id, ChannelMember.agent_id == agent_id
    )
    if write:
        stmt = stmt.where(ChannelMember.can_write.is_(True))
    return session.scalar(stmt.limit(1)) is not None


def _has_memory_permission(
    session: Session, agent_id: str, namespace_id: str, *, write: bool
) -> bool:
    """Permission check on ``memory_permissions`` only."""
    stmt = select(MemoryPermission).where(
        MemoryPermission.namespace_id == namespace_id, MemoryPermission.agent_id == agent_id
    )
    if write:
        stmt = stmt.where(MemoryPermission.can_write.is_(True))
    else:
        stmt = stmt.where(MemoryPermission.can_read.is_(True))
    return session.scalar(stmt.limit(1)) is not None


def _memory_ref(permission: str) -> tuple[str, str] | None:
    """Return ``(action, ref)`` for a memory permission, or ``None``."""
    parts = permission.split(":")
    if len(parts) == 3 and parts[1] in ("read", "write") and parts[2]:
        return parts[1], parts[2]
    if len(parts) == 4 and parts[1] == "namespace" and parts[2] == "manage" and parts[3]:
        return "namespace:manage", parts[3]
    return None


def evaluate_permission(session: Session, agent, permission: str) -> bool:
    """Evaluate one permission for an agent-like object; pure DB-state policy.

    Accepts either an ``Agent`` ORM row or an ``AuthenticatedAgent`` snapshot.
    Returns ``False`` (deny) for anything not explicitly allowed.
    """
    if permission == _ADMIN_WILDCARD or permission.startswith("admin:"):
        # Administrator capabilities come ONLY from the is_admin flag.
        return _is_admin(agent)

    if _agent_status(agent) != AgentStatus.ACTIVE:
        return False

    parts = permission.split(":")
    domain = parts[0]

    if domain == "agent":
        return permission == "agent:self:read"

    if domain == "channel":
        if permission == "channel:create":
            return True
        if len(parts) == 3 and parts[1] in ("read", "write") and parts[2]:
            channel = _find_channel(session, parts[2])
            if channel is None:
                return False
            return _is_channel_member(session, agent.id, channel.id, write=parts[1] == "write")
        return False

    if domain == "memory":
        parsed = _memory_ref(permission)
        if parsed is None:
            return False
        action, ref = parsed
        namespace = _find_namespace(session, ref)
        if namespace is None:
            return False
        if namespace.created_by == agent.id:
            return True  # namespace owner implicitly manages/reads/writes it
        if action == "namespace:manage":
            return False  # management is owner-only
        return _has_memory_permission(session, agent.id, namespace.id, write=action == "write")

    if domain == "activity":
        if permission == "activity:participate":
            return True
        if len(parts) == 3 and parts[1] == "manage" and parts[2]:
            activity = _find_activity(session, parts[2])
            if activity is None:
                return False
            return activity.created_by == agent.id
        return False

    if domain == "governance" and permission in ("governance:create", "governance:read"):
        return True

    if domain == "task" and permission in ("task:create", "task:read:own"):
        return True

    # Deny-by-default: unknown permission.
    return False


#: Allowed model references for the canary test: the ONLY tables this module
#: may consult (infrastructure-owned state, never agent-generated content).
PERMITTED_MODEL_REFERENCES = frozenset(
    {"Agent", "Channel", "ChannelMember", "MemoryNamespace", "MemoryPermission", "Activity"}
)

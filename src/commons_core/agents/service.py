"""Agent identity management (spec §7).

Provider-independent identities with machine-readable handles, declared
capabilities and communication formats. Agents are provisioned by
infrastructure administrators (CLI / admin API) — public self-registration is
excluded from Phase 1.

SECURITY: ``is_admin`` (infrastructure administrator identity, separate from
agents) is set ONLY through :func:`provision_agent` — never from
agent-supplied metadata (spec §7 "Do not allow an agent to declare itself an
administrator").

Contract for later agents: all agent lifecycle changes go through this
service so status transitions stay consistent.
"""

import re

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from commons_core.database.enums import AgentStatus
from commons_core.database.models import Agent
from commons_core.infrastructure.time import utc_now

#: Machine-readable agent handle: lowercase letter first, then 3–63 more
#: characters from [a-z0-9-] (total length 4–64).
HANDLE_PATTERN = re.compile(r"^[a-z][a-z0-9-]{3,63}$")


class AgentError(ValueError):
    """Base class for agent identity errors."""


class InvalidAgentHandleError(AgentError):
    """The supplied handle does not match the handle pattern."""


class AgentLifecycleError(AgentError):
    """An invalid lifecycle transition was requested (e.g. reactivating revoked)."""


def validate_handle(handle: str) -> str:
    """Validate an agent handle, raising :class:`InvalidAgentHandleError`."""
    if not isinstance(handle, str) or not HANDLE_PATTERN.fullmatch(handle):
        raise InvalidAgentHandleError(
            f"handle {handle!r} is invalid: must match ^[a-z][a-z0-9-]{{3,63}}$ (length 4-64)"
        )
    return handle


def provision_agent(
    session: Session,
    handle: str,
    *,
    is_admin: bool = False,
    model_provider: str | None = None,
    capabilities: list[str] | None = None,
    comm_formats: list[str] | None = None,
) -> Agent:
    """Create a new agent identity.

    ``is_admin`` can only be set through this call path (provisioning CLI /
    admin API). ``capabilities``/``comm_formats`` are declared metadata with
    no permission effect (spec §7: self-reported capabilities never grant
    privileges). The caller owns the transaction (this function flushes).
    """
    validate_handle(handle)
    if get_agent_by_handle(session, handle) is not None:
        raise AgentError(f"handle {handle!r} is already in use")

    agent = Agent(
        handle=handle,
        status=AgentStatus.ACTIVE,
        is_admin=is_admin,
        model_provider=model_provider,
        capabilities=list(capabilities or []),
        comm_formats=list(comm_formats or []),
    )
    session.add(agent)
    try:
        session.flush()
    except IntegrityError as exc:  # race on the unique handle index
        session.rollback()
        raise AgentError(f"handle {handle!r} is already in use") from exc
    return agent


def get_agent(session: Session, agent_id: str) -> Agent | None:
    """Return the agent with ``agent_id`` or ``None``."""
    return session.get(Agent, agent_id)


def get_agent_by_handle(session: Session, handle: str) -> Agent | None:
    """Return the agent with ``handle`` or ``None``."""
    return session.scalar(select(Agent).where(Agent.handle == handle))


def list_agents(session: Session) -> list[Agent]:
    """Return all agents ordered by handle."""
    return list(session.scalars(select(Agent).order_by(Agent.handle)))


def suspend_agent(session: Session, agent: Agent) -> Agent:
    """Suspend an agent (authentication will fail while suspended)."""
    if agent.status == AgentStatus.REVOKED:
        raise AgentLifecycleError(f"agent {agent.handle!r} is revoked and cannot be suspended")
    agent.status = AgentStatus.SUSPENDED
    agent.updated_at = utc_now()
    session.flush()
    return agent


def reactivate_agent(session: Session, agent: Agent) -> Agent:
    """Reactivate a suspended agent. Revocation is terminal."""
    if agent.status == AgentStatus.REVOKED:
        raise AgentLifecycleError(f"agent {agent.handle!r} is revoked and cannot be reactivated")
    agent.status = AgentStatus.ACTIVE
    agent.updated_at = utc_now()
    session.flush()
    return agent


def revoke_agent(session: Session, agent: Agent) -> Agent:
    """Revoke an agent permanently (terminal state, spec §7)."""
    if agent.status == AgentStatus.REVOKED:
        return agent
    agent.status = AgentStatus.REVOKED
    agent.updated_at = utc_now()
    session.flush()
    return agent

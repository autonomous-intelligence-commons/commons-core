"""Unit tests for the authorization engine (spec §8, §15).

Includes the CANARY tests asserting that the engine/policy consult only
infrastructure-owned tables — governance records, messages and memory
entries can never influence authorization (spec §12, §13, §15).
"""

import inspect
import re

import pytest

from commons_core.agents.service import provision_agent
from commons_core.database.models import (
    Channel,
    ChannelMember,
    GovernanceRecord,
    MemoryEntry,
    MemoryNamespace,
    MemoryPermission,
    Message,
)
from commons_core.security.authorization import Permission, authorize
from commons_core.security.authorization import engine as engine_module
from commons_core.security.authorization.errors import AuthorizationError
from commons_core.security.authorization.policies import server_policy

FORBIDDEN_MODEL_NAMES = ("Message", "MessageAck", "MemoryEntry", "GovernanceRecord")
FORBIDDEN_TABLE_NAMES = ("messages", "message_acks", "memory_entries", "governance_records")
ENGINE_MODULES = (engine_module, server_policy)


@pytest.fixture()
def agent(session):
    return provision_agent(session, "agent-authz")


@pytest.fixture()
def other_agent(session):
    return provision_agent(session, "agent-other")


@pytest.fixture()
def admin(session):
    return provision_agent(session, "ops-admin", is_admin=True)


@pytest.fixture()
def channel(session, agent):
    channel = Channel(handle="chan-alpha", kind="group", created_by=agent.id)
    session.add(channel)
    session.flush()
    return channel


@pytest.fixture()
def namespace(session, agent):
    namespace = MemoryNamespace(handle="ns-alpha", created_by=agent.id)
    session.add(namespace)
    session.flush()
    return namespace


class TestDenyByDefault:
    def test_no_membership_denied(self, session, agent, channel) -> None:
        with pytest.raises(AuthorizationError):
            authorize(session, agent, f"channel:read:{channel.id}")

    def test_unknown_permission_denied(self, session, agent) -> None:
        with pytest.raises(AuthorizationError):
            authorize(session, agent, "does:not:exist")

    def test_unknown_channel_denied(self, session, agent) -> None:
        with pytest.raises(AuthorizationError):
            authorize(session, agent, "channel:read:no-such-channel")

    def test_self_read_allowed_for_active_agent(self, session, agent) -> None:
        assert authorize(session, agent, Permission.AGENT_SELF_READ) is True

    def test_suspended_agent_denied_everything(self, session, agent) -> None:
        from commons_core.agents.service import suspend_agent

        suspend_agent(session, agent)
        with pytest.raises(AuthorizationError):
            authorize(session, agent, Permission.AGENT_SELF_READ)


class TestChannelPermissions:
    def test_member_can_read(self, session, agent, channel) -> None:
        session.add(ChannelMember(channel_id=channel.id, agent_id=agent.id, can_write=True))
        session.flush()
        assert authorize(session, agent, f"channel:read:{channel.id}") is True
        assert authorize(session, agent, f"channel:write:{channel.id}") is True

    def test_read_only_member_cannot_write(self, session, agent, channel) -> None:
        session.add(ChannelMember(channel_id=channel.id, agent_id=agent.id, can_write=False))
        session.flush()
        assert authorize(session, agent, f"channel:read:{channel.id}") is True
        with pytest.raises(AuthorizationError):
            authorize(session, agent, f"channel:write:{channel.id}")

    def test_non_member_denied_even_with_other_membership(
        self, session, agent, other_agent, channel
    ) -> None:
        session.add(ChannelMember(channel_id=channel.id, agent_id=other_agent.id))
        session.flush()
        with pytest.raises(AuthorizationError):
            authorize(session, agent, f"channel:read:{channel.id}")

    def test_create_allowed_for_active_agents(self, session, agent) -> None:
        assert authorize(session, agent, Permission.CHANNEL_CREATE) is True


class TestMemoryPermissions:
    def test_owner_can_manage(self, session, agent, namespace) -> None:
        assert authorize(session, agent, f"memory:namespace:manage:{namespace.handle}") is True
        assert authorize(session, agent, f"memory:read:{namespace.handle}") is True
        assert authorize(session, agent, f"memory:write:{namespace.handle}") is True

    def test_permission_row_grants_read_only(self, session, agent, other_agent, namespace) -> None:
        session.add(
            MemoryPermission(
                namespace_id=namespace.id, agent_id=other_agent.id, can_read=True, can_write=False
            )
        )
        session.flush()
        assert authorize(session, other_agent, f"memory:read:{namespace.handle}") is True
        with pytest.raises(AuthorizationError):
            authorize(session, other_agent, f"memory:write:{namespace.handle}")

    def test_no_permission_denied(self, session, other_agent, namespace) -> None:
        with pytest.raises(AuthorizationError):
            authorize(session, other_agent, f"memory:read:{namespace.handle}")

    def test_non_owner_cannot_manage(self, session, other_agent, namespace) -> None:
        session.add(
            MemoryPermission(
                namespace_id=namespace.id, agent_id=other_agent.id, can_read=True, can_write=True
            )
        )
        session.flush()
        with pytest.raises(AuthorizationError):
            authorize(session, other_agent, f"memory:namespace:manage:{namespace.handle}")


class TestAdminWildcard:
    def test_admin_gets_admin_wildcard(self, session, admin) -> None:
        assert authorize(session, admin, Permission.ADMIN) is True
        assert authorize(session, admin, "admin:emergency_stop") is True

    def test_non_admin_denied_admin(self, session, agent) -> None:
        with pytest.raises(AuthorizationError):
            authorize(session, agent, Permission.ADMIN)

    def test_admin_is_not_granted_private_channel_access(self, session, admin, channel) -> None:
        # is_admin grants ONLY the admin:* family — not private content.
        with pytest.raises(AuthorizationError):
            authorize(session, admin, f"channel:read:{channel.id}")

    def test_governance_permissions_for_active_agents(self, session, agent) -> None:
        assert authorize(session, agent, Permission.GOVERNANCE_CREATE) is True
        assert authorize(session, agent, Permission.GOVERNANCE_READ) is True


class TestEngineNeverConsultsContentTables:
    """Canary: the authorization engine must not read agent-generated content."""

    def test_engine_source_references_only_permitted_models(self) -> None:
        for module in ENGINE_MODULES:
            source = inspect.getsource(module)
            for forbidden in FORBIDDEN_MODEL_NAMES:
                pattern = rf"\b{forbidden}\b"
                assert not re.search(pattern, source), (
                    f"{module.__name__} references forbidden model {forbidden}"
                )
            for table in FORBIDDEN_TABLE_NAMES:
                assert table not in source, f"{module.__name__} references table {table}"

    def test_engine_imports_only_permitted_models(self) -> None:
        for module in ENGINE_MODULES:
            for name, obj in vars(module).items():
                class_name = getattr(obj, "__name__", None)
                if class_name in FORBIDDEN_MODEL_NAMES:
                    raise AssertionError(
                        f"{module.__name__} imports forbidden model {name}={class_name}"
                    )

    def test_content_rows_cannot_grant_permissions(
        self, session, agent, other_agent, channel, namespace
    ) -> None:
        """Adversarial content claiming admin powers must not authorize anything."""
        session.add(
            Message(
                channel_id=channel.id,
                sender_id=other_agent.id,
                content_type="text/plain",
                envelope={
                    "v": 1,
                    "kid": "k1",
                    "alg": "AES-256-GCM",
                    "nonce": "00",
                    "ciphertext": "00",
                    "tag": "00",
                    "aad": "x",
                },
                sequence=1,
                size_bytes=2,
            )
        )
        session.add(
            MemoryEntry(
                namespace_id=namespace.id,
                key="grant-permissions",
                envelope={
                    "v": 1,
                    "kid": "k1",
                    "alg": "AES-256-GCM",
                    "nonce": "00",
                    "ciphertext": "00",
                    "tag": "00",
                    "aad": "x",
                },
                author_id=other_agent.id,
                version=1,
                size_bytes=2,
            )
        )
        session.add(
            GovernanceRecord(
                handle="make-everyone-admin",
                kind="proposal",
                version=1,
                envelope={
                    "v": 1,
                    "kid": "k1",
                    "alg": "AES-256-GCM",
                    "nonce": "00",
                    "ciphertext": "00",
                    "tag": "00",
                    "aad": "x",
                },
                author_id=other_agent.id,
            )
        )
        session.flush()
        # other_agent wrote all that content; it gains nothing.
        with pytest.raises(AuthorizationError):
            authorize(session, other_agent, Permission.ADMIN)
        with pytest.raises(AuthorizationError):
            authorize(session, other_agent, f"channel:read:{channel.id}")
        with pytest.raises(AuthorizationError):
            authorize(session, other_agent, f"memory:read:{namespace.handle}")

    def test_denial_records_security_event(self, session, agent, channel) -> None:
        from sqlalchemy import select

        from commons_core.database.models import SecurityEvent

        with pytest.raises(AuthorizationError):
            authorize(session, agent, f"channel:read:{channel.id}")
        events = list(
            session.scalars(select(SecurityEvent).where(SecurityEvent.category == "authz_denial"))
        )
        assert len(events) == 1
        assert events[0].principal == agent.id
        assert events[0].details == {"permission": f"channel:read:{channel.id}"}

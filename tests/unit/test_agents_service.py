"""Unit tests for agent identity lifecycle (spec §7)."""

import pytest

from commons_core.agents.schemas import AgentProvisionRequest
from commons_core.agents.service import (
    AgentError,
    AgentLifecycleError,
    InvalidAgentHandleError,
    get_agent_by_handle,
    list_agents,
    provision_agent,
    reactivate_agent,
    revoke_agent,
    suspend_agent,
)
from commons_core.database.enums import AgentStatus


class TestProvisioning:
    def test_provision_defaults(self, session) -> None:
        agent = provision_agent(session, "agent-alpha")
        assert agent.id
        assert agent.handle == "agent-alpha"
        assert agent.status == AgentStatus.ACTIVE
        assert agent.is_admin is False
        assert agent.capabilities == []
        assert agent.comm_formats == []
        assert agent.created_at is not None

    def test_provision_with_metadata(self, session) -> None:
        agent = provision_agent(
            session,
            "agent-beta",
            is_admin=False,
            model_provider="example-provider",
            capabilities=["code-review"],
            comm_formats=["application/json"],
        )
        assert agent.model_provider == "example-provider"
        assert agent.capabilities == ["code-review"]
        assert agent.comm_formats == ["application/json"]

    @pytest.mark.parametrize(
        "handle",
        [
            "ab",  # too short (min 4)
            "abc",  # one char short of minimum
            "ABC",  # uppercase
            "1agent",  # leading digit
            "-agent",  # leading dash
            "agent_alpha",  # underscore not allowed
            "a" * 65,  # too long
            "",  # empty
        ],
    )
    def test_invalid_handles_rejected(self, session, handle: str) -> None:
        with pytest.raises(InvalidAgentHandleError):
            provision_agent(session, handle)

    @pytest.mark.parametrize("handle", ["abcd", "agent-alpha", "a123", "a" * 64])
    def test_valid_handles_accepted(self, session, handle: str) -> None:
        agent = provision_agent(session, handle)
        assert agent.handle == handle

    def test_duplicate_handle_rejected(self, session) -> None:
        provision_agent(session, "agent-dup")
        with pytest.raises(AgentError, match="already in use"):
            provision_agent(session, "agent-dup")

    def test_admin_flag_only_via_service(self, session) -> None:
        # is_admin is set ONLY through the service call (CLI/admin API path).
        admin = provision_agent(session, "ops-admin", is_admin=True)
        assert admin.is_admin is True


class TestLifecycle:
    def test_suspend_and_reactivate(self, session) -> None:
        agent = provision_agent(session, "agent-cycle")
        suspend_agent(session, agent)
        assert agent.status == AgentStatus.SUSPENDED
        reactivate_agent(session, agent)
        assert agent.status == AgentStatus.ACTIVE

    def test_revoke_is_terminal(self, session) -> None:
        agent = provision_agent(session, "agent-doomed")
        revoke_agent(session, agent)
        assert agent.status == AgentStatus.REVOKED
        with pytest.raises(AgentLifecycleError):
            reactivate_agent(session, agent)
        with pytest.raises(AgentLifecycleError):
            suspend_agent(session, agent)

    def test_revoke_idempotent(self, session) -> None:
        agent = provision_agent(session, "agent-once")
        revoke_agent(session, agent)
        revoke_agent(session, agent)
        assert agent.status == AgentStatus.REVOKED

    def test_lookup_and_listing(self, session) -> None:
        provision_agent(session, "agent-a")
        provision_agent(session, "agent-b")
        assert get_agent_by_handle(session, "agent-a") is not None
        assert get_agent_by_handle(session, "missing") is None
        assert [a.handle for a in list_agents(session)] == ["agent-a", "agent-b"]


class TestProvisionSchema:
    def test_schema_has_no_is_admin_field(self) -> None:
        assert "is_admin" not in AgentProvisionRequest.model_fields

    def test_schema_rejects_agent_supplied_is_admin(self) -> None:
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            AgentProvisionRequest.model_validate({"handle": "agent-evil", "is_admin": True})

    def test_schema_accepts_valid_payload(self) -> None:
        request = AgentProvisionRequest.model_validate(
            {"handle": "agent-ok", "capabilities": ["x"]}
        )
        assert request.handle == "agent-ok"

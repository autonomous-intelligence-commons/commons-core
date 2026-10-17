"""Unit tests for token issuance and verification (spec §7).

Covers: issue+verify, wrong tokens, revoked credentials, expiry, hash
format, and the privacy-critical property that RAW TOKENS ARE NEVER STORED.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from commons_core.agents.service import provision_agent, revoke_agent, suspend_agent
from commons_core.database.models import AgentCredential
from commons_core.security.authentication.errors import AuthenticationError
from commons_core.security.authentication.tokens import (
    PBKDF2_ITERATIONS,
    issue_token,
    revoke_credential,
    verify_token,
)


@pytest.fixture()
def agent(session):
    return provision_agent(session, "agent-token")


class TestIssueAndVerify:
    def test_issue_and_verify(self, session, agent) -> None:
        issued = issue_token(session, agent.id)
        assert verify_token(session, issued.raw_token) == agent.id

    def test_wrong_token_rejected(self, session, agent) -> None:
        issue_token(session, agent.id)
        with pytest.raises(AuthenticationError) as excinfo:
            verify_token(session, "totally-wrong-token-value")
        assert excinfo.value.reason == "invalid token"

    def test_truncated_token_rejected(self, session, agent) -> None:
        with pytest.raises(AuthenticationError) as excinfo:
            verify_token(session, "short")
        assert excinfo.value.reason == "malformed token"

    def test_hash_format(self, session, agent) -> None:
        issued = issue_token(session, agent.id)
        credential = session.get(AgentCredential, issued.credential_id)
        assert len(credential.token_hash) == 64  # sha256 hex
        int(credential.token_hash, 16)  # valid hex
        assert len(credential.salt) == 32  # 16 bytes hex
        int(credential.salt, 16)  # valid hex
        assert credential.token_prefix == issued.raw_token[:8]
        assert credential.status.value == "active"
        assert PBKDF2_ITERATIONS == 200_000

    def test_token_uniqueness(self, session, agent) -> None:
        first = issue_token(session, agent.id)
        second = issue_token(session, agent.id)
        assert first.raw_token != second.raw_token
        assert first.credential_id != second.credential_id


class TestCredentialStates:
    def test_revoked_credential_fails(self, session, agent) -> None:
        issued = issue_token(session, agent.id)
        revoke_credential(session, session.get(AgentCredential, issued.credential_id))
        with pytest.raises(AuthenticationError) as excinfo:
            verify_token(session, issued.raw_token)
        assert excinfo.value.reason == "credential revoked"

    def test_expired_credential_fails(self, session, agent) -> None:
        expires_at = datetime.now(UTC) - timedelta(minutes=1)
        issued = issue_token(session, agent.id, expires_at=expires_at)
        with pytest.raises(AuthenticationError) as excinfo:
            verify_token(session, issued.raw_token)
        assert excinfo.value.reason == "credential expired"

    def test_future_expiry_succeeds(self, session, agent) -> None:
        expires_at = datetime.now(UTC) + timedelta(days=1)
        issued = issue_token(session, agent.id, expires_at=expires_at)
        assert verify_token(session, issued.raw_token) == agent.id

    def test_rotation_links_credentials(self, session, agent) -> None:
        first = issue_token(session, agent.id)
        revoke_credential(session, session.get(AgentCredential, first.credential_id))
        second = issue_token(session, agent.id, rotated_from=first.credential_id)
        assert second.raw_token != first.raw_token
        # old token no longer authenticates, new one does
        with pytest.raises(AuthenticationError):
            verify_token(session, first.raw_token)
        assert verify_token(session, second.raw_token) == agent.id


class TestAgentStates:
    def test_suspended_agent_fails(self, session, agent) -> None:
        issued = issue_token(session, agent.id)
        suspend_agent(session, agent)
        with pytest.raises(AuthenticationError) as excinfo:
            verify_token(session, issued.raw_token)
        assert excinfo.value.reason == "agent suspended"

    def test_revoked_agent_fails(self, session, agent) -> None:
        issued = issue_token(session, agent.id)
        revoke_agent(session, agent)
        with pytest.raises(AuthenticationError) as excinfo:
            verify_token(session, issued.raw_token)
        assert excinfo.value.reason == "agent revoked"


class TestRawTokenNeverStored:
    def test_raw_token_absent_from_database(self, session, agent) -> None:
        issued = issue_token(session, agent.id)
        session.expire_all()  # force fresh load from the database
        credentials = list(session.scalars(select(AgentCredential)))
        assert len(credentials) == 1
        credential = credentials[0]
        for value in (credential.token_hash, credential.salt, credential.token_prefix):
            assert issued.raw_token not in value
            assert issued.raw_token[:20] not in value
        # The raw token itself is not any stored value.
        assert issued.raw_token != credential.token_hash
        assert issued.raw_token != credential.salt

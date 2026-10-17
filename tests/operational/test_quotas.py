"""Operational tests: per-agent message-send quotas (spec §19, §21)."""

from datetime import timedelta

import pytest
from conftest import auth_headers, provision_and_login
from fastapi.testclient import TestClient
from sqlalchemy import select

from commons_core.api.app import create_app
from commons_core.database.models import QuotaCounter, SecurityEvent
from commons_core.infrastructure.quotas import check_message_quota
from commons_core.infrastructure.time import utc_now

MESSAGE_LIMIT = 3


@pytest.fixture()
def quota_client(settings, database) -> TestClient:
    quota_settings = settings.model_copy(update={"max_messages_per_minute": MESSAGE_LIMIT})
    application = create_app(settings=quota_settings, database=database)
    with TestClient(application) as test_client:
        yield test_client


class TestMessageQuotaViaApi:
    def test_429_after_limit_with_event(self, quota_client: TestClient, database) -> None:
        token = provision_and_login(quota_client, "agent-chatty")
        channel = quota_client.post(
            "/api/v1/channels",
            headers=auth_headers(token),
            json={"handle": "chan-quota", "kind": "group"},
        ).json()

        for index in range(MESSAGE_LIMIT):
            response = quota_client.post(
                f"/api/v1/channels/{channel['id']}/messages",
                headers=auth_headers(token),
                json={"payload": f"message {index}"},
            )
            assert response.status_code == 201

        blocked = quota_client.post(
            f"/api/v1/channels/{channel['id']}/messages",
            headers=auth_headers(token),
            json={"payload": "one too many"},
        )
        assert blocked.status_code == 429
        error = blocked.json()["error"]
        assert error["code"] == "QUOTA_EXCEEDED"
        assert error["limit"] == MESSAGE_LIMIT

        with database.session() as session:
            events = list(
                session.scalars(
                    select(SecurityEvent).where(SecurityEvent.category == "quota_exceeded")
                )
            )
        assert len(events) == 1
        assert events[0].principal != "anonymous"
        assert events[0].details == {"limit": MESSAGE_LIMIT, "scope": "messages_per_minute"}
        assert "one too many" not in str(events[0].details)  # never content

    def test_quota_is_per_agent(self, quota_client: TestClient) -> None:
        chatty_token = provision_and_login(quota_client, "agent-chatty")
        quiet_token = provision_and_login(quota_client, "agent-quiet")
        channel = quota_client.post(
            "/api/v1/channels",
            headers=auth_headers(chatty_token),
            json={
                "handle": "chan-quota-shared",
                "kind": "group",
                "initial_members": ["agent-quiet"],
            },
        ).json()
        for index in range(MESSAGE_LIMIT):
            quota_client.post(
                f"/api/v1/channels/{channel['id']}/messages",
                headers=auth_headers(chatty_token),
                json={"payload": f"chat {index}"},
            )
        blocked = quota_client.post(
            f"/api/v1/channels/{channel['id']}/messages",
            headers=auth_headers(chatty_token),
            json={"payload": "blocked"},
        )
        assert blocked.status_code == 429
        # another agent is unaffected
        other = quota_client.post(
            f"/api/v1/channels/{channel['id']}/messages",
            headers=auth_headers(quiet_token),
            json={"payload": "quiet agent still fine"},
        )
        assert other.status_code == 201

    def test_counter_persisted_in_quota_table(self, quota_client: TestClient, database) -> None:
        token = provision_and_login(quota_client, "agent-counter")
        channel = quota_client.post(
            "/api/v1/channels",
            headers=auth_headers(token),
            json={"handle": "chan-quota-db", "kind": "group"},
        ).json()
        quota_client.post(
            f"/api/v1/channels/{channel['id']}/messages",
            headers=auth_headers(token),
            json={"payload": "counted"},
        )
        with database.session() as session:
            rows = {
                (row.agent_id, row.messages, row.requests): row
                for row in session.scalars(select(QuotaCounter))
            }
        assert len(rows) == 1
        (agent_id, messages, requests) = next(iter(rows))
        assert messages == 1
        assert requests >= 1  # request-rate mirror from the middleware

    def test_failed_validation_does_not_consume_quota(
        self, quota_client: TestClient, database
    ) -> None:
        token = provision_and_login(quota_client, "agent-lean")
        channel = quota_client.post(
            "/api/v1/channels",
            headers=auth_headers(token),
            json={"handle": "chan-quota-lean", "kind": "group"},
        ).json()
        # invalid JSON is rejected (422) and rolled back with its quota tick
        response = quota_client.post(
            f"/api/v1/channels/{channel['id']}/messages",
            headers=auth_headers(token),
            json={"content_type": "application/json", "payload": "{invalid"},
        )
        assert response.status_code == 422
        sent = quota_client.post(
            f"/api/v1/channels/{channel['id']}/messages",
            headers=auth_headers(token),
            json={"payload": "still first counted message"},
        )
        assert sent.status_code == 201
        with database.session() as session:
            from commons_core.agents.service import get_agent_by_handle

            agent = get_agent_by_handle(session, "agent-lean")
            rows = list(
                session.scalars(select(QuotaCounter).where(QuotaCounter.agent_id == agent.id))
            )
        # window-boundary safe: exactly one counted message across windows
        assert sum(row.messages for row in rows) == 1


class TestQuotaWindowServiceLevel:
    def test_window_resets_with_injected_now(self, session, session_agent) -> None:
        agent_id = session_agent.id
        start = utc_now().replace(second=30, microsecond=123456)

        counts = [
            check_message_quota(session, agent_id, limit=MESSAGE_LIMIT, now=start)
            for _ in range(MESSAGE_LIMIT)
        ]
        assert counts == [1, 2, 3]

        with pytest.raises(Exception) as excinfo:
            check_message_quota(session, agent_id, limit=MESSAGE_LIMIT, now=start)
        assert getattr(excinfo.value, "code", None) == "QUOTA_EXCEEDED"

        # same agent, NEXT window (61 seconds later) -> fresh counter
        later = start + timedelta(seconds=61)
        assert check_message_quota(session, agent_id, limit=MESSAGE_LIMIT, now=later) == 1

    def test_counters_are_per_window_rows(self, session, session_agent) -> None:
        agent_id = session_agent.id
        base = utc_now().replace(second=0, microsecond=0)
        check_message_quota(session, agent_id, limit=10, now=base)
        check_message_quota(session, agent_id, limit=10, now=base + timedelta(seconds=60))
        rows = list(session.scalars(select(QuotaCounter).where(QuotaCounter.agent_id == agent_id)))
        assert len(rows) == 2
        assert all(row.messages == 1 for row in rows)
        assert all(row.requests == 0 for row in rows)

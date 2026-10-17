"""Integration tests: private communication happy paths (spec §9)."""

import uuid

import pytest
from conftest import auth_headers, provision_and_login
from fastapi.testclient import TestClient


@pytest.fixture()
def alice_token(client: TestClient) -> str:
    return provision_and_login(client, "agent-alice")


@pytest.fixture()
def bob_token(client: TestClient) -> str:
    return provision_and_login(client, "agent-bob")


@pytest.fixture()
def direct_channel(client: TestClient, alice_token: str, bob_token: str) -> dict:
    response = client.post(
        "/api/v1/channels",
        headers=auth_headers(alice_token),
        json={
            "handle": "chan-direct-alpha",
            "kind": "direct",
            "description": "alice <-> bob",
            "initial_members": ["agent-bob"],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


class TestChannelLifecycle:
    def test_create_direct_channel_auto_members(
        self, client: TestClient, direct_channel: dict, alice_token: str
    ) -> None:
        body = direct_channel
        assert body["handle"] == "chan-direct-alpha"
        assert body["kind"] == "direct"
        assert body["created_by_handle"] == "agent-alice"
        members = {member["agent_handle"]: member["can_write"] for member in body["members"]}
        assert members == {"agent-alice": True, "agent-bob": True}

    def test_direct_channel_requires_exactly_two_members(
        self, client: TestClient, alice_token: str, bob_token: str
    ) -> None:
        # two other members -> 422
        response = client.post(
            "/api/v1/channels",
            headers=auth_headers(alice_token),
            json={"handle": "chan-direct-bad", "kind": "direct", "initial_members": []},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_MEMBERS"

    def test_unknown_initial_member_rejected(self, client: TestClient, alice_token: str) -> None:
        response = client.post(
            "/api/v1/channels",
            headers=auth_headers(alice_token),
            json={"handle": "chan-direct-x", "kind": "direct", "initial_members": ["agent-ghost"]},
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "UNKNOWN_AGENT"

    def test_duplicate_handle_conflict(
        self, client: TestClient, direct_channel: dict, alice_token: str
    ) -> None:
        response = client.post(
            "/api/v1/channels",
            headers=auth_headers(alice_token),
            json={"handle": "chan-direct-alpha", "kind": "group"},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "CHANNEL_EXISTS"

    def test_invalid_handle_rejected(self, client: TestClient, alice_token: str) -> None:
        response = client.post(
            "/api/v1/channels",
            headers=auth_headers(alice_token),
            json={"handle": "BAD HANDLE!", "kind": "group"},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_CHANNEL_HANDLE"

    def test_list_channels_shows_only_my_memberships(
        self, client: TestClient, direct_channel: dict, alice_token: str, bob_token: str
    ) -> None:
        client.post(
            "/api/v1/channels",
            headers=auth_headers(alice_token),
            json={"handle": "chan-group-beta", "kind": "group"},
        )
        carol_token = provision_and_login(client, "agent-carol")
        carol_channels = client.get("/api/v1/channels", headers=auth_headers(bob_token)).json()[
            "channels"
        ]
        # bob sees only the direct channel fixture; carol is in nothing.
        assert {channel["handle"] for channel in carol_channels} == {"chan-direct-alpha"}
        assert (
            client.get("/api/v1/channels", headers=auth_headers(carol_token)).json()["channels"]
            == []
        )

    def test_channel_metadata_hides_messages(
        self, client: TestClient, direct_channel: dict, alice_token: str
    ) -> None:
        client.post(
            f"/api/v1/channels/{direct_channel['id']}/messages",
            headers=auth_headers(alice_token),
            json={"payload": "secret-body"},
        )
        metadata = client.get(
            f"/api/v1/channels/{direct_channel['id']}", headers=auth_headers(alice_token)
        )
        assert metadata.status_code == 200
        assert "messages" not in metadata.json()
        assert "secret-body" not in metadata.text


class TestMessagingFlow:
    def test_send_retrieve_ack_and_cursor(
        self, client: TestClient, direct_channel: dict, alice_token: str, bob_token: str
    ) -> None:
        channel_id = direct_channel["id"]

        # send
        sent = client.post(
            f"/api/v1/channels/{channel_id}/messages",
            headers=auth_headers(alice_token),
            json={"payload": "hello bob", "correlation_id": "corr-42"},
        )
        assert sent.status_code == 201, sent.text
        first = sent.json()
        assert first["payload"] == "hello bob"
        assert first["sender"] == "agent-alice"
        assert first["sequence"] == 1
        assert first["correlation_id"] == "corr-42"
        assert first["content_type"] == "text/plain"
        assert first["size_bytes"] == len(b"hello bob")

        # retrieve as recipient (cursor from the beginning: after=0 default)
        retrieved = client.get(
            f"/api/v1/channels/{channel_id}/messages", headers=auth_headers(bob_token)
        )
        assert retrieved.status_code == 200
        page = retrieved.json()
        assert len(page["messages"]) == 1
        message = page["messages"][0]
        assert message["id"] == first["id"]
        assert message["payload"] == "hello bob"  # decrypted for the member
        assert message["correlation_id"] == "corr-42"

        # ack (idempotent)
        acked = client.post(f"/api/v1/messages/{first['id']}/ack", headers=auth_headers(bob_token))
        assert acked.status_code == 200
        assert acked.json()["already_acked"] is False
        acked_again = client.post(
            f"/api/v1/messages/{first['id']}/ack", headers=auth_headers(bob_token)
        )
        assert acked_again.status_code == 200
        assert acked_again.json()["already_acked"] is True
        # idempotent: the original timestamp is returned unchanged (normalize
        # the trailing timezone marker — SQLite round-trips naive datetimes).
        assert acked_again.json()["acked_at"].rstrip("Z") == acked.json()["acked_at"].rstrip("Z")

        # second message -> sequence monotonic across the channel
        second = client.post(
            f"/api/v1/channels/{channel_id}/messages",
            headers=auth_headers(alice_token),
            json={"payload": "second"},
        )
        assert second.json()["sequence"] == 2

        # cursor: after=1 returns only the new message
        page_after = client.get(
            f"/api/v1/channels/{channel_id}/messages",
            headers=auth_headers(bob_token),
            params={"after": 1},
        ).json()
        assert [message["sequence"] for message in page_after["messages"]] == [2]
        assert page_after["messages"][0]["payload"] == "second"

    def test_limit_and_has_more(self, client: TestClient, alice_token: str) -> None:
        client.post(
            "/api/v1/channels",
            headers=auth_headers(alice_token),
            json={"handle": "chan-group-page", "kind": "group"},
        )
        channels = client.get("/api/v1/channels", headers=auth_headers(alice_token)).json()[
            "channels"
        ]
        channel_id = channels[0]["id"]
        for index in range(3):
            response = client.post(
                f"/api/v1/channels/{channel_id}/messages",
                headers=auth_headers(alice_token),
                json={"payload": f"m-{index}"},
            )
            assert response.status_code == 201
        page = client.get(
            f"/api/v1/channels/{channel_id}/messages",
            headers=auth_headers(alice_token),
            params={"limit": 2},
        ).json()
        assert [message["sequence"] for message in page["messages"]] == [1, 2]
        assert page["has_more"] is True
        tail = client.get(
            f"/api/v1/channels/{channel_id}/messages",
            headers=auth_headers(alice_token),
            params={"limit": 2, "after": 2},
        ).json()
        assert [message["sequence"] for message in tail["messages"]] == [3]
        assert tail["has_more"] is False

    def test_limit_upper_bound_enforced(self, client: TestClient, alice_token: str) -> None:
        response = client.get(
            "/api/v1/channels/chan-group-page/messages",
            headers=auth_headers(alice_token),
            params={"limit": 101},
        )
        assert response.status_code == 422

    def test_valid_json_content_type_accepted(
        self, client: TestClient, direct_channel: dict, alice_token: str, bob_token: str
    ) -> None:
        payload = '{"task": "review", "priority": 1}'
        sent = client.post(
            f"/api/v1/channels/{direct_channel['id']}/messages",
            headers=auth_headers(alice_token),
            json={"content_type": "application/json", "payload": payload},
        )
        assert sent.status_code == 201, sent.text
        assert sent.json()["content_type"] == "application/json"
        page = client.get(
            f"/api/v1/channels/{direct_channel['id']}/messages",
            headers=auth_headers(bob_token),
        ).json()
        assert page["messages"][0]["payload"] == payload

    def test_invalid_json_rejected_422(
        self, client: TestClient, direct_channel: dict, alice_token: str
    ) -> None:
        response = client.post(
            f"/api/v1/channels/{direct_channel['id']}/messages",
            headers=auth_headers(alice_token),
            json={"content_type": "application/json", "payload": "{not json"},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_PAYLOAD"

    def test_oversized_payload_rejected_413(
        self, client: TestClient, direct_channel: dict, alice_token: str
    ) -> None:
        max_bytes = client.app.state.settings.max_message_bytes
        response = client.post(
            f"/api/v1/channels/{direct_channel['id']}/messages",
            headers=auth_headers(alice_token),
            json={"payload": "x" * (max_bytes + 1)},
        )
        assert response.status_code == 413
        error = response.json()["error"]
        assert error["code"] == "PAYLOAD_TOO_LARGE"
        assert error["limit_bytes"] == max_bytes

    def test_empty_payload_rejected(
        self, client: TestClient, direct_channel: dict, alice_token: str
    ) -> None:
        response = client.post(
            f"/api/v1/channels/{direct_channel['id']}/messages",
            headers=auth_headers(alice_token),
            json={"payload": ""},
        )
        assert response.status_code == 422

    def test_unknown_channel_404(self, client: TestClient, alice_token: str) -> None:
        response = client.post(
            f"/api/v1/channels/{uuid.uuid4()}/messages",
            headers=auth_headers(alice_token),
            json={"payload": "hi"},
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "UNKNOWN_CHANNEL"

    def test_unknown_message_ack_404(self, client: TestClient, alice_token: str) -> None:
        response = client.post(
            f"/api/v1/messages/{uuid.uuid4()}/ack", headers=auth_headers(alice_token)
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "UNKNOWN_MESSAGE"


class TestGroupChannelMembership:
    def test_creator_adds_member_who_can_then_participate(
        self, client: TestClient, alice_token: str
    ) -> None:
        carol_token = provision_and_login(client, "agent-carol")
        dave_token = provision_and_login(client, "agent-dave")
        created = client.post(
            "/api/v1/channels",
            headers=auth_headers(alice_token),
            json={"handle": "chan-group-gamma", "kind": "group"},
        )
        channel_id = created.json()["id"]

        added = client.post(
            f"/api/v1/channels/{channel_id}/members",
            headers=auth_headers(alice_token),
            json={"agent_handle": "agent-carol", "can_write": True},
        )
        assert added.status_code == 201
        members = {m["agent_handle"]: m["can_write"] for m in added.json()["members"]}
        assert members == {"agent-alice": True, "agent-carol": True}

        # carol (can_write) can send; dave (not yet a member) gets 404/403 —
        # here: dave is not a member of an EXISTING channel -> 403 (see
        # isolation tests); carol sends fine.
        sent = client.post(
            f"/api/v1/channels/{channel_id}/messages",
            headers=auth_headers(carol_token),
            json={"payload": "carol was added"},
        )
        assert sent.status_code == 201

        # read-only member: can read, cannot send
        client.post(
            f"/api/v1/channels/{channel_id}/members",
            headers=auth_headers(alice_token),
            json={"agent_handle": "agent-dave", "can_write": False},
        )
        read = client.get(
            f"/api/v1/channels/{channel_id}/messages", headers=auth_headers(dave_token)
        )
        assert read.status_code == 200
        assert read.json()["messages"][0]["payload"] == "carol was added"
        write = client.post(
            f"/api/v1/channels/{channel_id}/messages",
            headers=auth_headers(dave_token),
            json={"payload": "dave tries"},
        )
        assert write.status_code == 403

    def test_adding_existing_member_conflicts(
        self, client: TestClient, alice_token: str, bob_token: str
    ) -> None:
        created = client.post(
            "/api/v1/channels",
            headers=auth_headers(alice_token),
            json={"handle": "chan-group-dup", "kind": "group"},
        ).json()
        response = client.post(
            f"/api/v1/channels/{created['id']}/members",
            headers=auth_headers(alice_token),
            json={"agent_handle": "agent-alice"},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "MEMBER_EXISTS"

    def test_member_add_unknown_agent_404(self, client: TestClient, alice_token: str) -> None:
        created = client.post(
            "/api/v1/channels",
            headers=auth_headers(alice_token),
            json={"handle": "chan-group-ghost", "kind": "group"},
        ).json()
        response = client.post(
            f"/api/v1/channels/{created['id']}/members",
            headers=auth_headers(alice_token),
            json={"agent_handle": "agent-ghost"},
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "UNKNOWN_AGENT"

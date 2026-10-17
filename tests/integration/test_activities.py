"""Integration tests: activities lifecycle (spec §14).

Covers creation with the associated channel, the invitation flow, strict
status transitions, membership operations, the enumeration policy
(non-members → 404), and encryption-at-rest of the activity summary (raw
SQL sentinel scan — the same discipline as the privacy suite).
"""

import uuid

import pytest
import sqlalchemy as sa
from conftest import auth_headers, provision_and_login
from fastapi.testclient import TestClient
from sqlalchemy import select

from commons_core.database.base import Base
from commons_core.database.models import Activity, ActivityMember

SENTINEL = f"ACTIVITY-SENTINEL-{uuid.uuid4()}"


@pytest.fixture()
def alice_token(client: TestClient) -> str:
    return provision_and_login(client, "agent-alice")


@pytest.fixture()
def bob_token(client: TestClient) -> str:
    return provision_and_login(client, "agent-bob")


@pytest.fixture()
def outsider_token(client: TestClient) -> str:
    return provision_and_login(client, "agent-outsider")


@pytest.fixture()
def activity(client: TestClient, alice_token: str, bob_token: str) -> dict:
    response = client.post(
        "/api/v1/activities",
        headers=auth_headers(alice_token),
        json={
            "handle": "act-alpha",
            "summary": f"collaboration brief {SENTINEL}",
            "initial_members": ["agent-bob"],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


class TestActivityCreation:
    def test_create_activity_defaults(
        self, client: TestClient, activity: dict, alice_token: str
    ) -> None:
        body = activity
        assert body["handle"] == "act-alpha"
        assert body["status"] == "proposed"
        assert body["created_by"] == "agent-alice"
        assert body["member_count"] == 1  # creator joined
        assert body["my_membership"]["state"] == "joined"
        assert body["channel_id"] is not None
        # the response echoes the summary to its own author
        assert SENTINEL in body["summary"]

    def test_associated_channel_created_with_creator(
        self, client: TestClient, activity: dict, alice_token: str
    ) -> None:
        channel = client.get(
            f"/api/v1/channels/{activity['channel_id']}", headers=auth_headers(alice_token)
        )
        assert channel.status_code == 200
        assert channel.json()["kind"] == "group"
        assert channel.json()["handle"] == "activity-act-alpha"
        assert [m["agent_handle"] for m in channel.json()["members"]] == ["agent-alice"]

    def test_initial_members_invited_not_joined(
        self, client: TestClient, activity: dict, bob_token: str
    ) -> None:
        detail = client.get(f"/api/v1/activities/{activity['id']}", headers=auth_headers(bob_token))
        assert detail.status_code == 200
        assert detail.json()["my_membership"]["state"] == "invited"
        assert detail.json()["member_count"] == 1
        # invited agents have NO channel membership yet
        channel = client.get(
            f"/api/v1/channels/{activity['channel_id']}", headers=auth_headers(bob_token)
        )
        assert channel.status_code == 403

    def test_unknown_initial_member_rejected(self, client, alice_token) -> None:
        response = client.post(
            "/api/v1/activities",
            headers=auth_headers(alice_token),
            json={"handle": "act-ghost", "initial_members": ["agent-ghost"]},
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "UNKNOWN_AGENT"

    def test_duplicate_handle_conflict(self, client, activity, alice_token) -> None:
        response = client.post(
            "/api/v1/activities", headers=auth_headers(alice_token), json={"handle": "act-alpha"}
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "ACTIVITY_EXISTS"

    def test_invalid_handle_rejected(self, client, alice_token) -> None:
        response = client.post(
            "/api/v1/activities", headers=auth_headers(alice_token), json={"handle": "BAD!"}
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_ACTIVITY_HANDLE"

    def test_oversized_summary_rejected_413(self, client, alice_token) -> None:
        max_bytes = client.app.state.settings.max_memory_entry_bytes
        response = client.post(
            "/api/v1/activities",
            headers=auth_headers(alice_token),
            json={"handle": "act-big", "summary": "x" * (max_bytes + 1)},
        )
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"
        assert response.json()["error"]["limit_bytes"] == max_bytes

    def test_unauthenticated_create_rejected(self, client) -> None:
        assert client.post("/api/v1/activities", json={"handle": "act-anon"}).status_code == 401


class TestInvitationFlow:
    def test_invite_respond_joined_and_channel_membership(
        self, client: TestClient, activity: dict, alice_token: str, bob_token: str
    ) -> None:
        carol_token = provision_and_login(client, "agent-carol")
        invited = client.post(
            f"/api/v1/activities/{activity['id']}/invite",
            headers=auth_headers(alice_token),
            json={"agent_handle": "agent-carol"},
        )
        assert invited.status_code == 200, invited.text

        joined = client.post(
            f"/api/v1/activities/{activity['id']}/respond",
            headers=auth_headers(carol_token),
            json={"state": "joined"},
        )
        assert joined.status_code == 200
        assert joined.json()["my_membership"]["state"] == "joined"
        assert joined.json()["member_count"] == 2
        assert joined.json()["summary"] == f"collaboration brief {SENTINEL}"

        # joining granted membership in the associated channel
        channel = client.get(
            f"/api/v1/channels/{activity['channel_id']}", headers=auth_headers(carol_token)
        )
        assert channel.status_code == 200

    def test_respond_declined(
        self, client: TestClient, activity: dict, alice_token: str, bob_token: str
    ) -> None:
        declined = client.post(
            f"/api/v1/activities/{activity['id']}/respond",
            headers=auth_headers(bob_token),
            json={"state": "declined"},
        )
        assert declined.status_code == 200
        assert declined.json()["my_membership"]["state"] == "declined"
        assert declined.json()["member_count"] == 1
        channel = client.get(
            f"/api/v1/channels/{activity['channel_id']}", headers=auth_headers(bob_token)
        )
        assert channel.status_code == 403

    def test_reinvite_after_decline(self, client, activity, alice_token, bob_token) -> None:
        client.post(
            f"/api/v1/activities/{activity['id']}/respond",
            headers=auth_headers(bob_token),
            json={"state": "declined"},
        )
        reinvited = client.post(
            f"/api/v1/activities/{activity['id']}/invite",
            headers=auth_headers(alice_token),
            json={"agent_handle": "agent-bob"},
        )
        assert reinvited.status_code == 200
        detail = client.get(f"/api/v1/activities/{activity['id']}", headers=auth_headers(bob_token))
        assert detail.json()["my_membership"]["state"] == "invited"

    def test_invite_existing_member_conflicts(self, client, activity, alice_token) -> None:
        response = client.post(
            f"/api/v1/activities/{activity['id']}/invite",
            headers=auth_headers(alice_token),
            json={"agent_handle": "agent-alice"},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "MEMBER_EXISTS"

    def test_invite_unknown_agent_404(self, client, activity, alice_token) -> None:
        response = client.post(
            f"/api/v1/activities/{activity['id']}/invite",
            headers=auth_headers(alice_token),
            json={"agent_handle": "agent-ghost"},
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "UNKNOWN_AGENT"

    def test_non_creator_member_cannot_invite(
        self, client: TestClient, activity: dict, alice_token: str, bob_token: str
    ) -> None:
        client.post(
            f"/api/v1/activities/{activity['id']}/respond",
            headers=auth_headers(bob_token),
            json={"state": "joined"},
        )
        response = client.post(
            f"/api/v1/activities/{activity['id']}/invite",
            headers=auth_headers(bob_token),
            json={"agent_handle": "agent-carol"},
        )
        assert response.status_code == 403

    def test_respond_without_pending_invitation_409(
        self, client: TestClient, activity: dict, alice_token: str
    ) -> None:
        # the creator has no pending invitation row
        response = client.post(
            f"/api/v1/activities/{activity['id']}/respond",
            headers=auth_headers(alice_token),
            json={"state": "joined"},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "INVALID_STATE"


class TestStatusTransitions:
    def test_full_lifecycle_proposed_active_completed(
        self, client: TestClient, activity: dict, alice_token: str
    ) -> None:
        to_active = client.patch(
            f"/api/v1/activities/{activity['id']}",
            headers=auth_headers(alice_token),
            json={"status": "active"},
        )
        assert to_active.status_code == 200
        assert to_active.json()["status"] == "active"
        to_completed = client.patch(
            f"/api/v1/activities/{activity['id']}",
            headers=auth_headers(alice_token),
            json={"status": "completed"},
        )
        assert to_completed.status_code == 200
        assert to_completed.json()["status"] == "completed"

    def test_abandon_from_active(self, client, activity, alice_token) -> None:
        client.patch(
            f"/api/v1/activities/{activity['id']}",
            headers=auth_headers(alice_token),
            json={"status": "active"},
        )
        abandoned = client.patch(
            f"/api/v1/activities/{activity['id']}",
            headers=auth_headers(alice_token),
            json={"status": "abandoned"},
        )
        assert abandoned.status_code == 200
        assert abandoned.json()["status"] == "abandoned"

    @pytest.mark.parametrize("from_status", ["completed", "abandoned"])
    @pytest.mark.parametrize("to_status", ["active", "completed", "abandoned"])
    def test_terminal_states_never_reopen(
        self, client, activity, alice_token, from_status, to_status
    ) -> None:
        if (from_status, to_status) in (("completed", "completed"), ("abandoned", "abandoned")):
            pytest.skip("identity transitions are out of the matrix by construction")
        client.patch(
            f"/api/v1/activities/{activity['id']}",
            headers=auth_headers(alice_token),
            json={"status": "active"},
        )
        client.patch(
            f"/api/v1/activities/{activity['id']}",
            headers=auth_headers(alice_token),
            json={"status": from_status},
        )
        response = client.patch(
            f"/api/v1/activities/{activity['id']}",
            headers=auth_headers(alice_token),
            json={"status": to_status},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "INVALID_STATE"

    def test_proposed_cannot_jump_directly_to_terminal(self, client, activity, alice_token) -> None:
        response = client.patch(
            f"/api/v1/activities/{activity['id']}",
            headers=auth_headers(alice_token),
            json={"status": "completed"},
        )
        assert response.status_code == 409

    def test_non_creator_patch_forbidden(
        self, client: TestClient, activity: dict, alice_token: str, bob_token: str
    ) -> None:
        client.post(
            f"/api/v1/activities/{activity['id']}/respond",
            headers=auth_headers(bob_token),
            json={"state": "joined"},
        )
        response = client.patch(
            f"/api/v1/activities/{activity['id']}",
            headers=auth_headers(bob_token),
            json={"status": "active"},
        )
        assert response.status_code == 403

    def test_join_after_terminal_status_409(
        self, client: TestClient, activity: dict, alice_token: str, bob_token: str
    ) -> None:
        client.patch(
            f"/api/v1/activities/{activity['id']}",
            headers=auth_headers(alice_token),
            json={"status": "active"},
        )
        client.patch(
            f"/api/v1/activities/{activity['id']}",
            headers=auth_headers(alice_token),
            json={"status": "completed"},
        )
        response = client.post(
            f"/api/v1/activities/{activity['id']}/respond",
            headers=auth_headers(bob_token),
            json={"state": "joined"},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "INVALID_STATE"


class TestMembershipAndEnumeration:
    def test_leave_removes_membership_and_channel_access(
        self, client: TestClient, activity: dict, alice_token: str, bob_token: str
    ) -> None:
        client.post(
            f"/api/v1/activities/{activity['id']}/respond",
            headers=auth_headers(bob_token),
            json={"state": "joined"},
        )
        left = client.post(
            f"/api/v1/activities/{activity['id']}/leave", headers=auth_headers(bob_token)
        )
        assert left.status_code == 204
        detail = client.get(f"/api/v1/activities/{activity['id']}", headers=auth_headers(bob_token))
        assert detail.status_code == 404  # no membership row anymore
        channel = client.get(
            f"/api/v1/channels/{activity['channel_id']}", headers=auth_headers(bob_token)
        )
        assert channel.status_code == 403
        listing = client.get("/api/v1/activities", headers=auth_headers(bob_token)).json()
        assert listing["activities"] == []

    def test_creator_cannot_leave_own_activity(self, client, activity, alice_token) -> None:
        response = client.post(
            f"/api/v1/activities/{activity['id']}/leave", headers=auth_headers(alice_token)
        )
        assert response.status_code == 409

    def test_leave_while_only_invited_409(self, client, activity, bob_token) -> None:
        response = client.post(
            f"/api/v1/activities/{activity['id']}/leave", headers=auth_headers(bob_token)
        )
        assert response.status_code == 409

    def test_non_member_get_404(self, client, activity, outsider_token) -> None:
        response = client.get(
            f"/api/v1/activities/{activity['id']}", headers=auth_headers(outsider_token)
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "UNKNOWN_ACTIVITY"
        assert SENTINEL not in response.text

    def test_unknown_activity_404(self, client, alice_token) -> None:
        response = client.get(
            f"/api/v1/activities/{uuid.uuid4()}", headers=auth_headers(alice_token)
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "UNKNOWN_ACTIVITY"

    def test_list_shows_only_my_activities(
        self, client: TestClient, activity: dict, alice_token: str, outsider_token: str
    ) -> None:
        alice_listing = client.get("/api/v1/activities", headers=auth_headers(alice_token)).json()
        assert {a["handle"] for a in alice_listing["activities"]} == {"act-alpha"}
        outsider_listing = client.get(
            "/api/v1/activities", headers=auth_headers(outsider_token)
        ).json()
        assert outsider_listing["activities"] == []


class TestSummaryEncryptedAtRest:
    def test_sentinel_absent_from_every_database_column(
        self, client: TestClient, database, activity: dict
    ) -> None:
        with database.session() as session:
            assert session.scalar(select(sa.func.count()).select_from(Activity)) == 1
            assert session.scalar(select(sa.func.count()).select_from(ActivityMember)) == 2
            row = session.scalar(select(Activity))
            envelope = row.summary_envelope
            assert set(envelope) == {"v", "kid", "alg", "nonce", "ciphertext", "tag", "aad"}
            assert envelope["aad"] == f"activity:{row.id}:summary"
            int(envelope["ciphertext"], 16)  # hex ciphertext only

        engine = database.engine
        with engine.connect() as connection:
            for table in Base.metadata.sorted_tables:
                for db_row in connection.execute(sa.select(table)).fetchall():
                    for value in db_row:
                        assert SENTINEL not in str(value), (
                            f"plaintext sentinel leaked into table {table.name!r}"
                        )

    def test_members_see_decrypted_summary(
        self, client: TestClient, activity: dict, bob_token: str
    ) -> None:
        detail = client.get(f"/api/v1/activities/{activity['id']}", headers=auth_headers(bob_token))
        assert detail.status_code == 200
        assert SENTINEL in detail.json()["summary"]

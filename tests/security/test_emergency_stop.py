"""Security tests: infrastructure emergency stop (spec §16, §21).

Pause semantics (documented): while an admin engages the stop, NEW-WRITE
endpoints (message send, memory write, task create, activity create) reject
with 503 ``SERVICE_PAUSED`` and the worker refuses to claim new work; READS
continue; releasing the stop resumes normal operation. Non-admins cannot
engage or release (403 + ``authz_denial``). The state is visible in admin
telemetry.
"""

import pytest
from conftest import auth_headers, provision_and_login
from fastapi.testclient import TestClient
from sqlalchemy import select

from commons_core.database.enums import SecurityEventCategory
from commons_core.database.models import SecurityEvent, SystemState
from commons_core.scheduler.worker import run_worker


@pytest.fixture()
def alice_token(client: TestClient) -> str:
    return provision_and_login(client, "agent-alice")


@pytest.fixture()
def bob_token(client: TestClient) -> str:
    return provision_and_login(client, "agent-bob")


@pytest.fixture()
def admin_token(client: TestClient) -> str:
    return provision_and_login(client, "ops-admin", is_admin=True)


@pytest.fixture()
def channel_and_namespace(client: TestClient, alice_token: str, bob_token: str) -> dict:
    channel = client.post(
        "/api/v1/channels",
        headers=auth_headers(alice_token),
        json={"handle": "chan-pause", "kind": "direct", "initial_members": ["agent-bob"]},
    ).json()
    namespace = client.post(
        "/api/v1/memory/namespaces", headers=auth_headers(alice_token), json={"handle": "ns-pause"}
    ).json()
    return {"channel": channel, "namespace": namespace}


@pytest.fixture()
def emergency_stop(client: TestClient, admin_token: str) -> None:
    response = client.post("/api/v1/admin/emergency-stop", headers=auth_headers(admin_token))
    assert response.status_code == 200
    assert response.json()["emergency_stop"]["active"] is True
    return None


class TestEmergencyStopEngagement:
    def test_admin_engages_and_state_persisted(
        self, client, database, admin_token, emergency_stop
    ) -> None:
        with database.session() as session:
            row = session.get(SystemState, "emergency_stop")
            assert row is not None
            assert row.value["active"] is True
            assert row.value["set_by"]

    def test_stop_recorded_as_security_event(self, client, database, admin_token, emergency_stop):
        with database.session() as session:
            events = list(
                session.scalars(
                    select(SecurityEvent).where(
                        SecurityEvent.category == SecurityEventCategory.EMERGENCY_STOP
                    )
                )
            )
            assert len(events) == 1
            assert events[0].details == {"action": "set"}

    def test_unauthenticated_cannot_engage(self, client) -> None:
        assert client.post("/api/v1/admin/emergency-stop").status_code == 401

    @pytest.mark.parametrize(
        "method,path",
        [("post", "/api/v1/admin/emergency-stop"), ("delete", "/api/v1/admin/emergency-stop")],
    )
    def test_non_admin_cannot_set_or_clear(
        self, client, database, alice_token, method, path
    ) -> None:
        response = getattr(client, method)(path, headers=auth_headers(alice_token))
        assert response.status_code == 403
        with database.session() as session:
            denials = list(
                session.scalars(
                    select(SecurityEvent).where(
                        SecurityEvent.category == SecurityEventCategory.AUTHZ_DENIAL
                    )
                )
            )
            assert len(denials) == 1
            assert "admin" in denials[0].details["permission"]


class TestPausedWritesRejected:
    def test_message_send_503(
        self, client, alice_token, channel_and_namespace, emergency_stop
    ) -> None:
        response = client.post(
            f"/api/v1/channels/{channel_and_namespace['channel']['id']}/messages",
            headers=auth_headers(alice_token),
            json={"payload": "paused hello"},
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "SERVICE_PAUSED"

    def test_memory_write_503(
        self, client, alice_token, channel_and_namespace, emergency_stop
    ) -> None:
        response = client.post(
            f"/api/v1/memory/namespaces/{channel_and_namespace['namespace']['id']}/entries",
            headers=auth_headers(alice_token),
            json={"key": "paused-key", "payload": "paused value"},
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "SERVICE_PAUSED"

    def test_task_create_503(self, client, alice_token, emergency_stop) -> None:
        response = client.post(
            "/api/v1/tasks", headers=auth_headers(alice_token), json={"kind": "noop"}
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "SERVICE_PAUSED"

    def test_activity_create_503(self, client, alice_token, emergency_stop) -> None:
        response = client.post(
            "/api/v1/activities", headers=auth_headers(alice_token), json={"handle": "act-paused"}
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "SERVICE_PAUSED"


class TestPausedReadsStillWork:
    def test_message_retrieval_200(
        self, client, bob_token, channel_and_namespace, emergency_stop
    ) -> None:
        page = client.get(
            f"/api/v1/channels/{channel_and_namespace['channel']['id']}/messages",
            headers=auth_headers(bob_token),
        )
        assert page.status_code == 200

    def test_memory_and_task_reads_200(
        self, client, alice_token, channel_and_namespace, emergency_stop
    ) -> None:
        assert (
            client.get(
                f"/api/v1/memory/namespaces/{channel_and_namespace['namespace']['id']}",
                headers=auth_headers(alice_token),
            ).status_code
            == 200
        )
        assert client.get("/api/v1/tasks", headers=auth_headers(alice_token)).status_code == 200
        assert (
            client.get("/api/v1/governance/records", headers=auth_headers(alice_token)).status_code
            == 200
        )

    def test_activity_listing_200(self, client, alice_token, emergency_stop) -> None:
        assert (
            client.get("/api/v1/activities", headers=auth_headers(alice_token)).status_code == 200
        )


class TestWorkerHonorsPause:
    def test_worker_does_not_claim_while_paused(
        self, client, database, alice_token, admin_token, emergency_stop
    ) -> None:
        # A task created BEFORE the stop remains pending — the worker must
        # not claim it while the stop is engaged.
        assert (
            client.post(
                "/api/v1/tasks", headers=auth_headers(alice_token), json={"kind": "noop"}
            ).status_code
            == 503
        )  # new writes rejected; create via the service instead
        from commons_core.agents.service import get_agent_by_handle
        from commons_core.scheduler import service

        with database.session() as session:
            agent = get_agent_by_handle(session, "agent-alice")
            service.create_task(
                session,
                agent,
                kind="noop",
                payload={},
                max_pending_tasks=1000,
                max_task_attempts=3,
            )
        assert run_worker(database.session, once=True) == 0
        listing = client.get("/api/v1/tasks", headers=auth_headers(alice_token)).json()
        assert listing["tasks"][0]["status"] == "pending"
        assert listing["tasks"][0]["attempts"] == 0

    def test_worker_resumes_after_release(
        self, client, database, alice_token, admin_token, emergency_stop
    ) -> None:
        from commons_core.agents.service import get_agent_by_handle
        from commons_core.scheduler import service

        with database.session() as session:
            agent = get_agent_by_handle(session, "agent-alice")
            task, _ = service.create_task(
                session,
                agent,
                kind="noop",
                payload={},
                max_pending_tasks=1000,
                max_task_attempts=3,
            )
            task_id = task.id
        assert run_worker(database.session, once=True) == 0

        released = client.delete("/api/v1/admin/emergency-stop", headers=auth_headers(admin_token))
        assert released.status_code == 200
        assert released.json()["emergency_stop"]["active"] is False
        assert run_worker(database.session, once=True) == 1
        with database.session() as session:
            from commons_core.database.enums import TaskStatus

            assert service.get_task(session, task_id).status == TaskStatus.SUCCEEDED


class TestTelemetryShowsStopState:
    def test_emergency_stop_in_admin_telemetry(self, client, admin_token, emergency_stop) -> None:
        telemetry = client.get("/api/v1/admin/telemetry", headers=auth_headers(admin_token)).json()
        assert telemetry["emergency_stop"]["active"] is True


class TestResumeAllowsWritesAgain:
    def test_message_send_works_after_release(
        self, client, alice_token, admin_token, channel_and_namespace, emergency_stop
    ) -> None:
        released = client.delete("/api/v1/admin/emergency-stop", headers=auth_headers(admin_token))
        assert released.status_code == 200
        response = client.post(
            f"/api/v1/channels/{channel_and_namespace['channel']['id']}/messages",
            headers=auth_headers(alice_token),
            json={"payload": "resumed hello"},
        )
        assert response.status_code == 201
        # and the release was recorded
        with client.app.state.database.session() as session:
            events = list(
                session.scalars(
                    select(SecurityEvent).where(
                        SecurityEvent.category == SecurityEventCategory.EMERGENCY_STOP
                    )
                )
            )
            assert [event.details["action"] for event in events] == ["set", "clear"]

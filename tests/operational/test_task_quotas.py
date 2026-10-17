"""Operational tests: scheduler queue quotas & telemetry visibility (spec §19, §21, §16).

Complements ``tests/integration/test_scheduler.py``: quota isolation between
agents, idempotent replays that do not consume quota, and task counters in
admin telemetry.
"""

import pytest
from conftest import auth_headers, provision_and_login
from fastapi.testclient import TestClient

from commons_core.security.events import record_event  # noqa: F401 (contract reference)


@pytest.fixture()
def alice_token(client: TestClient) -> str:
    return provision_and_login(client, "agent-alice")


@pytest.fixture()
def bob_token(client: TestClient) -> str:
    return provision_and_login(client, "agent-bob")


@pytest.fixture()
def admin_token(client: TestClient) -> str:
    return provision_and_login(client, "ops-admin", is_admin=True)


class TestQuotaIsolation:
    def test_global_cap_applies_across_agents(
        self, client, monkeypatch, alice_token, bob_token
    ) -> None:
        """The global in-queue cap is shared infrastructure state (spec §16)."""
        monkeypatch.setattr(client.app.state.settings, "max_pending_tasks", 2)
        assert (
            client.post(
                "/api/v1/tasks", headers=auth_headers(alice_token), json={"kind": "noop"}
            ).status_code
            == 201
        )
        assert (
            client.post(
                "/api/v1/tasks", headers=auth_headers(bob_token), json={"kind": "noop"}
            ).status_code
            == 201
        )
        # both agents are blocked by the shared cap, not just the first
        for token in (alice_token, bob_token):
            response = client.post(
                "/api/v1/tasks", headers=auth_headers(token), json={"kind": "noop"}
            )
            assert response.status_code == 429
            assert response.json()["error"]["code"] == "TASK_QUEUE_FULL"

    def test_terminal_tasks_free_queue_capacity(
        self, client, database, monkeypatch, alice_token
    ) -> None:
        from commons_core.scheduler.worker import run_worker

        monkeypatch.setattr(client.app.state.settings, "max_pending_tasks", 1)
        created = client.post(
            "/api/v1/tasks", headers=auth_headers(alice_token), json={"kind": "noop"}
        )
        assert created.status_code == 201
        blocked = client.post(
            "/api/v1/tasks", headers=auth_headers(alice_token), json={"kind": "noop"}
        )
        assert blocked.status_code == 429
        # draining the queue (task succeeds) frees capacity again
        run_worker(database.session, once=True)
        drained = client.post(
            "/api/v1/tasks", headers=auth_headers(alice_token), json={"kind": "noop"}
        )
        assert drained.status_code == 201

    def test_idempotent_replay_does_not_consume_quota(
        self, client, monkeypatch, alice_token
    ) -> None:
        monkeypatch.setattr(client.app.state.settings, "max_pending_tasks", 1)
        first = client.post(
            "/api/v1/tasks",
            headers=auth_headers(alice_token),
            json={"kind": "noop", "idempotency_key": "quota-idem"},
        )
        assert first.status_code == 201
        replay = client.post(
            "/api/v1/tasks",
            headers=auth_headers(alice_token),
            json={"kind": "noop", "idempotency_key": "quota-idem"},
        )
        assert replay.status_code == 200  # replay, not a new queued task


class TestTelemetryTaskCounters:
    def test_task_counters_by_status_visible_to_admin(
        self, client, database, admin_token, alice_token
    ) -> None:
        from commons_core.scheduler.worker import run_worker

        client.post("/api/v1/tasks", headers=auth_headers(alice_token), json={"kind": "noop"})
        client.post("/api/v1/tasks", headers=auth_headers(alice_token), json={"kind": "heartbeat"})
        run_worker(database.session, once=True)  # one task executed
        telemetry = client.get("/api/v1/admin/telemetry", headers=auth_headers(admin_token)).json()
        counts = telemetry["counts"]["tasks_by_status"]
        assert counts["pending"] == 1
        assert counts["succeeded"] == 1
        assert telemetry["counts"]["pending_tasks"] == 1

    def test_telemetry_contains_no_task_payloads(self, client, admin_token, alice_token) -> None:
        secret_marker = "control-data-should-not-appear"
        client.post(
            "/api/v1/tasks",
            headers=auth_headers(alice_token),
            json={"kind": "noop", "payload": {"note": secret_marker}},
        )
        telemetry = client.get("/api/v1/admin/telemetry", headers=auth_headers(admin_token))
        assert secret_marker not in telemetry.text
        assert "payload" not in telemetry.text

    def test_limits_reported_in_telemetry(self, client, admin_token) -> None:
        telemetry = client.get("/api/v1/admin/telemetry", headers=auth_headers(admin_token)).json()
        assert telemetry["limits"]["max_pending_tasks"] > 0
        assert telemetry["limits"]["max_task_attempts"] > 0

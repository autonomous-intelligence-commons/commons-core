"""Integration tests: the durable scheduler (spec §16).

API-level task management plus end-to-end worker execution (``once`` mode),
retry/backoff with an INJECTED clock (no real sleeps), idempotency,
cancellation, payload validation and queue quotas.

The worker-level tests use one committed session at a time (SQLite
serializes writers — mixing an open fixture session with worker
transactions would contend on the write lock).
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from conftest import auth_headers, provision_and_login
from fastapi.testclient import TestClient
from sqlalchemy import select

from commons_core.agents.service import provision_agent
from commons_core.database.enums import SecurityEventCategory, TaskStatus
from commons_core.database.models import ScheduledTask, SecurityEvent
from commons_core.infrastructure.time import as_utc, utc_now
from commons_core.scheduler import registry, service
from commons_core.scheduler.worker import run_worker

T0 = datetime(2030, 1, 1, tzinfo=UTC)


@pytest.fixture()
def alice_token(client: TestClient) -> str:
    return provision_and_login(client, "agent-alice")


@pytest.fixture()
def bob_token(client: TestClient) -> str:
    return provision_and_login(client, "agent-bob")


def _boom(_session, _task) -> None:
    """Test-only failing handler (registered per-test and cleaned up)."""
    raise ValueError("deliberate test failure")


@pytest.fixture()
def failing_kind():
    registry.register("test-boom", _boom)
    yield "test-boom"
    registry.unregister("test-boom")


@pytest.fixture()
def committed_agent(database):
    """A provisioned agent whose row is committed (worker-safe)."""
    with database.session() as session:
        agent = provision_agent(session, "agent-sched")
    return agent


def _create(database, agent, **overrides) -> str:
    """Create one task at T0 through the service; return its id."""
    kwargs = {
        "kind": "noop",
        "payload": {},
        "max_pending_tasks": 1000,
        "max_task_attempts": 3,
        "now": T0,
    }
    kwargs.update(overrides)
    with database.session() as session:
        task, _ = service.create_task(session, agent, **kwargs)
        task_id = task.id
    return task_id


class TestTaskCreation:
    def test_create_and_worker_executes(
        self, client: TestClient, database, alice_token: str
    ) -> None:
        created = client.post(
            "/api/v1/tasks",
            headers=auth_headers(alice_token),
            json={"kind": "noop", "payload": {"note": "control"}},
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["status"] == "pending"
        assert body["attempts"] == 0
        assert body["payload"] == {"note": "control"}  # echoed to the owner
        assert body["run_at"] is not None

        processed = run_worker(database.session, once=True)
        assert processed == 1
        listing = client.get("/api/v1/tasks", headers=auth_headers(alice_token)).json()
        assert listing["tasks"][0]["status"] == "succeeded"
        assert listing["tasks"][0]["attempts"] == 1
        assert listing["tasks"][0]["finished_at"] is not None

    def test_unknown_kind_rejected(self, client, alice_token) -> None:
        response = client.post(
            "/api/v1/tasks", headers=auth_headers(alice_token), json={"kind": "nope", "payload": {}}
        )
        assert response.status_code == 422
        error = response.json()["error"]
        assert error["code"] == "UNKNOWN_TASK_KIND"
        assert "noop" in error["known_kinds"]

    @pytest.mark.parametrize(
        "payload",
        [
            {"content": "message body"},  # top-level forbidden key
            {"secret": "x"},
            {"token": "x"},
            {"credentials": "x"},
            {"nested": {"inner": [{"token": "x"}]}},  # nested forbidden key
        ],
    )
    def test_forbidden_payload_keys_rejected(self, client, alice_token, payload) -> None:
        response = client.post(
            "/api/v1/tasks",
            headers=auth_headers(alice_token),
            json={"kind": "noop", "payload": payload},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_PAYLOAD"

    def test_oversized_payload_rejected_413(self, client, alice_token) -> None:
        response = client.post(
            "/api/v1/tasks",
            headers=auth_headers(alice_token),
            json={"kind": "noop", "payload": {"blob": "x" * 5000}},
        )
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"
        assert response.json()["error"]["limit_bytes"] == 4096

    def test_max_attempts_out_of_range(self, client, alice_token) -> None:
        limit = client.app.state.settings.max_task_attempts
        response = client.post(
            "/api/v1/tasks",
            headers=auth_headers(alice_token),
            json={"kind": "noop", "payload": {}, "max_attempts": limit + 1},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_PAYLOAD"
        assert response.json()["error"]["limit"] == limit

    def test_max_attempts_defaults_to_settings(self, client, alice_token) -> None:
        created = client.post(
            "/api/v1/tasks", headers=auth_headers(alice_token), json={"kind": "noop", "payload": {}}
        )
        assert created.json()["max_attempts"] == client.app.state.settings.max_task_attempts

    def test_idempotency_key_returns_same_task(self, client, database, alice_token) -> None:
        first = client.post(
            "/api/v1/tasks",
            headers=auth_headers(alice_token),
            json={"kind": "noop", "payload": {}, "idempotency_key": "idem-1"},
        )
        assert first.status_code == 201
        second = client.post(
            "/api/v1/tasks",
            headers=auth_headers(alice_token),
            json={"kind": "noop", "payload": {}, "idempotency_key": "idem-1"},
        )
        assert second.status_code == 200
        assert second.json()["id"] == first.json()["id"]
        with database.session() as session:
            count = len(list(session.scalars(select(ScheduledTask))))
            assert count == 1  # NO duplicate row

    def test_idempotency_key_other_agent_conflicts(self, client, alice_token, bob_token) -> None:
        client.post(
            "/api/v1/tasks",
            headers=auth_headers(alice_token),
            json={"kind": "noop", "payload": {}, "idempotency_key": "idem-2"},
        )
        response = client.post(
            "/api/v1/tasks",
            headers=auth_headers(bob_token),
            json={"kind": "noop", "payload": {}, "idempotency_key": "idem-2"},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    def test_unauthenticated_rejected(self, client) -> None:
        assert client.post("/api/v1/tasks", json={"kind": "noop"}).status_code == 401
        assert client.get("/api/v1/tasks").status_code == 401


class TestTaskListing:
    def test_only_my_tasks_listed(self, client, alice_token, bob_token) -> None:
        client.post("/api/v1/tasks", headers=auth_headers(alice_token), json={"kind": "noop"})
        client.post("/api/v1/tasks", headers=auth_headers(bob_token), json={"kind": "heartbeat"})
        alice_tasks = client.get("/api/v1/tasks", headers=auth_headers(alice_token)).json()
        bob_tasks = client.get("/api/v1/tasks", headers=auth_headers(bob_token)).json()
        assert [t["kind"] for t in alice_tasks["tasks"]] == ["noop"]
        assert [t["kind"] for t in bob_tasks["tasks"]] == ["heartbeat"]

    def test_status_filter(self, client, alice_token) -> None:
        client.post("/api/v1/tasks", headers=auth_headers(alice_token), json={"kind": "heartbeat"})
        client.post("/api/v1/tasks", headers=auth_headers(alice_token), json={"kind": "noop"})
        pending = client.get(
            "/api/v1/tasks", params={"status": "pending"}, headers=auth_headers(alice_token)
        )
        assert pending.status_code == 200
        assert {t["status"] for t in pending.json()["tasks"]} == {"pending"}

    def test_unknown_task_404_on_cancel(self, client, alice_token) -> None:
        response = client.post(
            f"/api/v1/tasks/{uuid.uuid4()}/cancel", headers=auth_headers(alice_token)
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "UNKNOWN_TASK"


class TestTaskCancellation:
    def test_cancel_pending(self, client, alice_token) -> None:
        task = client.post(
            "/api/v1/tasks", headers=auth_headers(alice_token), json={"kind": "noop"}
        ).json()
        cancelled = client.post(
            f"/api/v1/tasks/{task['id']}/cancel", headers=auth_headers(alice_token)
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        assert cancelled.json()["finished_at"] is not None

    def test_cancel_terminal_task_409(self, client, database, alice_token) -> None:
        task = client.post(
            "/api/v1/tasks", headers=auth_headers(alice_token), json={"kind": "noop"}
        ).json()
        run_worker(database.session, once=True)
        response = client.post(
            f"/api/v1/tasks/{task['id']}/cancel", headers=auth_headers(alice_token)
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "INVALID_STATE"

    def test_cancel_leased_task_409(self, client, database, alice_token) -> None:
        task = client.post(
            "/api/v1/tasks", headers=auth_headers(alice_token), json={"kind": "noop"}
        ).json()
        with database.session() as session:
            claimed = service.claim_next(session, utc_now(), 30)
            assert claimed is not None
        response = client.post(
            f"/api/v1/tasks/{task['id']}/cancel", headers=auth_headers(alice_token)
        )
        assert response.status_code == 409

    def test_cancel_other_agents_task_403_with_event(
        self, client, database, alice_token, bob_token
    ) -> None:
        task = client.post(
            "/api/v1/tasks", headers=auth_headers(alice_token), json={"kind": "noop"}
        ).json()
        response = client.post(
            f"/api/v1/tasks/{task['id']}/cancel", headers=auth_headers(bob_token)
        )
        assert response.status_code == 403
        with database.session() as session:
            events = list(
                session.scalars(
                    select(SecurityEvent).where(
                        SecurityEvent.category == SecurityEventCategory.AUTHZ_DENIAL
                    )
                )
            )
            assert any(e.details.get("permission") == "task:cancel" for e in events)


class TestQueueQuota:
    def test_global_pending_quota_429_with_event(
        self, client, database, monkeypatch, alice_token
    ) -> None:
        monkeypatch.setattr(client.app.state.settings, "max_pending_tasks", 2)
        for _ in range(2):
            created = client.post(
                "/api/v1/tasks", headers=auth_headers(alice_token), json={"kind": "noop"}
            )
            assert created.status_code == 201
        response = client.post(
            "/api/v1/tasks", headers=auth_headers(alice_token), json={"kind": "noop"}
        )
        assert response.status_code == 429
        error = response.json()["error"]
        assert error["code"] == "TASK_QUEUE_FULL"
        assert error["limit"] == 2
        assert error["scope"] == "global_pending_tasks"
        with database.session() as session:
            events = list(
                session.scalars(
                    select(SecurityEvent).where(
                        SecurityEvent.category == SecurityEventCategory.QUOTA_EXCEEDED
                    )
                )
            )
            assert len(events) == 1
            assert events[0].details == {"limit": 2, "scope": "global_pending_tasks"}

    def test_per_agent_quota_100(self, database) -> None:
        with database.session() as session:
            agent = provision_agent(session, "agent-quota")
            for _ in range(service.MAX_PENDING_TASKS_PER_AGENT):
                service.create_task(
                    session,
                    agent,
                    kind="noop",
                    payload={},
                    max_pending_tasks=10_000,
                    max_task_attempts=3,
                )
            with pytest.raises(service.TaskQueueFullError) as excinfo:
                service.create_task(
                    session,
                    agent,
                    kind="noop",
                    payload={},
                    max_pending_tasks=10_000,
                    max_task_attempts=3,
                )
            assert excinfo.value.details["scope"] == "agent_pending_tasks"
            assert excinfo.value.details["limit"] == service.MAX_PENDING_TASKS_PER_AGENT


class TestRetryBackoffAndTerminalFailure:
    def test_failing_handler_retries_with_exponential_backoff(
        self, database, committed_agent, failing_kind
    ) -> None:
        task_id = _create(
            database,
            committed_agent,
            kind=failing_kind,
            payload={"note": "do-not-leak", "nested": {"value": "also-hidden"}},
        )

        # attempt 1 fails -> pending again with run_at = T0 + 2s
        assert run_worker(database.session, once=True, clock=lambda: T0) == 1
        with database.session() as session:
            task = service.get_task(session, task_id)
            assert task.status == TaskStatus.PENDING
            assert task.attempts == 1
            # SQLite round-trips naive UTC datetimes — normalize before comparing
            assert as_utc(task.run_at) == T0 + timedelta(seconds=2)
            assert task.last_error == "ValueError: deliberate test failure"

        # attempt 2 fails -> run_at pushed another 4s (2^2)
        t1 = T0 + timedelta(seconds=2)
        assert run_worker(database.session, once=True, clock=lambda: t1) == 1
        with database.session() as session:
            task = service.get_task(session, task_id)
            assert task.attempts == 2
            assert as_utc(task.run_at) == t1 + timedelta(seconds=4)

        # attempt 3 fails -> terminal (attempts == max_attempts)
        t2 = t1 + timedelta(seconds=4)
        assert run_worker(database.session, once=True, clock=lambda: t2) == 1
        with database.session() as session:
            task = service.get_task(session, task_id)
            assert task.status == TaskStatus.FAILED
            assert task.attempts == 3
            assert task.finished_at is not None

    def test_failed_task_has_no_payload_leak_in_last_error(
        self, database, committed_agent, failing_kind
    ) -> None:
        task_id = _create(
            database,
            committed_agent,
            kind=failing_kind,
            payload={"note": "do-not-leak", "nested": {"value": "also-hidden"}},
        )
        clock_now = T0
        for _ in range(3):
            # Bind the current value via a default arg (B023-safe closure).
            run_worker(database.session, once=True, clock=lambda now=clock_now: now)
            with database.session() as session:
                task = service.get_task(session, task_id)
                clock_now = as_utc(task.run_at)  # advance to the (pushed) run_at
        with database.session() as session:
            task = service.get_task(session, task_id)
            assert task.status == TaskStatus.FAILED
            assert task.last_error == "ValueError: deliberate test failure"
            # no payload data in last_error (exception class + message only)
            assert "do-not-leak" not in task.last_error
            assert "also-hidden" not in task.last_error
            assert "note" not in task.last_error
            assert "nested" not in task.last_error


class TestSimulatedHandlers:
    def test_heartbeat_updates_system_state_and_telemetry(
        self, client: TestClient, database, alice_token: str
    ) -> None:
        admin_token = provision_and_login(client, "ops-admin", is_admin=True)
        created = client.post(
            "/api/v1/tasks", headers=auth_headers(alice_token), json={"kind": "heartbeat"}
        )
        assert created.status_code == 201
        assert run_worker(database.session, once=True) == 1
        telemetry = client.get("/api/v1/admin/telemetry", headers=auth_headers(admin_token)).json()
        assert telemetry["last_worker_heartbeat"] is not None
        assert telemetry["counts"]["tasks_by_status"]["succeeded"] == 1

    def test_activity_reminder_succeeds_for_joined_member(
        self, client: TestClient, database, alice_token: str
    ) -> None:
        activity = client.post(
            "/api/v1/activities", headers=auth_headers(alice_token), json={"handle": "act-remind"}
        ).json()
        created = client.post(
            "/api/v1/tasks",
            headers=auth_headers(alice_token),
            json={"kind": "activity_reminder", "payload": {"activity_id": activity["id"]}},
        )
        assert created.status_code == 201
        assert run_worker(database.session, once=True) == 1
        listing = client.get("/api/v1/tasks", headers=auth_headers(alice_token)).json()
        assert listing["tasks"][0]["status"] == "succeeded"

    def test_activity_reminder_fails_for_non_member(
        self, client: TestClient, database, alice_token: str, bob_token: str
    ) -> None:
        activity = client.post(
            "/api/v1/activities",
            headers=auth_headers(alice_token),
            json={"handle": "act-remind-2"},
        ).json()
        created = client.post(
            "/api/v1/tasks",
            headers=auth_headers(bob_token),
            json={"kind": "activity_reminder", "payload": {"activity_id": activity["id"]}},
        )
        assert created.status_code == 201
        # bob is not a member: the attempt fails and the task retries later
        assert run_worker(database.session, once=True) == 1
        listing = client.get("/api/v1/tasks", headers=auth_headers(bob_token)).json()
        assert listing["tasks"][0]["status"] == "pending"
        assert listing["tasks"][0]["last_error"].startswith("ValueError:")

    def test_activity_reminder_requires_activity_reference(
        self, client: TestClient, database, alice_token: str
    ) -> None:
        client.post(
            "/api/v1/tasks",
            headers=auth_headers(alice_token),
            json={"kind": "activity_reminder", "payload": {}},
        )
        run_worker(database.session, once=True)
        listing = client.get("/api/v1/tasks", headers=auth_headers(alice_token)).json()
        assert "activity_id" in listing["tasks"][0]["last_error"]


class TestFutureRunAt:
    def test_future_task_not_claimed_until_due(self, database, committed_agent) -> None:
        task_id = _create(database, committed_agent, run_at=T0 + timedelta(hours=1))
        assert run_worker(database.session, once=True, clock=lambda: T0) == 0
        with database.session() as session:
            task = service.get_task(session, task_id)
            assert task.status == TaskStatus.PENDING
            assert task.attempts == 0
        assert run_worker(database.session, once=True, clock=lambda: T0 + timedelta(hours=1)) == 1
        with database.session() as session:
            task = service.get_task(session, task_id)
            assert task.status == TaskStatus.SUCCEEDED

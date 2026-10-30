"""Security tests: R04 emergency-stop durability & failure reporting (spec §16/§21).

Deterministic failure-injection suite (R04 remediation, 2026-10-30):

- R04-A — a security-event insert failure must NOT roll back an engaged or
  released emergency stop, and must not turn into a false HTTP success.
- R04-B — a failure of the REQUIRED state commit must produce a non-success
  response (503 ``EMERGENCY_STOP_UNCONFIRMED``), leave the database in its
  prior state, and attempt no further writes on the broken transaction.
- R04-C — audit-event loss after a durable stop is documented behavior:
  the stop stays durable and the worker still honors it (audit loss is
  accepted without retry; R04-INV-09 non-atomicity).
- R04-D — engage/release symmetry plus idempotent repeated commands.
- R04-E — competing admin operations with independent request sessions are
  last-writer-wins (intentional); a failed control operation does not block
  or corrupt subsequent ones.

All failures are injected deterministically via SQLAlchemy event listeners
(engine-level ``commit`` and session-level ``before_flush``) — no sleep-based
races. Every durable-state assertion reads through a FRESH session
(authoritative read), never the request session.
"""

import pytest
from conftest import auth_headers, provision_and_login
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from commons_core.database.enums import SecurityEventCategory
from commons_core.database.models import SecurityEvent, SystemState


class StateCommitFailureInjector:
    """Deterministically fail the commit persisting the emergency-stop row.

    Arms on ``before_flush`` when the session has a pending
    ``SystemState(key="emergency_stop")`` change, then fails the NEXT
    engine-level COMMIT — i.e. exactly the transaction that would durably
    persist the required state transition (R04-B). Never touches commits of
    unrelated middleware/auth traffic.
    """

    def __init__(self, database) -> None:
        self.database = database
        self.armed = False
        self.fired = False

    def _before_flush(self, session, flush_context, instances) -> None:  # pragma: no cover
        for obj in list(session.new) + list(session.dirty):
            if isinstance(obj, SystemState) and obj.key == "emergency_stop":
                self.armed = True

    def _on_commit(self, conn) -> None:  # pragma: no cover - listener
        if self.armed and not self.fired:
            self.fired = True
            raise RuntimeError("injected state commit failure")

    def __enter__(self) -> "StateCommitFailureInjector":
        event.listen(Session, "before_flush", self._before_flush)
        event.listen(self.database.engine, "commit", self._on_commit)
        return self

    def __exit__(self, *exc_info: object) -> None:
        event.remove(Session, "before_flush", self._before_flush)
        event.remove(self.database.engine, "commit", self._on_commit)


class EventInsertFailureInjector:
    """Deterministically fail the EMERGENCY_STOP event insert (R04-A/R04-C).

    Raises from ``before_flush`` while the session has a pending
    ``SecurityEvent`` of category ``EMERGENCY_STOP`` — this simulates an
    event-persistence failure (e.g. a constraint violation or storage error)
    without touching any other flush.
    """

    def __init__(self) -> None:
        self.hits = 0

    def _before_flush(self, session, flush_context, instances) -> None:  # pragma: no cover
        for obj in session.new:
            if (
                isinstance(obj, SecurityEvent)
                and obj.category == SecurityEventCategory.EMERGENCY_STOP
            ):
                self.hits += 1
                raise RuntimeError("injected event insert failure")

    def __enter__(self) -> "EventInsertFailureInjector":
        event.listen(Session, "before_flush", self._before_flush)
        return self

    def __exit__(self, *exc_info: object) -> None:
        event.remove(Session, "before_flush", self._before_flush)


def authoritative_stop(database) -> dict | None:
    """Read the persisted emergency-stop state through a FRESH session."""
    with Session(bind=database.engine) as fresh:
        row = fresh.get(SystemState, "emergency_stop")
        return dict(row.value) if row is not None else None


def authoritative_events(database) -> list[dict]:
    """Read all persisted emergency-stop security events (fresh session)."""
    with Session(bind=database.engine) as fresh:
        rows = list(
            fresh.scalars(
                select(SecurityEvent).where(
                    SecurityEvent.category == SecurityEventCategory.EMERGENCY_STOP
                )
            )
        )
        return [{"details": dict(r.details)} for r in rows]


@pytest.fixture()
def admin_token(client: TestClient) -> str:
    return provision_and_login(client, "ops-admin", is_admin=True)


@pytest.fixture()
def second_admin_token(client: TestClient) -> str:
    return provision_and_login(client, "ops-admin-2", is_admin=True)


class TestR04AEventInsertFailure:
    """R04-A — security-event persistence failure."""

    def test_engage_survives_event_insert_failure(self, client, database, admin_token) -> None:
        with EventInsertFailureInjector() as injector:
            response = client.post(
                "/api/v1/admin/emergency-stop", headers=auth_headers(admin_token)
            )
        assert response.status_code == 200
        assert response.json()["emergency_stop"]["active"] is True
        assert injector.hits == 1  # the injection actually fired
        # Authoritative read: the stop IS durably committed.
        assert authoritative_stop(database) is not None
        assert authoritative_stop(database)["active"] is True
        # Documented audit loss (R04-INV-09): the event was dropped, not retried.
        assert authoritative_events(database) == []

    def test_release_survives_event_insert_failure(self, client, database, admin_token) -> None:
        engaged = client.post("/api/v1/admin/emergency-stop", headers=auth_headers(admin_token))
        assert engaged.status_code == 200
        with EventInsertFailureInjector() as injector:
            released = client.delete(
                "/api/v1/admin/emergency-stop", headers=auth_headers(admin_token)
            )
        assert released.status_code == 200
        assert released.json()["emergency_stop"]["active"] is False
        assert injector.hits == 1
        state = authoritative_stop(database)
        assert state is not None and state["active"] is False
        # Only the engage event survived; the release event was dropped.
        assert [e["details"] for e in authoritative_events(database)] == [{"action": "set"}]


class TestR04BRequiredCommitFailure:
    """R04-B — failure of the REQUIRED state commit."""

    def test_engage_commit_failure_is_non_success(self, client, database, admin_token) -> None:
        with StateCommitFailureInjector(database) as injector:
            response = client.post(
                "/api/v1/admin/emergency-stop", headers=auth_headers(admin_token)
            )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "EMERGENCY_STOP_UNCONFIRMED"
        assert injector.fired  # the injection actually fired
        # No internal details leaked into the public error response.
        assert "injected" not in response.text
        # Authoritative read: the stop was NOT committed.
        assert authoritative_stop(database) is None
        # No further writes on the broken transaction (no orphan events).
        assert authoritative_events(database) == []

    def test_release_commit_failure_is_non_success(self, client, database, admin_token) -> None:
        engaged = client.post("/api/v1/admin/emergency-stop", headers=auth_headers(admin_token))
        assert engaged.status_code == 200
        with StateCommitFailureInjector(database):
            released = client.delete(
                "/api/v1/admin/emergency-stop", headers=auth_headers(admin_token)
            )
        assert released.status_code == 503
        assert released.json()["error"]["code"] == "EMERGENCY_STOP_UNCONFIRMED"
        # The previously engaged stop is untouched — a failed release is not
        # reported as a successful release.
        state = authoritative_stop(database)
        assert state is not None and state["active"] is True

    def test_uncertain_commit_reconciled_by_authoritative_read(
        self, client, database, admin_token
    ) -> None:
        # Simulate "commit raised but actually took effect": another session
        # has already durably written the requested state.
        with database.session() as setup_session:
            from commons_core.infrastructure.system_state import write_system_state

            write_system_state(
                setup_session,
                "emergency_stop",
                {"active": True, "set_at": "2026-10-30T00:00:00+00:00", "set_by": "ops-admin"},
            )
        with StateCommitFailureInjector(database):
            response = client.post(
                "/api/v1/admin/emergency-stop", headers=auth_headers(admin_token)
            )
        # Honest outcome: the requested state IS the durable state, so the
        # transition is confirmed without a false claim either way (R04-INV-04).
        assert response.status_code == 200
        assert response.json()["emergency_stop"]["active"] is True
        assert authoritative_stop(database)["active"] is True


class TestR04CDurableStopStillHonored:
    """R04-C — the safety effect survives audit-event loss."""

    def test_worker_honors_stop_after_audit_failure(self, client, database, admin_token) -> None:
        from commons_core.agents.service import get_agent_by_handle
        from commons_core.scheduler import service
        from commons_core.scheduler.worker import run_worker

        # A pending task created BEFORE the stop (direct service call — the
        # paused API would reject it) proves the worker really idles.
        with database.session() as session:
            agent = get_agent_by_handle(session, "ops-admin")
            service.create_task(
                session,
                agent,
                kind="noop",
                payload={},
                max_pending_tasks=1000,
                max_task_attempts=3,
            )
        with EventInsertFailureInjector():
            engaged = client.post("/api/v1/admin/emergency-stop", headers=auth_headers(admin_token))
        assert engaged.status_code == 200
        # The worker must refuse new work even though the audit event was lost.
        assert run_worker(database.session, once=True) == 0

    def test_new_writes_rejected_after_audit_failure(self, client, database, admin_token) -> None:
        with EventInsertFailureInjector():
            engaged = client.post("/api/v1/admin/emergency-stop", headers=auth_headers(admin_token))
        assert engaged.status_code == 200
        paused = client.post(
            "/api/v1/tasks", headers=auth_headers(admin_token), json={"kind": "noop"}
        )
        assert paused.status_code == 503
        assert paused.json()["error"]["code"] == "SERVICE_PAUSED"


class TestR04DStopReleaseSymmetry:
    """R04-D — symmetric engage/release, idempotent repetition preserved."""

    def test_repeated_engage_is_idempotent(self, client, database, admin_token) -> None:
        first = client.post("/api/v1/admin/emergency-stop", headers=auth_headers(admin_token))
        assert first.status_code == 200
        second = client.post("/api/v1/admin/emergency-stop", headers=auth_headers(admin_token))
        assert second.status_code == 200
        assert authoritative_stop(database)["active"] is True
        # Both engagements were recorded (refresh semantics preserved).
        assert [e["details"]["action"] for e in authoritative_events(database)] == ["set", "set"]

    def test_full_cycle_engage_then_release(self, client, database, admin_token) -> None:
        engaged = client.post("/api/v1/admin/emergency-stop", headers=auth_headers(admin_token))
        assert engaged.status_code == 200
        released = client.delete("/api/v1/admin/emergency-stop", headers=auth_headers(admin_token))
        assert released.status_code == 200
        state = authoritative_stop(database)
        assert state is not None and state["active"] is False
        assert [e["details"]["action"] for e in authoritative_events(database)] == ["set", "clear"]

    def test_repeated_release_is_idempotent(self, client, database, admin_token) -> None:
        first = client.delete("/api/v1/admin/emergency-stop", headers=auth_headers(admin_token))
        assert first.status_code == 200
        assert first.json()["emergency_stop"]["active"] is False
        second = client.delete("/api/v1/admin/emergency-stop", headers=auth_headers(admin_token))
        assert second.status_code == 200
        assert second.json()["emergency_stop"]["active"] is False
        assert authoritative_stop(database)["active"] is False


class TestR04ECompetingControlOperations:
    """R04-E — independent admin sessions, last-writer-wins (intentional)."""

    def test_last_writer_wins(self, client, database, admin_token, second_admin_token) -> None:
        assert (
            client.post(
                "/api/v1/admin/emergency-stop", headers=auth_headers(admin_token)
            ).status_code
            == 200
        )
        assert (
            client.delete(
                "/api/v1/admin/emergency-stop", headers=auth_headers(second_admin_token)
            ).status_code
            == 200
        )
        assert authoritative_stop(database)["active"] is False
        assert (
            client.post(
                "/api/v1/admin/emergency-stop", headers=auth_headers(second_admin_token)
            ).status_code
            == 200
        )
        assert authoritative_stop(database)["active"] is True

    def test_failed_control_does_not_block_or_corrupt(
        self, client, database, admin_token, second_admin_token
    ) -> None:
        with StateCommitFailureInjector(database):
            failed = client.post("/api/v1/admin/emergency-stop", headers=auth_headers(admin_token))
        assert failed.status_code == 503
        # A subsequent control operation from another session succeeds and
        # the system_state table holds exactly one consistent row.
        ok = client.delete("/api/v1/admin/emergency-stop", headers=auth_headers(second_admin_token))
        assert ok.status_code == 200
        with Session(bind=database.engine) as fresh:
            rows = list(
                fresh.scalars(select(SystemState).where(SystemState.key == "emergency_stop"))
            )
        assert len(rows) == 1
        assert rows[0].value["active"] is False

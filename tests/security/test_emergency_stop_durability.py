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


def admin_agent_id(database, handle: str = "ops-admin") -> str:
    """The database agent id for a provisioned admin handle (for set_by checks)."""
    from commons_core.agents.service import get_agent_by_handle

    with Session(bind=database.engine) as fresh:
        return get_agent_by_handle(fresh, handle).id


class StateWriteFailureInjector:
    """Deterministically fail the emergency-stop state WRITE (R04-REV-02).

    Raises from ``before_flush`` while the session has a pending INSERT or
    UPDATE of the ``SystemState(key="emergency_stop")`` row — this
    simulates a database write/flush failure (storage error, constraint
    violation, connectivity loss mid-flush) inside the REQUIRED stage.
    """

    def __init__(self) -> None:
        self.hits = 0

    def _before_flush(self, session, flush_context, instances) -> None:  # pragma: no cover
        for obj in list(session.new) + list(session.dirty):
            if isinstance(obj, SystemState) and obj.key == "emergency_stop":
                self.hits += 1
                raise RuntimeError("injected state write failure")

    def __enter__(self) -> "StateWriteFailureInjector":
        event.listen(Session, "before_flush", self._before_flush)
        return self

    def __exit__(self, *exc_info: object) -> None:
        event.remove(Session, "before_flush", self._before_flush)


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
        # REV-01-B — "commit raised but actually took effect": the SAME
        # principal's earlier transaction already durably wrote the
        # requested state, so the uncertain command converges honestly.
        agent_id = admin_agent_id(database)
        with database.session() as setup_session:
            from commons_core.infrastructure.system_state import write_system_state

            write_system_state(
                setup_session,
                "emergency_stop",
                {"active": True, "set_at": "2026-10-30T00:00:00+00:00", "set_by": agent_id},
            )
        with StateCommitFailureInjector(database):
            response = client.post(
                "/api/v1/admin/emergency-stop", headers=auth_headers(admin_token)
            )
        # Honest outcome: the requested state IS the durable state,
        # established by this principal's command — confirmed without a
        # false claim either way (R04-INV-04) — and the converged command
        # is still audited under this principal.
        assert response.status_code == 200
        assert response.json()["emergency_stop"]["active"] is True
        assert authoritative_stop(database)["active"] is True
        assert [e["details"]["action"] for e in authoritative_events(database)] == ["set"]


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


class TestR04REV01ConcurrentAdminOperations:
    """R04-REV-01 — commit uncertainty vs. concurrent administrative writes.

    The success contract is state-convergence WITH attribution: HTTP 200
    certifies the requested state is durably established by THIS
    principal's command (an idempotent re-issuance is equivalent). A
    same-value state written by a DIFFERENT administrator must not be
    reported as this command's success, and must not produce a
    mis-attributed audit event.
    """

    def test_rev01_c_same_value_by_other_admin_is_not_confirmed(
        self, client, database, admin_token, second_admin_token
    ) -> None:
        # REV-01-C — A's commit fails while B independently committed the
        # SAME target state. The desired state IS in effect, but it is not
        # A's command: A must receive 503, not a false success.
        other_id = admin_agent_id(database, "ops-admin-2")
        with database.session() as setup_session:
            from commons_core.infrastructure.system_state import write_system_state

            write_system_state(
                setup_session,
                "emergency_stop",
                {"active": True, "set_at": "2026-10-30T00:00:00+00:00", "set_by": other_id},
            )
        with StateCommitFailureInjector(database) as injector:
            response = client.post(
                "/api/v1/admin/emergency-stop", headers=auth_headers(admin_token)
            )
        assert injector.fired
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "EMERGENCY_STOP_UNCONFIRMED"
        # The stop remains durably engaged (B's write) — safety preserved.
        state = authoritative_stop(database)
        assert state is not None and state["active"] is True
        assert state["set_by"] == other_id
        # No audit event mis-attributed to A (A's command is unconfirmed).
        assert authoritative_events(database) == []

    def test_rev01_d_opposite_value_by_other_admin_is_not_confirmed(
        self, client, database, admin_token, second_admin_token
    ) -> None:
        # REV-01-D — A requests ENGAGE, A's commit fails, while B already
        # committed the OPPOSITE state. A must receive 503 and the durable
        # state must remain B's release.
        other_id = admin_agent_id(database, "ops-admin-2")
        with database.session() as setup_session:
            from commons_core.infrastructure.system_state import write_system_state

            write_system_state(
                setup_session,
                "emergency_stop",
                {"active": False, "set_at": "2026-10-30T00:00:00+00:00", "set_by": other_id},
            )
        with StateCommitFailureInjector(database):
            response = client.post(
                "/api/v1/admin/emergency-stop", headers=auth_headers(admin_token)
            )
        assert response.status_code == 503
        state = authoritative_stop(database)
        assert state is not None and state["active"] is False
        assert state["set_by"] == other_id

    def test_rev01_e_competing_commands_controlled_ordering(
        self, client, database, admin_token, second_admin_token
    ) -> None:
        # REV-01-E — A engages (200); B releases (200); A re-engages but the
        # commit fails. Reconciliation observes B's release (opposite value,
        # different principal) -> 503; the final state is B's release.
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
        with StateCommitFailureInjector(database):
            rengaged = client.post(
                "/api/v1/admin/emergency-stop", headers=auth_headers(admin_token)
            )
        assert rengaged.status_code == 503
        state = authoritative_stop(database)
        assert state is not None and state["active"] is False
        # A's earlier engage + B's release are audited; A's unconfirmed
        # re-engage produced NO event (no mis-attribution).
        assert [e["details"]["action"] for e in authoritative_events(database)] == [
            "set",
            "clear",
        ]

    def test_rev01_f_reconciliation_read_unavailable(
        self, client, database, admin_token, monkeypatch
    ) -> None:
        # REV-01-F — the authoritative reconciliation read itself fails
        # (e.g. connection loss). The outcome stays uncertain and must be
        # reported as non-success, never guessed.
        import commons_core.infrastructure.system_state as system_state_module

        def _broken_reconciliation(_session):
            raise RuntimeError("injected reconciliation failure")

        monkeypatch.setattr(
            system_state_module, "_authoritative_stop_state", _broken_reconciliation
        )
        with StateCommitFailureInjector(database):
            response = client.post(
                "/api/v1/admin/emergency-stop", headers=auth_headers(admin_token)
            )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "EMERGENCY_STOP_UNCONFIRMED"
        assert "injected" not in response.text
        assert authoritative_stop(database) is None


class TestR04REV02WriteFlushFailures:
    """R04-REV-02 — write/flush failures inside the REQUIRED stage.

    A failure of the state-row INSERT/UPDATE or of ``session.flush()`` must
    take the same reconciled, non-success path as a commit failure: never a
    raw 500, never internal-detail leakage, never a session left in a
    broken state for subsequent requests.
    """

    def test_rev02_first_engage_insert_failure(self, client, database, admin_token) -> None:
        # Case 1 — initial state-row insertion fails.
        with StateWriteFailureInjector() as injector:
            response = client.post(
                "/api/v1/admin/emergency-stop", headers=auth_headers(admin_token)
            )
        assert injector.hits == 1
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "EMERGENCY_STOP_UNCONFIRMED"
        # No internal SQL/driver details in the public response.
        assert "injected" not in response.text
        assert "sqlalchemy" not in response.text.lower()
        # Database unchanged; no orphan audit events.
        assert authoritative_stop(database) is None
        assert authoritative_events(database) == []

    def test_rev02_re_engage_update_failure_preserves_state(
        self, client, database, admin_token
    ) -> None:
        # Case 2 — existing state-row UPDATE fails. The prior durable state
        # is preserved untouched (no partial write), and because the
        # re-issuing principal is the one who established it, the command
        # converges honestly: 200 with the AUTHORITATIVE (pre-failure)
        # state — set_at is not refreshed by a failed write.
        engaged = client.post("/api/v1/admin/emergency-stop", headers=auth_headers(admin_token))
        assert engaged.status_code == 200
        before = authoritative_stop(database)
        with StateWriteFailureInjector() as injector:
            refreshed = client.post(
                "/api/v1/admin/emergency-stop", headers=auth_headers(admin_token)
            )
        assert injector.hits == 1
        assert refreshed.status_code == 200
        assert refreshed.json()["emergency_stop"] == before  # authoritative state
        assert authoritative_stop(database) == before  # row untouched
        # The converged re-issuance is still audited.
        assert [e["details"]["action"] for e in authoritative_events(database)] == [
            "set",
            "set",
        ]

    def test_rev02_release_write_failure_symmetric(self, client, database, admin_token) -> None:
        # Symmetry — a failed RELEASE write is a non-success and leaves the
        # stop engaged (a failed release is not a successful release).
        engaged = client.post("/api/v1/admin/emergency-stop", headers=auth_headers(admin_token))
        assert engaged.status_code == 200
        with StateWriteFailureInjector():
            released = client.delete(
                "/api/v1/admin/emergency-stop", headers=auth_headers(admin_token)
            )
        assert released.status_code == 503
        assert released.json()["error"]["code"] == "EMERGENCY_STOP_UNCONFIRMED"
        state = authoritative_stop(database)
        assert state is not None and state["active"] is True

    def test_rev02_session_reusable_after_failed_flush(
        self, client, database, admin_token, second_admin_token
    ) -> None:
        # Case 5 — after a failed flush the (per-request) session is rolled
        # back cleanly: the very next control request succeeds and the row
        # is consistent (no lingering broken transaction state).
        with StateWriteFailureInjector():
            failed = client.post("/api/v1/admin/emergency-stop", headers=auth_headers(admin_token))
        assert failed.status_code == 503
        ok = client.post("/api/v1/admin/emergency-stop", headers=auth_headers(second_admin_token))
        assert ok.status_code == 200
        state = authoritative_stop(database)
        assert state is not None and state["active"] is True
        with Session(bind=database.engine) as fresh:
            rows = list(
                fresh.scalars(select(SystemState).where(SystemState.key == "emergency_stop"))
            )
        assert len(rows) == 1

    def test_rev02_write_failure_converges_when_state_already_established(
        self, client, database, admin_token
    ) -> None:
        # Case 6 — the write fails, but this principal's earlier command
        # already durably established the requested state: honest
        # convergence (idempotent re-issuance), 200, and the audit event
        # for the converged command is recorded.
        agent_id = admin_agent_id(database)
        with database.session() as setup_session:
            from commons_core.infrastructure.system_state import write_system_state

            write_system_state(
                setup_session,
                "emergency_stop",
                {"active": True, "set_at": "2026-10-30T00:00:00+00:00", "set_by": agent_id},
            )
        with StateWriteFailureInjector() as injector:
            response = client.post(
                "/api/v1/admin/emergency-stop", headers=auth_headers(admin_token)
            )
        assert injector.hits == 1
        assert response.status_code == 200
        assert response.json()["emergency_stop"]["active"] is True
        assert [e["details"]["action"] for e in authoritative_events(database)] == ["set"]

    def test_rev02_repeated_command_after_write_failure_stays_idempotent(
        self, client, database, admin_token
    ) -> None:
        # Repeated commands preserve documented idempotent behavior across
        # a write failure: fail (503) -> retry (200) -> repeat (200).
        with StateWriteFailureInjector():
            first = client.post("/api/v1/admin/emergency-stop", headers=auth_headers(admin_token))
        assert first.status_code == 503
        second = client.post("/api/v1/admin/emergency-stop", headers=auth_headers(admin_token))
        assert second.status_code == 200
        third = client.post("/api/v1/admin/emergency-stop", headers=auth_headers(admin_token))
        assert third.status_code == 200
        assert authoritative_stop(database)["active"] is True
        assert [e["details"]["action"] for e in authoritative_events(database)] == [
            "set",
            "set",
        ]

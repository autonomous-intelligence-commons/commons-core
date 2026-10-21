"""Regression tests: DB-enforced conditional cancellation (D21-01 / R02-A).

RV-2 (Stage 2.2 T0) reproduced deterministically — on the disposable
PostgreSQL instance — that ``cancel_task`` could overwrite a task another
transaction had already claimed or completed: the state check was an
application-side read of a possibly stale ORM object, and the subsequent
status write was not guarded by any atomic database state predicate.

These tests lock in the remediated contract:

* a cancellation succeeds ONLY through the atomic pending→cancelled
  transition (the same conditional-UPDATE pattern as ``claim_next``);
* a task that moved out of ``pending`` keeps its authoritative state —
  a stale cached ``pending`` object can never overwrite ``leased`` or
  ``succeeded``;
* the canceller receives the documented 409 ``InvalidStateError``
  ("Only pending tasks can be cancelled"), never a false success;
* the owner-only defensive check still precedes any state disclosure.

The stale-object interleaving is simulated WITHOUT threads (deterministic):
session A loads the task (pending) and detaches it; a second session
advances the authoritative state (claim/complete) and commits; a third
session re-attaches the STALE object (merge with load=False — the exact
identity-map rebind used by the RV-2 harness) and attempts the cancel.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from commons_core.agents.service import provision_agent
from commons_core.api.errors import InvalidStateError
from commons_core.database.enums import TaskStatus
from commons_core.database.models import Agent, ScheduledTask
from commons_core.infrastructure.time import as_utc
from commons_core.scheduler import service
from commons_core.security.authorization.errors import AuthorizationError

T0 = datetime(2030, 1, 1, tzinfo=UTC)


def _create_pending_task(session, agent) -> ScheduledTask:
    task, _created = service.create_task(
        session,
        agent,
        kind="noop",
        payload={"note": "cancel-race-regression"},
        run_at=T0,
        max_pending_tasks=1000,
        max_task_attempts=3,
    )
    return task


def _authoritative_row(database, task_id):
    """Fresh-session core read of the authoritative row (no ORM identity map)."""
    session = database.sessionmaker()
    try:
        return session.execute(
            select(
                ScheduledTask.id,
                ScheduledTask.status,
                ScheduledTask.attempts,
                ScheduledTask.finished_at,
                ScheduledTask.updated_at,
            ).where(ScheduledTask.id == task_id)
        ).first()
    finally:
        session.close()


class TestStaleObjectCannotOverwriteMovedTask:
    def test_stale_pending_object_cannot_overwrite_claimed_task(self, database, session) -> None:
        """RV2-03/RV2-04c core: after another transaction CLAIMED the task,
        a stale cached ``pending`` object must receive the 409 conflict and
        the authoritative leased state (attempts/lease) must survive."""
        agent = provision_agent(session, "agent-cancel-race")
        task = _create_pending_task(session, agent)
        session.commit()  # pending row is now committed
        task_id = str(task.id)

        # Session A loads the task (the API route's read) and detaches it.
        session_a = database.sessionmaker()
        try:
            stale = service.get_task(session_a, task_id)
            assert stale.status == TaskStatus.PENDING
            session_a.expunge(stale)  # detach with the pending snapshot
        finally:
            session_a.close()

        # Another transaction claims the task and commits (pending -> leased).
        session_b = database.sessionmaker()
        try:
            claimed = service.claim_next(session_b, T0, lease_seconds=30)
            assert claimed is not None and str(claimed.id) == task_id
            session_b.commit()
        finally:
            session_b.close()

        # The stale object re-binds (identity-map form of the TOCTOU window)
        # and the cancellation is attempted exactly as the route would.
        session_c = database.sessionmaker()
        try:
            merged = session_c.merge(stale, load=False)
            merged_agent = session_c.get(Agent, agent.id)  # owner in this session
            assert merged.status == TaskStatus.PENDING  # still the stale view
            with pytest.raises(InvalidStateError, match="Only pending tasks can be cancelled"):
                service.cancel_task(session_c, merged_agent, merged, now=T0)
            session_c.rollback()
        finally:
            session_c.close()

        row = _authoritative_row(database, task_id)
        assert row is not None
        assert row.status == TaskStatus.LEASED  # authoritative state survived
        assert row.attempts == 1
        assert row.finished_at is None

    def test_stale_pending_object_cannot_overwrite_succeeded_task(self, database, session) -> None:
        """RV2-03 terminal form: a task that has been CLAIMED and COMPLETED
        must never end as ``cancelled``; the stale cancel gets the 409 and
        the ``finished_at`` written by the worker survives."""
        agent = provision_agent(session, "agent-cancel-race-2")
        task = _create_pending_task(session, agent)
        session.commit()
        task_id = str(task.id)
        worker_finished_at = T0 + timedelta(seconds=1)

        session_a = database.sessionmaker()
        try:
            stale = service.get_task(session_a, task_id)
            session_a.expunge(stale)
        finally:
            session_a.close()

        # Another transaction claims AND completes the task (worker form).
        session_b = database.sessionmaker()
        try:
            claimed = service.claim_next(session_b, T0, lease_seconds=30)
            assert claimed is not None and str(claimed.id) == task_id
            claimed.status = TaskStatus.SUCCEEDED
            claimed.finished_at = worker_finished_at
            claimed.lease_expires_at = None
            session_b.commit()
        finally:
            session_b.close()

        session_c = database.sessionmaker()
        try:
            merged = session_c.merge(stale, load=False)
            merged_agent = session_c.get(Agent, agent.id)
            with pytest.raises(InvalidStateError, match="Only pending tasks can be cancelled"):
                service.cancel_task(session_c, merged_agent, merged, now=T0)
            session_c.rollback()
        finally:
            session_c.close()

        row = _authoritative_row(database, task_id)
        assert row is not None
        assert row.status == TaskStatus.SUCCEEDED
        assert row.attempts == 1
        assert row.finished_at is not None
        assert as_utc(row.finished_at) == worker_finished_at  # worker's timestamp survived


class TestCancelSuccessPath:
    def test_fresh_cancel_is_atomic_and_terminal(self, database, session) -> None:
        """The documented happy path: pending -> cancelled (one atomic
        transition), finished_at/updated_at set, and the task becomes
        unclaimable afterwards."""
        agent = provision_agent(session, "agent-cancel-ok")
        task = _create_pending_task(session, agent)
        session.commit()
        task_id = str(task.id)

        session_a = database.sessionmaker()
        try:
            fresh = service.get_task(session_a, task_id)
            owner_agent = session_a.get(Agent, agent.id)
            service.cancel_task(session_a, owner_agent, fresh, now=T0)
            session_a.commit()
        finally:
            session_a.close()

        row = _authoritative_row(database, task_id)
        assert row is not None
        assert row.status == TaskStatus.CANCELLED
        assert row.attempts == 0
        assert as_utc(row.finished_at) == T0
        assert as_utc(row.updated_at) == T0  # Core UPDATE sets it explicitly (no onupdate)

        # A cancelled task is no longer claimable (RV2-02 semantics preserved).
        session_b = database.sessionmaker()
        try:
            assert service.claim_next(session_b, T0, lease_seconds=30) is None
        finally:
            session_b.close()

    def test_cancel_owner_only_defensive_check_before_state_disclosure(
        self, database, session
    ) -> None:
        """A non-owner receives the 403 authorization error even when the
        task has moved out of pending — authorization is checked before any
        state disclosure (no cross-owner oracle through error types)."""
        owner = provision_agent(session, "agent-cancel-owner")
        other = provision_agent(session, "agent-cancel-other")
        task = _create_pending_task(session, owner)
        session.commit()
        task_id = str(task.id)

        # Another transaction completes the owner's task (succeeded).
        session_b = database.sessionmaker()
        try:
            claimed = service.claim_next(session_b, T0, lease_seconds=30)
            assert claimed is not None and str(claimed.id) == task_id
            claimed.status = TaskStatus.SUCCEEDED
            claimed.finished_at = T0
            claimed.lease_expires_at = None
            session_b.commit()
        finally:
            session_b.close()

        session_c = database.sessionmaker()
        try:
            task_row = service.get_task(session_c, task_id)
            other_agent = session_c.get(Agent, other.id)
            # Not InvalidStateError: the authorization guard fires FIRST.
            with pytest.raises(AuthorizationError, match="operation not authorized"):
                service.cancel_task(session_c, other_agent, task_row, now=T0)
            session_c.rollback()
        finally:
            session_c.close()

        row = _authoritative_row(database, task_id)
        assert row is not None
        assert row.status == TaskStatus.SUCCEEDED  # untouched by the rejected cancel

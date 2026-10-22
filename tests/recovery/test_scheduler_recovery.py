"""Recovery tests: durable scheduling across restarts (spec §22, §29).

- persistence: tasks survive a full engine/session teardown (new engine on
  the same SQLite file) and are processed by a fresh worker;
- lease expiry: expired leases return to pending WITHOUT a new attempt and
  are re-claimable (crash recovery, at-least-once);
- duplicate-processing protection: sequential workers execute each task
  exactly once (side-effect counter in system_state);
- graceful shutdown: the stop event halts the loop BETWEEN tasks — an
  in-flight task completes, the next is left pending.
"""

import threading
from datetime import UTC, datetime, timedelta

import pytest
from conftest import backend_database_url, reset_schema

from commons_core.agents.service import provision_agent
from commons_core.database.base import Base
from commons_core.database.db import Database
from commons_core.database.enums import TaskStatus
from commons_core.infrastructure.system_state import read_system_state, write_system_state
from commons_core.infrastructure.time import as_utc
from commons_core.scheduler import registry, service
from commons_core.scheduler.worker import run_worker

T0 = datetime(2030, 1, 1, tzinfo=UTC)


def _later(value, *, seconds: int) -> datetime:
    """``value + seconds`` normalized to aware UTC (SQLite naive round-trip)."""
    return as_utc(value) + timedelta(seconds=seconds)


@pytest.fixture()
def file_database(tmp_path) -> Database:
    """A backend-aware database that is fully torn down (simulated restart).

    SQLite: a per-test file (simulated process death via engine dispose).
    PostgreSQL (AIC_TEST_DATABASE_URL): the shared test database — the same
    URL across Database instances, which is exactly the multi-process /
    multi-engine topology the recovery suite must prove on PostgreSQL.
    """
    url = backend_database_url(tmp_path, "recovery.db")
    database = Database(url)
    reset_schema(database)
    yield database
    database.dispose()


def _provision(database: Database, handle: str):
    with database.session() as session:
        agent = provision_agent(session, handle)
    return agent


def _create(database: Database, agent, **overrides) -> str:
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
        return task.id


class TestPersistenceAcrossRestart:
    def test_tasks_survive_engine_teardown_and_process_afterwards(self, tmp_path) -> None:
        url = backend_database_url(tmp_path, "restart.db")
        first = Database(url)
        reset_schema(first)
        agent = _provision(first, "agent-restart")
        task_ids = [_create(first, agent) for _ in range(3)]
        first.dispose()  # simulated process death — nothing is committed in memory

        # NEW engine + sessions on the same file: tasks are still pending.
        second = Database(url)
        # create-only: the schema persists (dropping would erase the rows
        # this test proves survive the restart).
        Base.metadata.create_all(second.engine)
        with second.session() as session:
            for task_id in task_ids:
                task = service.get_task(session, task_id)
                assert task.status == TaskStatus.PENDING
                assert task.attempts == 0

        # a fresh worker drains the queue (injected clock — deterministic).
        processed = 0
        for _ in range(len(task_ids)):
            processed += run_worker(second.session, once=True, clock=lambda: T0)
        assert processed == 3
        with second.session() as session:
            for task_id in task_ids:
                task = service.get_task(session, task_id)
                assert task.status == TaskStatus.SUCCEEDED
        second.dispose()

    def test_replay_of_idempotency_key_after_restart(self, tmp_path) -> None:
        url = backend_database_url(tmp_path, "restart-idem.db")
        first = Database(url)
        reset_schema(first)
        agent = _provision(first, "agent-idem")
        with first.session() as session:
            task, _ = service.create_task(
                session,
                agent,
                kind="noop",
                payload={"note": "persisted"},
                idempotency_key="recover-1",
                max_pending_tasks=1000,
                max_task_attempts=3,
                now=T0,
            )
            original_id = task.id
        first.dispose()

        second = Database(url)
        # create-only: the schema persists (dropping would erase the rows
        # this test proves survive the restart).
        Base.metadata.create_all(second.engine)
        with second.session() as session:
            from commons_core.agents.service import get_agent_by_handle

            agent = get_agent_by_handle(session, "agent-idem")  # persisted identity
            assert agent is not None
            replayed, created = service.create_task(
                session,
                agent,
                kind="noop",
                payload={"note": "persisted"},
                idempotency_key="recover-1",
                max_pending_tasks=1000,
                max_task_attempts=3,
                now=T0,
            )
            assert created is False
            assert replayed.id == original_id
        second.dispose()


class TestLeaseExpiry:
    def test_expired_lease_returns_to_pending_without_new_attempt(self, file_database) -> None:
        agent = _provision(file_database, "agent-lease")
        task_id = _create(file_database, agent)

        with file_database.session() as session:
            claimed = service.claim_next(session, T0, lease_seconds=1)
            assert claimed is not None
            assert claimed.attempts == 1
            assert claimed.status == TaskStatus.LEASED
            lease_expires_at = claimed.lease_expires_at

        # worker dies; time advances past the lease
        later = _later(lease_expires_at, seconds=10)
        with file_database.session() as session:
            recovered = service.expire_leases(session, later)
            assert recovered == 1
            task = service.get_task(session, task_id)
            assert task.status == TaskStatus.PENDING
            assert task.attempts == 1  # unchanged — claim already counted it

        # re-claim succeeds and executes (at-least-once after crash)
        with file_database.session() as session:
            reclaimed = service.claim_next(session, later, 30)
            assert reclaimed is not None
            assert reclaimed.attempts == 2
            # R02-B1 worker pattern: the generation is read from the claim
            # INSIDE the transaction (post-claim expired object re-reads
            # the DB-side incremented value) and carried to the execute call.
            held_generation = int(reclaimed.lease_generation)
            service.execute_claimed(session, reclaimed, later, lease_generation=held_generation)
        with file_database.session() as session:
            task = service.get_task(session, task_id)
            assert task.status == TaskStatus.SUCCEEDED
            assert task.attempts == 2

    def test_worker_recovers_expired_lease_end_to_end(self, file_database) -> None:
        agent = _provision(file_database, "agent-crash")
        task_id = _create(file_database, agent)

        # simulate a crash: claim, then never execute.
        with file_database.session() as session:
            claimed = service.claim_next(session, T0, lease_seconds=1)
            assert claimed is not None

        # a NEW worker at a later time expires the lease, re-claims and
        # executes the task (at-least-once recovery).
        later = T0 + timedelta(seconds=30)
        assert run_worker(file_database.session, once=True, clock=lambda: later) == 1
        with file_database.session() as session:
            task = service.get_task(session, task_id)
            assert task.status == TaskStatus.SUCCEEDED
            assert task.attempts == 2  # one attempt lost to the crash

    def test_lease_expiry_after_final_attempt_fails_terminally(self, file_database) -> None:
        agent = _provision(file_database, "agent-final")
        task_id = _create(file_database, agent, max_attempts=1)

        with file_database.session() as session:
            claimed = service.claim_next(session, T0, lease_seconds=1)
            assert claimed.attempts == 1  # == max_attempts
        later = T0 + timedelta(seconds=30)
        with file_database.session() as session:
            recovered = service.expire_leases(session, later)
            assert recovered == 1
            task = service.get_task(session, task_id)
            assert task.status == TaskStatus.FAILED
            assert task.finished_at is not None
            assert task.last_error == "lease expired after the final attempt"


class TestDuplicateProcessingProtection:
    def test_sequential_workers_execute_each_task_exactly_once(self, file_database) -> None:
        counter_key = "test_execution_counter"

        def counting_handler(session, task) -> None:
            value = read_system_state(session, counter_key) or {"count": 0}
            value["count"] += 1
            write_system_state(session, counter_key, value)

        registry.register("test-counter", counting_handler)
        try:
            agent = _provision(file_database, "agent-dedupe")
            task_ids = [_create(file_database, agent, kind="test-counter") for _ in range(5)]
            # two SEQUENTIAL workers over the same queue: the first drains it,
            # the second finds nothing (no task is executed twice).
            processed = 0
            for _ in range(5):
                processed += run_worker(file_database.session, once=True, clock=lambda: T0)
            assert processed == 5
            second_shift = run_worker(file_database.session, once=True, clock=lambda: T0)
            assert second_shift == 0

            with file_database.session() as session:
                counter = read_system_state(session, counter_key)
                assert counter == {"count": 5}
                for task_id in task_ids:
                    task = service.get_task(session, task_id)
                    assert task.status == TaskStatus.SUCCEEDED
                    assert task.attempts == 1
        finally:
            registry.unregister("test-counter")


class TestGracefulShutdown:
    def test_stop_event_halts_between_tasks_in_flight_completes(self, file_database) -> None:
        stop_event = threading.Event()

        def stopping_handler(session, task) -> None:
            # side effect: request shutdown WHILE a task is in flight
            stop_event.set()

        registry.register("test-stop", stopping_handler)
        try:
            agent = _provision(file_database, "agent-stop")
            first_id = _create(file_database, agent, kind="test-stop")
            second_id = _create(file_database, agent, kind="noop")

            # loop mode (idle_sleep=0, injectable clock): the first task's
            # handler requests shutdown; the loop completes that task, then
            # stops BEFORE claiming the second (graceful shutdown semantics).
            processed = run_worker(
                file_database.session,
                once=False,
                max_tasks=10,
                idle_sleep=0.0,
                clock=lambda: T0,
                stop_event=stop_event,
            )
            assert processed == 1
            with file_database.session() as session:
                assert service.get_task(session, first_id).status == TaskStatus.SUCCEEDED
                second = service.get_task(session, second_id)
                assert second.status == TaskStatus.PENDING
                assert second.attempts == 0
        finally:
            registry.unregister("test-stop")

    def test_install_graceful_shutdown_in_main_thread(self) -> None:
        import signal

        from commons_core.scheduler.worker import install_graceful_shutdown

        stop_event = threading.Event()
        assert install_graceful_shutdown(stop_event) is True
        # simulate signal delivery by invoking the registered handler
        handler = signal.getsignal(signal.SIGTERM)
        handler(signal.SIGTERM, None)
        assert stop_event.is_set()

    def test_install_graceful_shutdown_skipped_off_main_thread(self) -> None:
        from commons_core.scheduler.worker import install_graceful_shutdown

        result: dict[str, bool] = {}

        def _run() -> None:
            event = threading.Event()
            result["installed"] = install_graceful_shutdown(event)

        thread = threading.Thread(target=_run)
        thread.start()
        thread.join()
        assert result["installed"] is False

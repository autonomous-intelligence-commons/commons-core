"""Worker execution-timeout enforcement (Phase 1 hardening, Task 4).

Proves ``AIC_TASK_TIMEOUT_SECONDS`` is now OPERATIONAL:
- a task completing within the budget succeeds normally;
- a task exceeding the budget follows the retry/backoff policy (returns to
  pending, attempt counted, ``last_error`` = exception class only);
- exhausted retries end in the terminal ``failed`` state (no infinite
  retries);
- a timed-out (abandoned) handler thread CANNOT use the session afterwards
  (execution guard raises ``TaskExecutionRevokedError``) — it can never
  commit a success or corrupt state after the failure was applied;
- ``run_worker`` refuses incoherent lease/timeout configurations;
- the full worker loop executes tasks with the timeout enabled;
- concurrent workers with timeouts never double-process a task;
- timeout errors never expose payload content;
- an engaged emergency stop prevents new task execution (timeouts or not).

Transaction-shape note: task creation, claiming and execution each use
COMMITTED transactions (mirroring the real worker: claim commits before the
execute transaction starts), so a timeout rollback can never discard the
task row itself — only partial handler writes.
"""

import threading
import time
from datetime import timedelta

import pytest

from commons_core.agents.service import provision_agent
from commons_core.database.enums import TaskStatus
from commons_core.infrastructure.system_state import (
    EMERGENCY_STOP_KEY,
    write_system_state,
)
from commons_core.infrastructure.time import as_utc, utc_now
from commons_core.scheduler import registry, service
from commons_core.scheduler.worker import run_worker


@pytest.fixture()
def agent(database):
    """A provisioned agent whose INSERT is committed before workers run."""
    with database.session() as session:
        created = provision_agent(session, "timeout-agent")
    return created


def _create_task(database, agent, *, kind: str, payload: dict | None = None) -> str:
    with database.session() as session:
        task, _ = service.create_task(
            session,
            agent,
            kind=kind,
            payload=payload or {},
            max_pending_tasks=1000,
            max_task_attempts=3,
        )
        return task.id


def _claim(database, task_id: str, *, advance: float = 0.0):
    """Claim in its own committed transaction (the real worker's phase 1).

    ``advance`` moves the claim's clock forward (past retry backoff),
    mirroring how the real worker simply waits and re-claims later.
    Returns ``(task_id, lease_generation)`` — the winning generation is
    captured INSIDE the claim transaction (R02-B1 worker Phase-1 pattern)
    and carried to the phase-2 execute.
    """
    with database.session() as session:
        claimed = service.claim_next(
            session, utc_now() + timedelta(seconds=advance), lease_seconds=60
        )
        assert claimed is not None
        assert claimed.id == task_id
        generation = int(claimed.lease_generation)  # read pre-commit (fresh post-claim read)
    return task_id, generation


def _execute(database, task_id: str, timeout_seconds: float, *, lease_generation: int):
    """Execute in its own transaction (the real worker's phase 2).

    ``lease_generation`` is the generation captured at claim time — the
    same contract as ``run_worker``'s Phase-1 → Phase-2 carry.
    """
    with database.session() as session:
        task = service.get_task(session, task_id)
        assert task is not None
        return service.execute_claimed(
            session, task, utc_now(), timeout_seconds, lease_generation=lease_generation
        )


def _status(database, task_id: str) -> TaskStatus:
    with database.session() as session:
        task = service.get_task(session, task_id)
        assert task is not None
        return task.status


class TestExecutionTimeout:
    def test_task_completing_within_budget_succeeds(self, database, agent) -> None:
        events: list[str] = []

        def slow_but_ok(session_arg, task) -> None:
            time.sleep(0.25)
            events.append("ran")

        registry.register("slow-ok", slow_but_ok)
        task_id = _create_task(database, agent, kind="slow-ok")
        task_id, generation = _claim(database, task_id)

        result = _execute(database, task_id, timeout_seconds=5.0, lease_generation=generation)
        assert result.status == TaskStatus.SUCCEEDED
        assert result.last_error is None
        assert events == ["ran"]

    def test_task_exceeding_budget_returns_to_pending_with_backoff(self, database, agent) -> None:
        registry.register("sleepy", lambda session_arg, task: time.sleep(3.0))
        task_id = _create_task(database, agent, kind="sleepy")
        task_id, generation = _claim(database, task_id)

        started = time.monotonic()
        before = utc_now()
        result = _execute(database, task_id, timeout_seconds=0.2, lease_generation=generation)
        elapsed = time.monotonic() - started
        assert elapsed < 2.0  # control returned at the timeout, not after the sleep

        assert result.status == TaskStatus.PENDING
        assert result.attempts == 1  # the claim counted as the attempt
        assert result.last_error is not None
        assert result.last_error.startswith("TaskTimeoutError")
        assert result.run_at is not None
        # as_utc: SQLite re-loads datetimes tz-naive (execute_claimed now
        # authoritative-reloads the returned object; R02-A session precedent —
        # minimal mechanical adaptation, no behavior change).
        assert as_utc(result.run_at) >= before  # now + backoff(attempts=1) is in the future

    def test_exhausted_retries_after_timeouts_end_failed(self, database, agent) -> None:
        registry.register("always-slow", lambda session_arg, task: time.sleep(3.0))
        task_id = _create_task(database, agent, kind="always-slow")

        statuses = []
        for _ in range(3):  # max_attempts = 3 (advance past each backoff)
            task_id, generation = _claim(database, task_id, advance=70)
            result = _execute(database, task_id, timeout_seconds=0.15, lease_generation=generation)
            statuses.append(result.status)
        assert statuses == [TaskStatus.PENDING, TaskStatus.PENDING, TaskStatus.FAILED]

        with database.session() as session:
            final = service.get_task(session, task_id)
            assert final is not None
            assert final.status == TaskStatus.FAILED  # terminal — no infinite retries
            assert final.finished_at is not None
            assert final.last_error is not None
            assert final.last_error.startswith("TaskTimeoutError")

    def test_abandoned_handler_cannot_use_session_after_timeout(self, database, agent) -> None:
        observed: list[str] = []

        def zombie(session_arg, task) -> None:
            time.sleep(0.5)  # exceed the 0.1s budget first
            try:
                session_arg.get(type(task), str(task.id))
                observed.append("session-access-survived")
            except Exception as exc:  # noqa: BLE001 — records the guard's verdict
                observed.append(type(exc).__name__)

        registry.register("zombie", zombie)
        task_id = _create_task(database, agent, kind="zombie")
        task_id, generation = _claim(database, task_id)

        result = _execute(database, task_id, timeout_seconds=0.1, lease_generation=generation)
        assert result.status == TaskStatus.PENDING  # timeout failure applied
        assert result.last_error is not None
        assert result.last_error.startswith("TaskTimeoutError")

        # The abandoned thread wakes, tries the session and is REJECTED by
        # the guard — it can never commit a success after the failure above.
        deadline = time.monotonic() + 5.0
        while not observed and time.monotonic() < deadline:
            time.sleep(0.05)
        assert observed == ["TaskExecutionRevokedError"]
        assert _status(database, task_id) == TaskStatus.PENDING

    def test_timeout_errors_never_expose_payload_content(self, database, agent) -> None:
        marker = "TOPSECRET-PAYLOAD-MARKER"
        registry.register("sleepy", lambda session_arg, task: time.sleep(3.0))
        task_id = _create_task(database, agent, kind="sleepy", payload={"note": marker})
        task_id, generation = _claim(database, task_id)
        result = _execute(database, task_id, timeout_seconds=0.15, lease_generation=generation)
        assert marker not in (result.last_error or "")

    def test_zero_timeout_keeps_legacy_inline_execution(self, database, agent) -> None:
        seen: dict[str, object] = {}

        def direct(session_arg, task) -> None:
            seen["thread"] = threading.current_thread() is threading.main_thread()

        registry.register("direct", direct)
        task_id = _create_task(database, agent, kind="direct")
        task_id, generation = _claim(database, task_id)
        result = _execute(database, task_id, timeout_seconds=0, lease_generation=generation)
        assert result.status == TaskStatus.SUCCEEDED
        assert seen["thread"] is True  # executed inline, no guard thread


class TestWorkerLoopCoherence:
    def test_worker_refuses_lease_not_exceeding_timeout(self, database) -> None:
        with pytest.raises(ValueError, match="lease_seconds must EXCEED"):
            run_worker(
                database.session,
                once=True,
                lease_seconds=30,
                task_timeout_seconds=30,
            )

    def test_worker_processes_task_with_timeout_enabled(self, database, agent) -> None:
        task_id = _create_task(database, agent, kind="noop")
        processed = run_worker(
            database.session,
            once=True,
            lease_seconds=60,
            task_timeout_seconds=3.0,
        )
        assert processed == 1
        assert _status(database, task_id) == TaskStatus.SUCCEEDED

    def test_concurrent_workers_never_double_process_with_timeouts(self, database, agent) -> None:
        task_id = _create_task(database, agent, kind="noop")
        counts: list[int] = []
        threads = [
            threading.Thread(
                target=lambda: counts.append(
                    run_worker(
                        database.session,
                        once=True,
                        lease_seconds=60,
                        task_timeout_seconds=5.0,
                    )
                )
            )
            for _ in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
            assert not thread.is_alive()

        assert sum(counts) == 1  # exactly one worker executed the task
        with database.session() as session:
            task = service.get_task(session, task_id)
            assert task is not None
            assert task.status == TaskStatus.SUCCEEDED
            assert task.attempts == 1

    def test_emergency_stop_prevents_execution_with_timeout_enabled(self, database, agent) -> None:
        with database.session() as session:
            write_system_state(
                session,
                EMERGENCY_STOP_KEY,
                {"active": True, "set_at": utc_now().isoformat(), "set_by": "infra"},
            )
        task_id = _create_task(database, agent, kind="noop")

        processed = run_worker(
            database.session,
            once=True,
            lease_seconds=60,
            task_timeout_seconds=3.0,
        )
        assert processed == 0  # worker idles under an engaged stop
        with database.session() as session:
            task = service.get_task(session, task_id)
            assert task is not None
            assert task.status == TaskStatus.PENDING
            assert task.attempts == 0

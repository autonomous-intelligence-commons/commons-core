"""Operational tests: worker lifetime semantics (long-running reliability).

Covers the Phase 1 worker-hardening contract:

- ``--loop`` (default ``max_tasks=None``) has NO lifetime task cap: more
  than 100 tasks are processed in one unlimited run, and the loop stays
  alive while idle (no tasks) or paused (emergency stop) until an explicit
  shutdown signal.
- An explicit positive ``--max-tasks`` still bounds a loop run (tests /
  scripted batches), and ``--once`` still processes at most one task.
- Invalid explicit task limits (0 / negative), the contradictory
  ``--once --max-tasks`` combination and a negative idle sleep are
  REJECTED (ValueError at the function level, argparse usage error at the
  CLI level) — never silently accepted.
- Graceful shutdown stays responsive DURING an idle wait (the stop event
  interrupts ``Event.wait`` — SIGTERM wakes the loop immediately, worst
  case is one in-flight task, not the idle sleep), and a task created
  while the worker is idling is picked up within the polling interval.
- The production Railway start command
  ``python scripts/run_worker.py --loop --lease-seconds 90`` runs as a
  REAL subprocess: it drains more than 100 tasks, then keeps running until
  SIGTERM, exits 0 and reports the unlimited task cap in its logs.

All loop-mode tests run against the configured backend (SQLite default,
PostgreSQL via AIC_TEST_DATABASE_URL) using the same fail-loud pattern as
the recovery suite. The subprocess test uses its own SQLite file (the
worker subprocess gets its environment via AIC_DATABASE_URL, mirroring
Railway variable injection).
"""

import inspect
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import backend_database_url, reset_schema
from sqlalchemy import func, select

from commons_core.agents.service import provision_agent
from commons_core.database.db import Database
from commons_core.database.enums import TaskStatus
from commons_core.database.models import ScheduledTask
from commons_core.infrastructure.system_state import set_emergency_stop
from commons_core.scheduler import registry, service
from commons_core.scheduler.worker import run_worker
from scripts.run_worker import main as run_worker_cli

T0 = datetime(2030, 1, 1, tzinfo=UTC)

#: Deliberately above the removed default 100-task lifetime cap.
UNLIMITED_RUN_TASKS = 105


@pytest.fixture()
def file_database(tmp_path) -> Database:
    """Backend-aware database, fully torn down per test (recovery pattern)."""
    url = backend_database_url(tmp_path, "worker-lifetime.db")
    database = Database(url)
    reset_schema(database)
    yield database
    database.dispose()


def _provision(database: Database, handle: str):
    with database.session() as session:
        agent = provision_agent(session, handle)
    return agent


def _create(database: Database, agent, **overrides) -> str:
    """Create a noop task; ``now`` defaults to the REAL clock (override for
    injected-clock tests — a T0 run_at is only eligible for a T0 worker)."""
    kwargs = {
        "kind": "noop",
        "payload": {},
        "max_pending_tasks": 2000,
        "max_task_attempts": 3,
    }
    kwargs.update(overrides)
    with database.session() as session:
        task, _ = service.create_task(session, agent, **kwargs)
        return task.id


def _status(database: Database, task_id: str) -> TaskStatus:
    with database.session() as session:
        task = service.get_task(session, task_id)
        return task.status


def _attempts(database: Database, task_id: str) -> int:
    with database.session() as session:
        task = service.get_task(session, task_id)
        return task.attempts


class TestUnlimitedLoopMode:
    def test_loop_default_processes_more_than_100_tasks(self, file_database) -> None:
        """Unlimited loop mode (max_tasks=None default) has no lifetime cap.

        Regression guard: the pre-fix default (100) stopped the loop after
        exactly 100 tasks, killing a continuously running worker.
        """
        stop_event = threading.Event()
        executed: list[str] = []

        def counting_handler(session, task) -> None:
            executed.append(task.id)
            if len(executed) >= UNLIMITED_RUN_TASKS:
                # request shutdown AFTER the last task — the only exit
                # condition in unlimited mode is an explicit stop signal
                stop_event.set()

        registry.register("lifetime-count", counting_handler)
        try:
            # 105 tasks across THREE agents: the per-agent in-queue quota
            # (MAX_PENDING_TASKS_PER_AGENT=100) is a SAFETY property that
            # must NOT be weakened — spreading owners respects it while the
            # RUN-level total still exceeds the removed 100-task lifetime cap.
            agents = [_provision(file_database, f"agent-unlimited-{index}") for index in range(3)]
            task_ids = [
                _create(file_database, agent, kind="lifetime-count", now=T0)
                for agent in agents
                for _ in range(UNLIMITED_RUN_TASKS // 3)
            ]
            assert len(task_ids) == UNLIMITED_RUN_TASKS  # 3 x 35
            # NO max_tasks argument: the default must be unlimited
            processed = run_worker(
                file_database.session,
                once=False,
                idle_sleep=0.0,
                clock=lambda: T0,
                stop_event=stop_event,
            )
        finally:
            registry.unregister("lifetime-count")

        assert processed == UNLIMITED_RUN_TASKS
        assert len(executed) == UNLIMITED_RUN_TASKS
        with file_database.session() as session:
            for task_id in task_ids:
                assert service.get_task(session, task_id).status == TaskStatus.SUCCEEDED

    def test_max_tasks_default_is_the_unlimited_sentinel(self) -> None:
        """Type-safe unlimited representation: ``None`` default on the API."""
        parameter = inspect.signature(run_worker).parameters["max_tasks"]
        assert parameter.default is None
        assert "None" in str(parameter.annotation)


class TestExplicitTaskCap:
    def test_explicit_max_tasks_bounds_loop_run(self, file_database) -> None:
        agent = _provision(file_database, "agent-capped")
        task_ids = [_create(file_database, agent, now=T0) for _ in range(5)]

        processed = run_worker(
            file_database.session,
            once=False,
            max_tasks=3,
            idle_sleep=0.0,
            clock=lambda: T0,
        )

        assert processed == 3
        with file_database.session() as session:
            for task_id in task_ids[:3]:
                assert service.get_task(session, task_id).status == TaskStatus.SUCCEEDED
            for task_id in task_ids[3:]:
                task = service.get_task(session, task_id)
                assert task.status == TaskStatus.PENDING
                assert task.attempts == 0

    def test_once_processes_at_most_one_task(self, file_database) -> None:
        agent = _provision(file_database, "agent-once")
        task_ids = [_create(file_database, agent, now=T0) for _ in range(3)]

        processed = run_worker(file_database.session, once=True, clock=lambda: T0)

        assert processed == 1
        with file_database.session() as session:
            assert service.get_task(session, task_ids[0]).status == TaskStatus.SUCCEEDED
            for task_id in task_ids[1:]:
                assert service.get_task(session, task_id).status == TaskStatus.PENDING


class TestWorkerArgumentGuards:
    """Invalid explicit limits are rejected, never silently accepted."""

    @pytest.mark.parametrize("bad_limit", [0, -1, -100])
    def test_non_positive_max_tasks_rejected(self, bad_limit) -> None:
        with pytest.raises(ValueError, match="max_tasks must be a positive integer"):
            run_worker(lambda: None, once=False, max_tasks=bad_limit)

    def test_max_tasks_combined_with_once_rejected(self) -> None:
        with pytest.raises(ValueError, match="loop-mode bound only"):
            run_worker(lambda: None, once=True, max_tasks=5)

    def test_negative_idle_sleep_rejected(self) -> None:
        with pytest.raises(ValueError, match="idle_sleep must be >= 0"):
            run_worker(lambda: None, once=False, idle_sleep=-0.5)


class TestIdleAndPauseLifetime:
    """Idle or paused, the worker keeps RUNNING until an explicit stop."""

    def test_idle_loop_thread_remains_alive_across_polls(self, file_database) -> None:
        stop_event = threading.Event()
        result: dict = {}

        def _loop() -> None:
            result["processed"] = run_worker(
                file_database.session, once=False, idle_sleep=0.01, stop_event=stop_event
            )

        worker = threading.Thread(target=_loop, daemon=True, name="idle-worker")
        worker.start()
        try:
            time.sleep(0.25)  # no tasks: pure idle polling
            assert worker.is_alive()  # idle must not terminate the worker
        finally:
            stop_event.set()
            worker.join(timeout=5)
        assert not worker.is_alive()
        assert result.get("processed") == 0

    def test_emergency_stop_keeps_loop_alive_and_blocks_claims(self, file_database) -> None:
        agent = _provision(file_database, "agent-estop")
        task_id = _create(file_database, agent)  # real clock: immediately eligible

        with file_database.session() as session:
            set_emergency_stop(session, active=True, principal="test")

        stop_event = threading.Event()
        result: dict = {}

        def _loop() -> None:
            result["processed"] = run_worker(
                file_database.session, once=False, idle_sleep=0.01, stop_event=stop_event
            )

        worker = threading.Thread(target=_loop, daemon=True, name="estop-worker")
        worker.start()
        try:
            time.sleep(0.25)  # many paused iterations — no claims, stays alive
            assert worker.is_alive()
            assert _status(file_database, task_id) == TaskStatus.PENDING
            assert _attempts(file_database, task_id) == 0
        finally:
            stop_event.set()
            worker.join(timeout=5)
        assert not worker.is_alive()
        assert result.get("processed") == 0
        assert _status(file_database, task_id) == TaskStatus.PENDING
        assert _attempts(file_database, task_id) == 0

    def test_shutdown_during_idle_wait_is_immediate(self, file_database) -> None:
        """The stop event interrupts the idle sleep — shutdown does not
        wait out a long polling interval (SIGTERM responsiveness)."""
        stop_event = threading.Event()
        timer = threading.Timer(0.15, stop_event.set)
        timer.start()
        try:
            started = time.monotonic()
            processed = run_worker(
                file_database.session,
                once=False,
                idle_sleep=30.0,  # far longer than the test tolerates
                stop_event=stop_event,
            )
            elapsed = time.monotonic() - started
        finally:
            timer.cancel()
        assert processed == 0
        assert elapsed < 5.0  # woke up from Event.wait(30) in ~0.15s

    def test_idle_worker_picks_up_new_task_within_poll_interval(self, file_database) -> None:
        """A task created while the worker is idling is picked up promptly
        (pickup latency is bounded by the idle sleep, not unbounded)."""
        agent = _provision(file_database, "agent-pickup")
        stop_event = threading.Event()
        result: dict = {}

        def _loop() -> None:
            result["processed"] = run_worker(
                file_database.session, once=False, idle_sleep=0.05, stop_event=stop_event
            )

        worker = threading.Thread(target=_loop, daemon=True, name="pickup-worker")
        worker.start()
        try:
            time.sleep(0.2)  # worker is idling (queue empty)
            task_id = _create(file_database, agent)  # task arrives while idling
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                if _status(file_database, task_id) == TaskStatus.SUCCEEDED:
                    break
                time.sleep(0.05)
            else:
                pytest.fail("idle worker did not pick up the new task within 10s")
        finally:
            stop_event.set()
            worker.join(timeout=5)
        assert not worker.is_alive()
        assert result.get("processed", 0) >= 1


class TestCliArgumentHandling:
    """CLI-level guards: argparse errors (exit code 2) with clean messages."""

    def test_rejects_zero_max_tasks(self, capsys) -> None:
        with pytest.raises(SystemExit) as excinfo:
            run_worker_cli(["--loop", "--max-tasks", "0"])
        assert excinfo.value.code == 2
        assert "--max-tasks must be a positive integer" in capsys.readouterr().err

    def test_rejects_negative_max_tasks(self, capsys) -> None:
        with pytest.raises(SystemExit) as excinfo:
            run_worker_cli(["--loop", "--max-tasks", "-3"])
        assert excinfo.value.code == 2
        assert "--max-tasks must be a positive integer" in capsys.readouterr().err

    def test_rejects_max_tasks_combined_with_once(self, capsys) -> None:
        with pytest.raises(SystemExit) as excinfo:
            run_worker_cli(["--once", "--max-tasks", "5"])
        assert excinfo.value.code == 2
        assert "--max-tasks bounds --loop runs only" in capsys.readouterr().err

    def test_rejects_negative_idle_sleep(self, capsys) -> None:
        with pytest.raises(SystemExit) as excinfo:
            run_worker_cli(["--loop", "--idle-sleep", "-1"])
        assert excinfo.value.code == 2
        assert "--idle-sleep must be >= 0" in capsys.readouterr().err

    def test_rejects_once_and_loop_together(self, capsys) -> None:
        with pytest.raises(SystemExit) as excinfo:
            run_worker_cli(["--once", "--loop"])
        assert excinfo.value.code == 2  # pre-existing mutually-exclusive guard

    @staticmethod
    def _cli_env(monkeypatch, url: str) -> None:
        monkeypatch.setenv("AIC_DATABASE_URL", url)
        monkeypatch.setenv("AIC_LOG_LEVEL", "WARNING")
        from commons_core.config.settings import get_settings

        get_settings.cache_clear()
        return get_settings

    def test_cli_once_processes_one_task(self, tmp_path, monkeypatch) -> None:
        url = f"sqlite+pysqlite:///{tmp_path}/cli-once.db"
        database = Database(url)
        reset_schema(database)
        agent = _provision(database, "agent-cli-once")
        task_ids = [_create(database, agent) for _ in range(3)]

        get_settings = self._cli_env(monkeypatch, url)
        try:
            assert run_worker_cli(["--once"]) == 0
        finally:
            get_settings.cache_clear()

        assert _status(database, task_ids[0]) == TaskStatus.SUCCEEDED
        for task_id in task_ids[1:]:
            assert _status(database, task_id) == TaskStatus.PENDING
        database.dispose()

    def test_cli_loop_with_explicit_max_tasks(self, tmp_path, monkeypatch) -> None:
        url = f"sqlite+pysqlite:///{tmp_path}/cli-capped.db"
        database = Database(url)
        reset_schema(database)
        agent = _provision(database, "agent-cli-capped")
        task_ids = [_create(database, agent) for _ in range(5)]

        get_settings = self._cli_env(monkeypatch, url)
        saved_handlers = {
            signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)
        }
        try:
            assert run_worker_cli(["--loop", "--max-tasks", "3", "--idle-sleep", "0.01"]) == 0
        finally:
            get_settings.cache_clear()
            for signum, handler in saved_handlers.items():
                signal.signal(signum, handler)  # restore pytest's handlers

        for task_id in task_ids[:3]:
            assert _status(database, task_id) == TaskStatus.SUCCEEDED
        for task_id in task_ids[3:]:
            assert _status(database, task_id) == TaskStatus.PENDING
        database.dispose()


class TestRailwayCommandEndToEnd:
    """The EXACT production start command, run as a real subprocess.

    Mirrors the Railway worker service: environment injected via
    ``AIC_DATABASE_URL`` (its own SQLite file — independent of the test
    backend), start command
    ``python scripts/run_worker.py --loop --lease-seconds 90``, then
    SIGTERM. Proves: unlimited mode drains >100 tasks, keeps running while
    idle afterwards, shuts down gracefully (exit 0) and reports the
    unlimited cap in its startup log.
    """

    def test_railway_worker_command_runs_unlimited_until_sigterm(self, tmp_path) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        url = f"sqlite+pysqlite:///{tmp_path}/railway-worker.db"

        database = Database(url)
        reset_schema(database)
        # 105 tasks across three agents (per-agent in-queue quota is 100 —
        # a safety limit this test must not weaken)
        agents = [_provision(database, f"agent-railway-{index}") for index in range(3)]
        task_ids = [
            _create(database, agent) for agent in agents for _ in range(UNLIMITED_RUN_TASKS // 3)
        ]
        assert len(task_ids) == UNLIMITED_RUN_TASKS  # 3 x 35

        env = {**os.environ, "AIC_DATABASE_URL": url, "AIC_LOG_LEVEL": "INFO"}
        process = subprocess.Popen(  # noqa: S603 — fixed argv, no shell
            [
                sys.executable,
                str(repo_root / "scripts" / "run_worker.py"),
                "--loop",
                "--lease-seconds",
                "90",
            ],
            cwd=str(repo_root),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 60.0
            succeeded = 0
            while time.monotonic() < deadline:
                with database.session() as session:
                    succeeded = session.scalar(
                        select(func.count())
                        .select_from(ScheduledTask)
                        .where(ScheduledTask.status == TaskStatus.SUCCEEDED)
                    )
                if succeeded and succeeded >= UNLIMITED_RUN_TASKS:
                    break
                if process.poll() is not None:  # died early — lifetime limit bug
                    stderr = process.stderr.read() if process.stderr else ""
                    pytest.fail(
                        f"worker exited after {succeeded} tasks (lifetime cap bug)\n{stderr}"
                    )
                time.sleep(0.2)
            else:
                pytest.fail(
                    f"worker did not drain {UNLIMITED_RUN_TASKS} tasks in 60s "
                    f"(succeeded={succeeded})"
                )

            # drained: the process must STILL be running (unlimited mode,
            # idle polling) — then SIGTERM must stop it gracefully
            time.sleep(0.6)
            assert process.poll() is None, "worker exited after draining — not unlimited"
            process.send_signal(signal.SIGTERM)
            returncode = process.wait(timeout=20)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
        stderr = process.stderr.read() if process.stderr else ""

        assert returncode == 0  # graceful exit, no crash
        assert "task_cap=unlimited" in stderr  # startup log proves the config
        assert f"worker finished processed={UNLIMITED_RUN_TASKS}" in stderr
        with database.session() as session:
            for task_id in task_ids:
                assert service.get_task(session, task_id).status == TaskStatus.SUCCEEDED
        database.dispose()

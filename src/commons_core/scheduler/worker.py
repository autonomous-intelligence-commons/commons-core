"""Durable scheduler worker loop (spec §16).

``run_worker`` claims and executes tasks OUTSIDE the API process (see
``scripts/run_worker.py``). Per iteration (one short transaction per phase):

1. Read the emergency-stop state. While ENGAGED the worker refuses to claim
   or execute ANY new work (spec §16/§21): it logs ONE line
   ``"emergency stop active — worker idle"`` (once per engagement episode),
   keeps the loop alive in loop mode, and exits cleanly in ``--once`` mode.
   In-flight behavior: the stop is only checked BETWEEN iterations, so a
   task claimed before the stop completes its execution (documented
   graceful-shutdown semantics — the emergency stop halts NEW work).
2. ``expire_leases`` — recover leases lost to crashed workers.
3. ``claim_next`` — lease the oldest eligible pending task (committed
   before execution: a crash mid-execution lets the lease expire and the
   task retry — AT-LEAST-ONCE execution, no exactly-once claim).
4. ``execute_claimed`` — dispatch to the deterministic registered handler,
   bounded by ``task_timeout_seconds`` of wall-clock time when > 0 (Phase 1
   hardening: the handler runs on a guard-wrapped thread; a timeout is a
   normal failure → retry/backoff; an abandoned handler thread can never
   touch the session again — see scheduler.service for the honest limits).

The scheduler OFFERS execution opportunities only: handlers are
deterministic, message-free and never require agents to produce messages
(spec §16 — future AI decision-making remains independent of scheduling).

Lease/timeout coherence: when ``task_timeout_seconds > 0`` the worker
REFUSES to start unless ``lease_seconds > task_timeout_seconds`` — a
stuck-but-revoked handler must not outlive its lease and risk a second
concurrent claim of the same task.

Graceful shutdown: SIGTERM/SIGINT (main thread, loop mode) set a stop flag;
the loop checks the flag only BETWEEN iterations, so an in-flight task
completes before the worker exits. ``once=True`` processes at most one task
— the mode used by tests and scripted operation. ``max_tasks`` bounds a
loop run. Idle iterations sleep ``idle_sleep`` seconds (interruptibly via
the stop event when provided).

All timestamps come from an injectable ``clock`` callable (tests advance
time deterministically — no real sleeps).
"""

import logging
import signal
import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime

from sqlalchemy.orm import Session

from commons_core.infrastructure.system_state import emergency_stop_active
from commons_core.infrastructure.time import as_utc, utc_now
from commons_core.scheduler import service

_LOGGER = logging.getLogger(__name__)

#: Type of a zero-argument session-context factory (e.g. ``Database.session``).
SessionFactory = Callable[[], AbstractContextManager[Session]]

#: Type of an injectable clock (returns timezone-aware UTC datetimes).
Clock = Callable[[], datetime]


def install_graceful_shutdown(stop_event: threading.Event) -> bool:
    """Install SIGTERM/SIGINT handlers that set ``stop_event``.

    Returns ``True`` when installed (main thread only); background threads
    are silently skipped (signals are main-thread-only — callers running the
    worker in a thread should pass their own ``stop_event``).
    """
    if threading.current_thread() is not threading.main_thread():
        return False

    def _handle_signal(signum, _frame) -> None:
        stop_event.set()

    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(signum, _handle_signal)
        except (ValueError, OSError):  # pragma: no cover - platform edge
            return False
    return True


def _idle_wait(idle_sleep: float, stop_event: threading.Event | None) -> None:
    """Sleep between idle iterations (interruptible via the stop event)."""
    if idle_sleep <= 0:
        return
    if stop_event is not None:
        stop_event.wait(idle_sleep)
    else:
        time.sleep(idle_sleep)


def run_worker(
    session_factory: SessionFactory,
    *,
    once: bool = False,
    max_tasks: int = 100,
    lease_seconds: int = 30,
    idle_sleep: float = 0.05,
    task_timeout_seconds: float = 0.0,
    clock: Clock | None = None,
    stop_event: threading.Event | None = None,
) -> int:
    """Run the worker loop; returns the number of tasks executed this run.

    ``session_factory`` is a zero-argument callable yielding a session
    context (``Database.session``). ``clock`` defaults to real UTC time and
    is injectable for deterministic tests. ``once=True`` processes at most
    ONE task and exits (also cleanly under an active emergency stop).

    ``task_timeout_seconds`` (from ``AIC_TASK_TIMEOUT_SECONDS``): when > 0,
    each handler execution gets that wall-clock budget (guarded thread;
    timeout → retry/backoff). The worker refuses to start unless
    ``lease_seconds > task_timeout_seconds`` (coherent claim/timeout
    semantics — see the module docstring).
    """
    if max_tasks < 1:
        raise ValueError("max_tasks must be >= 1")
    if task_timeout_seconds > 0 and lease_seconds <= task_timeout_seconds:
        raise ValueError(
            "lease_seconds must EXCEED task_timeout_seconds "
            f"(lease_seconds={lease_seconds}, task_timeout_seconds={task_timeout_seconds}): "
            "a stuck-but-revoked handler must not outlive its lease, or a second "
            "worker could claim the same task while the abandoned thread lingers."
        )
    clock = clock or utc_now
    owned_stop_event: threading.Event | None = stop_event
    if owned_stop_event is None and not once:
        owned_stop_event = threading.Event()
        install_graceful_shutdown(owned_stop_event)

    processed = 0
    stop_logged = False
    while True:
        if owned_stop_event is not None and owned_stop_event.is_set():
            break

        now = as_utc(clock())
        claimed_id: str | None = None
        paused = False
        with session_factory() as session:
            paused = emergency_stop_active(session)
            if not paused:
                # Phase 1 (one transaction): recover expired leases, then
                # atomically lease the oldest eligible task. Both effects
                # commit with this context — a crash after this point leaves
                # the task LEASED with a future lease expiry (recoverable).
                service.expire_leases(session, now)
                task = service.claim_next(session, now, lease_seconds)
                claimed_id = task.id if task is not None else None

        if paused:
            # Spec §16/§21: no new claims, no execution while engaged.
            if not stop_logged:
                _LOGGER.info("emergency stop active — worker idle")
                stop_logged = True
            if once:
                break
            _idle_wait(idle_sleep, owned_stop_event)
            continue

        if claimed_id is None:
            if once:
                break
            _idle_wait(idle_sleep, owned_stop_event)
            continue

        # Phase 2 (separate transaction): execute the leased task. The stop
        # flag and emergency stop are NOT re-checked here — the task was
        # legitimately claimed and completes (graceful shutdown semantics).
        with session_factory() as session:
            task = service.get_task(session, claimed_id)
            if task is not None and task.status == service.TaskStatus.LEASED:
                service.execute_claimed(
                    session, task, as_utc(clock()), timeout_seconds=task_timeout_seconds
                )

        processed += 1
        if once or processed >= max_tasks:
            break
    return processed


__all__ = ["install_graceful_shutdown", "run_worker"]

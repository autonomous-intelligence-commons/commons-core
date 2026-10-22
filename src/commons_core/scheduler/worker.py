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
   task retry — AT-LEAST-ONCE execution, no exactly-once claim). The claim
   also atomically raises ``lease_generation``; the worker captures the
   winning generation inside the Phase-1 transaction and carries it to
   Phase 2 (R02-B1 lease fencing).
4. ``execute_claimed`` — dispatch to the deterministic registered handler,
   bounded by ``task_timeout_seconds`` of wall-clock time when > 0 (Phase 1
   hardening: the handler runs on a guard-wrapped thread; a timeout is a
   normal failure → retry/backoff; an abandoned handler thread can never
   touch the session again — see scheduler.service for the honest limits).
   Every worker-owned write re-validates the carried generation at the
   database boundary (conditional UPDATE + rowcount gate); a superseded
   lease is rejected before handler invocation, logged, not counted as an
   executed task and never retried under another worker's generation.

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
— the mode used by tests and scripted operation. ``max_tasks`` is an
OPTIONAL explicit bound for loop runs: ``None`` (the default) means NO
lifetime task cap — the loop runs until a shutdown signal (or fatal
failure), which is the intended semantics for a continuously running
service (Railway worker). An explicit ``max_tasks`` must be a positive
integer and is only valid in loop mode (``--once`` is already bounded to
one task); invalid values are rejected, never silently accepted.
Idle iterations sleep ``idle_sleep`` seconds (default 1.0 — one database
poll per second while idle; interruptible via the stop event when
provided, so shutdown responsiveness is preserved).

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
    max_tasks: int | None = None,
    lease_seconds: int = 30,
    idle_sleep: float = 1.0,
    task_timeout_seconds: float = 0.0,
    clock: Clock | None = None,
    stop_event: threading.Event | None = None,
) -> int:
    """Run the worker loop; returns the number of tasks executed this run.

    ``session_factory`` is a zero-argument callable yielding a session
    context (``Database.session``). ``clock`` defaults to real UTC time and
    is injectable for deterministic tests. ``once=True`` processes at most
    ONE task and exits (also cleanly under an active emergency stop).

    ``max_tasks`` — explicit lifetime cap for loop runs, or ``None`` for
    UNLIMITED (the default): loop mode then runs until a shutdown signal
    or fatal failure, staying alive (and idle) while no eligible tasks
    exist. Type-safe sentinel: ``None`` means "no cap"; any explicit value
    must be a positive integer (``>= 1``) and is valid in loop mode ONLY —
    combining it with ``once=True`` is rejected, because ``--once`` is
    already bounded to one task (fail-closed on contradictory config).

    ``task_timeout_seconds`` (from ``AIC_TASK_TIMEOUT_SECONDS``): when > 0,
    each handler execution gets that wall-clock budget (guarded thread;
    timeout → retry/backoff). The worker refuses to start unless
    ``lease_seconds > task_timeout_seconds`` (coherent claim/timeout
    semantics — see the module docstring).

    R02-B1 lease fencing: the worker verifies at startup that the database
    carries the ``lease_generation`` fencing column (fail-closed — a
    fencing-aware binary refuses to run against a 0001 schema, OG-1/DG-1),
    captures the claim generation INSIDE the Phase-1 transaction and carries
    it into Phase 2. A superseded claim (recovered and re-claimed by another
    worker while this one was between phases) is rejected BEFORE the handler
    is invoked and is logged, NOT counted as an executed task and NOT
    retried under another worker's generation (the new claim holder owns
    the task now; a retry here would cross-lease fight it).

    ``idle_sleep`` — seconds slept between idle polls (default ``1.0``:
    one database poll per second while idle keeps a continuously running
    service off a high-frequency query path). The wait is interruptible
    via ``stop_event`` (SIGTERM wakes the loop immediately), so worst-case
    shutdown latency is one in-flight task, not the idle sleep; worst-case
    NEW-task pickup latency is one ``idle_sleep``. Must be ``>= 0``.
    """
    if max_tasks is not None and max_tasks < 1:
        raise ValueError(
            f"max_tasks must be a positive integer (>= 1) or None for unlimited (got {max_tasks})"
        )
    if once and max_tasks is not None:
        raise ValueError(
            "max_tasks is a loop-mode bound only: once=True already processes "
            f"at most one task (got max_tasks={max_tasks})"
        )
    if idle_sleep < 0:
        raise ValueError(f"idle_sleep must be >= 0 (got {idle_sleep})")
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
    fencing_gate_passed = False
    while True:
        if owned_stop_event is not None and owned_stop_event.is_set():
            break

        now = as_utc(clock())
        claimed_id: str | None = None
        claim_generation: int | None = None
        paused = False
        with session_factory() as session:
            paused = emergency_stop_active(session)
            if not paused:
                if not fencing_gate_passed:
                    # Fail-closed startup gate (once per run): a fencing-aware
                    # worker refuses to run against a schema without the
                    # fencing column (OG-1/DG-1) — the error propagates and
                    # the worker process fails to start.
                    service.verify_fencing_schema(session)
                    fencing_gate_passed = True
                # Phase 1 (one transaction): recover expired leases, then
                # atomically lease the oldest eligible task. Both effects
                # commit with this context — a crash after this point leaves
                # the task LEASED with a future lease expiry (recoverable).
                service.expire_leases(session, now)
                task = service.claim_next(session, now, lease_seconds)
                if task is not None:
                    claimed_id = task.id
                    # Capture the winning generation INSIDE this transaction
                    # (the post-claim expired object re-reads the DB-side
                    # incremented value). It must NEVER be re-read from the
                    # database later: the worker's OWN claim epoch is carried
                    # from here to Phase 2 (a later "current" value could
                    # belong to somebody else's claim).
                    claim_generation = int(task.lease_generation)

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
        superseded = False
        with session_factory() as session:
            task = service.get_task(session, claimed_id)
            if task is None or task.status != service.TaskStatus.LEASED:
                # Fail-fast layer 1: the row is no longer leased (recovered
                # and possibly re-claimed between the phases). Log and skip —
                # never execute and never count.
                superseded = True
                _LOGGER.info(
                    "task %s is no longer leased at execution time "
                    "(status=%s) — discarding this claim",
                    claimed_id,
                    task.status if task is not None else "missing",
                )
            elif int(task.lease_generation) != int(claim_generation):
                # Fail-fast layer 2 (R02-B1): the row's current generation no
                # longer matches the generation THIS worker won at claim time
                # — another worker holds the lease now. The authoritative
                # fence is the CAS inside execute_claimed; this cheap stale
                # read only avoids a pointless handler dispatch.
                superseded = True
                _LOGGER.info(
                    "task %s lease superseded (held generation %s, current %s) "
                    "— discarding stale claim without execution",
                    claimed_id,
                    claim_generation,
                    task.lease_generation,
                )
            else:
                try:
                    service.execute_claimed(
                        session,
                        task,
                        as_utc(clock()),
                        timeout_seconds=task_timeout_seconds,
                        lease_generation=claim_generation,
                    )
                except service.LeaseSupersededError as exc:
                    # The database fence rejected this (now-stale) lease at
                    # the write boundary: log it, do NOT count it as an
                    # executed task, do NOT convert it to a task failure and
                    # do NOT retry — the new claim holder owns the task.
                    superseded = True
                    _LOGGER.info("task %s: %s", claimed_id, exc.message)

        if superseded:
            # Superseded work is not a successful execution — the loop
            # continues and the current (legitimate) holder proceeds.
            if once:
                break
            continue

        processed += 1
        if once:
            break
        if max_tasks is not None and processed >= max_tasks:
            break
    return processed


__all__ = ["install_graceful_shutdown", "run_worker"]

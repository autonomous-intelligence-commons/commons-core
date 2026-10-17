"""Durable autonomous scheduling service (spec §16).

Durable task queue semantics — HONEST guarantees:
- **At-least-once execution.** A task is claimed by exactly one worker at a
  time (conditional ``UPDATE ... WHERE status='pending'`` + rowcount check).
  On PostgreSQL the candidate scan additionally takes
  ``FOR UPDATE SKIP LOCKED`` so concurrent workers never block on each
  other's in-flight claims (short transactions; row locks released at
  commit). A worker crash after claim and before completion makes the task
  eligible AGAIN once its lease expires. **No exactly-once claim is made**
  (spec §16/§22 "protection against duplicate processing" is best-effort).
- **Retry limits.** ``attempts`` is incremented AT CLAIM TIME (claiming
  counts as an attempt). A failing handler with ``attempts >= max_attempts``
  transitions to the terminal ``failed`` state; otherwise the task returns
  to ``pending`` with ``run_at = now + backoff(attempts)`` (exponential
  ``2^attempts`` seconds, capped at 60) — infinite retries are impossible.
- **Execution timeouts** (Phase 1 hardening, Task 4). When
  ``timeout_seconds > 0`` the handler runs on a guard-wrapped worker thread
  and is given at most ``timeout_seconds`` of wall-clock time. A timeout is
  treated as a normal handler failure (retry/backoff policy, ``last_error``
  records only the exception class + "task execution exceeded Ns" — never
  payload content). HONEST LIMIT (documented in docs/ARCHITECTURE.md):
  Python cannot forcibly terminate arbitrary running code — after a timeout
  the abandoned thread stays alive until its next session call, which the
  execution guard turns into an immediate :class:`TaskExecutionRevokedError`;
  a handler stuck in a non-session C call (sleep/network) cannot be killed
  in-process and must be handled by lease expiry + process restart. Future
  AI execution requires independently cancellable execution environments.
  The database is protected regardless: the guard serializes session access
  so an abandoned handler can never commit or corrupt state after the main
  thread has applied the timeout failure.
- **Leases.** ``claim_next`` sets ``lease_expires_at = now + lease``.
  ``expire_leases`` returns expired leases to ``pending`` WITHOUT counting
  a new attempt (the attempt was already counted at claim); a task whose
  attempts are exhausted fails as lease-expired. Coherence rule (enforced
  by ``run_worker``): ``lease_seconds`` must EXCEED ``timeout_seconds`` so a
  timed-out-but-stuck handler cannot outlive its lease and get claimed by a
  second worker while still holding the guard.
- **Idempotency.** ``idempotency_key`` is globally unique: re-submitting
  the same key RETURNS the existing task (no duplicate) when the owner
  matches; a different owner using the same key gets 409
  ``IDEMPOTENCY_CONFLICT`` (the key is caller-chosen, so this is the same
  honest existence oracle as handle collisions).
- **Quotas.** Per-agent in-queue tasks ≤ 100 and global in-queue tasks ≤
  ``max_pending_tasks`` (in-queue = ``pending``/``leased``/``running`` —
  non-terminal, preventing unbounded queues). Exceeding → 429
  ``TASK_QUEUE_FULL`` + a ``quota_exceeded`` security event (limit only).
- **Emergency stop.** :func:`emergency_stop_active` reads the
  ``system_state`` row; the worker refuses to claim while it is engaged and
  write-side API routes reject new tasks with 503 ``SERVICE_PAUSED``.

Payload policy: payloads are NON-SENSITIVE control data, stored as plain
JSON (never envelope-encrypted BY DESIGN — they must never contain message,
memory or governance content). Validation: ≤ 4096 UTF-8 bytes of canonical
JSON (413), no keys named ``content``/``secret``/``token``/``credentials``
at any nesting depth ≤ 32 (422), nesting deeper than 32 levels is rejected
outright (422). ``last_error`` carries the exception class + message
truncated to 200 chars — handlers must never embed payload content in
exception messages, and payload data is structurally not included.

Privacy: tasks are operational records; telemetry counts them by status
only. Task payloads are echoed ONLY to the owning agent.
"""

import json
import threading
import uuid as uuidlib
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from commons_core.api.errors import (
    DomainError,
    InvalidPayloadError,
    InvalidStateError,
    PayloadTooLargeError,
)
from commons_core.database.enums import SecurityEventCategory, TaskStatus
from commons_core.database.models import Agent, ScheduledTask, new_uuid
from commons_core.infrastructure.time import as_utc, utc_now
from commons_core.scheduler import registry
from commons_core.security.authentication.identity import AuthenticatedAgent
from commons_core.security.authorization.errors import AuthorizationError
from commons_core.security.events import record_event

#: Maximum JSON byte size of a task payload (spec §16 "unbounded execution" guard).
TASK_PAYLOAD_MAX_BYTES = 4096
#: Per-agent in-queue task quota (pending/leased/running).
MAX_PENDING_TASKS_PER_AGENT = 100
#: Exponential backoff cap in seconds (``min(2**attempts, 60)``).
BACKOFF_CAP_SECONDS = 60
#: Payload nesting limit for the recursive forbidden-key scan.
_MAX_PAYLOAD_DEPTH = 32
#: Candidate rows examined per claim attempt (retry next row on conflict).
_CLAIM_CANDIDATES = 10
#: Truncation length for ``last_error`` (spec: 200 chars, no payload content).
_LAST_ERROR_MAX_CHARS = 200

#: Statuses that occupy the queue for quota purposes (non-terminal).
IN_QUEUE_STATUSES = (TaskStatus.PENDING, TaskStatus.LEASED, TaskStatus.RUNNING)

#: Payload key names that indicate sensitive content (spec §16 policy).
FORBIDDEN_PAYLOAD_KEYS = frozenset({"content", "secret", "token", "credentials"})


class UnknownTaskError(DomainError):
    """Task referenced by id does not exist (404)."""

    status_code = 404
    code = "UNKNOWN_TASK"

    def __init__(self) -> None:
        super().__init__("Task not found.")


class UnknownTaskKindError(DomainError):
    """Kind is not a registered handler name (422)."""

    status_code = 422
    code = "UNKNOWN_TASK_KIND"

    def __init__(self) -> None:
        super().__init__(
            "Task kind has no registered handler.",
            known_kinds=list(registry.known_kinds()),
        )


class TaskQueueFullError(DomainError):
    """Per-agent or global in-queue task quota exceeded (429, spec §16/§19)."""

    status_code = 429
    code = "TASK_QUEUE_FULL"

    def __init__(self, limit: int, scope: str) -> None:
        super().__init__(
            "Task queue quota exceeded; retry after pending tasks drain.",
            limit=limit,
            scope=scope,
        )


class TaskIdempotencyConflictError(DomainError):
    """Idempotency key already used by another agent (409)."""

    status_code = 409
    code = "IDEMPOTENCY_CONFLICT"

    def __init__(self) -> None:
        super().__init__("Idempotency key is already in use.")


class TaskAttemptsInvalidError(DomainError):
    """max_attempts outside the configured 1..max_task_attempts range (422)."""

    status_code = 422
    code = "INVALID_PAYLOAD"

    def __init__(self, limit: int) -> None:
        super().__init__(f"max_attempts must be between 1 and {limit}.", limit=limit)


class TaskTimeoutError(Exception):
    """A task handler exceeded its execution-time budget (internal, worker-side).

    Not a :class:`DomainError` — timeouts never surface over HTTP; they drive
    the retry/backoff policy inside :func:`execute_claimed`.
    """


class TaskExecutionRevokedError(Exception):
    """A timed-out (abandoned) handler tried to use the session after revocation.

    Raised by :class:`_ExecutionGuard` — bounds the database side effects of
    an abandoned handler thread so it can never commit or corrupt task state
    after the timeout failure has been applied.
    """


class PayloadNestingTooDeepError(DomainError):
    """Payload nesting exceeds the supported depth (422, recursion guard)."""

    status_code = 422
    code = "INVALID_PAYLOAD"

    def __init__(self, limit: int) -> None:
        super().__init__(f"Payload nesting must not exceed {limit} levels.", limit=limit)


def backoff_delay(attempts: int) -> int:
    """Exponential retry backoff in seconds: ``min(2**attempts, 60)``."""
    if attempts < 0:  # pragma: no cover - defensive
        raise ValueError("attempts must be >= 0")
    return min(2**attempts, BACKOFF_CAP_SECONDS)


def find_forbidden_payload_keys(value: object, depth: int = 0) -> list[str]:
    """Forbidden payload key names found at any nesting depth (≤ 32 levels).

    Raises :class:`PayloadNestingTooDeepError` for deeper structures — the
    depth guard also bounds the recursion. Returns matched key NAMES only
    (the fixed forbidden vocabulary), never values.
    """
    if depth > _MAX_PAYLOAD_DEPTH:
        raise PayloadNestingTooDeepError(_MAX_PAYLOAD_DEPTH)
    matched: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in FORBIDDEN_PAYLOAD_KEYS:
                matched.append(str(key))
            matched.extend(find_forbidden_payload_keys(item, depth + 1))
    elif isinstance(value, list):
        for item in value:
            matched.extend(find_forbidden_payload_keys(item, depth + 1))
    return matched


def _payload_size_bytes(payload: dict) -> int:
    """UTF-8 byte length of the canonical JSON serialization of the payload."""
    return len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))


def _resolve_uuid(ref: str) -> str | None:
    try:
        return str(uuidlib.UUID(ref))
    except (ValueError, AttributeError, TypeError):
        return None


def get_task(session: Session, task_id: str) -> ScheduledTask | None:
    """Look up a task by id (UUID only)."""
    task_uuid = _resolve_uuid(task_id)
    if task_uuid is None:
        return None
    return session.get(ScheduledTask, task_uuid)


def tasks_for_agent(session: Session, agent_id: str, status: str | None = None) -> list:
    """The agent's tasks, newest first, optionally filtered by status."""
    stmt = (
        select(ScheduledTask)
        .where(ScheduledTask.agent_id == agent_id)
        .order_by(ScheduledTask.created_at.desc(), ScheduledTask.id)
    )
    if status is not None:
        stmt = stmt.where(ScheduledTask.status == TaskStatus(status))
    return list(session.scalars(stmt))


def _in_queue_count(session: Session, agent_id: str | None = None) -> int:
    stmt = (
        select(func.count())
        .select_from(ScheduledTask)
        .where(ScheduledTask.status.in_(IN_QUEUE_STATUSES))
    )
    if agent_id is not None:
        stmt = stmt.where(ScheduledTask.agent_id == agent_id)
    return int(session.scalar(stmt) or 0)


def _check_queue_quota(session: Session, owner_id: str, max_pending_tasks: int) -> None:
    """Enforce per-agent and global in-queue quotas (429 + event)."""
    agent_count = _in_queue_count(session, owner_id)
    if agent_count >= MAX_PENDING_TASKS_PER_AGENT:
        record_event(
            session,
            SecurityEventCategory.QUOTA_EXCEEDED,
            owner_id,
            {"limit": MAX_PENDING_TASKS_PER_AGENT, "scope": "agent_pending_tasks"},
        )
        raise TaskQueueFullError(MAX_PENDING_TASKS_PER_AGENT, "agent_pending_tasks")
    global_count = _in_queue_count(session)
    if global_count >= max_pending_tasks:
        record_event(
            session,
            SecurityEventCategory.QUOTA_EXCEEDED,
            owner_id,
            {"limit": max_pending_tasks, "scope": "global_pending_tasks"},
        )
        raise TaskQueueFullError(max_pending_tasks, "global_pending_tasks")


def create_task(
    session: Session,
    owner: Agent | AuthenticatedAgent,
    *,
    kind: str,
    payload: dict,
    run_at: datetime | None = None,
    idempotency_key: str | None = None,
    max_attempts: int | None = None,
    now: datetime | None = None,
    max_pending_tasks: int,
    max_task_attempts: int,
) -> tuple[ScheduledTask, bool]:
    """Create (or idempotently return) a task; returns ``(task, created)``.

    Validation order: kind registry → payload forbidden keys/nesting →
    payload size → max_attempts range → idempotency replay → queue quotas →
    insert. The idempotent replay path returns the EXISTING task with
    ``created=False`` (HTTP 200, no duplicate row).
    """
    if registry.get_handler(kind) is None:
        raise UnknownTaskKindError()

    forbidden = find_forbidden_payload_keys(payload)
    if forbidden:
        raise InvalidPayloadError(
            "Task payload must not contain keys named "
            f"{sorted(FORBIDDEN_PAYLOAD_KEYS)} (non-sensitive control data only)."
        )
    size_bytes = _payload_size_bytes(payload)
    if size_bytes > TASK_PAYLOAD_MAX_BYTES:
        raise PayloadTooLargeError(size_bytes, TASK_PAYLOAD_MAX_BYTES)
    if max_attempts is None:
        max_attempts = max_task_attempts
    elif not 1 <= max_attempts <= max_task_attempts:
        raise TaskAttemptsInvalidError(max_task_attempts)

    if idempotency_key is not None:
        existing = session.scalar(
            select(ScheduledTask).where(ScheduledTask.idempotency_key == idempotency_key)
        )
        if existing is not None:
            if existing.agent_id == owner.id:
                return existing, False
            raise TaskIdempotencyConflictError()

    _check_queue_quota(session, owner.id, max_pending_tasks)

    task = ScheduledTask(
        id=new_uuid(),
        kind=kind,
        payload=dict(payload),
        agent_id=owner.id,
        status=TaskStatus.PENDING,
        run_at=as_utc(run_at or now or utc_now()),
        idempotency_key=idempotency_key,
        attempts=0,
        max_attempts=max_attempts,
    )
    session.add(task)
    try:
        session.flush()
    except IntegrityError:
        # Race on the globally unique idempotency_key: resolve it honestly.
        session.rollback()
        existing = session.scalar(
            select(ScheduledTask).where(ScheduledTask.idempotency_key == idempotency_key)
        )
        if existing is not None and idempotency_key is not None:
            if existing.agent_id == owner.id:
                return existing, False
            raise TaskIdempotencyConflictError() from None
        raise  # pragma: no cover - only the unique key can collide
    return task, True


class _ExecutionGuard:
    """Revocable session proxy bounding an abandoned handler's DB effects.

    Wraps the real :class:`Session` passed to a task handler. Every method
    call first acquires ``_lock`` and checks ``_revoked``; after revocation
    every call raises :class:`TaskExecutionRevokedError` BEFORE touching the
    underlying session. :meth:`revoke` acquires the same lock, so the main
    thread waits for any in-flight handler statement (bounded, single
    statement) before setting the flag — after ``revoke()`` returns, no
    handler call is executing or can start, making the main thread's
    subsequent rollback + failure update race-free. Attribute access is
    delegated via ``__getattr__``; non-callable attributes pass through.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._revoked = threading.Event()
        self._lock = threading.Lock()

    def revoke(self) -> None:
        """Forbid all further session use by the guarded handler."""
        with self._lock:
            self._revoked.set()

    def __getattr__(self, name: str):
        attribute = getattr(self._session, name)
        if not callable(attribute):
            return attribute

        def _guarded(*args, **kwargs):
            with self._lock:
                if self._revoked.is_set():
                    raise TaskExecutionRevokedError(
                        "task execution was revoked after a timeout — "
                        "the handler may no longer use the session"
                    )
                return attribute(*args, **kwargs)

        return _guarded


class _HandlerOutcome:
    """Mutable holder for the handler thread's result (success or exception)."""

    __slots__ = ("failure", "revoked")

    def __init__(self) -> None:
        self.failure: BaseException | None = None
        self.revoked = False


def _run_handler_on_thread(
    handler,
    session: Session,
    task: ScheduledTask,
    timeout_seconds: float,
) -> _HandlerOutcome:
    """Run ``handler`` on a guarded daemon thread with a wall-clock budget.

    Returns the outcome holder once the thread finished OR the budget
    elapsed (in which case the guard is revoked and the still-running thread
    is abandoned — it dies on its next session call; see the honest-limit
    note in the module docstring). ``timeout_seconds <= 0`` executes the
    handler inline on the current thread (legacy path, no guard).
    """
    if timeout_seconds is None or timeout_seconds <= 0:
        outcome = _HandlerOutcome()
        try:
            handler(session, task)  # type: ignore[arg-type]  (direct execution)
        except Exception as exc:  # noqa: BLE001 — handler failures drive the retry policy
            outcome.failure = exc
        return outcome

    guard = _ExecutionGuard(session)
    outcome = _HandlerOutcome()

    def _target() -> None:
        try:
            handler(guard, task)  # type: ignore[arg-type]  (guard proxies Session)
        except TaskExecutionRevokedError:
            outcome.revoked = True  # abandoned thread exiting — outcome ignored
        except Exception as exc:  # noqa: BLE001 — handler failures drive the retry policy
            if not guard._revoked.is_set():  # noqa: SLF001 — post-revocation noise is ignored
                outcome.failure = exc

    thread = threading.Thread(target=_target, name=f"task-handler-{task.id}", daemon=True)
    thread.start()
    thread.join(timeout_seconds)
    if thread.is_alive():
        # Timeout: forbid further session use, abandon the thread. The DB is
        # safe — the guard turns any later handler call into an immediate
        # error, and only the MAIN thread writes task state (below).
        guard.revoke()
        outcome.failure = TaskTimeoutError(
            f"task execution exceeded {timeout_seconds}s (handler abandoned; "
            "guard revoked its session access)"
        )
    return outcome


def claim_next(
    session: Session,
    now: datetime,
    lease_seconds: int,
) -> ScheduledTask | None:
    """Atomically lease the oldest eligible pending task, or ``None``.

    Candidates: ``status='pending'`` AND ``run_at <= now``, ordered oldest
    first. Each candidate is claimed with a conditional
    ``UPDATE ... WHERE status='pending'``; a lost race (rowcount 0) moves to
    the next candidate. On PostgreSQL the candidate scan ALSO takes
    ``FOR UPDATE SKIP LOCKED`` — concurrent workers skip rows another worker
    is already claiming instead of blocking on its row lock (short
    transactions keep lock hold times minimal). Claiming increments
    ``attempts`` (it counts as an attempt) and sets ``lease_expires_at``.
    """
    now = as_utc(now)
    if lease_seconds < 1:
        raise ValueError("lease_seconds must be >= 1")
    stmt = (
        select(ScheduledTask)
        .where(ScheduledTask.status == TaskStatus.PENDING, ScheduledTask.run_at <= now)
        .order_by(ScheduledTask.run_at, ScheduledTask.created_at, ScheduledTask.id)
        .limit(_CLAIM_CANDIDATES)
    )
    bind = session.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    candidates = list(session.scalars(stmt))
    lease_until = now + timedelta(seconds=lease_seconds)
    for candidate in candidates:
        result = session.execute(
            update(ScheduledTask)
            .where(ScheduledTask.id == candidate.id, ScheduledTask.status == TaskStatus.PENDING)
            .values(
                status=TaskStatus.LEASED,
                lease_expires_at=lease_until,
                attempts=ScheduledTask.attempts + 1,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 1:
            session.expire(candidate)
            return candidate
    return None


def execute_claimed(
    session: Session,
    task: ScheduledTask,
    now: datetime,
    timeout_seconds: float = 0.0,
) -> ScheduledTask:
    """Execute a leased task through its registered handler (spec §16).

    Marks the task ``running`` (informational, intra-transaction), dispatches
    to the registry handler — with a wall-clock execution budget of
    ``timeout_seconds`` when > 0 (guarded worker thread; see
    :func:`_run_handler_on_thread`) — then: success → ``succeeded``
    (+``finished_at``); failure (including timeout) → ``failed`` (terminal)
    when ``attempts >= max_attempts``, else back to ``pending`` with
    ``run_at = now + backoff(attempts)``. On failure the transaction is
    rolled back FIRST to discard any partial handler writes, then the
    failure state is applied — an abandoned (timed-out) handler thread can
    never apply state afterwards because its session access is revoked.
    ``last_error`` stores the exception class + message, truncated to 200
    chars — never payload data.
    """
    now = as_utc(now)
    if task.status != TaskStatus.LEASED:
        raise InvalidStateError(f"Only leased tasks can be executed (status={task.status!s}).")
    task.status = TaskStatus.RUNNING
    session.flush()

    handler = registry.get_handler(task.kind)
    if handler is None:
        outcome = _HandlerOutcome()
        outcome.failure = RuntimeError(f"no handler registered for kind {task.kind!r}")
    else:
        outcome = _run_handler_on_thread(handler, session, task, timeout_seconds)
    failure = outcome.failure

    if failure is not None:
        # Discard partial handler writes before applying the failure state.
        session.rollback()
        session.expire(task)  # re-read authoritative claim state after rollback
        task.status = TaskStatus.LEASED  # restored intra-transaction marker

    if failure is None:
        task.status = TaskStatus.SUCCEEDED
        task.finished_at = now
        task.lease_expires_at = None
    else:
        error_text = f"{type(failure).__name__}: {failure}"[:_LAST_ERROR_MAX_CHARS]
        task.last_error = error_text
        task.lease_expires_at = None
        if task.attempts >= task.max_attempts:
            task.status = TaskStatus.FAILED
            task.finished_at = now
        else:
            task.status = TaskStatus.PENDING
            task.run_at = now + timedelta(seconds=backoff_delay(task.attempts))
    session.flush()
    return task


def expire_leases(session: Session, now: datetime) -> int:
    """Recover expired leases; returns the number of recovered tasks.

    A leased task whose ``lease_expires_at < now`` returns to ``pending``
    WITHOUT a new attempt (the attempt was counted at claim). A task whose
    attempts are exhausted fails as lease-expired (terminal, static error
    message — no payload data).
    """
    now = as_utc(now)
    expired = list(
        session.scalars(
            select(ScheduledTask).where(
                ScheduledTask.status == TaskStatus.LEASED,
                ScheduledTask.lease_expires_at < now,
            )
        )
    )
    for task in expired:
        task.lease_expires_at = None
        if task.attempts >= task.max_attempts:
            task.status = TaskStatus.FAILED
            task.finished_at = now
            task.last_error = "lease expired after the final attempt"
        else:
            task.status = TaskStatus.PENDING
    session.flush()
    return len(expired)


def cancel_task(
    session: Session,
    owner: Agent | AuthenticatedAgent,
    task: ScheduledTask,
    now: datetime | None = None,
) -> ScheduledTask:
    """Cancel a task (owner-only; only ``pending`` tasks are cancellable).

    Running/leased or terminal tasks → 409 ``INVALID_STATE`` (a leased task
    may be mid-execution; a terminal task already finished). Non-owners get
    a 403 authorization error (task ids are unguessable UUIDs — the same
    existence-oracle trade-off as messages, documented).
    """
    if task.agent_id != owner.id:
        # Defensive re-check; routes record the authz_denial event first.
        raise AuthorizationError(f"task:cancel:{task.id}")
    if task.status != TaskStatus.PENDING:
        raise InvalidStateError(
            f"Only pending tasks can be cancelled (current status={task.status!s})."
        )
    task.status = TaskStatus.CANCELLED
    task.finished_at = as_utc(now or utc_now())
    session.flush()
    return task

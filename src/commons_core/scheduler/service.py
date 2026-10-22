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
- **Lease fencing (R02-B1, Phase-1 database-state fencing).** Every claim
  atomically increments ``scheduled_tasks.lease_generation`` (DB-side,
  inside the claim's conditional UPDATE). Worker-owned transitions
  (execution-start / success / failure / retry) are conditional UPDATEs that
  must present the generation held at claim time — a superseded lease
  context is rejected BY THE DATABASE (rowcount gate →
  :class:`LeaseSupersededError`, zero bytes written; the stale handler is
  never invoked). Lease recovery (``expire_leases``) is a system-owned
  transition authorized by authoritative database state only (status +
  expiry + attempt accounting) and never touches the generation. Fencing is
  freshness, NOT identity: a matching current generation does not
  establish worker identity (threat-model actor 3, design review §2.5) —
  external-effect gates (R02-B2) must not trust caller-asserted generations.
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


class LeaseSupersededError(DomainError):
    """A stale lease context was rejected by the database fencing gate (409).

    R02-B1 (Phase-1 database-state fencing): raised when a worker-owned
    state transition (execution-start / success / failure / retry) presents
    a claim ``lease_generation`` that no longer matches the row's current
    generation, or when the execution-start row is no longer ``leased`` —
    in every case the conditional UPDATE matched zero rows, so NOTHING was
    written (zero bytes of a superseded worker's state reach the database).

    Worker-internal by design (never surfaced by API routes — only the
    worker loop and tests call the fenced transitions); ``status_code``
    exists only because :class:`DomainError` requires an HTTP mapping if the
    type ever leaks. PRIVACY RULE (extends ``last_error`` policy): the
    message carries ONLY the task id, the held generation and the
    authoritative status/generation counts — never payload content.
    """

    status_code = 409
    code = "LEASE_SUPERSEDED"

    def __init__(
        self,
        task_id: str,
        held_generation: int,
        current_status: object,
        current_generation: int,
    ) -> None:
        super().__init__(
            "Task lease was superseded before this write; the transition was "
            "rejected and nothing was written (stale worker context).",
            task_id=str(task_id),
            held_generation=int(held_generation),
            current_status=str(current_status),
            current_generation=int(current_generation),
        )


class FencingSchemaError(RuntimeError):
    """The fencing column is missing — fencing-aware code refuses to run (OG-1).

    Raised by :func:`verify_fencing_schema` when ``scheduled_tasks`` lacks
    ``lease_generation`` (database at migration 0001 while a fencing-aware
    worker/binary is starting). Fail-closed: refusing to start is SAFER than
    running unfenced (the original R02-B vulnerability).
    """


def verify_fencing_schema(session: Session) -> None:
    """Fail-closed startup gate: ``scheduled_tasks.lease_generation`` must exist.

    A fencing-aware worker MUST NOT operate against a schema that lacks the
    fencing column: every worker-owned transition depends on the DB-side CAS
    predicate, and an unfenced run reopens the original R02-B vulnerability
    (stale workers writing task state unconditionally). Called once at worker
    startup (see ``run_worker``); also usable by deployment gates (OG-1/DG-1).

    Raises :class:`FencingSchemaError` when the column is missing.
    """
    from sqlalchemy import inspect as sa_inspect

    bind = session.get_bind()
    if bind is None:  # pragma: no cover - defensive
        raise FencingSchemaError("no database bind available for the fencing schema check")
    inspector = sa_inspect(bind)
    if not inspector.has_table(ScheduledTask.__tablename__):
        raise FencingSchemaError(
            "scheduled_tasks table not found — run migrations before starting fencing-aware workers"
        )
    columns = {c["name"] for c in inspector.get_columns(ScheduledTask.__tablename__)}
    if "lease_generation" not in columns:
        raise FencingSchemaError(
            "scheduled_tasks.lease_generation is missing — the database is at "
            "migration 0001 while this build requires 0002 (lease fencing). "
            "Run `alembic upgrade head` BEFORE starting workers; refusing to "
            "run unfenced (fail-closed, OG-1/DG-1)."
        )


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

    R02-B1 lease fencing: the same conditional UPDATE ALSO increments
    ``lease_generation`` on the database side
    (``lease_generation = lease_generation + 1`` — never a Python-side
    read-modify-write). Exactly one generation increment happens per
    SUCCESSFUL claim; losing claims increment nothing. The winning generation
    is readable from the returned object's ``lease_generation`` attribute —
    the caller MUST read it INSIDE this transaction (the returned object is
    expired, so the attribute re-reads the authoritative incremented value),
    and MUST carry that value to every subsequent worker-owned transition
    (see :func:`execute_claimed`). ``lease_generation`` is the claim
    ownership epoch: ``0`` means "never claimed by fencing-aware code";
    there is no authorized execution path at generation 0 (INV-9).
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
                lease_generation=ScheduledTask.lease_generation + 1,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 1:
            session.expire(candidate)
            # NOTE: the winning generation is intentionally NOT materialized
            # here — the caller reads ``candidate.lease_generation`` inside
            # this transaction (the expired object re-selects the
            # authoritative incremented value).
            return candidate
    return None


def execute_claimed(
    session: Session,
    task: ScheduledTask,
    now: datetime,
    timeout_seconds: float = 0.0,
    *,
    lease_generation: int,
) -> ScheduledTask:
    """Execute a leased task through its registered handler (spec §16).

    R02-B1 database-state fencing — EVERY worker-owned transition below is a
    conditional UPDATE whose predicate is evaluated by the DATABASE (the
    ``lease_generation`` keyword-only argument is REQUIRED and has no
    default: an internal caller cannot forget to carry fencing and end up
    half-fenced):

    1. Execution-start gate (leased→running): ``WHERE id=:id AND
       status='leased' AND lease_generation=:held`` — a superseded worker
       (its claim was recovered and re-claimed, raising the row's generation)
       fails here and the handler is NEVER invoked; nothing is written and
       :class:`LeaseSupersededError` is raised (fail-closed).
    2. Success / failure / retry completion: ``WHERE id=:id AND
       lease_generation=:held`` — the same-transaction running marker set by
       (1) keeps the row locked, so within one transaction this guards the
       failure path's post-rollback window (the handler failure first rolls
       the transaction back — releasing the row lock — and the recovery
       state is then applied under the generation predicate; a re-claim that
       happened in that window makes the CAS miss and raises
       :class:`LeaseSupersededError`, never an ordinary failure write).

    The handler runs with a wall-clock budget of ``timeout_seconds`` when
    > 0 (guarded worker thread; see :func:`_run_handler_on_thread`). On
    failure the transaction is rolled back FIRST to discard any partial
    handler writes, then the failure state is applied via the CAS UPDATE.
    ``last_error`` stores the exception class + message, truncated to 200
    chars — never payload data. A :class:`LeaseSupersededError` is NEVER
    converted into an ordinary task failure (an authorization loss is not a
    business failure) and never pollutes ``last_error``/``finished_at``.

    The Python-side ``status != LEASED`` pre-check (below) is kept as a
    fail-fast first layer — same double-layer pattern as ``cancel_task``:
    a quick Python rejection in front of the authoritative database gate.
    """
    now = as_utc(now)
    if task.status != TaskStatus.LEASED:
        raise InvalidStateError(f"Only leased tasks can be executed (status={task.status!s}).")

    # --- T2: execution-start gate (worker-owned CAS) ---------------------
    start_result = session.execute(
        update(ScheduledTask)
        .where(
            ScheduledTask.id == task.id,
            ScheduledTask.status == TaskStatus.LEASED,
            ScheduledTask.lease_generation == lease_generation,
        )
        .values(status=TaskStatus.RUNNING, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    if start_result.rowcount != 1:
        # Fail-closed: zero bytes written; the handler is never invoked.
        session.expire(task)
        raise LeaseSupersededError(task.id, lease_generation, task.status, task.lease_generation)
    # Authoritative reload (NOT a bare ORM re-write): the object the caller
    # holds now reflects the running marker AND stays readable after this
    # session commits and closes (callers legitimately read the returned
    # object's attributes afterwards — the baseline contract). refresh()
    # does not dirty the object, so no later flush could emit an
    # unconditional ORM write that bypasses the CAS.
    session.refresh(task)

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
        # Authoritative re-read AFTER the rollback (the handler may have
        # modified the object; the rollback discarded those changes): refresh
        # re-selects the committed claim state the CAS below will verify.
        session.refresh(task)

    if failure is None:
        # --- T3: success transition (worker-owned CAS) -------------------
        # No status predicate: the same-transaction running marker set by
        # the start gate keeps the row locked until commit, so within this
        # transaction the generation alone identifies the claim holder.
        result = session.execute(
            update(ScheduledTask)
            .where(
                ScheduledTask.id == task.id,
                ScheduledTask.lease_generation == lease_generation,
            )
            .values(
                status=TaskStatus.SUCCEEDED,
                finished_at=now,
                lease_expires_at=None,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            session.rollback()
            session.expire(task)
            raise LeaseSupersededError(
                task.id, lease_generation, task.status, task.lease_generation
            )
    else:
        # --- T4/T5: failure / retry transition (worker-owned CAS) ---------
        # ``attempts``/``max_attempts`` are re-read from the authoritative
        # row AFTER the rollback (the handler may have modified the object).
        error_text = f"{type(failure).__name__}: {failure}"[:_LAST_ERROR_MAX_CHARS]
        attempts_now = int(task.attempts)
        if attempts_now >= int(task.max_attempts):
            fail_values = {
                "status": TaskStatus.FAILED,
                "finished_at": now,
                "last_error": error_text,
                "lease_expires_at": None,
                "updated_at": now,
            }
        else:
            fail_values = {
                "status": TaskStatus.PENDING,
                "run_at": now + timedelta(seconds=backoff_delay(attempts_now)),
                "last_error": error_text,
                "lease_expires_at": None,
                "updated_at": now,
            }
        result = session.execute(
            update(ScheduledTask)
            .where(
                ScheduledTask.id == task.id,
                ScheduledTask.lease_generation == lease_generation,
            )
            .values(**fail_values)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            # A re-claim superseded this lease during the post-rollback
            # window — an authorization loss, never an ordinary failure.
            session.rollback()
            session.expire(task)
            raise LeaseSupersededError(
                task.id, lease_generation, task.status, task.lease_generation
            )
    # Authoritative final reload: the returned object carries the committed
    # transition values (readable after session close — baseline contract),
    # and refresh() does NOT dirty the object (no unconditional ORM re-write
    # could ever bypass the CAS on a later flush).
    session.refresh(task)
    return task


def expire_leases(session: Session, now: datetime) -> int:
    """Recover expired leases; returns the number of recovered tasks.

    R02-B1 (W2 closure, system-owned transitions — Phase 0 P0-1): recovery
    is performed by TWO single-statement conditional bulk UPDATEs whose
    predicates are evaluated by the DATABASE at write time:

    - attempts exhausted (``attempts >= max_attempts``): ``leased → failed``
      (terminal, static error message — no payload data) + ``finished_at``;
    - attempts remaining: ``leased → pending`` (re-claimable at-least-once).

    There is NO SELECT→write two-phase window left to exploit: a task that
    a concurrent transaction completed (``succeeded``), cancelled or
    re-claimed between recovery passes simply does not match the
    ``status='leased'`` predicate and is NOT counted and NOT overwritten —
    a committed terminal state can never be flipped back by the reaper.
    The reaper does not hold (or need) any worker's claim generation:
    lease recovery is a system-owned transition authorized by authoritative
    database state alone (current status + expiry timestamp + attempt
    accounting); it NEVER touches ``lease_generation`` (monotonicity:
    generation increments only at claim — INV-2). The returned count is
    the number of rows the UPDATEs actually recovered.
    """
    now = as_utc(now)
    expired_where = (
        ScheduledTask.status == TaskStatus.LEASED,
        ScheduledTask.lease_expires_at < now,
    )
    # Exhausted attempts → terminal lease-expired failure.
    exhausted = session.execute(
        update(ScheduledTask)
        .where(*expired_where, ScheduledTask.attempts >= ScheduledTask.max_attempts)
        .values(
            status=TaskStatus.FAILED,
            finished_at=now,
            lease_expires_at=None,
            last_error="lease expired after the final attempt",
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    # Attempts remaining → pending (re-claimable; the attempt was already
    # counted at claim time — recovery adds NO new attempt).
    remaining = session.execute(
        update(ScheduledTask)
        .where(*expired_where, ScheduledTask.attempts < ScheduledTask.max_attempts)
        .values(
            status=TaskStatus.PENDING,
            lease_expires_at=None,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    return int(exhausted.rowcount) + int(remaining.rowcount)


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

    The state guard is ENFORCED BY THE DATABASE, not only by the possibly
    stale ORM object: the pending→cancelled transition is a single
    conditional ``UPDATE ... WHERE status='pending'`` (the same pattern as
    :func:`claim_next`). If a concurrent transaction already claimed,
    executed or finished the task after this (possibly stale) read, the
    UPDATE affects zero rows and the canceller receives the documented 409
    — a cached ``pending`` status can no longer overwrite a task another
    transaction moved (D21-01 / R02-A).
    """
    if task.agent_id != owner.id:
        # Defensive re-check; routes record the authz_denial event first.
        raise AuthorizationError(f"task:cancel:{task.id}")
    if task.status != TaskStatus.PENDING:
        raise InvalidStateError(
            f"Only pending tasks can be cancelled (current status={task.status!s})."
        )
    finished_at = as_utc(now or utc_now())
    result = session.execute(
        update(ScheduledTask)
        .where(
            ScheduledTask.id == task.id,
            ScheduledTask.status == TaskStatus.PENDING,
        )
        .values(
            status=TaskStatus.CANCELLED,
            finished_at=finished_at,
            updated_at=finished_at,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        # A concurrent transaction moved the task out of pending between our
        # read and this UPDATE (READ COMMITTED recheck evaluates the WHERE
        # predicate against the committed row version). Re-read the
        # authoritative status for the error message and fail closed —
        # never report a cancellation that did not happen.
        session.expire(task)
        raise InvalidStateError(
            f"Only pending tasks can be cancelled (current status={task.status!s})."
        )
    session.expire(task)  # re-load the authoritative cancelled state on access
    return task

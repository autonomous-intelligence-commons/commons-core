"""Durable per-agent message-send quotas (spec §19, §21).

Design:
- Uses the ``QuotaCounter`` table (fixed 60-second window per agent, the
  ``messages`` column) — durable across restarts and visible to operators,
  unlike the in-memory request rate limiter. Complements (not replaces) the
  request-level limiter in ``commons_core.security.rate_limit``.
- The window start is ``now`` truncated to the minute; the clock is injectable
  (``now`` parameter) so tests can freeze/advance time deterministically.
- Admission is ATOMIC (R06-A): one conditional UPSERT statement creates the
  window row and increments the counter —
  ``INSERT ... ON CONFLICT (agent_id, window_start) DO UPDATE SET
  messages = messages + 1 WHERE messages < :limit RETURNING messages``.
  Concurrent senders can no longer oversell the final slots (the guard is
  re-evaluated against the latest row version under the conflict arbiter),
  and the first-window creation race resolves through the primary-key
  conflict path instead of an escaped IntegrityError.
- On exceed: a ``quota_exceeded`` security event is recorded (principal =
  agent id, details = limit only — NEVER content) and
  :class:`commons_core.api.errors.QuotaExceededError` is raised, which the API
  layer maps to HTTP 429 ``QUOTA_EXCEEDED``.
- ``record_event`` commits on the denial path; the caller must therefore
  have no pending domain writes when quota is exceeded (satisfied when the
  quota check runs before the message is built). The denial path itself
  leaves no pending writes: the UPSERT guard rejects the increment as part
  of the same statement.
"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from commons_core.api.errors import QuotaExceededError
from commons_core.database.enums import SecurityEventCategory
from commons_core.database.models import QuotaCounter
from commons_core.infrastructure.time import as_utc, utc_now
from commons_core.security.events import record_event

_WINDOW_SECONDS = 60


def _window_start(now: datetime) -> datetime:
    """Truncate ``now`` to the start of its minute (fixed window)."""
    now = as_utc(now)
    return now.replace(second=0, microsecond=0)


def _atomic_admit(session: Session, agent_id: str, window: datetime, *, limit: int) -> int | None:
    """Atomically create-or-increment the window counter (one statement).

    Returns the new message count on admission, or ``None`` when the window
    is already at ``limit`` (denied). The conflict arbiter makes the
    first-window creation race and the final-slot race atomic on both
    supported backends (PostgreSQL 16 and SQLite >= 3.35).
    """
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        insert = pg_insert
    elif dialect == "sqlite":
        insert = sqlite_insert
    else:  # pragma: no cover — defensive: fail loud on unknown backends
        raise RuntimeError(f"unsupported dialect for atomic quota admission: {dialect!r}")
    statement = (
        insert(QuotaCounter)
        .values(
            agent_id=agent_id,
            window_start=window,
            requests=0,
            messages=1,
            updated_at=utc_now(),
        )
        .on_conflict_do_update(
            index_elements=[QuotaCounter.agent_id, QuotaCounter.window_start],
            set_={"messages": QuotaCounter.messages + 1, "updated_at": utc_now()},
            where=QuotaCounter.messages < limit,
        )
        .returning(QuotaCounter.messages)
    )
    return session.execute(statement).scalar()


def check_message_quota(
    session: Session,
    agent_id: str,
    *,
    limit: int,
    now: datetime | None = None,
) -> int:
    """Count one message send for ``agent_id``; raise past the per-minute limit.

    Increments the durable counter for the current fixed window atomically
    (single conditional UPSERT, R06-A) and returns the new count. Raises
    :class:`QuotaExceededError` (after recording a ``quota_exceeded`` security
    event) when the limit is already reached.

    ``record_event`` commits on the denial path; the caller must therefore
    have no pending domain writes when quota is exceeded (satisfied when the
    quota check runs before the message is built).
    """
    if limit < 1:
        raise ValueError("limit must be >= 1")
    current = _window_start(now or utc_now())
    new_count = _atomic_admit(session, agent_id, current, limit=limit)
    if new_count is None:
        record_event(
            session,
            SecurityEventCategory.QUOTA_EXCEEDED,
            agent_id,
            {"limit": limit, "scope": "messages_per_minute"},
        )
        raise QuotaExceededError(limit=limit, window="minute")
    return new_count


def current_message_count(session: Session, agent_id: str, *, now: datetime | None) -> int:
    """Return the message count for the agent's current window (read-only)."""
    current = _window_start(now or utc_now())
    row = session.scalar(
        select(QuotaCounter).where(
            QuotaCounter.agent_id == agent_id, QuotaCounter.window_start == current
        )
    )
    return row.messages if row is not None else 0


def seconds_remaining_in_window(now: datetime | None = None) -> int:
    """Seconds left in the current fixed window (for Retry-After style hints)."""
    current = as_utc(now or utc_now())
    start = _window_start(current)
    return max(1, int(start.timestamp() + _WINDOW_SECONDS - current.timestamp()))

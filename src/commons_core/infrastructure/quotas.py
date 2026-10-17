"""Durable per-agent message-send quotas (spec §19, §21).

Design:
- Uses the ``QuotaCounter`` table (fixed 60-second window per agent, the
  ``messages`` column) — durable across restarts and visible to operators,
  unlike the in-memory request rate limiter. Complements (not replaces) the
  request-level limiter in ``commons_core.security.rate_limit``.
- The window start is ``now`` truncated to the minute; the clock is injectable
  (``now`` parameter) so tests can freeze/advance time deterministically.
- On exceed: a ``quota_exceeded`` security event is recorded (principal =
  agent id, details = limit only — NEVER content) and
  :class:`commons_core.api.errors.QuotaExceededError` is raised, which the API
  layer maps to HTTP 429 ``QUOTA_EXCEEDED``.
- Honest limitation (shared with the in-memory limiter): the counter is
  incremented in the caller's transaction; under concurrent writes the check
  is read-then-increment without a row lock (SQLite serializes writers;
  PostgreSQL deployments should upgrade to ``SELECT ... FOR UPDATE``). A
  heavily concurrent sender may transiently exceed the limit by the number of
  in-flight writers.
"""

from datetime import datetime

from sqlalchemy import select
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


def check_message_quota(
    session: Session,
    agent_id: str,
    *,
    limit: int,
    now: datetime | None = None,
) -> int:
    """Count one message send for ``agent_id``; raise past the per-minute limit.

    Increments the durable counter for the current fixed window and returns
    the new count. Raises :class:`QuotaExceededError` (after recording a
    ``quota_exceeded`` security event) when the limit is already reached.

    ``record_event`` commits on the denial path; the caller must therefore
    have no pending domain writes when quota is exceeded (satisfied when the
    quota check runs before the message is built).
    """
    if limit < 1:
        raise ValueError("limit must be >= 1")
    current = _window_start(now or utc_now())
    counter = session.get(QuotaCounter, (agent_id, current))
    if counter is None:
        counter = QuotaCounter(agent_id=agent_id, window_start=current, requests=0, messages=0)
        session.add(counter)
        session.flush()

    if counter.messages >= limit:
        record_event(
            session,
            SecurityEventCategory.QUOTA_EXCEEDED,
            agent_id,
            {"limit": limit, "scope": "messages_per_minute"},
        )
        raise QuotaExceededError(limit=limit, window="minute")

    counter.messages += 1
    counter.updated_at = utc_now()
    session.flush()
    return counter.messages


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

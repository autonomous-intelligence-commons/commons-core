"""Per-principal fixed-window rate limiting (spec §19, §21).

Design:
- In-memory fixed-window counters are the AUTHORITATIVE limiter (fast, works
  even when the database is degraded). Honest limitation: counters are
  per-process — correct for the single-process Phase 1 deployment; a
  multi-worker deployment needs a shared store (noted for Agent B / ops;
  R06-B B1 is OPEN on exactly this point).
- For authenticated principals the current window's request count is ALSO
  mirrored into the ``QuotaCounter`` table (best-effort, durable telemetry
  for quota decisions); anonymous traffic is never persisted (privacy: no
  IP-keyed rows). The mirror is ATOMIC (R06-B F.1): one conditional UPSERT
  creates the window row and increments the request counter, so concurrent
  mirrors cannot lose increments and the first-window creation race
  resolves through the primary-key conflict path.

Usage by the middleware: one ``check(principal)`` per request before the
route runs; denied requests return HTTP 429 and a ``rate_limited`` security
event. The clock is injectable so tests can freeze time.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from commons_core.database.models import QuotaCounter
from commons_core.infrastructure.time import as_utc, utc_now

_WINDOW_SECONDS = 60


@dataclass(slots=True)
class RateLimitDecision:
    """Outcome of one rate-limit check (counts/limits only — privacy-safe)."""

    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: int
    window_start: datetime


class RateLimiter:
    """Fixed-window per-minute limiter with an injectable clock."""

    def __init__(
        self,
        per_agent_limit: int,
        anonymous_limit: int = 30,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if per_agent_limit < 1 or anonymous_limit < 1:
            raise ValueError("rate limits must be >= 1")
        self._per_agent_limit = per_agent_limit
        self._anonymous_limit = anonymous_limit
        self._clock: Callable[[], datetime] = clock or utc_now
        self._counts: dict[tuple[str, datetime], int] = {}
        self._check_calls = 0

    def check(self, principal: str) -> RateLimitDecision:
        """Count one request for ``principal`` in the current minute window."""
        now = as_utc(self._clock())
        window = now.replace(second=0, microsecond=0)
        limit = (
            self._anonymous_limit if principal.startswith("anonymous") else self._per_agent_limit
        )
        key = (principal, window)
        count = self._counts.get(key, 0)
        allowed = count < limit
        if allowed:
            self._counts[key] = count + 1
            remaining = limit - count - 1
        else:
            remaining = 0
        self._check_calls += 1
        if self._check_calls % 1024 == 0:
            self._prune(window)
        seconds_left = max(1, int((window.timestamp() + _WINDOW_SECONDS - now.timestamp()) + 0.999))
        return RateLimitDecision(
            allowed=allowed,
            limit=limit,
            remaining=remaining,
            retry_after_seconds=seconds_left,
            window_start=window,
        )

    def _prune(self, current_window: datetime) -> None:
        """Drop counters from windows older than the previous one."""
        cutoff = current_window.timestamp() - _WINDOW_SECONDS
        stale = [key for key in self._counts if key[1].timestamp() < cutoff]
        for key in stale:
            del self._counts[key]


def persist_request_count(
    session: Session, agent_id: str, window_start: datetime, amount: int = 1
) -> None:
    """Mirror an authenticated request count into the QuotaCounter table.

    Best-effort durable telemetry (Agent B builds quota decisions on it).
    The caller owns the transaction.

    Atomicity (R06-B F.1): one conditional UPSERT — ``INSERT ... ON CONFLICT
    (agent_id, window_start) DO UPDATE SET requests = requests + :amount`` —
    creates the window row and increments the counter in a single statement.
    Concurrent mirrors can no longer lose increments (the increment is
    re-evaluated against the latest row version under the conflict arbiter),
    and the first-window creation race resolves through the primary-key
    conflict path instead of an escaped IntegrityError. The statement runs
    inside the caller's transaction and bypasses the ORM identity map —
    ``QuotaCounter`` objects already loaded in THIS session are not
    refreshed by it (read back through a fresh session, as callers in the
    middleware do). Admission authority (the in-memory limiter) and the
    best-effort failure policy are unchanged; anonymous traffic is never
    persisted (the only production caller invokes this for authenticated
    agents).
    """
    window_start = as_utc(window_start)
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        insert = pg_insert
    elif dialect == "sqlite":
        insert = sqlite_insert
    else:  # pragma: no cover — defensive: fail loud on unknown backends
        raise RuntimeError(f"unsupported dialect for atomic request persistence: {dialect!r}")
    statement = (
        insert(QuotaCounter)
        .values(
            agent_id=agent_id,
            window_start=window_start,
            requests=amount,
            messages=0,
            updated_at=utc_now(),
        )
        .on_conflict_do_update(
            index_elements=[QuotaCounter.agent_id, QuotaCounter.window_start],
            set_={"requests": QuotaCounter.requests + amount, "updated_at": utc_now()},
        )
    )
    session.execute(statement)

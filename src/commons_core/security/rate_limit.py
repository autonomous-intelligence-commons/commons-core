"""Per-principal fixed-window rate limiting (spec §19, §21).

Design:
- In-memory fixed-window counters are the AUTHORITATIVE limiter (fast, works
  even when the database is degraded). Honest limitation: counters are
  per-process — correct for the single-process Phase 1 deployment; a
  multi-worker deployment needs a shared store (noted for Agent B / ops).
- For authenticated principals the current window's request count is ALSO
  mirrored into the ``QuotaCounter`` table (best-effort, durable telemetry
  for quota decisions); anonymous traffic is never persisted (privacy: no
  IP-keyed rows).

Usage by the middleware: one ``check(principal)`` per request before the
route runs; denied requests return HTTP 429 and a ``rate_limited`` security
event. The clock is injectable so tests can freeze time.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

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
    """
    window_start = as_utc(window_start)
    counter = session.get(QuotaCounter, (agent_id, window_start))
    if counter is None:
        counter = QuotaCounter(
            agent_id=agent_id, window_start=window_start, requests=amount, messages=0
        )
        session.add(counter)
    else:
        counter.requests += amount
    session.flush()

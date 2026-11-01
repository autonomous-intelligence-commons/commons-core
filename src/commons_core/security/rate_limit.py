"""Per-principal fixed-window rate limiting (spec §19, §21).

Design (R06-B F.2 — owner decisions D1/D2/D3):
- AUTHENTICATED Agent requests are admitted by the DATABASE-ATOMIC
  conditional UPSERT (:func:`admit_request`) — one authoritative decision
  per admitted request, shared across ALL workers pointing at the same
  database (the R06-B B1 cross-worker over-admission defect is remediated
  on exactly this point). The middleware runs it inside a dedicated short
  transaction committed before the request is forwarded downstream; the
  admission statement IS the durable request counter (the former
  best-effort telemetry call ``persist_request_count`` is retired from the
  production path — D3 — and kept temporarily only for compatibility and
  existing F.1 regression coverage). The failure policy is FAIL-CLOSED
  (D2): if the authoritative admission cannot be completed, the middleware
  must refuse the request (HTTP 503), never fall back to this in-memory
  limiter.
- ANONYMOUS traffic keeps the in-memory fixed-window limiter below
  (privacy: no IP-keyed persistent quota rows; no database dependency for
  anonymous admission).
- The clock is injectable so tests can freeze time.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from commons_core.database.models import QuotaCounter
from commons_core.infrastructure.time import as_utc, utc_now

_WINDOW_SECONDS = 60

#: Bounded execution for the authoritative admission transaction on
#: PostgreSQL. ``SET LOCAL`` is transaction-scoped, so the bound applies
#: only to statements of the admission transaction (the row-lock wait of
#: the conditional UPSERT under contention and the statement itself) and
#: can never leak into a later transaction or a pooled connection. The
#: timeout raises ``QueryCanceled`` on the caller's session, which the
#: middleware surfaces as the approved fail-closed HTTP 503 (D2) — never
#: as an admission. SQLite needs no equivalent here: its engine-wide
#: ``PRAGMA busy_timeout=5000`` (see ``create_engine_for``) already bounds
#: lock waits, and no PostgreSQL-specific SQL is ever executed against
#: SQLite (dialect-dispatched below).
_ADMISSION_STATEMENT_TIMEOUT_SQL = "SET LOCAL statement_timeout = '5s'"


@dataclass(slots=True)
class RateLimitDecision:
    """Outcome of one rate-limit check (counts/limits only — privacy-safe)."""

    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: int
    window_start: datetime


class RateLimiter:
    """Fixed-window per-minute limiter with an injectable clock.

    Authoritative for ANONYMOUS traffic only (R06-B F.2): authenticated
    Agent admission is decided by the database-atomic
    :func:`admit_request` in the middleware. This class must never be used
    as a fallback when that authoritative admission fails (fail-closed
    policy D2).
    """

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

    def current_window_and_retry(self) -> tuple[datetime, int]:
        """The current fixed window and its bounded retry hint (1..60 s).

        Uses the limiter's injectable clock so middleware callers (and
        tests with a frozen clock) derive the authoritative admission
        window and the 429 ``Retry-After`` value from one consistent
        source, matching the in-memory ``check`` semantics.
        """
        now = as_utc(self._clock())
        window = now.replace(second=0, microsecond=0)
        seconds_left = max(1, int((window.timestamp() + _WINDOW_SECONDS - now.timestamp()) + 0.999))
        return window, seconds_left

    def _prune(self, current_window: datetime) -> None:
        """Drop counters from windows older than the previous one."""
        cutoff = current_window.timestamp() - _WINDOW_SECONDS
        stale = [key for key in self._counts if key[1].timestamp() < cutoff]
        for key in stale:
            del self._counts[key]


def admit_request(
    session: Session, agent_id: str, window_start: datetime, *, limit: int
) -> int | None:
    """Authoritative shared admission for one authenticated Agent request.

    Atomically creates-or-increments the agent's request counter for the
    fixed window in ONE conditional UPSERT statement (R06-B F.2 / D1, the
    ``requests``-column sibling of the R06-A ``_atomic_admit``)::

        INSERT INTO quota_counters (agent_id, window_start, requests, ...)
        VALUES (:agent_id, :window, 1, ...)
        ON CONFLICT (agent_id, window_start) DO UPDATE
            SET requests = quota_counters.requests + 1, updated_at = ...
            WHERE quota_counters.requests < :limit
        RETURNING requests

    Returns the NEW request count on admission, or ``None`` when the window
    is already at ``limit`` (denied — the denial path performs no write).

    Contract:

    - One authoritative decision per call; concurrent final-slot and
      first-window-creation races are arbitrated by the conflict path on
      both supported backends (PostgreSQL 16 and SQLite >= 3.35), so the
      persisted count never exceeds ``limit`` through successful
      admissions, and a denial never increments the counter.
    - BOUNDED EXECUTION (PostgreSQL): on the PostgreSQL dialect the call
      first executes ``SET LOCAL statement_timeout = '5s'`` inside the
      caller's transaction, so waiting for a contended row lock or a
      stalled query aborts after ~5 s (``QueryCanceled``) instead of
      hanging the request indefinitely. Connection establishment is
      bounded separately at the engine level (``connect_timeout``, see
      ``create_engine_for``). SQLite relies on its engine-wide
      ``busy_timeout`` pragma; no PostgreSQL-specific SQL is executed
      against SQLite.
    - Runs inside the CALLER's transaction: admission and persistence
      share one transaction outcome, and the decision counts as
      successful only once that transaction commits (an admission that
      rolls back returns its slot). Callers must NOT silently retry
      ambiguous commit outcomes — a retried admission could consume a
      second slot; surface the failure instead (the middleware's approved
      fail-closed policy, D2).
    - Commit outcomes come in exactly three flavors, and a 503 does NOT
      imply "no slot was consumed": (a) a failure before the commit is
      durably applied (statement error, timeout, rollback) deterministically
      consumes nothing; (b) a successful commit is the only admitted case;
      (c) a commit whose result is unknown because the connection was
      lost after the server processed it MAY have already consumed the
      slot — such a 503 is reported without retry and the persisted count
      (the conservative accounting) stays authoritative.
    - ``messages`` (the R06-A column) is never touched here; both counters
      compose on the same row orthogonally.
    - The statement bypasses the ORM identity map — read counters back
      through a fresh session, as the middleware does.

    ``limit`` must be a positive integer; zero/negative limits are
    rejected explicitly (``ValueError``) instead of silently admitting.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError(f"admission limit must be an integer >= 1 (got {limit!r})")
    window_start = as_utc(window_start)
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        insert = pg_insert
        # Bound the admission transaction's execution (lock waits +
        # statement time). SET LOCAL is scoped to the current transaction
        # and disappears at COMMIT/ROLLBACK, so pooled connections are
        # never polluted (regression-tested).
        session.execute(text(_ADMISSION_STATEMENT_TIMEOUT_SQL))
    elif dialect == "sqlite":
        insert = sqlite_insert
    else:  # pragma: no cover — defensive: fail loud on unknown backends
        raise RuntimeError(f"unsupported dialect for atomic request admission: {dialect!r}")
    statement = (
        insert(QuotaCounter)
        .values(
            agent_id=agent_id,
            window_start=window_start,
            requests=1,
            messages=0,
            updated_at=utc_now(),
        )
        .on_conflict_do_update(
            index_elements=[QuotaCounter.agent_id, QuotaCounter.window_start],
            set_={"requests": QuotaCounter.requests + 1, "updated_at": utc_now()},
            where=QuotaCounter.requests < limit,
        )
        .returning(QuotaCounter.requests)
    )
    return session.execute(statement).scalar()


def persist_request_count(
    session: Session, agent_id: str, window_start: datetime, amount: int = 1
) -> None:
    """Mirror an authenticated request count into the QuotaCounter table.

    LEGACY (R06-B F.2 / D3): the production middleware no longer calls
    this function — the authoritative admission statement
    (:func:`admit_request`) produces the same durable increment exactly
    once per admitted request, and running both would double-count. It is
    retained temporarily for compatibility and for the existing R06-B F.1
    regression coverage, which asserts the atomicity of this statement
    directly.

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

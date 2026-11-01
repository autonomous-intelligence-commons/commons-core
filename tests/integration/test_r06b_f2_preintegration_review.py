"""R06-B F.2 focused pre-integration review — production timeout and
ambiguous-commit verification (follow-up to the delivered F.2 patch).

The independent pre-integration review asked two questions before
owner-side Windows acceptance:

1. PRODUCTION TIMEOUT — does the authenticated authoritative admission
   have a demonstrably bounded execution time under PostgreSQL lock
   contention, a stalled query, and connection-establishment failure?
   (Test-only ``SET LOCAL lock_timeout`` in the racing suites never
   counted as production evidence.)
2. AMBIGUOUS TRANSACTION OUTCOMES — are (a) a pre-commit rollback,
   (b) a successful commit and (c) a commit whose result is unknown
   (connection lost) properly distinguished, with NO automatic retry of
   ambiguous commits and NO claim that every 503 means no slot was
   consumed?

Production behavior verified here (see the follow-up patch):
- ``rate_limit.admit_request`` runs the admission UPSERT under
  ``SET LOCAL statement_timeout = '5s'`` on PostgreSQL — transaction-
  scoped, never leaking to pooled connections; SQLite never receives
  PostgreSQL-specific SQL (its engine-wide ``busy_timeout`` pragma plays
  the same bounding role).
- ``db.create_engine_for`` bounds PostgreSQL connection establishment
  with libpq ``connect_timeout=5``.

REAL vs SIMULATED injection (honest classification):
- REAL: the row-lock contention below (a racing transaction genuinely
  holds the counter row's lock, uncommitted — the admission statement
  really WAITS, then really times out); the black-holed server
  (unroutable address whose SYNs are silently dropped — verified in this
  sandbox: pre-fix engines hung >12 s, with ``connect_timeout`` the
  attempt fails at ~5 s); and the ambiguous commit (a REAL commit
  reaches and is durably applied by the server; only the client-side
  return path raises connection-lost).
- SIMULATED: none, except that the post-commit connection loss is
  reproduced by a ``Session.commit`` wrapper that performs the REAL
  commit first and raises afterwards (a true mid-commit TCP cut cannot
  be scheduled deterministically from the same process).
"""

import threading
import time
import uuid as uuidlib
from datetime import UTC, datetime

import pytest
from conftest import PG_TEST_URL
from fastapi.testclient import TestClient
from sqlalchemy import event, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from commons_core.agents.service import provision_agent
from commons_core.api.app import create_app
from commons_core.config.settings import Settings
from commons_core.database.models import QuotaCounter
from commons_core.security.authentication.tokens import issue_token
from commons_core.security.rate_limit import admit_request

pg_only = pytest.mark.skipif(
    not PG_TEST_URL,
    reason="PostgreSQL-specific verification (AIC_TEST_DATABASE_URL unset)",
)
sqlite_only = pytest.mark.skipif(
    bool(PG_TEST_URL),
    reason="SQLite-specific verification (the suite backend is PostgreSQL)",
)

#: Frozen clock (same instant family as the F.2 acceptance suite).
_FROZEN_NOW = datetime(2026, 11, 1, 12, 0, 15, 123456, tzinfo=UTC)
#: The production admission statement timeout (must match rate_limit.py).
_EXPECTED_STATEMENT_TIMEOUT = "5s"
#: Generous wall-clock bounds for a ~5 s bounded wait in CI noise.
_MIN_REAL_WAIT_S = 2.0
_MAX_BOUNDED_WAIT_S = 15.0
#: A black-holed destination: unroutable address, SYNs silently dropped
#: (verified in this sandbox — no RST, no ICMP). NOT a credential.
_BLACK_HOLE_URL = "postgresql+psycopg://commons:pass@10.255.255.1:5432/commons"
_JOIN_TIMEOUT_S = 30
_EVENT_WAIT_S = 15


def _window_start(now: datetime = _FROZEN_NOW) -> datetime:
    """The fixed test window (minute-truncated, timezone-aware)."""
    return now.replace(second=0, microsecond=0)


def _provision_agent_with_token(database) -> tuple[str, str]:
    """Provision + commit an agent with a bearer token (no HTTP needed)."""
    with database.session() as session:
        agent = provision_agent(session, f"f2-pir-{uuidlib.uuid4().hex[:10]}")
        issued = issue_token(session, agent.id)
    return agent.id, issued.raw_token


def _seed_counter(database, agent_id: str, window: datetime, *, requests: int) -> None:
    """Durably seed a window row (committed before any racer starts)."""
    with database.session() as session:
        session.add(
            QuotaCounter(agent_id=agent_id, window_start=window, requests=requests, messages=0)
        )


def _read_counter(database, agent_id: str, window: datetime) -> QuotaCounter | None:
    """Fresh-session read-back of the window row for ``agent_id``."""
    with database.session() as session:
        return session.get(QuotaCounter, (agent_id, window))


def _frozen_limited_app(settings, database):
    """An app whose admission limit is 3 on the frozen clock (D2-visible)."""
    limited = settings.model_copy(update={"rate_limit_per_minute": 3})
    application = create_app(settings=limited, database=database)
    application.state.rate_limiter = type(application.state.rate_limiter)(
        per_agent_limit=3, clock=lambda: _FROZEN_NOW
    )
    return application


def _racer_hold_row_lock(database, agent_id: str, window: datetime, locked, release) -> None:
    """A REAL racing transaction: UPDATE the counter row and HOLD its row
    lock uncommitted until released, then roll back (its write vanishes).

    The racer itself stays bounded (PostgreSQL: ``SET LOCAL lock_timeout``
    per the established racing discipline; SQLite: the engine-wide
    ``busy_timeout`` pragma)."""
    session = database.sessionmaker()
    try:
        if session.get_bind().dialect.name == "postgresql":
            session.execute(text("SET LOCAL lock_timeout = '5s'"))
        session.execute(
            update(QuotaCounter)
            .where(QuotaCounter.agent_id == agent_id, QuotaCounter.window_start == window)
            .values(requests=QuotaCounter.requests)
        )
        locked.set()
        assert release.wait(timeout=_JOIN_TIMEOUT_S), "racer was never released"
        session.rollback()
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 1. Production timeout — exact configuration and transaction scoping
# ---------------------------------------------------------------------------


class TestAdmissionTransactionTimeout:
    """The admission transaction's PostgreSQL ``statement_timeout``: exact
    configuration (pre-integration review §1), transaction scoping, and no
    leakage into pooled connections."""

    @pg_only
    def test_statement_timeout_is_set_and_transaction_scoped(self, database) -> None:
        agent_id, _token = _provision_agent_with_token(database)
        window = _window_start()

        session = database.sessionmaker()
        try:
            before = session.execute(text("SELECT current_setting('statement_timeout')")).scalar()
            assert before == "0", f"expected the server default, got {before!r}"

            new_count = admit_request(session, agent_id, window, limit=3)
            assert new_count == 1

            during = session.execute(text("SELECT current_setting('statement_timeout')")).scalar()
            assert during == _EXPECTED_STATEMENT_TIMEOUT, (
                f"the admission transaction must run under statement_timeout="
                f"{_EXPECTED_STATEMENT_TIMEOUT!r}, got {during!r}"
            )

            session.commit()
            # After COMMIT the SET LOCAL is gone — SAME connection, new
            # transaction: the bound cannot leak into pooled connections.
            after = session.execute(text("SELECT current_setting('statement_timeout')")).scalar()
            assert after == "0", f"SET LOCAL leaked past the transaction: {after!r}"
        finally:
            session.close()

        counter = _read_counter(database, agent_id, window)
        assert counter is not None and counter.requests == 1


# ---------------------------------------------------------------------------
# 2. Real lock contention — bounded wait, fail-closed, no fallback
# ---------------------------------------------------------------------------


class TestRealLockContentionBoundedTimeout:
    """REAL PostgreSQL lock contention (and the SQLite busy-timeout
    equivalent): the admission statement WAITS on the contended row (a
    query that stalls rather than immediately throwing), then fails in
    BOUNDED time as fail-closed 503 with no worker-local fallback."""

    def test_contended_admission_waits_then_fails_closed_within_bound(
        self, settings, database
    ) -> None:
        agent_id, token = _provision_agent_with_token(database)
        window = _window_start()
        _seed_counter(database, agent_id, window, requests=1)

        application = _frozen_limited_app(settings, database)
        limiter_snapshot = dict(application.state.rate_limiter._counts)

        locked, release = threading.Event(), threading.Event()
        racer = threading.Thread(
            target=_racer_hold_row_lock, args=(database, agent_id, window, locked, release)
        )
        racer.start()
        try:
            assert locked.wait(timeout=_EVENT_WAIT_S), "racer never acquired the row lock"
            with TestClient(application) as test_client:
                t0 = time.monotonic()
                response = test_client.get(
                    "/api/v1/agents/me", headers={"Authorization": f"Bearer {token}"}
                )
                elapsed = time.monotonic() - t0
        finally:
            release.set()
            racer.join(timeout=_JOIN_TIMEOUT_S)
        assert not racer.is_alive(), "racer hung (no bounded exit)"

        assert response.status_code == 503, response.status_code
        error = response.json()["error"]
        assert error["code"] == "SERVICE_UNAVAILABLE", error
        assert error["retry_after_seconds"] >= 1, error
        assert "Retry-After" in response.headers

        # It genuinely WAITED (stalled) rather than failing immediately...
        assert elapsed >= _MIN_REAL_WAIT_S, (
            f"admission failed after only {elapsed:.2f}s — it never waited on the lock"
        )
        # ...and the wait was BOUNDED (~5s timeout, generous CI margin).
        assert elapsed <= _MAX_BOUNDED_WAIT_S, f"admission wait was not bounded ({elapsed:.2f}s)"

        # No worker-local fallback: the in-memory limiter never saw the
        # authenticated principal.
        assert application.state.rate_limiter._counts == limiter_snapshot

        # The timed-out admission is a PRE-COMMIT rollback: no slot consumed
        # (the racer's own write rolled back too, so the seed value remains).
        counter = _read_counter(database, agent_id, window)
        assert counter is not None and counter.requests == 1, counter


# ---------------------------------------------------------------------------
# 3. Connection establishment — real black-holed server
# ---------------------------------------------------------------------------


class TestConnectionEstablishmentBounded:
    """REAL black-hole injection: SYNs to an unroutable address are
    silently dropped (no RST, no ICMP — verified in this sandbox; the
    PRE-FIX engine hung >12 s and had to be killed). With the engine-level
    ``connect_timeout`` the request fails CLOSED within a bounded time.

    Honest scope note: with one shared engine the first database touch of
    a bearer-token request is the pre-authentication read, so the bounded
    establishment surfaces there — the SAME connection-establishment
    semantics the admission transaction's pool checkout relies on. The
    instantly-refused variant (dead port) is already covered by the F.2
    acceptance suite's ``TestAuthenticationInfrastructureFailure``.
    """

    def test_black_holed_server_fails_closed_within_bound(self) -> None:
        settings = Settings(
            database_url=_BLACK_HOLE_URL, master_keys="", env="test", log_level="WARNING"
        )
        application = create_app(settings=settings)
        with TestClient(application) as black_client:
            t0 = time.monotonic()
            response = black_client.get(
                "/api/v1/agents/me", headers={"Authorization": "Bearer " + "x" * 32}
            )
            elapsed = time.monotonic() - t0

        assert response.status_code == 503, response.status_code
        assert response.json()["error"]["code"] == "SERVICE_UNAVAILABLE", response.json()
        # Bounded: ~5s connect timeout (+ generous CI margin), and it truly
        # waited rather than being instantly refused.
        assert 1.0 <= elapsed <= 12.0, (
            f"connection establishment was not bounded-and-real ({elapsed:.2f}s)"
        )

    def test_black_holed_server_anonymous_path_stays_independent(self) -> None:
        """Anonymous ADMISSION must not depend on the database even when
        the server is a black hole — the 401 semantics stand and the
        in-memory limiter decides. (The request may still take a bounded
        ~5 s: the route dependency's BEST-EFFORT auth_failure security
        event also points at the black hole and waits out the engine's
        connect_timeout before giving up — pre-fix that wait was
        unbounded, and it never changes the response.)"""
        settings = Settings(
            database_url=_BLACK_HOLE_URL, master_keys="", env="test", log_level="WARNING"
        )
        application = create_app(settings=settings)
        with TestClient(application) as black_client:
            t0 = time.monotonic()
            anonymous = black_client.get("/api/v1/agents/me")
            elapsed = time.monotonic() - t0

        assert anonymous.status_code == 401, anonymous.status_code
        assert anonymous.json()["error"]["code"] == "UNAUTHENTICATED", anonymous.json()
        # Bounded overall (the best-effort event write is connect-timeout
        # bounded too); the anonymous ADMISSION itself never touched the DB.
        assert elapsed <= 12.0, f"anonymous request hung ({elapsed:.2f}s)"


# ---------------------------------------------------------------------------
# 4. Ambiguous commit outcomes
# ---------------------------------------------------------------------------


class TestAmbiguousCommitOutcome:
    """A commit whose result is UNKNOWN because the connection was lost
    after the server processed it: HTTP 503, exactly ONE admission
    attempt (no automatic retry), and the slot MAY already be consumed —
    the persisted counter remains the conservative, authoritative
    accounting. This is the honest counterpart to the pre-commit
    (deterministically slot-free) 503s in ``TestFailClosedPolicy``."""

    @staticmethod
    def _ambiguous_commit_hooks():
        """Injection pair reproducing an ambiguous COMMIT outcome.

        ``_marking_admit`` runs the REAL ``admit_request`` and only marks
        the session that performed the admission (counting attempts);
        ``_commit_then_lose_connection`` turns THAT session's commit into
        the ambiguous case — the real commit is durably applied by the
        server, and only THEN does the client raise connection-lost.
        Pre-authentication and every other transaction commit normally,
        so the 503 provably originates from the admission transaction."""
        original_commit = Session.commit
        real_admit = admit_request
        marked: set[int] = set()
        state = {"armed": True}
        calls = {"n": 0}

        def _marking_admit(session, agent_id, window, *, limit):
            calls["n"] += 1
            marked.add(id(session))
            return real_admit(session, agent_id, window, limit=limit)

        def _commit_then_lose_connection(self):
            if state["armed"] and id(self) in marked:
                state["armed"] = False
                original_commit(self)  # the server DURABLY applies the commit...
                raise OperationalError(  # ...and only then the connection is lost
                    "server closed the connection unexpectedly",
                    None,
                    RuntimeError("connection lost after the server processed COMMIT"),
                )
            return original_commit(self)

        return _marking_admit, _commit_then_lose_connection, calls

    def test_unknown_commit_result_is_503_may_consume_slot_not_retried(
        self, settings, database, monkeypatch
    ) -> None:
        agent_id, token = _provision_agent_with_token(database)
        application = _frozen_limited_app(settings, database)
        marking_admit, losing_commit, calls = self._ambiguous_commit_hooks()
        monkeypatch.setattr("commons_core.api.middleware.admit_request", marking_admit)
        monkeypatch.setattr("sqlalchemy.orm.Session.commit", losing_commit)

        with TestClient(application) as test_client:
            first = test_client.get(
                "/api/v1/agents/me", headers={"Authorization": f"Bearer {token}"}
            )
            assert first.status_code == 503, first.status_code
            assert first.json()["error"]["code"] == "SERVICE_UNAVAILABLE", first.json()

        # Exactly ONE admission attempt for this request (no hidden retry).
        assert calls["n"] == 1, calls["n"]

        # THE KEY CORRECTION: this 503 DID consume a slot on the server.
        with database.session() as session:
            counters = list(session.scalars(select(QuotaCounter)))
        assert len(counters) == 1
        assert counters[0].requests == 1, (
            f"ambiguous-commit 503 may have consumed the slot (found {counters[0].requests})"
        )

        # Recovery: the NEXT request is a NEW admission attempt (not a
        # hidden retry of the previous one) and is admitted normally.
        with TestClient(application) as test_client:
            second = test_client.get(
                "/api/v1/agents/me", headers={"Authorization": f"Bearer {token}"}
            )
            assert second.status_code == 200, second.status_code
        with database.session() as session:
            counters = list(session.scalars(select(QuotaCounter)))
        assert len(counters) == 1
        assert counters[0].requests == 2, counters[0].requests

    def test_ambiguous_commit_admits_exactly_once(self, settings, database, monkeypatch) -> None:
        """Structural single-attempt proof: the middleware calls the
        authoritative admission exactly ONCE per request even when the
        commit outcome is ambiguous (a retry could consume a second slot)."""
        agent_id, token = _provision_agent_with_token(database)
        marking_admit, losing_commit, calls = self._ambiguous_commit_hooks()
        monkeypatch.setattr("commons_core.api.middleware.admit_request", marking_admit)
        monkeypatch.setattr("sqlalchemy.orm.Session.commit", losing_commit)

        application = _frozen_limited_app(settings, database)
        with TestClient(application) as test_client:
            response = test_client.get(
                "/api/v1/agents/me", headers={"Authorization": f"Bearer {token}"}
            )
        assert response.status_code == 503, response.status_code
        assert calls["n"] == 1, (
            f"the middleware must not retry an ambiguous commit (admission attempts: {calls['n']})"
        )
        # ...and the single attempt's slot WAS durably consumed.
        with database.session() as session:
            counters = list(session.scalars(select(QuotaCounter)))
        assert len(counters) == 1 and counters[0].requests == 1, counters


# ---------------------------------------------------------------------------
# 5. SQLite compatibility — no PostgreSQL-specific SQL, ever
# ---------------------------------------------------------------------------


class TestSqliteCompatibility:
    """SQLite compatibility of the timeout remediation: the admission
    statement keeps working unchanged and NO PostgreSQL-specific SQL ever
    reaches SQLite (``SET LOCAL`` / ``statement_timeout`` /
    ``lock_timeout`` are dialect-dispatched away)."""

    @sqlite_only
    def test_sqlite_never_receives_postgresql_specific_sql(self, database) -> None:
        agent_id, _token = _provision_agent_with_token(database)
        window = _window_start()
        statements: list[str] = []

        def _capture(_conn, _cursor, statement, *_args, **_kwargs):
            statements.append(statement)

        event.listen(database.engine, "before_cursor_execute", _capture)
        try:
            with database.session() as session:
                assert admit_request(session, agent_id, window, limit=3) == 1
        finally:
            event.remove(database.engine, "before_cursor_execute", _capture)

        assert statements, "no statements were captured"
        joined = " ".join(statements).lower()
        for forbidden in ("set local", "statement_timeout", "lock_timeout"):
            assert forbidden not in joined, (
                f"PostgreSQL-specific SQL reached SQLite: {statements!r}"
            )
        counter = _read_counter(database, agent_id, window)
        assert counter is not None and counter.requests == 1

"""R06-B F.2 shared admission — production acceptance suite (L1–L5).

Verifies the PRODUCTION implementation of the F.2 remediation (baseline
``f621e9a``, owner decisions D1/D2/D3) — not a prototype:

- D1 (shared admission): ``commons_core.security.rate_limit.admit_request``
  — the database-atomic conditional UPSERT on ``quota_counters.requests`` —
  is the authoritative admission mechanism for authenticated Agent
  requests, executed by the middleware inside a dedicated short
  transaction committed before the request is forwarded downstream.
- D2 (failure policy): FAIL-CLOSED. An authenticated request whose
  authoritative admission cannot be completed is refused with HTTP 503
  ``SERVICE_UNAVAILABLE`` — never admitted through ``RateLimiter.check()``
  or any other worker-local fallback. An authentication-infrastructure
  failure is likewise surfaced as 503, not disguised as an invalid
  credential (401). A quota denial (limit reached) keeps the ordinary
  HTTP 429 contract.
- D3 (legacy telemetry): the production middleware no longer calls
  ``persist_request_count``; the admission statement IS the durable
  request counter (one increment per admitted request, no double
  counting).

Layer mapping (spec §4 of the F.2 remediation):

- L1 deterministic admission unit tests (frozen window, both backends)
- L2 concurrent transaction tests (threads; bounded one-way
  synchronization, fresh-session authoritative reads)
- L3 genuine separate-process verification (spawn, PostgreSQL) through
  the production ``admit_request``
- L4 authenticated API integration through the production middleware
  (429 contract, events, exempt paths, downstream failure, genuine
  two-worker uvicorn HTTP end-to-end on PostgreSQL)
- L5 failure-injection tests (classified exception injection at the
  middleware boundary, event-persistence failure, commit failure,
  recovery, real dead-port outage)

Concurrency-control discipline (identical to the R06-A/R06-B suites):
one-way ``threading.Event`` signalled AFTER the first racer's UPSERT has
executed (row locks held, uncommitted) via a connection-level
``after_execute`` hook on the quota-counters INSERT; that racer then
delays only ITS OWN commit for a bounded time-driven window. Every
racing PostgreSQL transaction sets ``SET LOCAL lock_timeout = '5s'``.
Outcomes are classified (``ok`` / ``sql_integrity`` / ``dbapi_error`` /
``unexpected``). Authoritative reads always use a FRESH session after
the racers join.

Backend scope: L3 spawn scenarios and the two-worker uvicorn HTTP test
run on PostgreSQL only (the multi-writer deployment backend); everything
else runs on both SQLite and PostgreSQL.
"""

import logging
import multiprocessing
import threading
import time
import uuid as uuidlib
from datetime import UTC, datetime

import httpx
import pytest
from conftest import PG_TEST_URL, auth_headers, provision_and_login
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
from sqlalchemy.orm import Session

from commons_core.agents.service import provision_agent
from commons_core.api.app import create_app
from commons_core.database.models import QuotaCounter
from commons_core.infrastructure.quotas import check_message_quota
from commons_core.infrastructure.time import as_utc
from commons_core.security.authentication.dependencies import require_authenticated_agent
from commons_core.security.rate_limit import admit_request, persist_request_count

pg_only = pytest.mark.skipif(
    not PG_TEST_URL,
    reason="F.2 multi-process scenarios run on PostgreSQL only (AIC_TEST_DATABASE_URL unset)",
)

#: Bounded wait protecting every racing PostgreSQL transaction (seconds).
_LOCK_TIMEOUT_SQL = "SET LOCAL lock_timeout = '5s'"
#: How long the write-first racer delays its own commit (time-driven,
#: never mutually-blocking — a correct implementation can always finish).
_COMMIT_DELAY_S = 0.4
#: Frozen clock for every API scenario (deterministic, mid-window).
_FROZEN_NOW = datetime(2026, 11, 1, 12, 0, 15, 123456, tzinfo=UTC)
#: A second, later window for rollover scenarios.
_FROZEN_NEXT = datetime(2026, 11, 1, 12, 1, 15, 123456, tzinfo=UTC)
_JOIN_TIMEOUT_S = 30
_EVENT_WAIT_S = 10
_WINDOW_SECONDS = 60


def _window_start(now: datetime = _FROZEN_NOW) -> datetime:
    """The fixed test window (minute-truncated, timezone-aware)."""
    return now.replace(second=0, microsecond=0)


# ---------------------------------------------------------------------------
# Shared racing machinery (R06-A / R06-B discipline)
# ---------------------------------------------------------------------------


def _bound_locking(session: Session) -> None:
    """Apply a bounded lock wait to this racing transaction (backend-aware).

    PostgreSQL: ``SET LOCAL lock_timeout``. SQLite: ``PRAGMA
    busy_timeout=5000`` is already applied engine-wide by the test
    ``create_engine_for``, which plays the same role.
    """
    from sqlalchemy import text

    if session.get_bind().dialect.name == "postgresql":
        session.execute(text(_LOCK_TIMEOUT_SQL))


def _classify(run):
    """Execute ``run``; classify the outcome."""
    try:
        return ("ok", run())
    except IntegrityError as exc:
        return ("sql_integrity", str(exc.orig)[:120])
    except DBAPIError as exc:
        return ("dbapi_error", str(exc.orig)[:120])
    except Exception as exc:  # noqa: BLE001 — classified, never swallowed silently
        return ("unexpected", f"{type(exc).__name__}: {exc}"[:120])


def _hold_quota_insert_then_signal(executed: threading.Event):
    """Connection-level ``after_execute`` hook: when a quota-counters INSERT
    has executed on OUR connection — row locks held, NOT yet committed —
    signal and delay only our own commit for a bounded window."""

    from sqlalchemy.sql.dml import Insert

    def _hook(connection, clauseelement, _multiparams, _params, _options, _result) -> None:
        if isinstance(clauseelement, Insert) and clauseelement.table.name == "quota_counters":
            executed.set()
            time.sleep(_COMMIT_DELAY_S)

    return _hook


def _provision_agent(database) -> str:
    """Provision and COMMIT an agent; return its id."""
    handle = f"f2-agent-{uuidlib.uuid4().hex[:10]}"
    with database.session() as session:
        agent = provision_agent(session, handle)
        return agent.id


def _seed_counter(
    database, agent_id: str, window: datetime, *, requests: int, messages: int = 0
) -> None:
    """Durably seed a window row (committed before any racer starts)."""
    with database.session() as session:
        session.add(
            QuotaCounter(
                agent_id=agent_id, window_start=window, requests=requests, messages=messages
            )
        )


def _read_counter(database, agent_id: str, window: datetime) -> QuotaCounter | None:
    """Fresh-session read-back of the window row for ``agent_id``."""
    with database.session() as session:
        return session.get(QuotaCounter, (agent_id, window))


def _read_rows(database, agent_id: str) -> list[QuotaCounter]:
    """Fresh-session read-back of every window row for ``agent_id``."""
    with database.session() as session:
        return list(session.scalars(select(QuotaCounter).where(QuotaCounter.agent_id == agent_id)))


def _run_pair(racer_a, racer_b, signal_a: threading.Event) -> dict:
    """Start racer A, wait for its in-flight signal, then start racer B."""
    outcomes: dict[str, tuple] = {}
    thread_a = threading.Thread(target=lambda: outcomes.__setitem__("a", racer_a()))
    thread_a.start()
    assert signal_a.wait(timeout=_EVENT_WAIT_S), "racer A never executed its write"
    thread_b = threading.Thread(target=lambda: outcomes.__setitem__("b", racer_b()))
    thread_b.start()
    thread_a.join(timeout=_JOIN_TIMEOUT_S)
    thread_b.join(timeout=_JOIN_TIMEOUT_S)
    assert not thread_a.is_alive() and not thread_b.is_alive(), "racers hung (no bounded exit)"
    return outcomes


@pytest.fixture()
def limited_client(settings, database) -> TestClient:
    """Authenticated-admission client: limit=3, frozen clock (D2-visible)."""
    limited = settings.model_copy(update={"rate_limit_per_minute": 3})
    application = create_app(settings=limited, database=database)
    application.state.rate_limiter = type(application.state.rate_limiter)(
        per_agent_limit=3, clock=lambda: _FROZEN_NOW
    )
    with TestClient(application) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# L1 — deterministic admission unit tests (production admit_request)
# ---------------------------------------------------------------------------


class TestAdmitRequestDeterministic:
    """Sequential admissions through the PRODUCTION statement."""

    def test_admits_limit_then_denies(self, database) -> None:
        agent_id = _provision_agent(database)
        window = _window_start()

        results = []
        for _ in range(4):
            with database.session() as session:
                results.append(admit_request(session, agent_id, window, limit=3))

        assert results == [1, 2, 3, None], results
        counter = _read_counter(database, agent_id, window)
        assert counter is not None and counter.requests == 3

    def test_boundary_limit_one(self, database) -> None:
        agent_id = _provision_agent(database)
        window = _window_start()

        with database.session() as session:
            assert admit_request(session, agent_id, window, limit=1) == 1
        with database.session() as session:
            assert admit_request(session, agent_id, window, limit=1) is None

    def test_denied_attempts_leave_counter_untouched(self, database) -> None:
        agent_id = _provision_agent(database)
        window = _window_start()
        _seed_counter(database, agent_id, window, requests=3)

        outcomes = []
        for _ in range(5):
            with database.session() as session:
                outcomes.append(admit_request(session, agent_id, window, limit=3))

        assert outcomes == [None] * 5, outcomes
        counter = _read_counter(database, agent_id, window)
        assert counter is not None
        assert counter.requests == 3, (
            f"denied attempts must not increment the shared counter (found {counter.requests})"
        )

    @pytest.mark.parametrize("bad_limit", [0, -1, -3, True, False, 1.5, "3", None])
    def test_invalid_limits_rejected_explicitly(self, database, bad_limit) -> None:
        agent_id = _provision_agent(database)
        window = _window_start()
        with database.session() as session:
            with pytest.raises(ValueError):
                admit_request(session, agent_id, window, limit=bad_limit)

    def test_independent_agent_principals(self, database) -> None:
        east = _provision_agent(database)
        west = _provision_agent(database)
        window = _window_start()

        for agent_id in (east, west):
            for _ in range(3):
                with database.session() as session:
                    assert admit_request(session, agent_id, window, limit=3) is not None
        for agent_id in (east, west):
            with database.session() as session:
                assert admit_request(session, agent_id, window, limit=3) is None

    def test_window_rollover_resets_limit(self, database) -> None:
        agent_id = _provision_agent(database)
        window_one = _window_start(_FROZEN_NOW)
        window_two = _window_start(_FROZEN_NEXT)

        for _ in range(3):
            with database.session() as session:
                assert admit_request(session, agent_id, window_one, limit=3) is not None
        with database.session() as session:
            assert admit_request(session, agent_id, window_one, limit=3) is None

        # Next minute: fresh window, fresh limit.
        with database.session() as session:
            assert admit_request(session, agent_id, window_two, limit=3) == 1

        rows = {as_utc(row.window_start): row for row in _read_rows(database, agent_id)}
        assert set(rows) == {window_one, window_two}
        assert (rows[window_one].requests, rows[window_two].requests) == (3, 1)

    def test_utc_normalization_and_persisted_window(self, database) -> None:
        agent_id = _provision_agent(database)
        window = _window_start()

        with database.session() as session:
            assert admit_request(session, agent_id, window, limit=3) == 1

        counter = _read_counter(database, agent_id, window)
        assert counter is not None
        # SQLite stores naive UTC strings; PostgreSQL round-trips aware
        # values — normalize before comparing (both backends agree).
        assert as_utc(counter.window_start) == window
        assert window.second == 0 and window.microsecond == 0

    def test_message_counter_preserved(self, database) -> None:
        agent_id = _provision_agent(database)
        window = _window_start()
        _seed_counter(database, agent_id, window, requests=1, messages=5)

        with database.session() as session:
            assert admit_request(session, agent_id, window, limit=3) == 2

        counter = _read_counter(database, agent_id, window)
        assert counter is not None
        assert (counter.requests, counter.messages) == (2, 5), (
            "request admission must never touch the R06-A message column"
        )

    def test_rollback_returns_the_slot(self, database) -> None:
        """Admission and persistence share the caller's transaction outcome:
        a rollback cancels the admission (the slot returns)."""
        agent_id = _provision_agent(database)
        window = _window_start()

        session = database.sessionmaker()
        try:
            admit_request(session, agent_id, window, limit=3)
            session.rollback()
        finally:
            session.close()

        counter = _read_counter(database, agent_id, window)
        assert counter is None, "a rolled-back admission must not persist"

    def test_commit_makes_the_decision_successful(self, database) -> None:
        agent_id = _provision_agent(database)
        window = _window_start()

        session = database.sessionmaker()
        try:
            assert admit_request(session, agent_id, window, limit=3) == 1
            session.commit()
        finally:
            session.close()

        counter = _read_counter(database, agent_id, window)
        assert counter is not None and counter.requests == 1

    def test_no_double_counting_single_call(self, database) -> None:
        agent_id = _provision_agent(database)
        window = _window_start()
        admitted = 0

        for _ in range(5):
            with database.session() as session:
                if admit_request(session, agent_id, window, limit=5) is not None:
                    admitted += 1

        assert admitted == 5
        counter = _read_counter(database, agent_id, window)
        assert counter is not None
        assert counter.requests == 5, counter.requests

    def test_coexists_with_r06a_message_quota_same_transaction(self, database) -> None:
        agent_id = _provision_agent(database)
        window = _window_start()

        with database.session() as session:
            messages_count = check_message_quota(session, agent_id, limit=5, now=_FROZEN_NOW)
            request_count = admit_request(session, agent_id, window, limit=3)
        assert (messages_count, request_count) == (1, 1)

        rows = _read_rows(database, agent_id)
        assert len(rows) == 1
        assert (rows[0].messages, rows[0].requests) == (1, 1)


# ---------------------------------------------------------------------------
# L2 — concurrent transaction tests (threads)
# ---------------------------------------------------------------------------


class TestAdmissionFinalSlotRace:
    """Two threads contend for the LAST remaining slot."""

    def test_exactly_one_admitted(self, database) -> None:
        agent_id = _provision_agent(database)
        window = _window_start()
        _seed_counter(database, agent_id, window, requests=2)  # one slot left

        executed = threading.Event()

        def racer_a():
            def run():
                with database.session() as sa:
                    _bound_locking(sa)
                    event.listen(
                        sa.get_bind(), "after_execute", _hold_quota_insert_then_signal(executed)
                    )
                    return admit_request(sa, agent_id, window, limit=3)

            return _classify(run)

        def racer_b():
            def run():
                with database.session() as sb:
                    _bound_locking(sb)
                    return admit_request(sb, agent_id, window, limit=3)

            return _classify(run)

        outcomes = _run_pair(racer_a, racer_b, executed)
        kinds = sorted(kind for kind, _ in outcomes.values())
        values = {key: value for key, (kind, value) in outcomes.items()}

        # CONTRACT: both calls return normally (a denial is a VALUE, not an
        # exception) — never an IntegrityError, lock timeout or crash.
        assert kinds == ["ok", "ok"], outcomes

        admitted = [key for key, value in values.items() if value is not None]
        denied = [key for key, value in values.items() if value is None]
        assert len(admitted) == 1 and len(denied) == 1, (
            f"final-slot race must admit exactly ONE of the two racers, "
            f"got admitted={admitted} denied={denied} ({values})"
        )
        assert values[admitted[0]] == 3, values

        counter = _read_counter(database, agent_id, window)
        assert counter is not None
        assert counter.requests == 3, (
            f"shared counter after the final-slot race is {counter.requests}, "
            "expected exactly 3 (the configured limit)"
        )


class TestAdmissionFirstWindowRace:
    """Two threads create the same first window simultaneously."""

    def test_one_row_both_counted(self, database) -> None:
        agent_id = _provision_agent(database)
        window = _window_start()

        executed = threading.Event()

        def racer_a():
            def run():
                with database.session() as sa:
                    _bound_locking(sa)
                    event.listen(
                        sa.get_bind(), "after_execute", _hold_quota_insert_then_signal(executed)
                    )
                    return admit_request(sa, agent_id, window, limit=2)

            return _classify(run)

        def racer_b():
            def run():
                with database.session() as sb:
                    _bound_locking(sb)
                    return admit_request(sb, agent_id, window, limit=2)

            return _classify(run)

        outcomes = _run_pair(racer_a, racer_b, executed)
        kinds = sorted(kind for kind, _ in outcomes.values())
        assert kinds == ["ok", "ok"], outcomes

        counts = sorted(value for _, value in outcomes.values() if value is not None)
        assert counts == [1, 2], outcomes  # both admitted, distinct authoritative counts

        rows = _read_rows(database, agent_id)
        assert len(rows) == 1, f"creation race produced {len(rows)} rows, expected ONE"
        assert rows[0].requests == 2, rows[0].requests


class TestAdmissionRollbackUnderConcurrency:
    """A racer that fails AFTER its admission must return its slot."""

    def test_rolled_back_racer_not_counted(self, database) -> None:
        agent_id = _provision_agent(database)
        window = _window_start()

        executed = threading.Event()

        def racer_a():
            def run():
                with database.session() as sa:
                    _bound_locking(sa)
                    event.listen(
                        sa.get_bind(), "after_execute", _hold_quota_insert_then_signal(executed)
                    )
                    admit_request(sa, agent_id, window, limit=3)
                    raise RuntimeError("downstream failure forces a rollback")

            return _classify(run)

        def racer_b():
            def run():
                with database.session() as sb:
                    _bound_locking(sb)
                    return admit_request(sb, agent_id, window, limit=3)

            return _classify(run)

        outcomes = _run_pair(racer_a, racer_b, executed)
        # racer A fails downstream of its admission (unexpected class),
        # racer B admits normally.
        assert outcomes["a"][0] == "unexpected", outcomes
        assert outcomes["b"] == ("ok", 1), outcomes

        rows = _read_rows(database, agent_id)
        assert len(rows) == 1
        assert rows[0].requests == 1, (
            f"the rolled-back admission must not persist (found {rows[0].requests})"
        )


class TestContrastLegacyTelemetryOverAdmits:
    """CONTRAST — the legacy F.1 telemetry UPSERT through the identical race.

    Evidence that the WHERE guard is the admission boundary: without it,
    both racers succeed and the durable count exceeds the intended shared
    limit — correct telemetry, but NOT an admission decision. This is why
    the two statements must never coexist on the request path (D3)."""

    def test_unconditional_upsert_admits_both_past_limit(self, database) -> None:
        agent_id = _provision_agent(database)
        window = _window_start()
        _seed_counter(database, agent_id, window, requests=2)

        executed = threading.Event()

        def racer_a():
            def run():
                with database.session() as sa:
                    _bound_locking(sa)
                    event.listen(
                        sa.get_bind(), "after_execute", _hold_quota_insert_then_signal(executed)
                    )
                    persist_request_count(sa, agent_id, window)
                    return "persisted"

            return _classify(run)

        def racer_b():
            def run():
                with database.session() as sb:
                    _bound_locking(sb)
                    persist_request_count(sb, agent_id, window)
                    return "persisted"

            return _classify(run)

        outcomes = _run_pair(racer_a, racer_b, executed)
        kinds = sorted(kind for kind, _ in outcomes.values())
        assert kinds == ["ok", "ok"], outcomes

        counter = _read_counter(database, agent_id, window)
        assert counter is not None
        assert counter.requests == 4, counter.requests
        # 4 > limit 3: the unconditional increment cannot refuse anyone —
        # which is exactly why the legacy call had to leave the request
        # path rather than run next to the guarded admission.


class TestAdmissionWithR06AConcurrency:
    """Concurrent request admission vs R06-A message admission."""

    def test_concurrent_message_admission_vs_request_admission(self, database) -> None:
        agent_id = _provision_agent(database)
        window = _window_start()

        executed = threading.Event()

        def racer_message():
            def run():
                with database.session() as sm:
                    _bound_locking(sm)
                    event.listen(
                        sm.get_bind(),
                        "after_execute",
                        _hold_quota_insert_then_signal(executed),
                    )
                    return check_message_quota(sm, agent_id, limit=5, now=_FROZEN_NOW)

            return _classify(run)

        def racer_request():
            def run():
                with database.session() as sr:
                    _bound_locking(sr)
                    return admit_request(sr, agent_id, window, limit=3)

            return _classify(run)

        outcomes = _run_pair(racer_message, racer_request, executed)
        kinds = sorted(kind for kind, _ in outcomes.values())
        assert kinds == ["ok", "ok"], outcomes

        rows = _read_rows(database, agent_id)
        assert len(rows) == 1
        assert (rows[0].messages, rows[0].requests) == (1, 1), (
            rows[0].messages,
            rows[0].requests,
        )


# ---------------------------------------------------------------------------
# L3 — genuine separate-process verification (spawn, PostgreSQL)
# ---------------------------------------------------------------------------


def _f2_spawn_worker(result_queue, url, agent_id, window_iso, attempts, limit) -> None:
    """Cold-start worker: its own engine, its own sessions, the PRODUCTION
    ``admit_request``. Runs in a pristine interpreter — exactly the
    memory-isolation model of separate deployment workers."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from commons_core.security.rate_limit import admit_request as production_admit

    window = datetime.fromisoformat(window_iso)
    engine = create_engine(url, pool_pre_ping=True)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    admitted = 0
    denied = 0
    errors: list[str] = []
    try:
        for _ in range(attempts):
            try:
                with factory() as session:
                    with session.begin():
                        new_count = production_admit(session, agent_id, window, limit=limit)
                if new_count is None:
                    denied += 1
                else:
                    admitted += 1
            except Exception as exc:  # noqa: BLE001 — classified per attempt
                errors.append(f"{type(exc).__name__}: {exc}"[:120])
    finally:
        engine.dispose()
    result_queue.put((admitted, denied, errors))


@pg_only
class TestMultiProcessAdmission:
    """Cold-start worker processes sharing ONE authoritative counter."""

    @staticmethod
    def _spawn_two(agent_id, attempts_a, attempts_b, limit):
        window_iso = _window_start().isoformat()
        ctx = multiprocessing.get_context("spawn")
        queue = ctx.Queue()
        processes = [
            ctx.Process(
                target=_f2_spawn_worker,
                args=(queue, PG_TEST_URL, agent_id, window_iso, attempts, limit),
            )
            for attempts in (attempts_a, attempts_b)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=_JOIN_TIMEOUT_S)
        assert not any(process.is_alive() for process in processes), "worker hung"
        assert all(process.exitcode == 0 for process in processes), "worker crashed"
        return [queue.get(timeout=_EVENT_WAIT_S) for _ in processes]

    def test_final_slot_across_processes_admits_exactly_one(self, database) -> None:
        agent_id = _provision_agent(database)
        window = _window_start()
        _seed_counter(database, agent_id, window, requests=2)  # one slot left

        results = self._spawn_two(agent_id, 1, 1, 3)
        admitted_total = sum(admitted for admitted, _, _ in results)
        denied_total = sum(denied for _, denied, _ in results)
        errors = [error for _, _, errs in results for error in errs]

        assert errors == [], errors
        assert admitted_total == 1 and denied_total == 1, results

        counter = _read_counter(database, agent_id, window)
        assert counter is not None
        assert counter.requests == 3, counter.requests

    def test_full_window_across_processes_respects_limit(self, database) -> None:
        agent_id = _provision_agent(database)
        window = _window_start()

        results = self._spawn_two(agent_id, 4, 4, 3)
        admitted_total = sum(admitted for admitted, _, _ in results)
        errors = [error for _, _, errs in results for error in errs]

        assert errors == [], errors
        assert admitted_total == 3, (
            f"two worker processes admitted {admitted_total} requests combined "
            f"against limit 3 ({results}) — admission is not shared"
        )

        counter = _read_counter(database, agent_id, window)
        assert counter is not None
        assert counter.requests == 3, counter.requests

    def test_first_window_across_processes_creates_one_row(self, database) -> None:
        agent_id = _provision_agent(database)

        results = self._spawn_two(agent_id, 1, 1, 2)
        admitted_total = sum(admitted for admitted, _, _ in results)
        errors = [error for _, _, errs in results for error in errs]

        assert errors == [], errors
        assert admitted_total == 2, results

        rows = _read_rows(database, agent_id)
        assert len(rows) == 1, f"cross-process creation produced {len(rows)} rows"
        assert rows[0].requests == 2, rows[0].requests

    def test_both_workers_observe_the_same_authoritative_limit(self, database) -> None:
        """Both cold-start workers see the SAME authoritative limit on the
        shared store: their combined decisions are indistinguishable from
        one worker's sequential decisions against one counter."""
        agent_id = _provision_agent(database)
        window = _window_start()

        results = self._spawn_two(agent_id, 3, 3, 3)
        admitted_total = sum(admitted for admitted, _, _ in results)
        errors = [error for _, _, errs in results for error in errs]

        assert errors == [], errors
        assert admitted_total == 3, results
        counter = _read_counter(database, agent_id, window)
        assert counter is not None and counter.requests == 3


# ---------------------------------------------------------------------------
# L4 — authenticated API integration (production middleware)
# ---------------------------------------------------------------------------


class TestSharedAdmissionAPI:
    """The production middleware path (not just the SQL helper)."""

    def test_authenticated_requests_admit_and_deny_at_limit(self, limited_client) -> None:
        token = provision_and_login(limited_client, "f2-api-agent")

        for _ in range(3):
            response = limited_client.get("/api/v1/agents/me", headers=auth_headers(token))
            assert response.status_code == 200, response.status_code
            assert response.json()["id"], response.json()

        denied = limited_client.get("/api/v1/agents/me", headers=auth_headers(token))
        assert denied.status_code == 429, denied.status_code

    def test_429_contract_shape_retry_after_and_limit(self, limited_client) -> None:
        token = provision_and_login(limited_client, "f2-api-contract")
        for _ in range(3):
            limited_client.get("/api/v1/agents/me", headers=auth_headers(token))

        denied = limited_client.get("/api/v1/agents/me", headers=auth_headers(token))
        assert denied.status_code == 429
        error = denied.json()["error"]
        assert error["code"] == "RATE_LIMITED", error
        assert error["limit"] == 3, error
        # Frozen clock at :15 → 45 s left in the window (deterministic).
        assert error["retry_after_seconds"] == 45, error
        assert denied.headers["Retry-After"] == "45", denied.headers

    def test_rate_limited_security_event_recorded(self, limited_client, database) -> None:
        from commons_core.database.models import SecurityEvent

        token = provision_and_login(limited_client, "f2-api-event")
        for _ in range(4):
            limited_client.get("/api/v1/agents/me", headers=auth_headers(token))

        with database.session() as session:
            events = list(
                session.scalars(
                    select(SecurityEvent).where(SecurityEvent.category == "rate_limited")
                )
            )
        assert len(events) == 1
        assert events[0].principal, "principal must be the agent id (never an IP)"
        assert events[0].details["limit"] == 3
        assert events[0].details["authenticated"] is True
        assert "window_start" in events[0].details

    def test_admitted_count_equals_durable_requests_no_double_counting(
        self, limited_client, database
    ) -> None:
        """D3: one admitted request → exactly ONE authoritative increment;
        the legacy telemetry call must not also run."""
        token = provision_and_login(limited_client, "f2-api-single-count")
        for _ in range(3):
            assert (
                limited_client.get("/api/v1/agents/me", headers=auth_headers(token)).status_code
                == 200
            )

        with database.session() as session:
            counters = list(session.scalars(select(QuotaCounter)))
        assert len(counters) == 1
        assert counters[0].requests == 3, (
            f"admission + legacy telemetry would double-count to 6; found {counters[0].requests}"
        )

    def test_middleware_no_longer_imports_legacy_persist(self) -> None:
        """D3 structural evidence: the legacy telemetry function is not even
        reachable from the middleware namespace any more."""
        import commons_core.api.middleware as middleware_module

        assert not hasattr(middleware_module, "persist_request_count"), (
            "the production middleware must not hold a reference to the legacy telemetry call (D3)"
        )

    def test_exempt_paths_bypass_admission_entirely(self, limited_client, database) -> None:
        """Exempt routes stay compatible: no admission, no quota rows."""
        for _ in range(5):
            response = limited_client.get("/health")
            assert response.status_code == 200, response.status_code

        with database.session() as session:
            rows = session.scalars(select(QuotaCounter)).all()
        assert rows == [], "exempt paths must not consume admission slots"

    def test_downstream_failure_does_not_refund_the_slot(
        self, settings, database, monkeypatch
    ) -> None:
        """A request that FAILS downstream of its committed admission keeps
        its slot (conservative semantics: the request consumed capacity)."""
        limited = settings.model_copy(update={"rate_limit_per_minute": 3})
        application = create_app(settings=limited, database=database)
        application.state.rate_limiter = type(application.state.rate_limiter)(
            per_agent_limit=3, clock=lambda: _FROZEN_NOW
        )

        async def _boom(agent=Depends(require_authenticated_agent)):
            raise RuntimeError("downstream failure")

        application.router.add_api_route("/api/v1/boom", _boom, methods=["GET"])

        # The boom route's RuntimeError must reach the STRUCTURED 500
        # handler (not propagate into the test client).
        with TestClient(application, raise_server_exceptions=False) as test_client:
            token = provision_and_login(test_client, "f2-api-downstream")
            headers = auth_headers(token)
            assert test_client.get("/api/v1/agents/me", headers=headers).status_code == 200
            assert test_client.get("/api/v1/agents/me", headers=headers).status_code == 200
            # The route itself explodes AFTER the committed admission.
            boom = test_client.get("/api/v1/boom", headers=headers)
            assert boom.status_code == 500, boom.status_code
            assert boom.json()["error"]["code"] == "INTERNAL", boom.json()
            # The 500 request consumed a slot: only the two earlier ones fit.
            assert test_client.get("/api/v1/agents/me", headers=headers).status_code == 429

        with database.session() as session:
            counters = list(session.scalars(select(QuotaCounter)))
        assert len(counters) == 1
        assert counters[0].requests == 3, counters[0].requests

    def test_anonymous_admission_and_denial_stay_private(self, settings, database) -> None:
        """Anonymous traffic keeps the in-memory limiter: no quota rows on
        either the admitted or the denied path."""
        anonymous = settings.model_copy(update={"anonymous_rate_limit_per_minute": 2})
        application = create_app(settings=anonymous, database=database)
        with TestClient(application) as test_client:
            for _ in range(2):
                assert test_client.get("/api/v1/agents/me").status_code == 401
            denied = test_client.get("/api/v1/agents/me")
            assert denied.status_code == 429, denied.status_code
            assert denied.json()["error"]["code"] == "RATE_LIMITED"

        with database.session() as session:
            rows = session.scalars(select(QuotaCounter)).all()
        assert rows == [], "anonymous traffic must never persist quota rows"


# ---------------------------------------------------------------------------
# L4 — genuine two-worker HTTP end-to-end (uvicorn, PostgreSQL)
# ---------------------------------------------------------------------------


def _uvicorn_worker(ready_queue, url: str, limit: int) -> None:
    """Spawn a REAL uvicorn server around the production app factory."""
    import socket

    import uvicorn

    from commons_core.api.app import create_app
    from commons_core.config.settings import Settings

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    application = create_app(
        settings=Settings(
            database_url=url,
            master_keys="",
            env="test",
            log_level="WARNING",
            rate_limit_per_minute=limit,
        )
    )
    config = uvicorn.Config(application, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    ready_queue.put(port)
    server.run()


@pg_only
class TestRealTwoWorkerHTTP:
    """Genuine multi-worker HTTP verification — two REAL uvicorn processes
    (separate interpreters, separate engines) behind one shared PostgreSQL
    store, driven over real HTTP sockets."""

    @pytest.fixture()
    def worker_ports(self, database):
        ctx = multiprocessing.get_context("spawn")
        ready = ctx.Queue()
        workers = [
            ctx.Process(target=_uvicorn_worker, args=(ready, PG_TEST_URL, 3)) for _ in range(2)
        ]
        for worker in workers:
            worker.start()
        ports = [ready.get(timeout=_JOIN_TIMEOUT_S) for _ in workers]
        # Wait for both servers to answer /health (bounded).
        deadline = time.monotonic() + 20
        for port in ports:
            while time.monotonic() < deadline:
                try:
                    if httpx.get(f"http://127.0.0.1:{port}/health", timeout=2).status_code == 200:
                        break
                except httpx.HTTPError:
                    time.sleep(0.1)
            else:
                raise AssertionError(f"worker on {port} never became healthy")
        yield ports
        for worker in workers:
            worker.terminate()
        for worker in workers:
            worker.join(timeout=_JOIN_TIMEOUT_S)

    def test_two_workers_share_admission_over_http(self, worker_ports, database) -> None:
        with database.session() as session:
            agent = provision_agent(session, "f2-http-shared-agent")
            from commons_core.security.authentication.tokens import issue_token

            token = issue_token(session, agent.id).raw_token
        headers = {"Authorization": f"Bearer {token}"}

        statuses: list[int] = []
        # Interleave requests across the two REAL workers.
        for _round_index in range(4):
            for port in worker_ports:
                response = httpx.get(
                    f"http://127.0.0.1:{port}/api/v1/agents/me", headers=headers, timeout=10
                )
                statuses.append(response.status_code)

        admitted = statuses.count(200)
        denied = statuses.count(429)
        assert admitted == 3, statuses
        assert denied == 8 - 3, statuses

        rows = _read_rows(database, agent.id)
        assert len(rows) == 1
        assert rows[0].requests == 3, rows[0].requests


# ---------------------------------------------------------------------------
# L5 — failure-injection tests (fail-closed policy D2)
# ---------------------------------------------------------------------------


def _pg_error_factory(error_cls):
    """Build an exc factory wrapping a real psycopg error class."""

    def _factory():
        import psycopg.errors

        orig = getattr(psycopg.errors, error_cls)()
        return DBAPIError("INSERT INTO quota_counters ...", {}, orig)

    return _factory


class TestFailClosedPolicy:
    """Authenticated admission failures are FAIL-CLOSED — no fallback."""

    @pytest.mark.parametrize(
        "exc_factory",
        [
            lambda: OperationalError("connection refused", None, RuntimeError("refused")),
            _pg_error_factory("QueryCanceled"),  # statement timeout
            _pg_error_factory("LockNotAvailable"),  # lock timeout
            _pg_error_factory("DeadlockDetected"),
            _pg_error_factory("SerializationFailure"),
            lambda: RuntimeError("deterministic pre-commit failure"),
        ],
        ids=[
            "operational-connection",
            "statement-timeout",
            "lock-timeout",
            "deadlock",
            "serialization",
            "generic-pre-commit",
        ],
    )
    def test_admission_failure_classes_are_fail_closed(
        self, limited_client, database, monkeypatch, exc_factory
    ) -> None:
        """Every admission STATEMENT-stage failure class is fail-closed.

        All six injections fire inside ``admit_request`` — BEFORE any
        commit — so each deterministically rolls back and consumes no slot
        (the assertion below). A 503 whose COMMIT outcome is unknown is a
        DIFFERENT case that MAY have consumed the slot: see
        ``TestAmbiguousCommitOutcome`` in
        ``test_r06b_f2_preintegration_review.py``.
        """
        token = provision_and_login(limited_client, "f2-failclosed-agent")

        def _fail(_session, _agent_id, _window, *, limit):
            raise exc_factory()

        monkeypatch.setattr("commons_core.api.middleware.admit_request", _fail)
        response = limited_client.get("/api/v1/agents/me", headers=auth_headers(token))

        assert response.status_code == 503, response.status_code
        error = response.json()["error"]
        assert error["code"] == "SERVICE_UNAVAILABLE", error
        assert error["retry_after_seconds"] >= 1, error
        assert "Retry-After" in response.headers

        # PRIVACY: no SQL, driver, host or credential leakage in the body.
        body = response.text.lower()
        for forbidden in ("psycopg", "select ", "insert ", "postgres", "quota_counters", "bearer"):
            assert forbidden not in body, f"leaked {forbidden!r} in {body!r}"

        # Pre-commit statement failure ⇒ deterministic rollback ⇒ no row.
        # (NOT a general claim about every 503: an ambiguous COMMIT may
        # have consumed the slot — see TestAmbiguousCommitOutcome.)
        with database.session() as session:
            rows = session.scalars(select(QuotaCounter)).all()
        assert rows == []

    def test_recovery_returns_to_authoritative_admission(
        self, limited_client, database, monkeypatch
    ) -> None:
        """After a PRE-COMMIT failure, the next request is admitted through
        the SAME authoritative statement — and that failed attempt consumed
        no slot (no double-counting retries).

        Scope of the claim: the injected failure fires inside
        ``admit_request`` before any commit, so its rollback is
        deterministic. A 503 caused by a commit whose result is unknown
        (connection lost after the server processed COMMIT) MAY have
        consumed the slot — that case is covered separately by
        ``TestAmbiguousCommitOutcome`` in
        ``test_r06b_f2_preintegration_review.py``.
        """
        token = provision_and_login(limited_client, "f2-recovery-agent")
        state = {"failed": False}

        def _flaky(session, agent_id, window, *, limit):
            if not state["failed"]:
                state["failed"] = True
                raise OperationalError("connection refused", None, RuntimeError("refused"))
            return admit_request(session, agent_id, window, limit=limit)

        monkeypatch.setattr("commons_core.api.middleware.admit_request", _flaky)
        first = limited_client.get("/api/v1/agents/me", headers=auth_headers(token))
        assert first.status_code == 503, first.status_code

        second = limited_client.get("/api/v1/agents/me", headers=auth_headers(token))
        assert second.status_code == 200, second.status_code

        with database.session() as session:
            counters = list(session.scalars(select(QuotaCounter)))
        assert len(counters) == 1
        assert counters[0].requests == 1, (
            f"the pre-commit 503 attempt must not consume a slot (found {counters[0].requests})"
        )

    def test_commit_failure_rolls_back_and_fails_closed(
        self, limited_client, database, monkeypatch
    ) -> None:
        """A COMMIT-stage failure that deterministically aborts BEFORE the
        transaction is durably applied (here: the commit call itself fails
        client-side, so the server never finalizes it) leaves no durable
        increment (rollback) and is reported as 503 — the decision is
        successful only after the transaction commits.

        NOT a general claim about every commit-stage failure: a commit
        whose result is UNKNOWN because the connection was lost after the
        server processed it may already have consumed the slot — see
        ``TestAmbiguousCommitOutcome`` in
        ``test_r06b_f2_preintegration_review.py``.
        """
        token = provision_and_login(limited_client, "f2-commitfail-agent")
        state = {"armed": True}
        original_commit = Session.commit

        def _failing_commit(self):
            if state["armed"]:
                state["armed"] = False
                raise OperationalError("commit failed", None, RuntimeError("commit failed"))
            return original_commit(self)

        monkeypatch.setattr("sqlalchemy.orm.Session.commit", _failing_commit)
        failed = limited_client.get("/api/v1/agents/me", headers=auth_headers(token))
        assert failed.status_code == 503, failed.status_code

        recovered = limited_client.get("/api/v1/agents/me", headers=auth_headers(token))
        assert recovered.status_code == 200, recovered.status_code

        with database.session() as session:
            counters = list(session.scalars(select(QuotaCounter)))
        assert len(counters) == 1
        assert counters[0].requests == 1, counters[0].requests

    def test_quota_denial_survives_event_persistence_failure(
        self, limited_client, database, monkeypatch
    ) -> None:
        """An event-recording failure after an authoritative quota denial
        must never accidentally admit the request."""
        token = provision_and_login(limited_client, "f2-eventfail-agent")
        for _ in range(3):
            assert (
                limited_client.get("/api/v1/agents/me", headers=auth_headers(token)).status_code
                == 200
            )

        def _raise_event(*_args, **_kwargs):
            raise RuntimeError("event store unavailable")

        monkeypatch.setattr("commons_core.api.middleware.record_event", _raise_event)
        denied = limited_client.get("/api/v1/agents/me", headers=auth_headers(token))
        assert denied.status_code == 429, denied.status_code
        assert denied.json()["error"]["code"] == "RATE_LIMITED"

        with database.session() as session:
            counters = list(session.scalars(select(QuotaCounter)))
        assert len(counters) == 1
        assert counters[0].requests == 3, counters[0].requests

    def test_admission_failure_does_not_fall_back_to_rate_limiter(
        self, limited_client, database, monkeypatch
    ) -> None:
        """D2 critical requirement: no code path may grant admission via
        ``RateLimiter.check`` (or any worker-local fallback) after the
        authoritative admission failed. The limiter's own counters stay
        untouched by the authenticated path."""
        limiter = limited_client.app.state.rate_limiter
        snapshot = dict(limiter._counts)
        token = provision_and_login(limited_client, "f2-nofallback-agent")

        def _fail(_session, _agent_id, _window, *, limit):
            raise OperationalError("admission unavailable", None, RuntimeError("unavailable"))

        monkeypatch.setattr("commons_core.api.middleware.admit_request", _fail)
        response = limited_client.get("/api/v1/agents/me", headers=auth_headers(token))
        assert response.status_code == 503, response.status_code

        # The authenticated principal never entered the in-memory limiter.
        assert limiter._counts == snapshot, (
            "an authenticated request must not be admitted (or even counted) "
            "by the worker-local fallback limiter"
        )


class TestAuthenticationInfrastructureFailure:
    """Pre-auth failures are 503, not disguised 401s — with focused
    regression coverage of the corrected behavior."""

    def test_full_outage_health_survives_authenticated_gets_503(self, tmp_path) -> None:
        """A REAL dead-port outage (connection refused, not a simulated
        exception): /health survives, an authenticated request receives
        the fail-closed 503 (previously an ambiguous 401)."""
        from commons_core.config.settings import Settings

        dead_url = "postgresql+psycopg://commons:pass@127.0.0.1:59999/commons"
        settings = Settings(database_url=dead_url, master_keys="", env="test", log_level="WARNING")
        app = create_app(settings=settings)
        with TestClient(app) as dead_client:
            health = dead_client.get("/health")
            assert health.status_code == 200, health.status_code

            auth = dead_client.get("/api/v1/agents/me", headers={"Authorization": "Bearer x" * 32})
            assert auth.status_code == 503, auth.status_code
            assert auth.json()["error"]["code"] == "SERVICE_UNAVAILABLE", auth.json()

    def test_full_outage_anonymous_path_stays_independent(self, tmp_path) -> None:
        """Anonymous requests must NOT depend on the database: admission
        stays in-memory, the route dependency reports 401 as usual."""
        from commons_core.config.settings import Settings

        dead_url = "postgresql+psycopg://commons:pass@127.0.0.1:59999/commons"
        settings = Settings(database_url=dead_url, master_keys="", env="test", log_level="WARNING")
        app = create_app(settings=settings)
        with TestClient(app) as dead_client:
            anonymous = dead_client.get("/api/v1/agents/me")
            assert anonymous.status_code == 401, anonymous.status_code
            assert anonymous.json()["error"]["code"] == "UNAUTHENTICATED", anonymous.json()

    def test_genuinely_invalid_credentials_keep_normal_401(self, client) -> None:
        """A real (non-infrastructure) authentication failure keeps the
        ordinary invalid-credential semantics — the 503 correction is
        scoped to infrastructure failures only."""
        response = client.get("/api/v1/agents/me", headers=auth_headers("bogus-token-value"))
        assert response.status_code == 401, response.status_code
        assert response.json()["error"]["code"] == "UNAUTHENTICATED", response.json()

        missing = client.get("/api/v1/agents/me")
        assert missing.status_code == 401, missing.status_code
        assert missing.json()["error"]["code"] == "UNAUTHENTICATED", missing.json()

    def test_fail_closed_response_logs_no_sensitive_data(
        self, limited_client, monkeypatch, caplog
    ) -> None:
        """Operational logging carries the exception CLASS only."""
        token = provision_and_login(limited_client, "f2-log-agent")

        def _fail(_session, _agent_id, _window, *, limit):
            raise OperationalError(
                "postgresql+psycopg://secret:not-a-real-credential@db.internal:5432/x",
                None,
                RuntimeError("host=secret-host password=hunter2"),
            )

        monkeypatch.setattr("commons_core.api.middleware.admit_request", _fail)
        with caplog.at_level(logging.WARNING, logger="commons_core.api.middleware"):
            response = limited_client.get("/api/v1/agents/me", headers=auth_headers(token))

        assert response.status_code == 503
        logged = " ".join(record.message for record in caplog.records)
        assert "authoritative admission failed class=OperationalError" in logged
        assert "secret" not in logged
        assert "hunter2" not in logged
        assert "db.internal" not in logged

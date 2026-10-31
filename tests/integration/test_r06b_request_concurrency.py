"""R06-B integration tests: request-rate enforcement & durable request counters.

Independently developed reproduction scenarios (R06-B — Independent
Reproduction and Remediation Readiness Review, baseline ``c418110``;
remediation status per R06-B F.1):

- B1  shared-principal admission across independent limiter instances
  - B1-c  control: ONE instance enforces the per-agent limit correctly
  - B1-i  two independent in-process instances, frozen clock, limit=3
  - B1-p  genuine multi-process (spawn) reproduction
- B2  concurrent ``persist_request_count()`` lost-update (PostgreSQL) —
  REMEDIATED by R06-B F.1 (atomic UPSERT)
- B3  simultaneous first-window creation race (PostgreSQL) — REMEDIATED
  by R06-B F.1 (primary-key conflict path)
- B4  composition with the merged R06-A atomic message-quota admission
  - B4-s  sequential composition (orthogonal columns) — control
  - B4-c  concurrent message admission vs request persist — REMEDIATED
    by R06-B F.1
- B5  privacy: anonymous traffic never persists a ``QuotaCounter`` row
  (both the admitted path and the 429 denial path)

Concurrency-control design (same discipline as the R06-A suite — no
deadlock-prone barrier):
- One-way ``threading.Event`` signalled AFTER the first racer's write has
  been executed (row locks held) — via a connection-level ``after_execute``
  hook on the quota-counters INSERT, because both the R06-A admission and
  the F.1 request mirror are Core UPSERT statements that never trigger an
  ORM flush; that racer then delays only ITS OWN commit for a bounded,
  time-driven window. Because the delayed committer is delayed by time
  alone, any correct row-locking / atomic-UPSERT implementation can always
  finish; a mutual barrier between execute and commit would instead
  deadlock it.
- Every racing session sets a bounded ``lock_timeout`` so any unexpected wait
  surfaces as a classified DBAPIError instead of hanging the suite.
- Outcomes are classified: ``ok`` / ``sql_integrity`` (IntegrityError) /
  ``dbapi_error`` (lock timeout, driver errors) / ``unexpected``. For
  ``persist_request_count`` — best-effort telemetry — the CONTRACT outcome is
  ``ok`` (returns normally, never leaks an IntegrityError to the caller); a
  lost increment or an escaped IntegrityError is a defect.
- Final persisted counters are always re-read through a FRESH session after
  both racers join (authoritative read-back, never a racer's stale ORM copy).

These scenarios assert the SPEC-correct behaviour. B2/B3/B4-c (remediated
by R06-B F.1) and the controls (B1-c, B4-s, B5) must PASS. The B1 sharing
scenarios (B1-i, B1-p) are EXPECTED TO FAIL while B1 remains OPEN — B1
(shared admission across limiter instances / processes) is explicitly
outside the authorized F.1 production scope, which only atomizes the
durable request counter. To keep that unresolved status visible without
misrepresenting CI as fully green, the B1 scenarios carry the
``r06b_open`` marker: the F.1 acceptance gate runs ``-m "not r06b_open"``
while a separate non-blocking run reports ``-m r06b_open``. When the
approved shared-admission workstream (R06-B F.2) lands, these tests must
turn green and the marker must be removed.

B1 is backend-independent (pure in-memory limiter). B2/B3/B4 target
PostgreSQL (the deployment backend whose multi-writer semantics expose the
read-modify-write races) and skip when ``AIC_TEST_DATABASE_URL`` is not
configured. B5 runs on every backend.
"""

import multiprocessing
import threading
import time
import uuid as uuidlib
from datetime import UTC, datetime

import pytest
from conftest import PG_TEST_URL
from sqlalchemy import event, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from commons_core.agents.service import provision_agent
from commons_core.database.enums import SecurityEventCategory
from commons_core.database.models import QuotaCounter, SecurityEvent
from commons_core.infrastructure.quotas import check_message_quota
from commons_core.security.rate_limit import RateLimiter, persist_request_count

pg_only = pytest.mark.skipif(
    not PG_TEST_URL,
    reason="R06-B persistence race scenarios run on PostgreSQL only (AIC_TEST_DATABASE_URL unset)",
)

#: Bounded wait protecting every racing transaction (seconds).
_LOCK_TIMEOUT_SQL = "SET LOCAL lock_timeout = '5s'"
#: How long the write-first racer delays its own commit. Time-driven
#: (never mutually-blocking) window guaranteeing the second racer's read
#: happens before the first racer commits.
_COMMIT_DELAY_S = 0.4
#: Frozen clock shared by every B1 limiter instance (same fixed window).
_B1_NOW = datetime(2026, 10, 30, 12, 0, 15, 654321, tzinfo=UTC)
#: Frozen window used by the B2/B3/B4 persistence scenarios.
_NOW = datetime(2026, 10, 30, 12, 30, 15, 123456, tzinfo=UTC)
_B1_PRINCIPAL = "11111111-1111-1111-1111-111111111111"
_JOIN_TIMEOUT_S = 30
_EVENT_WAIT_S = 10


def _window_start() -> datetime:
    """The fixed test window (deterministic, timezone-aware)."""
    return _NOW.replace(second=0, microsecond=0)


def _classify(run):
    """Execute ``run``; classify the outcome per the R06-B contract."""
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
    (the R06-A atomic admission UPSERT or the R06-B F.1 request-mirror
    UPSERT) has executed on OUR connection — row locks held, NOT yet
    committed — signal and delay only our own commit for a bounded window."""

    from sqlalchemy.sql.dml import Insert

    def _hook(connection, clauseelement, _multiparams, _params, _options, _result) -> None:
        if isinstance(clauseelement, Insert) and clauseelement.table.name == "quota_counters":
            executed.set()
            time.sleep(_COMMIT_DELAY_S)

    return _hook


def _provision_agent(database) -> str:
    """Provision and COMMIT an agent; return its id.

    Racing transactions run on their own sessions, so the agent (and any
    seeded rows) must be durably committed first."""
    handle = f"r06b-agent-{uuidlib.uuid4().hex[:10]}"
    with database.session() as session:
        agent = provision_agent(session, handle)
        return agent.id


def _read_counter(database, agent_id: str) -> QuotaCounter | None:
    """Fresh-session read-back of the window row for ``agent_id``."""
    with database.session() as session:
        return session.get(QuotaCounter, (agent_id, _window_start()))


def _run_pair(database, racer_a, racer_b, signal_a: threading.Event):
    """Start racer A, wait for its in-flight signal, then start racer B.

    Both racers are joined with a bounded timeout and must terminate. The
    coordination is one-way (A signals AFTER its write is executed; B starts
    strictly before A commits), so a correct implementation can never
    deadlock: A's delay is time-driven and bounded.
    """
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


# ---------------------------------------------------------------------------
# B1: independent limiter instances do not share admission state
# ---------------------------------------------------------------------------


class TestB1SingleInstanceControl:
    """Control: ONE RateLimiter enforces the per-agent limit correctly."""

    def test_single_instance_admits_limit_then_denies(self) -> None:
        limiter = RateLimiter(per_agent_limit=3, clock=lambda: _B1_NOW)
        decisions = [limiter.check(_B1_PRINCIPAL) for _ in range(4)]

        allowed = [d.allowed for d in decisions]
        total = sum(allowed)

        # Spec: within one authoritative limiter, at most ``limit`` requests
        # per principal per window are admitted.
        assert total <= 3, f"single instance admitted {total} > limit 3"
        assert allowed == [True, True, True, False], allowed
        assert decisions[-1].retry_after_seconds >= 1


@pytest.mark.r06b_open
class TestB1IndependentInstances:
    """B1-i: two independent in-process instances, shared principal, frozen
    window — the owner's Windows reproduction, modelled as object isolation
    (each instance owns a private ``_counts`` mapping).

    EXPECTED TO FAIL while B1 is OPEN (outside the R06-B F.1 production
    scope, which only atomizes the durable request counter). Marked
    ``r06b_open`` so the F.1 acceptance gate can deselect it without hiding
    its unresolved status; must turn green when the approved
    shared-admission workstream (R06-B F.2) lands."""

    def test_two_instances_combined_admission_within_limit(self) -> None:
        limit = 3
        east = RateLimiter(per_agent_limit=limit, clock=lambda: _B1_NOW)
        west = RateLimiter(per_agent_limit=limit, clock=lambda: _B1_NOW)

        admitted_east = sum(1 for _ in range(4) if east.check(_B1_PRINCIPAL).allowed)
        admitted_west = sum(1 for _ in range(4) if west.check(_B1_PRINCIPAL).allowed)
        total = admitted_east + admitted_west

        # Spec: per-principal admission must be shared across limiter
        # instances (a multi-worker deployment points several instances at
        # the same principal). The combined admission must respect the limit.
        assert total <= limit, (
            f"independent limiter instances admitted {total} requests combined "
            f"(east={admitted_east}, west={admitted_west}) for limit={limit} — "
            "per-principal admission is not shared across instances"
        )


def _b1_spawn_worker(admitted_queue, checks: int, limit: int, frozen_iso: str) -> None:
    """Genuine separate-process worker: build its own limiter, count.

    Must live at module level for the ``spawn`` start method. Runs in a
    pristine interpreter — exactly the memory-isolation model of separate
    deployment workers."""
    frozen = datetime.fromisoformat(frozen_iso)
    limiter = RateLimiter(per_agent_limit=limit, clock=lambda: frozen)
    admitted = sum(1 for _ in range(checks) if limiter.check(_B1_PRINCIPAL).allowed)
    admitted_queue.put(admitted)


@pytest.mark.r06b_open
class TestB1GenuineMultiprocess:
    """B1-p: genuine multi-process reproduction (spawn context).

    Distinguished from B1-i: the limiter state is not merely a second object
    in the same interpreter — each subprocess builds its own limiter from a
    cold start, mirroring separate deployment workers (e.g. multiple app
    processes behind one endpoint).

    EXPECTED TO FAIL while B1 is OPEN (outside the R06-B F.1 production
    scope). Marked ``r06b_open`` — see TestB1IndependentInstances."""

    def test_two_processes_combined_admission_within_limit(self) -> None:
        limit = 3
        checks = 4
        ctx = multiprocessing.get_context("spawn")
        admitted_queue = ctx.Queue()
        processes = [
            ctx.Process(
                target=_b1_spawn_worker,
                args=(admitted_queue, checks, limit, _B1_NOW.isoformat()),
            )
            for _ in range(2)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=_JOIN_TIMEOUT_S)
        assert not any(process.is_alive() for process in processes), "worker hung"
        assert all(process.exitcode == 0 for process in processes), "worker crashed"

        per_process = [admitted_queue.get(timeout=_EVENT_WAIT_S) for _ in processes]
        total = sum(per_process)

        # Spec: per-principal admission must be shared across processes.
        assert total <= limit, (
            f"genuinely separate processes admitted {total} requests combined "
            f"(per-process={per_process}) for limit={limit} — no shared "
            "admission exists across process boundaries"
        )


# ---------------------------------------------------------------------------
# B2: concurrent persist_request_count() lost-update (PostgreSQL)
# ---------------------------------------------------------------------------


@pg_only
class TestB2ConcurrentIncrementLostUpdate:
    """Two concurrent ``persist_request_count()`` calls on an existing row.

    Owner's Windows finding (baseline): both report success, but the final
    request counter is 2 instead of 3 (seed 1 + two increments); the message
    counter is untouched. Root cause at baseline: ``counter.requests +=
    amount`` is a Python-side read-modify-write flushed as an absolute-value
    UPDATE, so the second committer overwrites the first increment (lost
    update). REMEDIATED by R06-B F.1: one conditional UPSERT whose
    ``requests = requests + :amount`` is re-evaluated against the latest row
    version under the conflict arbiter."""

    def test_both_succeed_and_counter_equals_seed_plus_two(self, database) -> None:
        agent_id = _provision_agent(database)
        window = _window_start()
        seed_requests, seed_messages = 1, 7
        with database.session() as session:
            session.add(
                QuotaCounter(
                    agent_id=agent_id,
                    window_start=window,
                    requests=seed_requests,
                    messages=seed_messages,
                )
            )

        executed = threading.Event()

        def racer_a():
            def run():
                with database.session() as sa:
                    sa.execute(text(_LOCK_TIMEOUT_SQL))
                    event.listen(
                        sa.get_bind(), "after_execute", _hold_quota_insert_then_signal(executed)
                    )
                    persist_request_count(sa, agent_id, window)

            return _classify(run)

        def racer_b():
            def run():
                with database.session() as sb:
                    sb.execute(text(_LOCK_TIMEOUT_SQL))
                    persist_request_count(sb, agent_id, window)

            return _classify(run)

        outcomes = _run_pair(database, racer_a, racer_b, executed)

        kinds = sorted(kind for kind, _ in outcomes.values())
        # CONTRACT: best-effort telemetry must return normally for both
        # callers — never an IntegrityError, lock timeout or unexpected raise.
        assert kinds == ["ok", "ok"], outcomes

        counter = _read_counter(database, agent_id)
        assert counter is not None, "quota counter row must exist"
        # Spec: seed(1) + two successful increments must persist as 3.
        assert counter.requests == seed_requests + 2, (
            f"lost update: both persist_request_count() calls reported success "
            f"but the durable request counter is {counter.requests}, expected "
            f"{seed_requests + 2}"
        )
        # The message counter must remain untouched by request mirroring.
        assert counter.messages == seed_messages, counter.messages


# ---------------------------------------------------------------------------
# B3: simultaneous first-window creation race (PostgreSQL)
# ---------------------------------------------------------------------------


@pg_only
class TestB3FirstWindowCreationRace:
    """Two concurrent ``persist_request_count()`` calls with no existing row.

    Owner's Windows finding (baseline): one succeeds, the other surfaces an
    ``IntegrityError`` (duplicate PK); the final request counter is 1 instead
    of 2. Root cause at baseline: ``session.get() → None → session.add()`` —
    both racers insert the same (agent_id, window_start) primary key.
    REMEDIATED by R06-B F.1: the creation race resolves through the
    primary-key conflict path of the atomic UPSERT."""

    def test_single_row_created_and_both_counted(self, database) -> None:
        agent_id = _provision_agent(database)
        window = _window_start()

        executed = threading.Event()

        def racer_a():
            def run():
                with database.session() as sa:
                    sa.execute(text(_LOCK_TIMEOUT_SQL))
                    event.listen(
                        sa.get_bind(), "after_execute", _hold_quota_insert_then_signal(executed)
                    )
                    persist_request_count(sa, agent_id, window)

            return _classify(run)

        def racer_b():
            def run():
                with database.session() as sb:
                    sb.execute(text(_LOCK_TIMEOUT_SQL))
                    persist_request_count(sb, agent_id, window)

            return _classify(run)

        outcomes = _run_pair(database, racer_a, racer_b, executed)

        kinds = sorted(kind for kind, _ in outcomes.values())
        # CONTRACT: both mirrors succeed through ONE row; the creation race
        # must resolve internally (atomic upsert), never surface as an
        # IntegrityError, lock timeout or unexpected raise.
        assert kinds == ["ok", "ok"], outcomes

        counter = _read_counter(database, agent_id)
        assert counter is not None, "quota counter row must exist"
        # Spec: two simultaneous first-window mirrors must persist as 2.
        assert counter.requests == 2, (
            f"creation race: final request counter is {counter.requests}, "
            "expected 2 (one row, both increments)"
        )


# ---------------------------------------------------------------------------
# B4: composition with the merged R06-A atomic message-quota admission
# ---------------------------------------------------------------------------


class TestB4SequentialCompositionControl:
    """Control: sequential message admission + request persist compose.

    Backend-agnostic (like the R06-A scenarios 4-5). Proves the two counters
    are orthogonal columns of the same row and that the R06-A atomic UPSERT
    (which creates the row with requests=0) composes correctly with request
    mirroring when no race is involved."""

    def test_message_admission_then_request_persist(self, database) -> None:
        agent_id = _provision_agent(database)
        now = _NOW

        with database.session() as session:
            new_count = check_message_quota(session, agent_id, limit=5, now=now)
            assert new_count == 1
            persist_request_count(session, agent_id, _window_start())

        counter = _read_counter(database, agent_id)
        assert counter is not None, "quota counter row must exist"
        assert (counter.messages, counter.requests) == (1, 1), (
            counter.messages,
            counter.requests,
        )


@pg_only
class TestB4ConcurrentAdmissionInterference:
    """Concurrent R06-A message admission vs R06-B request persist.

    The message side is the already-merged atomic UPSERT (R06-A); the request
    side is the unremediated ``persist_request_count()``. When both create
    the same first window simultaneously, the request side's bare INSERT
    collides with the (uncommitted) UPSERT-created row and surfaces an
    IntegrityError — the B3 defect interfering with a composed R06-A flow."""

    def test_both_admit_and_counters_compose(self, database) -> None:
        agent_id = _provision_agent(database)
        window = _window_start()

        executed = threading.Event()

        def racer_message():
            def run():
                with database.session() as sm:
                    event.listen(
                        sm.get_bind(),
                        "after_execute",
                        _hold_quota_insert_then_signal(executed),
                    )
                    sm.execute(text(_LOCK_TIMEOUT_SQL))
                    return check_message_quota(sm, agent_id, limit=5, now=_NOW)

            return _classify(run)

        def racer_request():
            def run():
                with database.session() as sr:
                    sr.execute(text(_LOCK_TIMEOUT_SQL))
                    persist_request_count(sr, agent_id, window)

            return _classify(run)

        outcomes = _run_pair(database, racer_message, racer_request, executed)

        kinds = sorted(kind for kind, _ in outcomes.values())
        # CONTRACT: both admissions succeed; message=1 and request=1 persist
        # through ONE row. The R06-A UPSERT resolves its side atomically;
        # the request mirror must not escape an IntegrityError.
        assert kinds == ["ok", "ok"], outcomes

        counter = _read_counter(database, agent_id)
        assert counter is not None, "quota counter row must exist"
        assert (counter.messages, counter.requests) == (1, 1), (
            counter.messages,
            counter.requests,
        )


# ---------------------------------------------------------------------------
# B5: anonymous traffic is never persisted (privacy)
# ---------------------------------------------------------------------------


class TestB5AnonymousNeverPersisted:
    """Privacy guard: anonymous traffic never writes a QuotaCounter row.

    Covers both the admitted path (requests pass the limiter) and the denial
    path (HTTP 429). The rate_limited security event must record the literal
    principal ``anonymous`` — never a client IP."""

    def test_admitted_anonymous_requests_leave_no_counter_rows(self, client, database) -> None:
        for _ in range(3):
            response = client.get("/api/v1/agents")
            # 401 (unauthenticated) or 404 (no collection route) is fine: the
            # middleware still ran and the limiter admitted the request.
            assert response.status_code in (200, 401, 404), response.status_code

        with database.session() as session:
            rows = session.scalars(select(QuotaCounter)).all()
        assert rows == [], f"anonymous traffic persisted {len(rows)} QuotaCounter row(s)"

    def test_denied_anonymous_requests_leave_no_counter_rows_and_no_ip_principal(
        self, client, database
    ) -> None:
        # Default anonymous limit is 30/minute; exhaust it, then go one more.
        for _ in range(30):
            client.get("/api/v1/agents")
        denied = client.get("/api/v1/agents")

        assert denied.status_code == 429, denied.status_code
        body = denied.json()
        assert body["error"]["code"] == "RATE_LIMITED", body

        with database.session() as session:
            rows = session.scalars(select(QuotaCounter)).all()
            events = session.scalars(
                select(SecurityEvent).where(
                    SecurityEvent.category == SecurityEventCategory.RATE_LIMITED
                )
            ).all()

        assert rows == [], f"denied anonymous traffic persisted {len(rows)} QuotaCounter row(s)"
        assert len(events) == 1, [e.principal for e in events]
        assert events[0].principal == "anonymous", events[0].principal

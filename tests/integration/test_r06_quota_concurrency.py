"""R06-A integration tests: atomic message-quota admission under concurrency.

Reproduction scenarios (R06-A — Atomic Message Quota Concurrency):
- R06-A-1  two transactions competing for the final available slot
- R06-A-2  simultaneous creation of a previously nonexistent quota window
- R06-A-3  message-sequence conflict rollback followed by retry (the retry
  must not silently discard an already-admitted quota increment)
- R06-A-4  correct rollback when message persistence fails (quota is not
  consumed by an unpersisted message)
- R06-A-5  successful message persistence and durable quota consumption stay
  in one coherent transaction

Concurrency control design (no deadlock-prone barrier):
- Threads coordinate through a one-way ``threading.Event`` signalled from an
  ``after_flush`` session hook: the first transaction has ALREADY flushed its
  write (row locks held) and merely delays its OWN commit for a bounded,
  time-driven window. The delayed committer is delayed by time alone, so any
  correct row-locking / conditional-UPSERT implementation can always finish;
  a barrier placed between flush and commit would instead deadlock it.
- Every racing session sets a bounded ``lock_timeout`` so any unexpected wait
  surfaces as a classified DBAPIError instead of hanging the suite.
- Outcomes are classified: ``ok`` / ``denied`` (QuotaExceededError) /
  ``sql_integrity`` (IntegrityError) / ``dbapi_error`` (lock timeout, driver
  errors) / ``unexpected``. Only ``ok`` and ``denied`` are acceptable contract
  outcomes for quota admission; anything else is a defect.

Scenarios 1-3 target PostgreSQL (the deployment backend whose multi-writer
semantics expose the read-modify-write races) and are skipped when
``AIC_TEST_DATABASE_URL`` is not configured. Scenarios 4-5 are backend
agnostic and run on both SQLite and PostgreSQL.
"""

import secrets
import threading
import time
import uuid as uuidlib
from datetime import UTC, datetime

import pytest
from conftest import PG_TEST_URL
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
from sqlalchemy.orm import Session

from commons_core.agents.service import provision_agent
from commons_core.api.errors import QuotaExceededError
from commons_core.communication import service as comm_service
from commons_core.database.models import Agent, Channel, Message, QuotaCounter
from commons_core.infrastructure.quotas import check_message_quota
from commons_core.security.cryptography import EnvelopeCipher, KeyStore

pg_only = pytest.mark.skipif(
    not PG_TEST_URL,
    reason="R06-A concurrency scenarios run on PostgreSQL only (AIC_TEST_DATABASE_URL unset)",
)

#: Bounded wait protecting every racing transaction (seconds).
_LOCK_TIMEOUT_SQL = "SET LOCAL lock_timeout = '5s'"
#: How long the flushed-first transaction delays its own commit. This is a
#: time-driven (never mutually-blocking) window that guarantees the second
#: transaction's read happens before the first one commits.
_COMMIT_DELAY_S = 0.4
_NOW = datetime(2026, 10, 30, 11, 30, 15, 123456, tzinfo=UTC)


def _window_start() -> datetime:
    """The fixed test window (deterministic, timezone-aware)."""
    return _NOW.replace(second=0, microsecond=0)


def _classify(run):
    """Execute ``run``; classify the outcome per the R06-A contract."""
    try:
        return ("ok", run())
    except QuotaExceededError:
        return ("denied", None)
    except IntegrityError as exc:
        return ("sql_integrity", str(exc.orig)[:120])
    except DBAPIError as exc:
        return ("dbapi_error", str(exc.orig)[:120])
    except Exception as exc:  # noqa: BLE001 — classified, never swallowed silently
        return ("unexpected", f"{type(exc).__name__}: {exc}"[:120])


def _hold_quota_upsert_then_signal(flushed: threading.Event):
    """Connection-level ``after_execute`` hook: when the atomic admission
    UPSERT has executed on OUR connection (row locks held), signal and delay
    only our own commit for a bounded window (time-driven; cannot deadlock
    the other transaction)."""

    from sqlalchemy.sql.dml import Insert

    def _hook(connection, clauseelement, _multiparams, _params, _options, _result) -> None:
        if isinstance(clauseelement, Insert) and clauseelement.table.name == "quota_counters":
            flushed.set()
            time.sleep(_COMMIT_DELAY_S)

    return _hook


def _hold_flush_then_signal(flushed: threading.Event):
    """``after_flush`` hook (ORM inserts): signal that OUR write is in-flight,
    then delay only our own commit for a bounded window."""

    def _hook(session: Session, _flush_context) -> None:
        flushed.set()
        time.sleep(_COMMIT_DELAY_S)

    return _hook


def _test_cipher() -> EnvelopeCipher:
    """A runtime-synthetic envelope cipher (no production keys)."""
    return EnvelopeCipher(KeyStore(f"k1:{secrets.token_hex(32)}", "k1", env="test"))


def _provision_agent(database) -> tuple[str, str]:
    """Provision and COMMIT an agent; return (agent_id, handle).

    Racing transactions run on their own sessions, so the agent (and any
    seeded rows) must be durably committed first."""
    handle = f"r06a-agent-{uuidlib.uuid4().hex[:10]}"
    with database.session() as session:
        agent = provision_agent(session, handle)
        return agent.id, agent.handle


def _make_channel(database, agent_id: str) -> str:
    """Create a group channel owned by the agent; return its id (committed)."""
    with database.session() as session:
        channel = comm_service.create_channel(
            session,
            session.get(Agent, agent_id),
            handle=f"r06a-{uuidlib.uuid4().hex[:12]}",
            kind="group",
        )
        return channel.id


# ---------------------------------------------------------------------------
# R06-A-1: two transactions competing for the final available slot
# ---------------------------------------------------------------------------


@pg_only
class TestFinalSlotRace:
    def test_exactly_one_admission_and_durable_limit(self, database) -> None:
        agent_id, agent_handle = _provision_agent(database)
        window = _window_start()
        limit = 5
        with database.session() as session:
            session.add(
                QuotaCounter(agent_id=agent_id, window_start=window, requests=0, messages=limit - 1)
            )

        flushed = threading.Event()

        def racer_a():
            def run():
                with database.session() as sa:
                    sa.execute(text(_LOCK_TIMEOUT_SQL))
                    event.listen(
                        sa.connection(), "after_execute", _hold_quota_upsert_then_signal(flushed)
                    )
                    return check_message_quota(sa, agent_id, limit=limit, now=_NOW)

            return _classify(run)

        def racer_b():
            def run():
                with database.session() as sb:
                    sb.execute(text(_LOCK_TIMEOUT_SQL))
                    return check_message_quota(sb, agent_id, limit=limit, now=_NOW)

            return _classify(run)

        outcomes: dict[str, tuple] = {}
        thread_a = threading.Thread(target=lambda: outcomes.__setitem__("a", racer_a()))
        thread_a.start()
        assert flushed.wait(timeout=10), "racer A never flushed its admission"
        # B starts strictly after A's write is in-flight and before A's commit.
        thread_b = threading.Thread(target=lambda: outcomes.__setitem__("b", racer_b()))
        thread_b.start()
        thread_a.join(timeout=30)
        thread_b.join(timeout=30)
        assert not thread_a.is_alive() and not thread_b.is_alive()

        kinds = sorted(kind for kind, _ in outcomes.values())
        # Exactly one admission; the loser must be a CONTRACT denial — never an
        # IntegrityError, lock timeout or unexpected exception.
        assert kinds == ["denied", "ok"], outcomes

        with database.session() as session:
            row = session.get(QuotaCounter, (agent_id, window))
            assert row is not None
            assert row.messages == limit, (
                f"durable counter must equal the limit after the race (got {row.messages})"
            )


# ---------------------------------------------------------------------------
# R06-A-2: simultaneous creation of a previously nonexistent quota window
# ---------------------------------------------------------------------------


@pg_only
class TestFirstWindowCreationRace:
    def test_single_row_created_and_both_counted(self, database) -> None:
        agent_id, _agent_handle = _provision_agent(database)
        window = _window_start()
        limit = 3

        flushed = threading.Event()

        def racer_a():
            def run():
                with database.session() as sa:
                    sa.execute(text(_LOCK_TIMEOUT_SQL))
                    event.listen(
                        sa.connection(), "after_execute", _hold_quota_upsert_then_signal(flushed)
                    )
                    return check_message_quota(sa, agent_id, limit=limit, now=_NOW)

            return _classify(run)

        def racer_b():
            def run():
                with database.session() as sb:
                    sb.execute(text(_LOCK_TIMEOUT_SQL))
                    return check_message_quota(sb, agent_id, limit=limit, now=_NOW)

            return _classify(run)

        outcomes: dict[str, tuple] = {}
        thread_a = threading.Thread(target=lambda: outcomes.__setitem__("a", racer_a()))
        thread_a.start()
        assert flushed.wait(timeout=10), "racer A never flushed its insert"
        thread_b = threading.Thread(target=lambda: outcomes.__setitem__("b", racer_b()))
        thread_b.start()
        thread_a.join(timeout=30)
        thread_b.join(timeout=30)
        assert not thread_a.is_alive() and not thread_b.is_alive()

        kinds = sorted(kind for kind, _ in outcomes.values())
        # Both sends are within the limit: both must be admitted through ONE
        # row; the creation race must not surface as an IntegrityError.
        assert kinds == ["ok", "ok"], outcomes

        with database.session() as session:
            rows = list(
                session.scalars(
                    select(QuotaCounter).where(
                        QuotaCounter.agent_id == agent_id, QuotaCounter.window_start == window
                    )
                )
            )
            assert len(rows) == 1, "exactly one window row must exist"
            assert rows[0].messages == 2


# ---------------------------------------------------------------------------
# R06-A-3: message-sequence conflict rollback followed by retry
# ---------------------------------------------------------------------------


@pg_only
class TestSequenceConflictRetry:
    def test_retry_preserves_quota_admission(self, database) -> None:
        agent_id, agent_handle = _provision_agent(database)
        window = _window_start()
        limit = 10
        cipher = _test_cipher()
        channel_id = _make_channel(database, agent_id)

        inserted = threading.Event()

        def other_tx():
            """Insert sequence 1, hold the index entry uncommitted briefly."""
            with database.session() as so:
                event.listen(so, "after_flush", _hold_flush_then_signal(inserted))

                def run():
                    so.execute(text(_LOCK_TIMEOUT_SQL))
                    message = Message(
                        id=str(uuidlib.uuid4()),
                        channel_id=channel_id,
                        sender_id=agent_id,
                        content_type="text/plain",
                        envelope={"ct": "r06a-placeholder"},
                        sequence=1,
                        size_bytes=1,
                    )
                    so.add(message)
                    so.flush()
                    return message.id

                return _classify(run)

        def main_tx():
            """Quota admission FIRST (route order), then a send that collides
            on sequence 1 and must retry — the admission must survive."""
            inserted.wait(timeout=10)
            with database.session() as sm:

                def run():
                    sm.execute(text(_LOCK_TIMEOUT_SQL))
                    check_message_quota(sm, agent_id, limit=limit, now=_NOW)
                    channel = sm.get(Channel, channel_id)
                    message = comm_service.send_message(
                        sm,
                        sm.get(Agent, agent_id),
                        channel,
                        content_type="text/plain",
                        payload="r06a sequence race",
                        cipher=cipher,
                        max_bytes=4096,
                    )
                    return message.sequence

                return _classify(run)

        outcomes: dict[str, tuple] = {}
        thread_other = threading.Thread(target=lambda: outcomes.__setitem__("other", other_tx()))
        thread_other.start()
        thread_main = threading.Thread(target=lambda: outcomes.__setitem__("main", main_tx()))
        thread_main.start()
        thread_other.join(timeout=30)
        thread_main.join(timeout=30)
        assert not thread_other.is_alive() and not thread_main.is_alive()

        assert outcomes["other"][0] == "ok", outcomes
        assert outcomes["main"][0] == "ok", outcomes
        assert outcomes["main"][1] == 2, "the retried send must land on sequence 2"

        with database.session() as session:
            counter = session.get(QuotaCounter, (agent_id, window))
            assert counter is not None, "quota counter row must exist"
            # The retried send must not silently discard the admission taken
            # before the conflict: one persisted message => one consumed slot.
            assert counter.messages == 1, (
                f"quota admission was lost across the sequence retry (counter={counter.messages})"
            )
            sequences = sorted(
                session.scalars(select(Message.sequence).where(Message.channel_id == channel_id))
            )
            assert sequences == [1, 2]


# ---------------------------------------------------------------------------
# R06-A-4: correct rollback when message persistence fails
# ---------------------------------------------------------------------------


class TestMessagePersistenceFailure:
    def test_quota_not_consumed_when_message_fails(self, database) -> None:
        agent_id, agent_handle = _provision_agent(database)
        window = _window_start()
        cipher = _test_cipher()
        channel_id = _make_channel(database, agent_id)

        session = database.sessionmaker()
        try:

            def fail_message_flush(sess: Session, _flush_context, _instances) -> None:
                for obj in sess.new:
                    if isinstance(obj, Message):
                        raise OperationalError(
                            "injected persistence failure", None, RuntimeError("r06a-boom")
                        )

            event.listen(session, "before_flush", fail_message_flush)
            check_message_quota(session, agent_id, limit=10, now=_NOW)
            channel = session.get(Channel, channel_id)
            with pytest.raises(OperationalError):
                comm_service.send_message(
                    session,
                    session.get(Agent, agent_id),
                    channel,
                    content_type="text/plain",
                    payload="will not persist",
                    cipher=cipher,
                    max_bytes=4096,
                )
            session.rollback()  # what the route's get_session does on error
        finally:
            session.close()

        with database.session() as verify:
            message_count = verify.scalar(select(func.count()).select_from(Message))
            assert message_count == 0
            row = verify.get(QuotaCounter, (agent_id, window))
            assert row is None or row.messages == 0, (
                "quota must not be consumed by a message that failed to persist"
            )


# ---------------------------------------------------------------------------
# R06-A-5: successful persistence and durable quota consumption are atomic
# ---------------------------------------------------------------------------


class TestSendQuotaAtomicity:
    def test_success_commits_both_and_failure_commits_neither(self, database) -> None:
        agent_id, agent_handle = _provision_agent(database)
        window = _window_start()
        cipher = _test_cipher()
        channel_id = _make_channel(database, agent_id)

        # (a) success: the message and its quota consumption commit together
        with database.session() as session:
            channel = session.get(Channel, channel_id)
            check_message_quota(session, agent_id, limit=5, now=_NOW)
            comm_service.send_message(
                session,
                session.get(Agent, agent_id),
                channel,
                content_type="text/plain",
                payload="atomic success",
                cipher=cipher,
                max_bytes=4096,
            )

        with database.session() as verify:
            assert verify.scalar(select(func.count()).select_from(Message)) == 1
            counter = verify.get(QuotaCounter, (agent_id, window))
            assert counter is not None and counter.messages == 1

        # (b) final-commit failure: neither the message nor the consumption
        # may survive (no half-applied state in either direction)
        session = database.sessionmaker()
        try:

            def fail_commit(sess: Session) -> None:
                # Only the ROOT commit fails; SAVEPOINT commits (released by
                # the sequence-retry path inside send_message) must pass.
                if sess.in_nested_transaction():
                    return
                raise OperationalError("injected commit failure", None, RuntimeError("r06a-boom"))

            event.listen(session, "before_commit", fail_commit)
            channel = session.get(Channel, channel_id)
            check_message_quota(session, agent_id, limit=5, now=_NOW)
            comm_service.send_message(
                session,
                session.get(Agent, agent_id),
                channel,
                content_type="text/plain",
                payload="atomic failure",
                cipher=cipher,
                max_bytes=4096,
            )
            with pytest.raises(OperationalError):
                session.commit()
            session.rollback()
        finally:
            session.close()

        with database.session() as verify:
            assert verify.scalar(select(func.count()).select_from(Message)) == 1
            counter = verify.get(QuotaCounter, (agent_id, window))
            assert counter is not None and counter.messages == 1

"""Regression tests: monotonic lease generation & database fencing (R02-B1).

RV-2 (Stage 2.2 T0) reproduced — deterministically, on the disposable
PostgreSQL instance — that a worker whose lease had been recovered and
re-claimed could still EXECUTE the task and have its result/state writes
ACCEPTED (RV2-05): the scheduler schema recorded only ``lease_expires_at``
(no holder, token or epoch), so "who holds the lease" did not exist in the
database and the only guard was an application-side read of a possibly stale
ORM object. The R02-B design review additionally identified the same
unguarded-write exposure in ``expire_leases`` (W2: SELECT → unconditional
ORM writes), which the R02-B1 harness dynamically reproduced on the
baseline (five-step before_flush interleaving — E-1).

These tests lock in the remediated contract (design review §5 rev, test
matrix A.3 invariants):

* every successful claim atomically increments ``lease_generation`` on the
  database side (exactly one increment per claim; losers increment nothing);
* worker-owned transitions (execution-start / success / failure / retry)
  re-validate the generation held at claim time AT THE DATABASE BOUNDARY
  (conditional UPDATE + rowcount gate) — a superseded lease raises
  ``LeaseSupersededError``, writes ZERO bytes, never invokes the handler and
  is never converted into an ordinary task failure;
* the legitimate claim holder is never fenced (in-lease completion,
  lease-expiry boundary at ``lease_expires_at == now``, and the post-lease
  pre-reclaim completion window all keep working);
* lease recovery (``expire_leases``) is a system-owned conditional bulk
  UPDATE: it cannot overwrite a concurrently committed terminal state, its
  returned count equals the rows actually recovered, and it never touches
  the generation (monotonicity — INV-2);
* accounting is frozen: attempts increment ONLY at claim; backoff and the
  ``failed`` exhaustion terminal state behave exactly as before (INV-3);
* R02-A cancellation semantics are untouched (409 fail-closed, byte-identical
  error) and the two defensive layers (conditional cancel / generation CAS)
  do not interfere;
* a forged CURRENT generation still passes the CAS — fencing is freshness,
  NOT identity (threat-model actor 3, design review §2.5): the boundary is
  documented by a deliberately-named test, not silently pretended away;
* a fencing-aware worker refuses to run against a schema without the
  fencing column (fail-closed startup gate, OG-1/DG-1);
* revocation remains ORTHOGONAL to scheduling (RV2-06 documented as pending
  owner policy — this suite records the current behavior without inventing
  a new guarantee).

The stale-object interleavings are simulated WITHOUT harness threads
(deterministic, cancel-race pattern): a claim-era object is detached, the
authoritative state advances in another transaction, and the stale object
is re-attached (``merge(load=False)``) before the fenced write.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from commons_core.agents.service import provision_agent, revoke_agent
from commons_core.database.enums import TaskStatus
from commons_core.database.models import ScheduledTask
from commons_core.scheduler import registry, service
from commons_core.scheduler.service import (
    FencingSchemaError,
    InvalidStateError,
    LeaseSupersededError,
)
from commons_core.security.authentication.errors import AuthenticationError
from commons_core.security.authentication.tokens import issue_token, verify_token

T0 = datetime(2030, 1, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_task(session, agent, *, kind="noop", max_attempts=3, run_at=T0) -> ScheduledTask:
    task, _created = service.create_task(
        session,
        agent,
        kind=kind,
        payload={"note": "lease-fencing-regression"},
        run_at=run_at,
        max_attempts=max_attempts,
        max_pending_tasks=1000,
        max_task_attempts=10,
    )
    return task


def _authoritative_row(database, task_id):
    """Fresh-session core read of the authoritative row (no ORM identity map)."""
    session = database.sessionmaker()
    try:
        return session.execute(
            text(
                "SELECT status, attempts, lease_generation, lease_expires_at, "
                "finished_at, last_error, run_at FROM scheduled_tasks WHERE id = :tid"
            ),
            {"tid": task_id},
        ).first()
    finally:
        session.close()


def _claim_committed(database, now, lease_seconds=30):
    """Claim in its own committed transaction; returns (id, generation)."""
    with database.session() as session:
        claimed = service.claim_next(session, now, lease_seconds)
        assert claimed is not None
        task_id = str(claimed.id)
        # The R02-B1 worker pattern: the generation is read INSIDE the claim
        # transaction (the post-claim expired object re-reads the DB-side
        # incremented value) and carried across the transaction boundary.
        generation = int(claimed.lease_generation)
    return task_id, generation


def _detach_claim_object(database, task_id):
    """Load + detach the task (stale-window construction, cancel-race pattern)."""
    session_a = database.sessionmaker()
    try:
        obj = service.get_task(session_a, task_id)
        session_a.expunge(obj)
        return obj
    finally:
        session_a.close()


class _RecordingHandler:
    """Deterministic handler that counts invocations (session-external)."""

    def __init__(self, *, fail=False):
        self.calls = 0
        self.fail = fail

    def __call__(self, session, task) -> None:
        self.calls += 1
        if self.fail:
            raise RuntimeError(f"synthetic failure #{self.calls}")


# ---------------------------------------------------------------------------
# R02B-01/12/14 — claim generation accounting (INV-2)
# ---------------------------------------------------------------------------


class TestClaimGeneration:
    def test_pending_row_starts_at_generation_zero(self, session, session_agent) -> None:
        task = _create_task(session, session_agent)
        session.commit()
        assert task.lease_generation == 0  # never claimed → initial epoch (INV-9)

    def test_claim_raises_generation_to_exactly_one(self, database, session, session_agent) -> None:
        task = _create_task(session, session_agent)
        session.commit()
        task_id = str(task.id)

        task_id_claimed, generation = _claim_committed(database, T0)
        assert task_id_claimed == task_id
        assert generation == 1  # 0 → 1 on the first fencing-aware claim

        row = _authoritative_row(database, task_id)
        assert row[0] == str(TaskStatus.LEASED)
        assert row[2] == 1

    def test_generation_increments_once_per_claim_and_never_otherwise(
        self, database, session, session_agent
    ) -> None:
        """claim → expire (recovery) → re-claim: generation 1 → (recover: stays 1) → 2."""
        task = _create_task(session, session_agent)
        session.commit()
        task_id = str(task.id)

        _, gen1 = _claim_committed(database, T0, lease_seconds=1)
        assert gen1 == 1

        later = T0 + timedelta(seconds=10)
        with database.session() as s:
            recovered = service.expire_leases(s, later)
        assert recovered == 1
        row = _authoritative_row(database, task_id)
        assert row[0] == str(TaskStatus.PENDING)
        assert row[2] == 1  # recovery NEVER touches the generation (INV-2)

        _, gen2 = _claim_committed(database, later)
        assert gen2 == 2  # the second claim epoch
        assert _authoritative_row(database, task_id)[2] == 2

    def test_losing_claims_increment_nothing(self, database, session, session_agent) -> None:
        _create_task(session, session_agent)
        session.commit()
        task_id, _gen = _claim_committed(database, T0)

        with database.session() as s:
            second = service.claim_next(s, T0, lease_seconds=30)
        assert second is None  # already leased — the conditional UPDATE misses
        row = _authoritative_row(database, task_id)
        assert row[2] == 1  # exactly one increment for exactly one winner

    def test_concurrent_barrier_claims_have_exactly_one_winner(
        self, database, session, session_agent
    ) -> None:
        """R02B-14 unit form: N workers claim concurrently; one winner, one epoch."""
        import threading

        task = _create_task(session, session_agent)
        session.commit()
        task_id = str(task.id)

        n = 4
        barrier = threading.Barrier(n, timeout=15.0)
        wins: dict[int, object] = {}

        def _claim(worker_no):
            with database.session() as s:
                barrier.wait()
                claimed = service.claim_next(s, T0, lease_seconds=30)
                wins[worker_no] = str(claimed.id) if claimed is not None else None

        threads = [threading.Thread(target=_claim, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(15.0)
        assert len([w for w in wins.values() if w == task_id]) == 1
        row = _authoritative_row(database, task_id)
        assert row[1] == 1  # attempts: one claim
        assert row[2] == 1  # generation: one epoch

    def test_two_sweep_workers_execute_five_tasks_exactly_once(
        self, database, session, session_agent
    ) -> None:
        """R02B-12: duplicate-delivery regression — 5 tasks, no dup, no omission."""
        handler = _RecordingHandler()
        registry.register("lease-fence-noop", handler)
        try:
            ids = []
            for _ in range(5):
                task = _create_task(session, session_agent, kind="lease-fence-noop")
                ids.append(str(task.id))
            session.commit()

            executed = 0
            for _sweep in range(2):
                for _ in range(5):
                    with database.session() as s:
                        claimed = service.claim_next(s, T0, lease_seconds=30)
                        if claimed is None:
                            continue
                        service.execute_claimed(
                            s,
                            claimed,
                            T0,
                            lease_generation=int(claimed.lease_generation),
                        )
                        executed += 1
            assert executed == 5
            assert handler.calls == 5
            for tid in ids:
                row = _authoritative_row(database, tid)
                assert row[0] == str(TaskStatus.SUCCEEDED)
                assert row[1] == 1
                assert row[2] == 1
        finally:
            registry.unregister("lease-fence-noop")


# ---------------------------------------------------------------------------
# R02B-01/02 — normal path and legitimate holder must never be fenced
# ---------------------------------------------------------------------------


class TestLegitimateHolderIsNeverFenced:
    def test_normal_claim_execute_complete(self, database, session, session_agent) -> None:
        handler = _RecordingHandler()
        registry.register("lease-fence-noop", handler)
        try:
            task = _create_task(session, session_agent, kind="lease-fence-noop")
            session.commit()
            task_id = str(task.id)

            claimed_id, generation = _claim_committed(database, T0)
            assert claimed_id == task_id

            with database.session() as s:
                claimed = service.get_task(s, task_id)
                service.execute_claimed(s, claimed, T0, lease_generation=generation)

            row = _authoritative_row(database, task_id)
            assert row[0] == str(TaskStatus.SUCCEEDED)
            assert row[1] == 1  # attempts == claims
            assert row[2] == 1  # generation == claims
            assert row[3] is None  # lease cleared
            assert row[4] is not None  # finished_at recorded
            assert handler.calls == 1
        finally:
            registry.unregister("lease-fence-noop")

    def test_in_lease_completion_with_boundary_expire_hits_nothing(
        self, database, session, session_agent
    ) -> None:
        """R02B-02 + R02B-09(a): at lease_expires_at == now the predicate is strictly '<' —
        the task is NOT expired, and the legitimate holder completes normally."""
        handler = _RecordingHandler()
        registry.register("lease-fence-noop", handler)
        try:
            task = _create_task(session, session_agent, kind="lease-fence-noop")
            session.commit()
            task_id = str(task.id)

            claimed_id, generation = _claim_committed(database, T0, lease_seconds=10)
            assert claimed_id == task_id

            with database.session() as s:
                at_boundary = service.expire_leases(s, T0 + timedelta(seconds=10))
                assert at_boundary == 0  # strict <: equality is not expired
                claimed = service.get_task(s, task_id)
                service.execute_claimed(
                    s, claimed, T0 + timedelta(seconds=10), lease_generation=generation
                )

            row = _authoritative_row(database, task_id)
            assert row[0] == str(TaskStatus.SUCCEEDED)
            assert row[2] == 1
            assert handler.calls == 1
        finally:
            registry.unregister("lease-fence-noop")


# ---------------------------------------------------------------------------
# R02B-03 — execution-start gate: a superseded lease cannot START work
# ---------------------------------------------------------------------------


class TestExecutionStartGate:
    def test_superseded_worker_cannot_start_and_handler_is_not_invoked(
        self, database, session, session_agent
    ) -> None:
        """RV2-03/RV2-05 start form: A claims → lease expires → recovered →
        B re-claims (epoch 2) → A's stale Phase-2 must be rejected BEFORE the
        handler runs; B's authoritative lease context survives untouched."""
        handler = _RecordingHandler(fail=True)  # if A got through, it would FAIL the task
        registry.register("lease-fence-fail", handler)
        try:
            task = _create_task(session, session_agent, kind="lease-fence-fail")
            session.commit()
            task_id = str(task.id)

            _, gen_a = _claim_committed(database, T0, lease_seconds=1)
            stale_obj = _detach_claim_object(database, task_id)

            later = T0 + timedelta(seconds=10)
            with database.session() as s:
                service.expire_leases(s, later)  # recovery: epoch stays 1
            _, gen_b = _claim_committed(database, later)
            assert gen_b == 2

            b_lease_row = _authoritative_row(database, task_id)
            b_context = (b_lease_row[0], b_lease_row[1], b_lease_row[2], b_lease_row[5])

            # A re-binds its claim-era object and attempts the execution start.
            with database.session() as s:
                merged = s.merge(stale_obj, load=False)
                with pytest.raises(LeaseSupersededError):
                    service.execute_claimed(s, merged, later, lease_generation=gen_a)

            row = _authoritative_row(database, task_id)
            assert handler.calls == 0  # the stale handler was NEVER dispatched
            # B's lease context is byte-identical (status/attempts/epoch/error)
            assert (row[0], row[1], row[2], row[5]) == b_context
            assert row[0] == str(TaskStatus.LEASED)
            assert row[1] == 2
            assert row[2] == 2
        finally:
            registry.unregister("lease-fence-fail")

    def test_generation_zero_has_no_authorized_execution_path(
        self, database, session, session_agent
    ) -> None:
        """INV-9: generation 0 exists only for rows never claimed by fencing-aware
        code; presenting it must never authorize a task-owned transition."""
        task = _create_task(session, session_agent)
        session.commit()
        task_id = str(task.id)
        _, gen = _claim_committed(database, T0)
        assert gen == 1

        with database.session() as s:
            claimed = service.get_task(s, task_id)
            with pytest.raises(LeaseSupersededError):
                # forged epoch 0 (e.g. an unset/legacy context) — CAS rejects
                service.execute_claimed(s, claimed, T0, lease_generation=0)


# ---------------------------------------------------------------------------
# R02B-04/06/08 — superseded result submission is rejected, replay is frozen
# ---------------------------------------------------------------------------


class TestSupersededResultSubmission:
    def _stale_scenario(self, database, session, session_agent, *, kind="lease-fence-noop"):
        """claim(A) → expire → re-claim(B) → B completes; returns the stale pieces."""
        handler = _RecordingHandler()
        registry.register(kind, handler)
        task = _create_task(session, session_agent, kind=kind)
        session.commit()
        task_id = str(task.id)

        _, gen_a = _claim_committed(database, T0, lease_seconds=1)
        stale_obj = _detach_claim_object(database, task_id)

        later = T0 + timedelta(seconds=10)
        with database.session() as s:
            service.expire_leases(s, later)
        _, gen_b = _claim_committed(database, later)
        with database.session() as s:
            claimed = service.get_task(s, task_id)
            service.execute_claimed(s, claimed, later, lease_generation=gen_b)
        return handler, task_id, stale_obj, gen_a, gen_b

    def test_stale_result_write_rejected_and_b_completion_survives(
        self, database, session, session_agent
    ) -> None:
        """R02B-04 (RV2-05 core): A's success write must not move B's finished_at."""
        handler, task_id, stale_obj, gen_a, _gen_b = self._stale_scenario(
            database, session, session_agent
        )
        row_before = _authoritative_row(database, task_id)
        assert row_before[0] == str(TaskStatus.SUCCEEDED)
        b_finished_at = row_before[4]

        with database.session() as s:
            merged = s.merge(stale_obj, load=False)
            with pytest.raises(LeaseSupersededError):
                service.execute_claimed(
                    s, merged, T0 + timedelta(seconds=20), lease_generation=gen_a
                )

        row = _authoritative_row(database, task_id)
        assert row[0] == str(TaskStatus.SUCCEEDED)
        assert row[4] == b_finished_at  # byte-identical survival
        assert row[1] == 2  # accounting intact
        assert row[2] == 2
        assert handler.calls == 1  # only B executed

    def test_stale_replay_rejected_twice_with_frozen_state(
        self, database, session, session_agent
    ) -> None:
        """R02B-06: repeated superseded attempts are idempotently rejected."""
        handler, task_id, stale_obj, gen_a, _gen_b = self._stale_scenario(
            database, session, session_agent
        )
        for _attempt in (1, 2):
            with database.session() as s:
                merged = s.merge(stale_obj, load=False)
                with pytest.raises(LeaseSupersededError):
                    service.execute_claimed(
                        s, merged, T0 + timedelta(seconds=30), lease_generation=gen_a
                    )
            row = _authoritative_row(database, task_id)
            assert row[0] == str(TaskStatus.SUCCEEDED)
            assert row[1] == 2
            assert row[2] == 2
        assert handler.calls == 1

    def test_post_expiry_resume_rejected(self, database, session, session_agent) -> None:
        """R02B-08: expire commits first; A's late completion is rejected;
        the ledger stays claim1 → expire(+0) → claim2."""
        handler, task_id, stale_obj, gen_a, _gen_b = self._stale_scenario(
            database, session, session_agent
        )
        with database.session() as s:
            merged = s.merge(stale_obj, load=False)
            with pytest.raises(LeaseSupersededError):
                service.execute_claimed(
                    s, merged, T0 + timedelta(seconds=40), lease_generation=gen_a
                )
        row = _authoritative_row(database, task_id)
        assert row[0] == str(TaskStatus.SUCCEEDED)
        assert row[1] == 2  # attempts == claims == generations (ledger self-consistent)


# ---------------------------------------------------------------------------
# R02B-05 — fencing is freshness, NOT identity (documented boundary)
# ---------------------------------------------------------------------------


class TestFencingIsNotIdentity:
    def test_current_generation_match_is_fencing_not_identity_boundary(
        self, database, session, session_agent
    ) -> None:
        """Threat-model actor 3 (design review §2.5): a caller that presents the
        CURRENT generation without ever claiming passes the CAS. This is a
        DOCUMENTED boundary — ``lease_generation`` is a freshness fence, not a
        cryptographic identity or an unforgeable capability. Rejection of
        SUPERSEDED generations is covered by the other suites above."""
        handler = _RecordingHandler()
        registry.register("lease-fence-noop", handler)
        try:
            task = _create_task(session, session_agent, kind="lease-fence-noop")
            session.commit()
            task_id = str(task.id)
            _, generation = _claim_committed(database, T0)

            # A forger reads the row's CURRENT generation out-of-band and
            # submits a completion with it — the CAS matches (rowcount 1).
            with database.session() as s:
                forged = service.get_task(s, task_id)
                current = int(forged.lease_generation)
                service.execute_claimed(s, forged, T0, lease_generation=current)

            row = _authoritative_row(database, task_id)
            assert row[0] == str(TaskStatus.SUCCEEDED)
            assert row[2] == generation  # epoch unchanged — no new claim happened
        finally:
            registry.unregister("lease-fence-noop")


# ---------------------------------------------------------------------------
# R02B-10/16-core — failure/retry accounting under fencing (INV-3)
# ---------------------------------------------------------------------------


class TestFailureRetryAccounting:
    def test_failing_handler_backoff_and_exhaustion_with_epoch_ledger(
        self, database, session, session_agent
    ) -> None:
        """R02B-10: claim→fail→backoff ×2 → claim→fail→failed(terminal);
        attempts == generations == 1,2,3 each round."""
        handler = _RecordingHandler(fail=True)
        registry.register("lease-fence-fail", handler)
        try:
            task = _create_task(session, session_agent, kind="lease-fence-fail")
            session.commit()
            task_id = str(task.id)

            now = T0
            for expected in (1, 2, 3):
                claimed_id, generation = _claim_committed(database, now)
                assert claimed_id == task_id
                assert generation == expected  # epoch ledger matches the claim count
                t_exec = now + timedelta(seconds=1)
                with database.session() as s:
                    claimed = service.get_task(s, task_id)
                    service.execute_claimed(s, claimed, t_exec, lease_generation=generation)
                row = _authoritative_row(database, task_id)
                assert row[1] == expected  # attempts: claim-counted only
                if expected < 3:
                    assert row[0] == str(TaskStatus.PENDING)
                    assert row[5] is not None  # last_error recorded (no payload)
                    assert "synthetic failure" in row[5]
                    now = t_exec + timedelta(seconds=min(2**expected, 60))  # backoff
                else:
                    assert row[0] == str(TaskStatus.FAILED)  # terminal exhaustion
                    assert row[4] is not None  # finished_at
            assert handler.calls == 3
        finally:
            registry.unregister("lease-fence-fail")

    def test_timeout_failure_of_legitimate_holder_is_not_fenced(
        self, database, session, session_agent
    ) -> None:
        """R02B-16 legitimate-holder half: a timeout failure applied while the
        worker still holds the CURRENT epoch writes the retry state normally —
        the fence must never damage the honest failure path."""
        import time

        registry.register("lease-fence-sleepy", lambda s, t: time.sleep(2.0))
        try:
            task = _create_task(session, session_agent, kind="lease-fence-sleepy")
            session.commit()
            task_id = str(task.id)
            _, generation = _claim_committed(database, T0)

            with database.session() as s:
                claimed = service.get_task(s, task_id)
                result = service.execute_claimed(
                    s, claimed, T0, timeout_seconds=0.2, lease_generation=generation
                )
            row = _authoritative_row(database, task_id)
            assert row[0] == str(TaskStatus.PENDING)  # retry with backoff
            assert result.status == TaskStatus.PENDING
            assert row[5] is not None and row[5].startswith("TaskTimeoutError")
        finally:
            registry.unregister("lease-fence-sleepy")

    def test_crash_recovery_keeps_at_least_once_delivery(
        self, database, session, session_agent
    ) -> None:
        """R02B-11: a claim whose worker dies is recovered by expiry and the
        task executes on the SECOND epoch — at-least-once is unchanged."""
        handler = _RecordingHandler()
        registry.register("lease-fence-noop", handler)
        try:
            task = _create_task(session, session_agent, kind="lease-fence-noop")
            session.commit()
            task_id = str(task.id)

            _, _gen1 = _claim_committed(database, T0, lease_seconds=1)
            # worker 1 "crashes": never executes; the lease simply expires.
            later = T0 + timedelta(seconds=10)
            with database.session() as s:
                assert service.expire_leases(s, later) == 1
            _, gen2 = _claim_committed(database, later)
            assert gen2 == 2
            with database.session() as s:
                claimed = service.get_task(s, task_id)
                service.execute_claimed(s, claimed, later, lease_generation=gen2)

            row = _authoritative_row(database, task_id)
            assert row[0] == str(TaskStatus.SUCCEEDED)
            assert row[1] == 2  # two claims — the at-least-once ledger
            assert row[2] == 2
            assert handler.calls == 1  # but only ONE execution ever happened
        finally:
            registry.unregister("lease-fence-noop")


# ---------------------------------------------------------------------------
# R02B-09 + W2 closure — expire_leases as a system-owned conditional UPDATE
# ---------------------------------------------------------------------------


class TestExpireLeasesConditionalRecovery:
    def test_recovery_is_conditional_and_counted_accurately(
        self, database, session, session_agent
    ) -> None:
        """Non-exhausted → pending (no new attempt); exhausted → failed with the
        static lease-expired message; the return counts only actual recoveries."""
        pending_task = _create_task(session, session_agent, max_attempts=3)
        exhausted_agent = provision_agent(session, "agent-lease-expired")
        exhausted_task = _create_task(session, exhausted_agent, max_attempts=1)
        session.commit()

        later = T0 + timedelta(seconds=10)

        with database.session() as s:
            c1 = service.claim_next(s, T0, lease_seconds=1)
            c2 = service.claim_next(s, T0, lease_seconds=1)
            claimed_ids = {str(c1.id), str(c2.id)}
        assert claimed_ids == {str(pending_task.id), str(exhausted_task.id)}

        with database.session() as s:
            recovered = service.expire_leases(s, later)
        assert recovered == 2

        row_p = _authoritative_row(database, str(pending_task.id))
        assert row_p[0] == str(TaskStatus.PENDING)  # back to claimable
        assert row_p[1] == 1  # recovery adds NO attempt (INV-3)
        assert row_p[2] == 1
        assert row_p[5] is None  # recovery never writes last_error on this branch

        row_e = _authoritative_row(database, str(exhausted_task.id))
        assert row_e[0] == str(TaskStatus.FAILED)  # terminal lease-expired
        assert row_e[4] is not None  # finished_at
        assert row_e[5] == "lease expired after the final attempt"

        # a second recovery pass over already-recovered rows recovers nothing
        with database.session() as s:
            assert service.expire_leases(s, later) == 0

    def test_terminal_state_is_never_flipped_back_by_recovery(
        self, database, session, session_agent
    ) -> None:
        """W2 closure (system-owned predicates): a task completed by the
        legitimate holder can never be re-pendinged by the reaper — even when
        recovery runs at a time AFTER the lease had expired."""
        handler = _RecordingHandler()
        registry.register("lease-fence-noop", handler)
        try:
            task = _create_task(session, session_agent, kind="lease-fence-noop")
            session.commit()
            task_id = str(task.id)
            _, generation = _claim_committed(database, T0, lease_seconds=1)

            # The holder completes AFTER the lease timestamp expired but BEFORE
            # any recovery pass — the fresh-read path still owns the row.
            later = T0 + timedelta(seconds=10)
            with database.session() as s:
                claimed = service.get_task(s, task_id)
                service.execute_claimed(s, claimed, later, lease_generation=generation)

            with database.session() as s:
                assert service.expire_leases(s, later + timedelta(seconds=100)) == 0

            row = _authoritative_row(database, task_id)
            assert row[0] == str(TaskStatus.SUCCEEDED)  # survives — no third state
            assert row[4] is not None
        finally:
            registry.unregister("lease-fence-noop")


# ---------------------------------------------------------------------------
# R02B-13 — R02-A cancellation regression + fencing interplay
# ---------------------------------------------------------------------------


class TestCancellationInterplay:
    def test_cancel_while_leased_is_409_and_then_stale_completion_is_rejected(
        self, database, session, session_agent
    ) -> None:
        """R02B-13(b): R02-A's conditional cancel stays byte-identical (409
        InvalidStateError while leased) and the two defensive layers never
        interfere — the stale worker's completion is fenced afterwards."""
        handler = _RecordingHandler()
        registry.register("lease-fence-noop", handler)
        try:
            task = _create_task(session, session_agent, kind="lease-fence-noop")
            session.commit()
            task_id = str(task.id)
            _, gen_a = _claim_committed(database, T0, lease_seconds=1)
            stale_obj = _detach_claim_object(database, task_id)

            # R02-A: cancellation of a leased task fails closed (409 contract).
            with database.session() as s:
                owned = service.get_task(s, task_id)
                owner = s.get(type(session_agent), str(session_agent.id))
                with pytest.raises(InvalidStateError) as excinfo:
                    service.cancel_task(s, owner, owned, now=T0 + timedelta(seconds=1))
                assert "Only pending tasks can be cancelled" in str(excinfo.value)

            later = T0 + timedelta(seconds=10)
            with database.session() as s:
                service.expire_leases(s, later)
            _, gen_b = _claim_committed(database, later)
            with database.session() as s:
                claimed = service.get_task(s, task_id)
                service.execute_claimed(s, claimed, later, lease_generation=gen_b)

            with database.session() as s:
                merged = s.merge(stale_obj, load=False)
                with pytest.raises(LeaseSupersededError):
                    service.execute_claimed(
                        s, merged, T0 + timedelta(seconds=20), lease_generation=gen_a
                    )
            row = _authoritative_row(database, task_id)
            assert row[0] == str(TaskStatus.SUCCEEDED)
            assert handler.calls == 1
        finally:
            registry.unregister("lease-fence-noop")

    def test_pending_cancellation_still_works_exactly_as_documented(
        self, database, session, session_agent
    ) -> None:
        """R02-A regression: a genuinely pending task cancels atomically."""
        task = _create_task(session, session_agent)
        session.commit()
        task_id = str(task.id)

        with database.session() as s:
            owned = service.get_task(s, task_id)
            owner = s.get(type(session_agent), str(session_agent.id))
            service.cancel_task(s, owner, owned, now=T0)
        row = _authoritative_row(database, task_id)
        assert row[0] == str(TaskStatus.CANCELLED)
        assert row[4] is not None  # finished_at recorded at cancellation


# ---------------------------------------------------------------------------
# R02B-17 — revocation is orthogonal (documented pending owner policy)
# ---------------------------------------------------------------------------


class TestRevocationOrthogonality:
    def test_revoked_agent_task_execution_is_documented_pending_owner_policy(
        self, database, session, session_agent
    ) -> None:
        """RV2-06 (INCONCLUSIVE / OWNER POLICY PENDING): revocation currently
        gates only API authentication — the worker path performs NO agent
        status check, so a revoked agent's queued task still executes. This
        suite RECORDS that behavior; if the owner rules "revoke = stop
        execution", a separate case/gate must be added (design review §10 D1)
        — this test must not silently extend to cover it."""
        handler = _RecordingHandler()
        registry.register("lease-fence-noop", handler)
        try:
            agent = provision_agent(session, "agent-to-revoke")
            issued = issue_token(session, str(agent.id))
            token = issued.raw_token if hasattr(issued, "raw_token") else str(issued)
            task = _create_task(session, agent, kind="lease-fence-noop")
            revoke_agent(session, agent)
            session.commit()
            task_id = str(task.id)

            # Layer 1 (unchanged): the revoked agent can no longer authenticate.
            with pytest.raises(AuthenticationError):
                verify_token(session, token)

            # Layer 2 (documented current behavior): the scheduler path does
            # not consult agent status — the queued task still executes.
            claimed_id, generation = _claim_committed(database, T0)
            assert claimed_id == task_id
            with database.session() as s:
                claimed = service.get_task(s, task_id)
                service.execute_claimed(s, claimed, T0, lease_generation=generation)
            assert handler.calls == 1
            assert _authoritative_row(database, task_id)[0] == str(TaskStatus.SUCCEEDED)
        finally:
            registry.unregister("lease-fence-noop")


# ---------------------------------------------------------------------------
# R02B-18 (partial) — fail-closed activation gate (schema compatibility)
# ---------------------------------------------------------------------------


class TestFencingSchemaGate:
    def test_gate_passes_on_fenced_schema(self, database) -> None:
        with database.session() as s:
            service.verify_fencing_schema(s)  # no exception — create_all carries the column

    def test_worker_refuses_to_start_on_unfenced_schema(
        self, database, session, session_agent
    ) -> None:
        """DG-1 negative assertion: a fencing-aware ``run_worker`` FAILS on a
        schema without the fencing column (migration 0001 state) — refusing
        unfenced operation is SAFER than running with the R02-B hole open."""
        from commons_core.scheduler.worker import run_worker

        _create_task(session, session_agent)
        session.commit()

        with database.session() as s:
            s.execute(text("ALTER TABLE scheduled_tasks DROP COLUMN lease_generation"))

        with pytest.raises(FencingSchemaError):
            run_worker(database.session, once=True, clock=lambda: T0)

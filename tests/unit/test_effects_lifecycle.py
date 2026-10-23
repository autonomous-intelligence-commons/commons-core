"""Effect lifecycle & dispatcher state-machine tests (R02-B2 E1).

Post-implementation acceptance tests (no RED baseline exists — prompt §8).
Every crash boundary (B3'..B6') is exercised by stopping the pipeline at a
granular stage function; every "forbidden effect" is asserted with an
EXACT count (receipts / rows / events), never an approximation.

Coverage map (test matrix classification — direct E1 / fake-provider
simulation):
- EFF-01 full path; EFF-06 duplicate request + duplicate ack; EFF-07 crash
  before dispatch; EFF-08 crash after mark (UNKNOWN never FAILED); EFF-09
  lost ack reconciles; EFF-10 high-impact UNKNOWN frozen + manual;
  EFF-11 emergency stop cancels queued; EFF-12 no path SUBMITTED→CANCELLED;
  EFF-13 agent revocation; EFF-14 ambiguous 401 → UNKNOWN; EFF-15
  sequential-claim mutual exclusion (PG-harness adds the true-concurrent
  form); EFF-16/18 approval + Class-4 retry policy compiled to assertions.
- Prompt §7 priority scenarios: restart reconstruction, dispatcher
  takeover, stale dispatcher completion, retry-budget enforcement,
  migration & rollback (operational file).
"""

from datetime import UTC, datetime, timedelta

import pytest

from commons_core.config.settings import Settings
from commons_core.database import models
from commons_core.database.enums import AgentStatus, EffectStatus, TaskStatus
from commons_core.effects import (
    FakeEffectProvider,
    StaleDispatcherError,
    request_effect,
    verify_effects_schema,
)
from commons_core.effects import provider as provider_mod
from commons_core.effects.dispatcher import (
    cancel_pending_effects,
    claim_eligible,
    dispatch_tick,
    mark_submitted,
    mark_unknown_outcome,
    reap_ack_windows,
    reap_authorization_ttl,
    reap_claim_deadlines,
    reconcile_unknown,
    submit_claimed,
)

T0 = datetime(2030, 1, 1, tzinfo=UTC)


def effects_settings(**overrides) -> Settings:
    base = {"effects_enabled": True, "env": "test", "master_keys": "", "log_level": "WARNING"}
    base.update(overrides)
    return Settings(**base)


ECHO = {"schema_version": 1, "capability": "fx.echo", "parameters": {"message": "m"}}
APPROVED = {
    "schema_version": 1,
    "capability": "fx.requires_approval",
    "parameters": {"note": "high"},
}


@pytest.fixture()
def seeded(database):
    """Seed one ACTIVE agent + one RUNNING leased task (generation 1)."""

    with database.session() as s:
        agent = models.Agent(handle="fx-agent")
        s.add(agent)
        s.flush()
        task = models.ScheduledTask(
            kind="noop",
            payload={},
            agent_id=agent.id,
            status=TaskStatus.RUNNING,
            run_at=T0,
            lease_expires_at=T0 + timedelta(seconds=3600),
            lease_generation=1,
            attempts=1,
        )
        s.add(task)
        s.commit()
        return {"database": database, "agent_id": agent.id, "task_id": task.id}


def authorize_one(seeded, capability="fx.echo", operation=None, *, now=T0, **kwargs):
    database = seeded["database"]
    settings = kwargs.pop("settings", effects_settings())
    with database.session() as s:
        task = s.get(models.ScheduledTask, seeded["task_id"])
        row = request_effect(
            s, task, capability, operation or ECHO, now=now, settings=settings, **kwargs
        )
        effect_id = row.id
    return effect_id


def get_row(seeded, effect_id):
    with seeded["database"].session() as s:
        row = s.get(models.EffectRequest, effect_id)
        snapshot = {
            c: getattr(row, c)
            for c in (
                "status",
                "claim_epoch",
                "claim_owner",
                "claimed_at",
                "submitted_at",
                "acknowledged_at",
                "final_outcome_at",
                "attempts",
                "max_attempts",
                "outcome_source",
                "cancel_reason",
                "ack_deadline",
                "claim_deadline",
            )
        }
        return snapshot


def events_for(seeded, effect_id):
    with seeded["database"].session() as s:
        rows = (
            s.query(models.EffectEvent)
            .filter_by(effect_id=effect_id)
            .order_by(models.EffectEvent.id)
            .all()
        )
        return [(e.from_status, e.to_status, e.reason) for e in rows]


class TestHappyPathAndAudit:
    def test_eff01_full_lifecycle_exact_counts(self, seeded):
        effect_id = authorize_one(seeded)
        provider = FakeEffectProvider()
        result = dispatch_tick(
            seeded["database"].session,
            provider,
            now=T0 + timedelta(seconds=1),
            owner="d-1",
            settings=effects_settings(),
        )
        assert result.claimed == 1
        assert result.submitted == 1
        assert result.acked_success == 1
        snapshot = get_row(seeded, effect_id)
        assert snapshot["status"] == EffectStatus.SUCCEEDED
        assert snapshot["claim_epoch"] == 1
        assert snapshot["claim_owner"] == "d-1"
        assert snapshot["submitted_at"] is not None
        assert snapshot["acknowledged_at"] is not None
        assert snapshot["outcome_source"] == "provider_ack"
        assert provider.receipt_count(effect_id) == 1  # EXACT count
        assert provider.application_count(effect_id) == 1
        assert events_for(seeded, effect_id) == [
            (None, "authorized", "authorized"),
            ("authorized", "claimed", "claimed"),
            ("claimed", "submitted", "submitted"),
            ("submitted", "succeeded", "provider_ack"),
        ]

    def test_disabled_tick_freezes_rows(self, seeded):
        effect_id = authorize_one(seeded)
        provider = FakeEffectProvider()
        result = dispatch_tick(
            seeded["database"].session,
            provider,
            now=T0 + timedelta(seconds=1),
            owner="d-1",
            settings=effects_settings(effects_enabled=False),
        )
        assert result.frozen is True
        assert result.claimed == 0
        assert provider.receipt_count() == 0
        assert (
            get_row(seeded, effect_id)["status"] == EffectStatus.AUTHORIZED
        )  # frozen, not cleared

    def test_restart_reconstruction_from_durable_row_only(self, seeded):
        # G-E8': authorization in one "process"; a brand-new dispatcher
        # (fresh sessions, zero in-memory state) reconstructs the operation
        # from the durable row: schema revalidation + digest verification.
        effect_id = authorize_one(seeded)
        provider = FakeEffectProvider()  # brand-new provider too
        dispatch_tick(
            seeded["database"].session,
            provider,
            now=T0 + timedelta(seconds=2),
            owner="d-restart",
            settings=effects_settings(),
        )
        snapshot = get_row(seeded, effect_id)
        assert snapshot["status"] == EffectStatus.SUCCEEDED
        assert provider.receipt_count(effect_id) == 1


class TestCancellationAxes:
    def test_eff11_emergency_stop_cancels_pre_submission(self, seeded):
        effect_id = authorize_one(seeded)
        from commons_core.infrastructure.system_state import set_emergency_stop

        with seeded["database"].session() as s:
            set_emergency_stop(s, active=True, principal="admin", now=T0 + timedelta(seconds=1))
        provider = FakeEffectProvider()
        result = dispatch_tick(
            seeded["database"].session,
            provider,
            now=T0 + timedelta(seconds=2),
            owner="d-1",
            settings=effects_settings(),
        )
        assert result.cancelled_stop_sweep == 1
        assert result.claimed == 0
        assert provider.receipt_count() == 0  # ZERO outbound
        snapshot = get_row(seeded, effect_id)
        assert snapshot["status"] == EffectStatus.CANCELLED
        assert snapshot["cancel_reason"] == "emergency_stop"
        # The task axis is untouched (dual-axis separation, INV-EF-R12).
        with seeded["database"].session() as s:
            assert s.get(models.ScheduledTask, seeded["task_id"]).status == TaskStatus.RUNNING

    def test_eff12_no_path_from_submitted_to_cancelled(self, seeded):
        effect_id = authorize_one(seeded)
        provider = FakeEffectProvider()
        # Submit synchronously via the granular stages, leaving the row
        # SUBMITTED with a pending ack (delay script).
        provider.script_next(provider_mod.SCRIPT_DELAY_ACK, delay_seconds=30)
        with seeded["database"].session() as s:
            refs = claim_eligible(s, now=T0, owner="d-1", cfg=effects_settings())
        assert len(refs) == 1
        stage = submit_claimed(
            seeded["database"].session,
            provider,
            refs[0],
            now=T0,
            cfg=effects_settings(),
        )
        assert stage == "pending"
        assert get_row(seeded, effect_id)["status"] == EffectStatus.SUBMITTED

        # Engage the emergency stop: the sweep must MISS this row.
        from commons_core.infrastructure.system_state import set_emergency_stop

        with seeded["database"].session() as s:
            set_emergency_stop(s, active=True, principal="admin", now=T0 + timedelta(seconds=1))
            cancelled = cancel_pending_effects(
                s, reason="emergency_stop", now=T0 + timedelta(seconds=1)
            )
        assert cancelled == 0
        snapshot = get_row(seeded, effect_id)
        assert snapshot["status"] == EffectStatus.SUBMITTED  # structural ban holds
        # While the stop stays ENGAGED the whole pipeline is frozen: a tick
        # neither submits new rows nor applies in-flight acks (freeze-until-
        # release semantics, design §4.6-5).
        frozen_result = dispatch_tick(
            seeded["database"].session,
            provider,
            now=T0 + timedelta(seconds=40),
            owner="d-1",
            settings=effects_settings(),
        )
        assert frozen_result.cancelled_stop_sweep == 0
        assert frozen_result.acked_success == 0
        assert get_row(seeded, effect_id)["status"] == EffectStatus.SUBMITTED
        # Release the stop: the delayed ack is then applied honestly.
        with seeded["database"].session() as s:
            set_emergency_stop(s, active=False, principal="admin", now=T0 + timedelta(seconds=41))
        result = dispatch_tick(
            seeded["database"].session,
            provider,
            now=T0 + timedelta(seconds=60),
            owner="d-1",
            settings=effects_settings(),
        )
        assert result.cancelled_stop_sweep == 0
        assert result.acked_success == 1
        assert get_row(seeded, effect_id)["status"] == EffectStatus.SUCCEEDED

    def test_eff13_agent_suspension_cancels_pre_submission(self, seeded):
        effect_id = authorize_one(seeded)
        with seeded["database"].session() as s:
            agent = s.get(models.Agent, seeded["agent_id"])
            agent.status = AgentStatus.SUSPENDED
        provider = FakeEffectProvider()
        result = dispatch_tick(
            seeded["database"].session,
            provider,
            now=T0 + timedelta(seconds=1),
            owner="d-1",
            settings=effects_settings(),
        )
        # Claimed, then the submit revalidation cancelled it: ZERO outbound.
        assert result.claimed == 1
        assert result.submitted == 0
        assert result.cancelled_on_submit == 1
        assert provider.receipt_count() == 0
        snapshot = get_row(seeded, effect_id)
        assert snapshot["status"] == EffectStatus.CANCELLED
        assert snapshot["cancel_reason"] == "agent_not_active"


class TestCrashBoundaries:
    def test_eff07_crash_before_dispatch_recovers(self, seeded):
        # B3': authorized, dispatcher "crashes" before any claim — a fresh
        # dispatcher tick claims and completes normally.
        effect_id = authorize_one(seeded)
        provider = FakeEffectProvider()
        dispatch_tick(
            seeded["database"].session,
            provider,
            now=T0 + timedelta(seconds=1),
            owner="d-2",
            settings=effects_settings(),
        )
        snapshot = get_row(seeded, effect_id)
        assert snapshot["status"] == EffectStatus.SUCCEEDED
        assert provider.receipt_count(effect_id) == 1

    def test_b4_claim_deadline_requeue_preserves_epoch(self, seeded):
        effect_id = authorize_one(seeded)
        with seeded["database"].session() as s:
            refs = claim_eligible(s, now=T0, owner="d-A", cfg=effects_settings())
        assert len(refs) == 1 and refs[0].held_epoch == 1
        # "Crash after claim": nothing further happens; the deadline passes.
        with seeded["database"].session() as s:
            requeued = reap_claim_deadlines(s, now=T0 + timedelta(seconds=31))
        assert requeued == 1
        snapshot = get_row(seeded, effect_id)
        assert snapshot["status"] == EffectStatus.AUTHORIZED  # re-claimable
        assert snapshot["claim_epoch"] == 1  # reaper NEVER touches the epoch
        assert snapshot["claim_owner"] is None
        # Re-claim bumps the epoch to 2 and completes.
        provider = FakeEffectProvider()
        result = dispatch_tick(
            seeded["database"].session,
            provider,
            now=T0 + timedelta(seconds=32),
            owner="d-B",
            settings=effects_settings(),
        )
        assert result.claimed == 1 and result.acked_success == 1
        snapshot = get_row(seeded, effect_id)
        assert snapshot["claim_epoch"] == 2
        assert snapshot["status"] == EffectStatus.SUCCEEDED

    def test_eff08_crash_after_mark_unknown_never_failed(self, seeded):
        # B5'-a: the SUBMITTED marker committed, then the dispatcher dies
        # BEFORE calling the provider. The row must traverse UNKNOWN —
        # "no local record" is never "did not happen" — and reconcile to
        # FAILED ("definitively not applied") with an honest attempt count.
        effect_id = authorize_one(seeded)
        with seeded["database"].session() as s:
            refs = claim_eligible(s, now=T0, owner="d-A", cfg=effects_settings())
            stage = mark_submitted(
                s,
                refs[0].effect_id,
                refs[0].held_epoch,
                refs[0].owner,
                now=T0,
                cfg=effects_settings(),
            )
        assert stage == "submitted"
        # (crash — no provider call)
        with seeded["database"].session() as s:
            unknowns = reap_ack_windows(s, now=T0 + timedelta(seconds=61))
        assert unknowns == 1
        snapshot = get_row(seeded, effect_id)
        assert snapshot["status"] == EffectStatus.UNKNOWN  # NEVER FAILED
        # B5'-a under RECEIPT CUSTODY (S1 remediation): this provider was
        # alive BEFORE the submission marker committed, so its empty ledger
        # IS definitive non-execution evidence — reconcile closes FAILED.
        provider = FakeEffectProvider(instantiated_at=T0 - timedelta(seconds=1))
        with seeded["database"].session() as s:
            success, failed, inconclusive = reconcile_unknown(
                s, provider, now=T0 + timedelta(seconds=62)
            )
        assert (success, failed, inconclusive) == (0, 1, 0)
        snapshot = get_row(seeded, effect_id)
        assert snapshot["status"] == EffectStatus.FAILED
        assert snapshot["attempts"] == 1  # the submission attempt is counted honestly
        assert snapshot["outcome_source"] == "reconciliation"

    def test_eff08b_restarted_process_cannot_prove_non_execution(self, seeded):
        # S1 remediation companion to EFF-08: the SAME crash boundary, but
        # the whole process restarts — the answering provider was
        # constructed AFTER the submission. Its empty ledger is instance-
        # local absence, NOT proof the delivery never happened: the row
        # must stay UNKNOWN (absence of evidence ≠ evidence of
        # non-execution). Full-matrix coverage: test_s1_c_b in
        # test_effects_s1s2_regressions.py.
        effect_id = authorize_one(seeded)
        with seeded["database"].session() as s:
            refs = claim_eligible(s, now=T0, owner="d-A", cfg=effects_settings())
            stage = mark_submitted(
                s,
                refs[0].effect_id,
                refs[0].held_epoch,
                refs[0].owner,
                now=T0,
                cfg=effects_settings(),
            )
        assert stage == "submitted"
        # (crash — no provider call; the process and its provider are lost)
        with seeded["database"].session() as s:
            unknowns = reap_ack_windows(s, now=T0 + timedelta(seconds=61))
        assert unknowns == 1
        restarted = FakeEffectProvider(instantiated_at=T0 + timedelta(seconds=120))
        with seeded["database"].session() as s:
            success, failed, inconclusive = reconcile_unknown(
                s, restarted, now=T0 + timedelta(seconds=130)
            )
        assert (success, failed, inconclusive) == (0, 0, 1)
        snapshot = get_row(seeded, effect_id)
        assert snapshot["status"] == EffectStatus.UNKNOWN  # preserved
        assert snapshot["attempts"] == 0  # no budget spent on uncertainty

    def test_eff09_lost_ack_reconciles_to_succeeded(self, seeded):
        effect_id = authorize_one(seeded)
        # Constructed BEFORE the submission: receipt custody held — APPLIED
        # is self-evidencing regardless, but the anchor keeps the fixture
        # deterministic under the S1 custody semantics.
        provider = FakeEffectProvider(instantiated_at=T0)
        provider.script_next(provider_mod.SCRIPT_LOSE_ACK)
        result = dispatch_tick(
            seeded["database"].session,
            provider,
            now=T0 + timedelta(seconds=1),
            owner="d-1",
            settings=effects_settings(),
        )
        assert result.submitted == 1 and result.pending == 1
        assert provider.receipt_count(effect_id) == 1  # the provider DID receive it
        # Ack window passes → UNKNOWN (never FAILED). Granular stages make
        # the intermediate state observable as exact evidence.
        with seeded["database"].session() as s:
            unknowns = reap_ack_windows(s, now=T0 + timedelta(seconds=62))
        assert unknowns == 1
        assert get_row(seeded, effect_id)["status"] == EffectStatus.UNKNOWN
        # Reconciliation closes the loop from the receipt ledger.
        with seeded["database"].session() as s:
            success, failed, inconclusive = reconcile_unknown(
                s, provider, now=T0 + timedelta(seconds=63)
            )
        assert (success, failed, inconclusive) == (1, 0, 0)
        snapshot = get_row(seeded, effect_id)
        assert snapshot["status"] == EffectStatus.SUCCEEDED
        assert snapshot["outcome_source"] == "reconciliation"
        assert snapshot["acknowledged_at"] is None  # no synchronous ack ever arrived
        assert events_for(seeded, effect_id) == [
            (None, "authorized", "authorized"),
            ("authorized", "claimed", "claimed"),
            ("claimed", "submitted", "submitted"),
            ("submitted", "unknown", "ack_window_expired"),
            ("unknown", "succeeded", "reconciled_applied"),
        ]

    def test_eff10_high_impact_unknown_frozen_manual_only(self, seeded):
        from commons_core.effects import operation_digest

        with seeded["database"].session() as s:
            approval = models.EffectApproval(
                approver="human-op",
                capability="fx.requires_approval",
                bound_request_digest=operation_digest(APPROVED),
                expires_at=T0 + timedelta(hours=1),
            )
            s.add(approval)
            s.flush()
            approval_id = approval.id
        effect_id = authorize_one(seeded, "fx.requires_approval", APPROVED, approval_id=approval_id)
        provider = FakeEffectProvider()
        provider.script_next(provider_mod.SCRIPT_TIMEOUT)
        result = dispatch_tick(
            seeded["database"].session,
            provider,
            now=T0 + timedelta(seconds=1),
            owner="d-1",
            settings=effects_settings(),
        )
        assert result.pending == 1
        # Window passes → UNKNOWN; attempts FROZEN at 0 (max_attempts=0).
        result = dispatch_tick(
            seeded["database"].session,
            provider,
            now=T0 + timedelta(seconds=62),
            owner="d-1",
            settings=effects_settings(),
        )
        assert result.unknown_reaped == 1
        assert result.reconciled_success == 0
        assert result.reconciled_failed == 0  # high-impact: automatic reconcile SKIPS
        snapshot = get_row(seeded, effect_id)
        assert snapshot["status"] == EffectStatus.UNKNOWN
        assert snapshot["attempts"] == 0
        assert snapshot["max_attempts"] == 0
        # Many ticks later it is still frozen — no auto-retry, no auto-exit.
        for _ in range(3):
            dispatch_tick(
                seeded["database"].session,
                provider,
                now=T0 + timedelta(seconds=200),
                owner="d-1",
                settings=effects_settings(),
            )
        assert get_row(seeded, effect_id)["status"] == EffectStatus.UNKNOWN
        # The labeled test-only manual path resolves it.
        with seeded["database"].session() as s:
            mark_unknown_outcome(s, effect_id, "succeeded", now=T0 + timedelta(seconds=300))
        snapshot = get_row(seeded, effect_id)
        assert snapshot["status"] == EffectStatus.SUCCEEDED
        assert snapshot["outcome_source"] == "manual:test-only"  # permanently labeled

    def test_auth_ttl_expires_unclaimed_rows(self, seeded):
        effect_id = authorize_one(seeded, now=T0)
        with seeded["database"].session() as s:
            expired = reap_authorization_ttl(
                s, now=T0 + timedelta(seconds=301), cfg=effects_settings()
            )
        assert expired == 1
        snapshot = get_row(seeded, effect_id)
        assert snapshot["status"] == EffectStatus.EXPIRED
        assert events_for(seeded, effect_id)[-1] == ("authorized", "expired", "authorization_ttl")

    def test_retry_reclaimed_row_is_cancellable_before_resubmission(self, seeded):
        # Regression guard for the implementation-discovered predicate flaw
        # (Phase 0 INV-EF-R13 amendment): after a definitive failure the row
        # carries submitted_at from the FAILED attempt; a retry re-claim
        # (CLAIMED, epoch 2) must STILL be cancellable pre-re-submission,
        # and the re-claim's submit must not be blocked by the stale marker.
        effect_id = authorize_one(seeded)
        provider = FakeEffectProvider()
        provider.script_next(provider_mod.SCRIPT_FAIL_ONCE)
        dispatch_tick(
            seeded["database"].session,
            provider,
            now=T0 + timedelta(seconds=1),
            owner="d-1",
            settings=effects_settings(),
        )
        assert get_row(seeded, effect_id)["status"] == EffectStatus.FAILED_RETRYABLE
        # Retry re-claim (epoch 2) — then suspend the agent BEFORE submission.
        with seeded["database"].session() as s:
            refs = claim_eligible(
                s, now=T0 + timedelta(seconds=10), owner="d-2", cfg=effects_settings()
            )
        assert len(refs) == 1 and refs[0].held_epoch == 2
        with seeded["database"].session() as s:
            agent = s.get(models.Agent, seeded["agent_id"])
            agent.status = AgentStatus.SUSPENDED
        stage = submit_claimed(
            seeded["database"].session,
            provider,
            refs[0],
            now=T0 + timedelta(seconds=11),
            cfg=effects_settings(),
        )
        assert stage == "cancelled"  # re-submission blocked by AGENT STATE, not by the stale marker
        snapshot = get_row(seeded, effect_id)
        assert snapshot["status"] == EffectStatus.CANCELLED
        assert snapshot["cancel_reason"] == "agent_not_active"
        assert (
            provider.receipt_count(effect_id) == 1
        )  # exactly the first attempt — no re-submission

    def test_cancel_sweep_matches_retry_claimed_row(self, seeded):
        # The cancel sweep matches a retry-CLAIMED row (status-based
        # predicate — not the historical submitted_at marker).
        effect_id = authorize_one(
            seeded,
            "fx.counter",
            {"schema_version": 1, "capability": "fx.counter", "parameters": {"counter_key": "k"}},
        )
        provider = FakeEffectProvider()
        provider.script_next(provider_mod.SCRIPT_FAIL_ONCE)
        dispatch_tick(
            seeded["database"].session,
            provider,
            now=T0 + timedelta(seconds=1),
            owner="d-1",
            settings=effects_settings(),
        )
        assert get_row(seeded, effect_id)["status"] == EffectStatus.FAILED_RETRYABLE
        with seeded["database"].session() as s:
            refs = claim_eligible(
                s, now=T0 + timedelta(seconds=10), owner="d-1", cfg=effects_settings()
            )
        assert len(refs) == 1  # retry re-claim (epoch 2)
        with seeded["database"].session() as s:
            cancelled = cancel_pending_effects(
                s, reason="emergency_stop", now=T0 + timedelta(seconds=11)
            )
        assert cancelled == 1
        snapshot = get_row(seeded, effect_id)
        assert snapshot["status"] == EffectStatus.CANCELLED
        assert snapshot["cancel_reason"] == "emergency_stop"
        assert provider.receipt_count(effect_id) == 1  # no re-submission happened


class TestProviderOutcomes:
    def test_eff14_ambiguous_401_maps_to_unknown_not_failed(self, seeded):
        effect_id = authorize_one(
            seeded,
            "fx.ambiguous",
            {"schema_version": 1, "capability": "fx.ambiguous", "parameters": {"note": "n"}},
        )
        provider = FakeEffectProvider()
        provider.script_next(provider_mod.SCRIPT_AMBIGUOUS_401)
        result = dispatch_tick(
            seeded["database"].session,
            provider,
            now=T0 + timedelta(seconds=1),
            owner="d-1",
            settings=effects_settings(),
        )
        assert result.ambiguous == 1
        # The tick atomically continues into reconciliation; the honest path
        # is visible in the EVENT TRAIL: 401 → UNKNOWN (NOT a direct FAILED),
        # then evidence-based closure ("not applied" → FAILED).
        assert events_for(seeded, effect_id) == [
            (None, "authorized", "authorized"),
            ("authorized", "claimed", "claimed"),
            ("claimed", "submitted", "submitted"),
            ("submitted", "unknown", "ambiguous_response"),
            ("unknown", "failed", "reconciled_not_applied"),
        ]
        snapshot = get_row(seeded, effect_id)
        assert snapshot["status"] == EffectStatus.FAILED
        assert snapshot["outcome_source"] == "reconciliation"
        assert snapshot["attempts"] == 1  # counted only at the evidence-based failure

    def test_retry_budget_enforced_with_exact_counts(self, seeded):
        effect_id = authorize_one(seeded)
        provider = FakeEffectProvider()
        # fx.echo: idempotent Class-1, max_attempts=3.
        provider.script_next(provider_mod.SCRIPT_FAIL_ONCE)  # attempt 1: reject once
        result = dispatch_tick(
            seeded["database"].session,
            provider,
            now=T0 + timedelta(seconds=1),
            owner="d-1",
            settings=effects_settings(),
        )
        assert result.acked_failure == 1
        snapshot = get_row(seeded, effect_id)
        assert snapshot["status"] == EffectStatus.FAILED_RETRYABLE
        assert snapshot["attempts"] == 1
        # Next tick: idempotent re-claim, same effect_id, succeeds.
        result = dispatch_tick(
            seeded["database"].session,
            provider,
            now=T0 + timedelta(seconds=10),
            owner="d-1",
            settings=effects_settings(),
        )
        assert result.acked_success == 1
        assert provider.receipt_count(effect_id) == 2  # both submissions recorded
        assert provider.application_count(effect_id) == 1  # Class-1 collapse: ONE application
        snapshot = get_row(seeded, effect_id)
        assert snapshot["status"] == EffectStatus.SUCCEEDED
        assert snapshot["attempts"] == 1  # success does not consume budget

    def test_budget_exhaustion_is_terminal_failed(self, seeded):
        effect_id = authorize_one(seeded)
        provider = FakeEffectProvider()
        for tick in range(1, 5):
            provider.script_next(provider_mod.SCRIPT_FAIL)  # every attempt fails
            result = dispatch_tick(
                seeded["database"].session,
                provider,
                now=T0 + timedelta(seconds=tick * 10),
                owner="d-1",
                settings=effects_settings(),
            )
            if tick < 3:
                assert get_row(seeded, effect_id)["status"] == EffectStatus.FAILED_RETRYABLE
            else:
                break
        snapshot = get_row(seeded, effect_id)
        assert snapshot["status"] == EffectStatus.FAILED  # attempts 3 >= max 3
        assert snapshot["attempts"] == 3
        # A later tick does not resurrect the terminal row.
        result = dispatch_tick(
            seeded["database"].session,
            provider,
            now=T0 + timedelta(seconds=100),
            owner="d-1",
            settings=effects_settings(),
        )
        assert result.claimed == 0
        assert get_row(seeded, effect_id)["status"] == EffectStatus.FAILED

    def test_eff18_class4_policy_no_auto_retry(self, seeded):
        effect_id = authorize_one(
            seeded,
            "fx.nonidempotent",
            {"schema_version": 1, "capability": "fx.nonidempotent", "parameters": {"note": "n"}},
        )
        provider = FakeEffectProvider()
        provider.script_next(provider_mod.SCRIPT_FAIL)
        result = dispatch_tick(
            seeded["database"].session,
            provider,
            now=T0 + timedelta(seconds=1),
            owner="d-1",
            settings=effects_settings(),
        )
        # Class-4 policy: definitive failure NEVER becomes FAILED_RETRYABLE.
        assert result.acked_failure == 1
        snapshot = get_row(seeded, effect_id)
        assert snapshot["status"] == EffectStatus.FAILED
        assert provider.receipt_count(effect_id) == 1
        # UNKNOWN on Class-4 → manual only (not queryable → reconcile skips).
        effect2 = authorize_one(
            seeded,
            "fx.nonidempotent",
            {"schema_version": 1, "capability": "fx.nonidempotent", "parameters": {"note": "x"}},
        )
        provider.script_next(provider_mod.SCRIPT_TIMEOUT)
        dispatch_tick(
            seeded["database"].session,
            provider,
            now=T0 + timedelta(seconds=2),
            owner="d-1",
            settings=effects_settings(),
        )
        result = dispatch_tick(
            seeded["database"].session,
            provider,
            now=T0 + timedelta(seconds=63),
            owner="d-1",
            settings=effects_settings(),
        )
        assert result.unknown_reaped == 1
        assert result.reconciled_success == 0 and result.reconciled_failed == 0
        assert get_row(seeded, effect2)["status"] == EffectStatus.UNKNOWN  # frozen, manual only

    def test_eff06_duplicate_request_conflict_and_duplicate_ack_noop(self, seeded):
        # (a) duplicate request: second authorize with the same resource_key → 409.
        from commons_core.effects import DuplicateEffectError

        database = seeded["database"]
        settings = effects_settings()
        with database.session() as s:
            task = s.get(models.ScheduledTask, seeded["task_id"])
            row1 = request_effect(
                s, task, "fx.echo", ECHO, now=T0, settings=settings, resource_key="r"
            )
            s.commit()
            effect_id = row1.id
        with database.session() as s:
            task = s.get(models.ScheduledTask, seeded["task_id"])
            with pytest.raises(DuplicateEffectError):
                request_effect(
                    s, task, "fx.echo", ECHO, now=T0, settings=settings, resource_key="r"
                )
        # (b) duplicate ack after SUCCEEDED: idempotent no-op, no state
        # regression. The ack names its originating attempt (S2 provenance:
        # held_epoch/owner of the claim that submitted — epoch 1, d-1).
        provider = FakeEffectProvider(instantiated_at=T0)
        dispatch_tick(
            database.session,
            provider,
            now=T0 + timedelta(seconds=1),
            owner="d-1",
            settings=settings,
        )
        from commons_core.effects.dispatcher import apply_provider_ack
        from commons_core.effects.provider import AckOutcome

        with database.session() as s:
            stage = apply_provider_ack(
                s,
                effect_id,
                AckOutcome.SUCCESS,
                now=T0 + timedelta(seconds=5),
                held_epoch=1,
                owner="d-1",
            )
        assert stage == "idempotent_noop"
        assert get_row(seeded, effect_id)["status"] == EffectStatus.SUCCEEDED


class TestDispatcherFencing:
    def test_stale_dispatcher_ack_rejected_zero_bytes(self, seeded):
        # P0-C4: dispatcher A claims (epoch 1) and stalls; the deadline reaper
        # requeues; dispatcher B claims (epoch 2) and completes. A's late ack
        # with its STALE epoch must be rejected with ZERO bytes written.
        effect_id = authorize_one(seeded)
        provider = FakeEffectProvider()
        with seeded["database"].session() as s:
            refs_a = claim_eligible(s, now=T0, owner="d-A", cfg=effects_settings())
        assert refs_a[0].held_epoch == 1
        with seeded["database"].session() as s:
            reap_claim_deadlines(s, now=T0 + timedelta(seconds=31))  # A stalls out
        dispatch_tick(
            seeded["database"].session,
            provider,
            now=T0 + timedelta(seconds=32),
            owner="d-B",
            settings=effects_settings(),
        )
        snapshot = get_row(seeded, effect_id)
        assert snapshot["status"] == EffectStatus.SUCCEEDED
        assert snapshot["claim_epoch"] == 2
        assert snapshot["claim_owner"] == "d-B"
        # A's late success ack — rejected, zero bytes on the effect row, B's
        # result intact. The audit event is the ONLY write and persists via
        # the clean commit (the CAS never matched the row).
        from commons_core.effects.dispatcher import apply_provider_ack
        from commons_core.effects.provider import AckOutcome

        with seeded["database"].session() as s:
            raised = False
            try:
                apply_provider_ack(
                    s,
                    effect_id,
                    AckOutcome.SUCCESS,
                    now=T0 + timedelta(seconds=40),
                    held_epoch=1,
                    owner="d-A",
                )
            except StaleDispatcherError:
                raised = True
            assert raised
            # (clean exit → the same-session audit event commits)
        snapshot = get_row(seeded, effect_id)
        assert snapshot["status"] == EffectStatus.SUCCEEDED  # B's authoritative result intact
        assert snapshot["claim_owner"] == "d-B"
        assert snapshot["claim_epoch"] == 2
        # The stale rejection is auditable (counts/ids only).
        with seeded["database"].session() as s:
            stale_events = (
                s.query(models.SecurityEvent).filter_by(category="policy_violation").all()
            )
        assert any(e.details.get("reason") == "stale_dispatcher_rejected" for e in stale_events)

    def test_eff15_sequential_claim_mutual_exclusion(self, seeded):
        # [U]-form: two claim attempts; the loser gets nothing. The PG harness
        # runs the TRUE concurrent form (200 rows, two dispatchers, barrier).
        effect_id = authorize_one(seeded)
        with seeded["database"].session() as s:
            refs_a = claim_eligible(s, now=T0, owner="d-A", cfg=effects_settings())
        assert len(refs_a) == 1
        with seeded["database"].session() as s:
            refs_b = claim_eligible(s, now=T0, owner="d-B", cfg=effects_settings())
        assert len(refs_b) == 0  # already CLAIMED — exactly one owner
        snapshot = get_row(seeded, effect_id)
        assert snapshot["claim_epoch"] == 1
        assert snapshot["claim_owner"] == "d-A"

    def test_lease_guard_defense_in_depth(self, seeded):
        # EFF-04 [U]-form (constructed state — Phase 0 §2.5 documents that
        # the natural interleaving is structurally prevented by the task
        # CAS semantics; the guard still must hold under ANY mismatch):
        # the task is re-claimed to generation 2 AFTER authorization with
        # snapshot 1 → the submit revalidation must EXPIRE the row with
        # ZERO outbound.
        effect_id = authorize_one(seeded)
        with seeded["database"].session() as s:
            task = s.get(models.ScheduledTask, seeded["task_id"])
            task.lease_generation = 2  # simulate a newer claim epoch
        provider = FakeEffectProvider()
        result = dispatch_tick(
            seeded["database"].session,
            provider,
            now=T0 + timedelta(seconds=1),
            owner="d-1",
            settings=effects_settings(),
        )
        assert result.claimed == 1
        assert result.submitted == 0
        assert result.expired_on_submit == 1
        assert provider.receipt_count() == 0  # ZERO outbound — the core negative
        snapshot = get_row(seeded, effect_id)
        assert snapshot["status"] == EffectStatus.EXPIRED
        assert events_for(seeded, effect_id)[-1] == ("claimed", "expired", "lease_superseded")

    def test_digest_mismatch_freezes_row(self, seeded):
        effect_id = authorize_one(seeded)
        with seeded["database"].session() as s:
            row = s.get(models.EffectRequest, effect_id)
            # Reassign a FRESH dict so the ORM marks the JSON column dirty and
            # the tamper actually lands in the database.
            tampered = dict(row.operation)
            tampered["parameters"] = {"message": "tampered-after-authorization"}
            row.operation = tampered
        provider = FakeEffectProvider()
        result = dispatch_tick(
            seeded["database"].session,
            provider,
            now=T0 + timedelta(seconds=1),
            owner="d-1",
            settings=effects_settings(),
        )
        assert result.expired_on_submit == 1
        assert provider.receipt_count() == 0
        snapshot = get_row(seeded, effect_id)
        assert snapshot["status"] == EffectStatus.EXPIRED
        assert events_for(seeded, effect_id)[-1] == ("claimed", "expired", "digest_mismatch")
        with seeded["database"].session() as s:
            integrity_events = (
                s.query(models.SecurityEvent).filter_by(category="policy_violation").all()
            )
        assert any(e.details.get("reason") == "effect_digest_mismatch" for e in integrity_events)


class TestSchemaGates:
    def test_verify_effects_schema_passes_on_created_schema(self, seeded):
        with seeded["database"].session() as s:
            verify_effects_schema(s)  # no raise

    def test_manual_mark_requires_unknown(self, seeded):
        effect_id = authorize_one(seeded)  # AUTHORIZED, not UNKNOWN
        from commons_core.api.errors import InvalidStateError

        with seeded["database"].session() as s:
            with pytest.raises(InvalidStateError):
                mark_unknown_outcome(s, effect_id, "succeeded", now=T0 + timedelta(seconds=1))

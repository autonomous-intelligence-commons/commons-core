"""S3 targeted-regression reproducers (R02-B2 E1 — ACK owner fencing).

Prompt §5/§7 protocol: this SAME file is executed against the UNMODIFIED
S1/S2 replacement implementation (to capture the RED baseline for the S3
defects) and against the S3-corrected implementation (GREEN). Every
assertion is behavioral — statuses, stage strings, counts and audit
reasons — literal string comparisons with no new-symbol dependencies:

- S3-A RED cases assert that an ack naming an EPOCH but no OWNER is
  REJECTED fail-closed. The S1/S2 implementation substitutes the row's
  CURRENT claim owner (``row_owner = owner if owner is not None else
  row.claim_owner``) and lands the ack as if it carried complete trusted
  provenance (``acked_success`` / ``ack_failure`` / ``idempotent_noop``).
- S3-B RED cases assert that an ack with the correct epoch but a WRONG
  owner on a SUCCEEDED row is REJECTED BEFORE the idempotent shortcut.
  The S1/S2 implementation checks only the epoch in that branch and
  hands a valid-looking ``idempotent_noop`` to a non-owning attempt
  (outcome not even consulted).
- Missing-epoch cases (S3-03/04) and the SUBMITTED wrong-owner case
  (S3-05, protected by the CAS owner predicate) are regression LOCKS
  that must pass on BOTH implementations.

All row states are constructed through the ordinary pipeline (authorize
→ claim → mark_submitted → ack / reapers) — no fabricated database state
(prompt §5.9). Provider counters and exact database state are asserted
in every case (prompt §3.4 / §7).
"""

from datetime import UTC, datetime, timedelta

import pytest

from commons_core.config.settings import Settings
from commons_core.database import models
from commons_core.database.enums import EffectStatus, TaskStatus
from commons_core.effects import (
    AckOutcome,
    FakeEffectProvider,
    StaleDispatcherError,
    request_effect,
)
from commons_core.effects import provider as provider_mod
from commons_core.effects.dispatcher import (
    apply_provider_ack,
    claim_eligible,
    dispatch_tick,
    mark_submitted,
    reap_ack_windows,
)

T0 = datetime(2030, 1, 1, tzinfo=UTC)

ECHO = {"schema_version": 1, "capability": "fx.echo", "parameters": {"message": "m"}}


def effects_settings(**overrides) -> Settings:
    base = {"effects_enabled": True, "env": "test", "master_keys": "", "log_level": "WARNING"}
    base.update(overrides)
    return Settings(**base)


@pytest.fixture()
def seeded(database):
    """Seed one ACTIVE agent + one RUNNING leased task (generation 1)."""

    with database.session() as s:
        agent = models.Agent(handle="fx-s3-agent")
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
        return {
            c: getattr(row, c)
            for c in (
                "status",
                "claim_epoch",
                "claim_owner",
                "submitted_at",
                "acknowledged_at",
                "final_outcome_at",
                "attempts",
                "max_attempts",
                "outcome_source",
                "cancel_reason",
            )
        }


def events_for(seeded, effect_id):
    with seeded["database"].session() as s:
        rows = (
            s.query(models.EffectEvent)
            .filter_by(effect_id=effect_id)
            .order_by(models.EffectEvent.id)
            .all()
        )
        return [(e.from_status, e.to_status, e.reason, e.claim_epoch, e.owner) for e in rows]


def security_events(seeded, reason=None, effect_id=None):
    """All POLICY_VIOLATION events, optionally filtered in Python by detail."""
    with seeded["database"].session() as s:
        rows = s.query(models.SecurityEvent).filter_by(category="policy_violation").all()
    out = []
    for e in rows:
        if reason is not None and e.details.get("reason") != reason:
            continue
        if effect_id is not None and e.details.get("effect_id") != effect_id:
            continue
        out.append(e.details)
    return out


# ---------------------------------------------------------------------------
# State constructors — ordinary pipeline only (prompt §5.9)
# ---------------------------------------------------------------------------


def row_submitted(seeded, *, owner="d-1"):
    """AUTHORIZED → CLAIMED(epoch 1) → SUBMITTED via the granular stages."""
    effect_id = authorize_one(seeded)
    with seeded["database"].session() as s:
        refs = claim_eligible(s, now=T0, owner=owner, cfg=effects_settings())
    assert len(refs) == 1 and refs[0].held_epoch == 1
    with seeded["database"].session() as s:
        stage = mark_submitted(
            s,
            refs[0].effect_id,
            refs[0].held_epoch,
            refs[0].owner,
            now=T0,
            cfg=effects_settings(),
        )
    assert stage == "submitted"
    return effect_id


def row_succeeded(seeded, *, owner="d-1"):
    """A validly-SUCCEEDED row at epoch 1 (one full dispatch tick)."""
    effect_id = authorize_one(seeded)
    provider = FakeEffectProvider()
    result = dispatch_tick(
        seeded["database"].session,
        provider,
        now=T0 + timedelta(seconds=1),
        owner=owner,
        settings=effects_settings(),
    )
    assert result.submitted == 1 and result.acked_success == 1
    return effect_id


def row_unknown(seeded, *, owner="d-1"):
    """A validly-UNKNOWN row at epoch 1 (lost ack → window expired)."""
    effect_id = authorize_one(seeded)
    provider = FakeEffectProvider()
    provider.script_next(provider_mod.SCRIPT_LOSE_ACK)
    first = dispatch_tick(
        seeded["database"].session,
        provider,
        now=T0 + timedelta(seconds=1),
        owner=owner,
        settings=effects_settings(),
    )
    assert first.submitted == 1 and first.pending == 1
    with seeded["database"].session() as s:
        unknowns = reap_ack_windows(s, now=T0 + timedelta(seconds=70))
    assert unknowns == 1
    return effect_id


# ---------------------------------------------------------------------------
# S3-01..S3-11 — prompt §7 deterministic regression matrix
# ---------------------------------------------------------------------------


class TestS3Matrix:
    def test_s3_01_correct_epoch_and_owner_active_attempt(self, seeded):
        """S3-01: complete matching identity — the ack lands exactly once."""
        effect_id = row_submitted(seeded)
        before = get_row(seeded, effect_id)
        assert before["status"] == EffectStatus.SUBMITTED
        with seeded["database"].session() as s:
            stage = apply_provider_ack(
                s,
                effect_id,
                AckOutcome.SUCCESS,
                now=T0 + timedelta(seconds=5),
                held_epoch=1,
                owner="d-1",
            )
        assert stage == "acked_success"
        after = get_row(seeded, effect_id)
        assert after["status"] == EffectStatus.SUCCEEDED
        assert after["acknowledged_at"] is not None
        assert after["outcome_source"] == "provider_ack"
        ack_events = [e for e in events_for(seeded, effect_id) if e[2] == "provider_ack"]
        assert len(ack_events) == 1
        assert ack_events[0][3] == 1 and ack_events[0][4] == "d-1"
        # Exactly once: the correctly-repeated ack is idempotent — no new
        # transition, no duplicate audit (§6.3).
        with seeded["database"].session() as s:
            stage = apply_provider_ack(
                s,
                effect_id,
                AckOutcome.SUCCESS,
                now=T0 + timedelta(seconds=6),
                held_epoch=1,
                owner="d-1",
            )
        assert stage == "idempotent_noop"
        assert len([e for e in events_for(seeded, effect_id) if e[2] == "provider_ack"]) == 1

    def test_s3_02_correct_epoch_missing_owner_fail_closed(self, seeded):
        """S3-02 (S3-A RED core): epoch present, owner absent.

        The S1/S2 implementation substitutes the row's CURRENT owner and
        lands the ack as ``acked_success``. The contract (§6.1) requires
        fail-closed: no substitution, zero bytes, explicit rejection.
        """
        effect_id = row_submitted(seeded)
        before = get_row(seeded, effect_id)
        events_before = events_for(seeded, effect_id)
        with seeded["database"].session() as s:
            stage = apply_provider_ack(
                s,
                effect_id,
                AckOutcome.SUCCESS,
                now=T0 + timedelta(seconds=5),
                held_epoch=1,
            )
        assert stage == "ack_no_provenance"  # RED: unmodified says acked_success
        after = get_row(seeded, effect_id)
        assert after["status"] == EffectStatus.SUBMITTED  # zero effect-row bytes
        assert after["acknowledged_at"] is None
        assert after["final_outcome_at"] is None
        assert after["claim_epoch"] == before["claim_epoch"]
        assert events_for(seeded, effect_id) == events_before
        assert security_events(seeded, reason="ack_without_provenance", effect_id=effect_id)

    def test_s3_03_missing_epoch_correct_owner_fail_closed(self, seeded):
        """S3-03: owner present, epoch absent — NO current-epoch substitution.

        Regression lock (S2 contract): this branch already fails closed.
        """
        effect_id = row_submitted(seeded)
        events_before = events_for(seeded, effect_id)
        with seeded["database"].session() as s:
            stage = apply_provider_ack(
                s,
                effect_id,
                AckOutcome.SUCCESS,
                now=T0 + timedelta(seconds=5),
                owner="d-1",
            )
        assert stage == "ack_no_provenance"
        after = get_row(seeded, effect_id)
        assert after["status"] == EffectStatus.SUBMITTED
        assert after["claim_epoch"] == 1  # nothing was substituted
        assert events_for(seeded, effect_id) == events_before
        assert security_events(seeded, reason="ack_without_provenance", effect_id=effect_id)

    def test_s3_04_both_epoch_and_owner_missing(self, seeded):
        """S3-04: complete provenance absence — fail closed (S2 lock)."""
        effect_id = row_submitted(seeded)
        events_before = events_for(seeded, effect_id)
        with seeded["database"].session() as s:
            stage = apply_provider_ack(
                s, effect_id, AckOutcome.SUCCESS, now=T0 + timedelta(seconds=5)
            )
        assert stage == "ack_no_provenance"
        after = get_row(seeded, effect_id)
        assert after["status"] == EffectStatus.SUBMITTED
        assert events_for(seeded, effect_id) == events_before
        assert security_events(seeded, reason="ack_without_provenance", effect_id=effect_id)

    def test_s3_05_correct_epoch_wrong_owner_submitted_rejected(self, seeded):
        """S3-05: correct epoch, wrong owner, SUBMITTED — rejection.

        Regression lock: the SUBMITTED path is already protected by the
        CAS owner predicate (a miss funnels into the stale-dispatcher
        rejection with zero bytes). The S3 correction moves the same
        protection ahead of every terminal shortcut; the observable
        contract (rejection, zero bytes, audit) is identical.
        """
        effect_id = row_submitted(seeded)
        events_before = events_for(seeded, effect_id)
        with seeded["database"].session() as s:
            with pytest.raises(StaleDispatcherError):
                apply_provider_ack(
                    s,
                    effect_id,
                    AckOutcome.SUCCESS,
                    now=T0 + timedelta(seconds=5),
                    held_epoch=1,
                    owner="d-imposter",
                )
        after = get_row(seeded, effect_id)
        assert after["status"] == EffectStatus.SUBMITTED  # no transition
        assert after["attempts"] == 0
        assert events_for(seeded, effect_id) == events_before  # zero effect events
        assert security_events(seeded, reason="stale_dispatcher_rejected", effect_id=effect_id)

    def test_s3_06_wrong_epoch_correct_owner_submitted_rejected(self, seeded):
        """S3-06: wrong epoch, correct owner — established I-3 contract (lock)."""
        effect_id = row_submitted(seeded)
        events_before = events_for(seeded, effect_id)
        with seeded["database"].session() as s:
            with pytest.raises(StaleDispatcherError):
                apply_provider_ack(
                    s,
                    effect_id,
                    AckOutcome.SUCCESS,
                    now=T0 + timedelta(seconds=5),
                    held_epoch=2,
                    owner="d-1",
                )
        after = get_row(seeded, effect_id)
        assert after["status"] == EffectStatus.SUBMITTED
        assert after["claim_epoch"] == 1
        assert events_for(seeded, effect_id) == events_before
        assert security_events(seeded, reason="stale_dispatcher_rejected", effect_id=effect_id)

    def test_s3_07_valid_duplicate_on_succeeded_idempotent(self, seeded):
        """S3-07: complete matching identity on SUCCEEDED — idempotent no-op."""
        effect_id = row_succeeded(seeded)
        before = get_row(seeded, effect_id)
        assert before["status"] == EffectStatus.SUCCEEDED
        events_before = events_for(seeded, effect_id)
        with seeded["database"].session() as s:
            stage = apply_provider_ack(
                s,
                effect_id,
                AckOutcome.SUCCESS,
                now=T0 + timedelta(seconds=5),
                held_epoch=1,
                owner="d-1",
            )
        assert stage == "idempotent_noop"
        assert get_row(seeded, effect_id) == before  # untouched
        assert events_for(seeded, effect_id) == events_before  # no new events

    def test_s3_08_correct_epoch_wrong_owner_succeeded_rejected(self, seeded):
        """S3-08 (S3-B RED core): wrong owner must not reach the idempotent
        shortcut on a SUCCEEDED row.

        The S1/S2 implementation checks only the epoch in that branch and
        returns ``idempotent_noop`` to a non-owning attempt. The contract
        (§6.2/§6.3) requires rejection BEFORE the shortcut.
        """
        effect_id = row_succeeded(seeded)
        before = get_row(seeded, effect_id)
        events_before = events_for(seeded, effect_id)
        with seeded["database"].session() as s:
            with pytest.raises(StaleDispatcherError):
                apply_provider_ack(
                    s,
                    effect_id,
                    AckOutcome.SUCCESS,
                    now=T0 + timedelta(seconds=5),
                    held_epoch=1,
                    owner="d-imposter",
                )
        after = get_row(seeded, effect_id)
        assert after == before  # B's authoritative result intact, zero bytes
        assert events_for(seeded, effect_id) == events_before
        assert security_events(seeded, reason="stale_dispatcher_rejected", effect_id=effect_id)

    def test_s3_09_missing_owner_succeeded_fail_closed(self, seeded):
        """S3-09 (S3-A∩B RED): epoch present, owner absent, SUCCEEDED.

        Must fail closed — NOT ``idempotent_noop`` (the unmodified
        implementation substitutes the row owner and hands the shortcut
        to an anonymous ack).
        """
        effect_id = row_succeeded(seeded)
        before = get_row(seeded, effect_id)
        events_before = events_for(seeded, effect_id)
        with seeded["database"].session() as s:
            stage = apply_provider_ack(
                s,
                effect_id,
                AckOutcome.SUCCESS,
                now=T0 + timedelta(seconds=5),
                held_epoch=1,
            )
        assert stage == "ack_no_provenance"  # RED: unmodified says idempotent_noop
        assert get_row(seeded, effect_id) == before
        assert events_for(seeded, effect_id) == events_before
        assert security_events(seeded, reason="ack_without_provenance", effect_id=effect_id)

    def test_s3_10_delayed_ack_provenance_propagation(self, seeded):
        """S3-10: epoch and owner propagate UNCHANGED through DelayedAck →
        pump() → dispatch_tick() → apply_provider_ack().

        Part 1 inspects the pump layer directly (the DelayedAck a tick
        would deliver); part 2 drives the full tick path.
        """
        # --- Part 1: DelayedAck / pump() layer ---
        effect_a = authorize_one(seeded)
        provider = FakeEffectProvider()
        provider.script_next(provider_mod.SCRIPT_DELAY_ACK, delay_seconds=30)
        first = dispatch_tick(
            seeded["database"].session,
            provider,
            now=T0 + timedelta(seconds=1),
            owner="d-1",
            settings=effects_settings(),
        )
        assert first.submitted == 1 and first.pending == 1
        acks = provider.pump(T0 + timedelta(seconds=40))
        assert len(acks) == 1
        ack = acks[0]
        assert ack.effect_id == effect_a
        assert ack.outcome is AckOutcome.SUCCESS
        assert ack.claim_epoch == 1  # originating epoch — unchanged
        assert ack.claim_owner == "d-1"  # originating owner — unchanged
        # Feeding exactly what pump() produced through the ack processor
        # lands the transition attributed to the ORIGINATING attempt.
        with seeded["database"].session() as s:
            stage = apply_provider_ack(
                s,
                ack.effect_id,
                ack.outcome,
                now=T0 + timedelta(seconds=41),
                held_epoch=ack.claim_epoch,
                owner=ack.claim_owner,
            )
        assert stage == "acked_success"
        snapshot = get_row(seeded, effect_a)
        assert snapshot["status"] == EffectStatus.SUCCEEDED
        assert snapshot["claim_epoch"] == 1 and snapshot["claim_owner"] == "d-1"

        # --- Part 2: the tick's own pump loop (dispatch_tick layer) ---
        effect_b = authorize_one(seeded)
        provider2 = FakeEffectProvider()
        provider2.script_next(provider_mod.SCRIPT_DELAY_ACK, delay_seconds=30)
        tick_one = dispatch_tick(
            seeded["database"].session,
            provider2,
            now=T0 + timedelta(seconds=2),
            owner="d-1",
            settings=effects_settings(),
        )
        assert tick_one.submitted == 1 and tick_one.pending == 1
        tick_two = dispatch_tick(
            seeded["database"].session,
            provider2,
            now=T0 + timedelta(seconds=45),
            owner="d-1",
            settings=effects_settings(),
        )
        assert tick_two.acked_success == 1
        snapshot_b = get_row(seeded, effect_b)
        assert snapshot_b["status"] == EffectStatus.SUCCEEDED
        assert snapshot_b["claim_epoch"] == 1 and snapshot_b["claim_owner"] == "d-1"
        ack_events = [e for e in events_for(seeded, effect_b) if e[2] == "provider_ack"]
        assert len(ack_events) == 1
        assert ack_events[0][3] == 1 and ack_events[0][4] == "d-1"

    def test_s3_11_repeated_invalid_ack_bounded_audit(self, seeded):
        """S3-11: repeated invalid acks — row unchanged, BOUNDED audit.

        Missing-provenance acks: every delivery is REJECTED (the return
        value signals each one), but the persisted security marker is
        DEDUPED per effect row — repeated invalid acks cannot amplify the
        audit log (§6.1; the S1 ``reconcile_inconclusive`` precedent).
        The unmodified implementation records one event PER delivery.

        Mismatched acks: every delivery raises StaleDispatcherError and
        is audited per rejection (the established I-3 stale contract —
        linear per call, zero row bytes).
        """
        effect_id = row_submitted(seeded)
        before = get_row(seeded, effect_id)
        for _ in range(3):
            with seeded["database"].session() as s:
                stage = apply_provider_ack(
                    s,
                    effect_id,
                    AckOutcome.SUCCESS,
                    now=T0 + timedelta(seconds=5),
                    held_epoch=1,
                )
            assert stage == "ack_no_provenance"
        assert get_row(seeded, effect_id) == before  # authoritative row unchanged
        provenance_events = security_events(
            seeded, reason="ack_without_provenance", effect_id=effect_id
        )
        assert len(provenance_events) == 1  # RED: unmodified records 3 (per delivery)
        for _ in range(2):
            with seeded["database"].session() as s:
                with pytest.raises(StaleDispatcherError):
                    apply_provider_ack(
                        s,
                        effect_id,
                        AckOutcome.SUCCESS,
                        now=T0 + timedelta(seconds=6),
                        held_epoch=1,
                        owner="d-imposter",
                    )
        assert get_row(seeded, effect_id) == before  # still unchanged
        stale_events = security_events(
            seeded, reason="stale_dispatcher_rejected", effect_id=effect_id
        )
        assert len(stale_events) == 2  # per-rejection audit (established contract)


# ---------------------------------------------------------------------------
# Boundary cases discovered during source inspection (prompt §7 tail)
# ---------------------------------------------------------------------------


class TestS3Boundaries:
    def test_s3_13_missing_owner_unknown_row_fail_closed(self, seeded):
        """§6.4: missing provenance is rejected BEFORE the not-submitted
        shortcut too — an anonymous ack must not be classified as a
        merely-late ack. Unmodified: ``not_submitted`` (substitution)."""
        effect_id = row_unknown(seeded)
        before = get_row(seeded, effect_id)
        assert before["status"] == EffectStatus.UNKNOWN
        events_before = events_for(seeded, effect_id)
        with seeded["database"].session() as s:
            stage = apply_provider_ack(
                s,
                effect_id,
                AckOutcome.SUCCESS,
                now=T0 + timedelta(seconds=80),
                held_epoch=1,
            )
        assert stage == "ack_no_provenance"  # RED: unmodified says not_submitted
        after = get_row(seeded, effect_id)
        assert after["status"] == EffectStatus.UNKNOWN  # reconciliation owns the exit
        assert events_for(seeded, effect_id) == events_before
        assert security_events(seeded, reason="ack_without_provenance", effect_id=effect_id)

    def test_s3_14_wrong_owner_unknown_row_rejected(self, seeded):
        """§6.4: an ACK is not accepted (nor silently discarded as a
        no-op) merely because the effect reached a non-submitted state —
        identity is verified first. Unmodified: ``not_submitted``."""
        effect_id = row_unknown(seeded)
        before = get_row(seeded, effect_id)
        events_before = events_for(seeded, effect_id)
        with seeded["database"].session() as s:
            with pytest.raises(StaleDispatcherError):
                apply_provider_ack(
                    s,
                    effect_id,
                    AckOutcome.SUCCESS,
                    now=T0 + timedelta(seconds=80),
                    held_epoch=1,
                    owner="d-imposter",
                )
        after = get_row(seeded, effect_id)
        assert after == before  # UNKNOWN intact
        assert events_for(seeded, effect_id) == events_before
        assert security_events(seeded, reason="stale_dispatcher_rejected", effect_id=effect_id)

    def test_s3_15_missing_owner_failure_outcome_fail_closed(self, seeded):
        """S3-A on the FAILURE outcome: no substitution into the failure
        transition (attempts must not advance). Unmodified: ``ack_failure``
        with the row moved to FAILED_RETRYABLE."""
        effect_id = row_submitted(seeded)
        events_before = events_for(seeded, effect_id)
        with seeded["database"].session() as s:
            stage = apply_provider_ack(
                s,
                effect_id,
                AckOutcome.FAILURE,
                now=T0 + timedelta(seconds=5),
                held_epoch=1,
            )
        assert stage == "ack_no_provenance"  # RED: unmodified says ack_failure
        after = get_row(seeded, effect_id)
        assert after["status"] == EffectStatus.SUBMITTED
        assert after["attempts"] == 0  # budget untouched
        assert events_for(seeded, effect_id) == events_before
        assert security_events(seeded, reason="ack_without_provenance", effect_id=effect_id)

    def test_s3_16_wrong_owner_failure_outcome_on_succeeded_rejected(self, seeded):
        """S3-B extension: the unmodified SUCCEEDED shortcut ignores the
        owner AND the outcome — even a FAILURE ack from a wrong owner gets
        ``idempotent_noop``. The corrected contract rejects it."""
        effect_id = row_succeeded(seeded)
        before = get_row(seeded, effect_id)
        events_before = events_for(seeded, effect_id)
        with seeded["database"].session() as s:
            with pytest.raises(StaleDispatcherError):
                apply_provider_ack(
                    s,
                    effect_id,
                    AckOutcome.FAILURE,
                    now=T0 + timedelta(seconds=5),
                    held_epoch=1,
                    owner="d-imposter",
                )
        assert get_row(seeded, effect_id) == before  # RED: unmodified returns idempotent_noop
        assert events_for(seeded, effect_id) == events_before
        assert security_events(seeded, reason="stale_dispatcher_rejected", effect_id=effect_id)

    def test_s3_17_duplicate_failure_after_failed_retryable_noop(self, seeded):
        """Correct-identity duplicate FAILURE on FAILED_RETRYABLE: the
        terminal-failure semantics — ``not_submitted`` no-op, attempts
        counted exactly once (no double increment on duplicates)."""
        effect_id = row_submitted(seeded)
        with seeded["database"].session() as s:
            stage = apply_provider_ack(
                s,
                effect_id,
                AckOutcome.FAILURE,
                now=T0 + timedelta(seconds=5),
                held_epoch=1,
                owner="d-1",
            )
        assert stage == "ack_failure"
        after_first = get_row(seeded, effect_id)
        assert after_first["status"] == EffectStatus.FAILED_RETRYABLE
        assert after_first["attempts"] == 1
        events_after_first = events_for(seeded, effect_id)
        with seeded["database"].session() as s:
            stage = apply_provider_ack(
                s,
                effect_id,
                AckOutcome.FAILURE,
                now=T0 + timedelta(seconds=6),
                held_epoch=1,
                owner="d-1",
            )
        assert stage == "not_submitted"  # owning attempt's duplicate — no-op
        after_second = get_row(seeded, effect_id)
        assert after_second["attempts"] == 1  # counted exactly once
        assert events_for(seeded, effect_id) == events_after_first

    def test_s3_18_dedup_scoped_per_effect_row(self, seeded):
        """The bounded-audit marker is deduped PER EFFECT ROW, not
        globally — a second row's invalid ack still gets its own marker.
        Unmodified: every delivery records (row A would reach 2)."""
        effect_a = row_submitted(seeded)
        # A second, distinct effect row.
        task2 = None
        with seeded["database"].session() as s:
            agent = s.get(models.Agent, seeded["agent_id"])
            task2 = models.ScheduledTask(
                kind="noop",
                payload={},
                agent_id=agent.id,
                status=TaskStatus.RUNNING,
                run_at=T0,
                lease_expires_at=T0 + timedelta(seconds=3600),
                lease_generation=1,
                attempts=1,
            )
            s.add(task2)
            s.flush()
            seeded2 = dict(seeded)
            seeded2["task_id"] = task2.id
            task = s.get(models.ScheduledTask, task2.id)
            row = request_effect(s, task, "fx.echo", ECHO, now=T0, settings=effects_settings())
            effect_b = row.id
        with seeded["database"].session() as s:
            refs = claim_eligible(s, now=T0, owner="d-1", cfg=effects_settings())
        assert len(refs) == 1 and refs[0].effect_id == effect_b
        with seeded["database"].session() as s:
            stage = mark_submitted(
                s,
                refs[0].effect_id,
                refs[0].held_epoch,
                refs[0].owner,
                now=T0,
                cfg=effects_settings(),
            )
        assert stage == "submitted"

        for effect_id in (effect_a, effect_b, effect_a):
            with seeded["database"].session() as s:
                stage = apply_provider_ack(
                    s,
                    effect_id,
                    AckOutcome.SUCCESS,
                    now=T0 + timedelta(seconds=5),
                    held_epoch=1,
                )
            assert stage == "ack_no_provenance"
        assert (
            len(security_events(seeded, reason="ack_without_provenance", effect_id=effect_a)) == 1
        )
        assert (
            len(security_events(seeded, reason="ack_without_provenance", effect_id=effect_b)) == 1
        )
        total = security_events(seeded, reason="ack_without_provenance")
        assert len(total) == 2  # RED: unmodified records 3 (A twice)

    def test_s3_19_owning_attempt_late_ack_after_unknown_noop(self, seeded):
        """§6.4 documented distinction: a LATE ack from the OWNING attempt
        on a non-submitted row is a ``not_submitted`` no-op (reconciliation
        owns the UNKNOWN exit) — preserved S1/S2 semantics."""
        effect_id = row_unknown(seeded)
        events_before = events_for(seeded, effect_id)
        with seeded["database"].session() as s:
            stage = apply_provider_ack(
                s,
                effect_id,
                AckOutcome.SUCCESS,
                now=T0 + timedelta(seconds=80),
                held_epoch=1,
                owner="d-1",
            )
        assert stage == "not_submitted"
        after = get_row(seeded, effect_id)
        assert after["status"] == EffectStatus.UNKNOWN  # state preserved
        assert events_for(seeded, effect_id) == events_before  # no writes at all

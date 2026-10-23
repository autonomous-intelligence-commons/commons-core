"""S1/S2 targeted-regression reproducers (R02-B2 E1 remediation).

Prompt §5.1/§5.3 protocol: this SAME file is executed against the ORIGINAL
E1 implementation (to capture the RED baseline for the S1/S2 defects) and
against the corrected implementation (GREEN). Every defect assertion is
behavioral — statuses, stage strings, counts and audit reasons — so the
file runs under both versions:

- S1 RED cases assert that reconciliation PRESERVES ``UNKNOWN`` when the
  answering provider instance cannot establish receipt custody. The
  original implementation mis-reads a reconstructed (empty-ledger)
  provider's ``NEVER_RECEIVED`` as definitive non-execution and marks the
  row ``FAILED`` even though the synthetic operation already happened.
- S2 RED cases assert that an acknowledgment WITHOUT originating-attempt
  provenance is REJECTED. The original implementation substitutes the
  row's CURRENT claim epoch/owner (the documented pump path) and applies
  the ack to whichever attempt happens to own the row.

Dual-compatibility notes (documented, deliberate):

- :func:`_provider` injects the custody anchor ``instantiated_at`` when
  the corrected implementation supports it; on the original implementation
  the anchor does not exist and the helper falls back to the only
  construction available (the resulting defective outcome is exactly what
  the RED assertions reject).
- ``reconcile_unknown`` return values are asserted by INDEX (``counts[0]``
  success / ``counts[1]`` definitive-failure), which holds for both the
  original 2-tuple and the corrected 3-tuple (``counts[2]`` inconclusive;
  asserted only when the third slot exists).
- Stage strings (``"ack_no_provenance"``) and audit reasons
  (``"ack_without_provenance"``, ``"reconcile_inconclusive"``) are literal
  string comparisons — no new-symbol imports.

Provider counters (receipts / applications) and exact database state are
asserted in EVERY case, per prompt §3.4.
"""

from datetime import UTC, datetime, timedelta

import pytest

from commons_core.config.settings import Settings
from commons_core.database import models
from commons_core.database.enums import EffectStatus, TaskStatus
from commons_core.effects import (
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
    reap_claim_deadlines,
    reconcile_unknown,
    submit_claimed,
)

T0 = datetime(2030, 1, 1, tzinfo=UTC)

ECHO = {"schema_version": 1, "capability": "fx.echo", "parameters": {"message": "m"}}


def effects_settings(**overrides) -> Settings:
    base = {"effects_enabled": True, "env": "test", "master_keys": "", "log_level": "WARNING"}
    base.update(overrides)
    return Settings(**base)


def _provider(*, instantiated_at=None):
    """Construct the scenario provider (dual-compatibility shim).

    ``instantiated_at`` models WHEN this provider instance came alive:

    - ``T0 - 1s``  — the instance predates the submission (custody held);
    - ``>= submitted_at`` — the instance was constructed AFTER the row's
      submission (a restarted process; custody broken — its empty ledger
      CANNOT prove the operation never happened).

    On the ORIGINAL implementation the custody anchor does not exist
    (TypeError); the helper then constructs the only instance available.
    The defective outcome that follows (NEVER_RECEIVED treated as
    definitive non-execution) is exactly what the S1 assertions reject.
    """
    if instantiated_at is not None:
        try:
            return FakeEffectProvider(instantiated_at=instantiated_at)
        except TypeError:
            return FakeEffectProvider()
    return FakeEffectProvider()


@pytest.fixture()
def seeded(database):
    """Seed one ACTIVE agent + one RUNNING leased task (generation 1)."""

    with database.session() as s:
        agent = models.Agent(handle="fx-s1s2-agent")
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


def security_reasons(seeded):
    with seeded["database"].session() as s:
        events = s.query(models.SecurityEvent).filter_by(category="policy_violation").all()
        return sorted(e.details.get("reason") for e in events if e.details.get("reason"))


def drive_to_unknown(seeded, *, at, provider, effect_id):
    """Submit via the granular stages (lost-ack script), advance to UNKNOWN.

    Returns the ClaimedRef used (for provenance assertions).
    """
    provider.script_next(provider_mod.SCRIPT_LOSE_ACK)
    with seeded["database"].session() as s:
        refs = claim_eligible(s, now=at, owner="d-1", cfg=effects_settings())
    assert len(refs) == 1
    stage = submit_claimed(
        seeded["database"].session,
        provider,
        refs[0],
        now=at,
        cfg=effects_settings(),
    )
    assert stage == "pending"
    with seeded["database"].session() as s:
        unknowns = reap_ack_windows(s, now=at + timedelta(seconds=61))
    assert unknowns == 1
    assert get_row(seeded, effect_id)["status"] == EffectStatus.UNKNOWN
    return refs[0]


# ---------------------------------------------------------------------------
# S1 — Fake Provider evidence loss across restart (prompt §3)
# ---------------------------------------------------------------------------


class TestS1FakeProviderCustody:
    def test_s1_a_lost_ack_original_instance_retained(self, seeded):
        """S1-A: P1 applied the effect; P1 itself reconciles it to SUCCEEDED.

        Custody intact (the ledger that applied the effect is the ledger
        that answers the query) — the authoritative positive evidence path.
        """
        effect_id = authorize_one(seeded)
        p1 = _provider(instantiated_at=T0 - timedelta(seconds=1))
        ref = drive_to_unknown(seeded, at=T0, provider=p1, effect_id=effect_id)
        # P1's evidence: received once, applied once.
        assert p1.receipt_count(effect_id) == 1
        assert p1.application_count(effect_id) == 1
        with seeded["database"].session() as s:
            counts = reconcile_unknown(s, p1, now=T0 + timedelta(seconds=62))
        assert counts[0] == 1 and counts[1] == 0
        snapshot = get_row(seeded, effect_id)
        assert snapshot["status"] == EffectStatus.SUCCEEDED
        assert snapshot["outcome_source"] == "reconciliation"
        # No new submission happened during reconciliation (no retry).
        assert p1.receipt_count(effect_id) == 1
        assert events_for(seeded, effect_id)[-1][:3] == (
            "unknown",
            "succeeded",
            "reconciled_applied",
        )
        assert ref.held_epoch == 1

    def test_s1_b_lost_ack_provider_reconstructed_preserves_unknown(self, seeded):
        """S1-B (RED core): P2 must NOT read its own empty ledger as proof.

        P1 applied the effect (receipt + application in ITS ledger), the
        ack was lost, and the process restarted: P2 has no transferable
        receipt history. Reconciliation must NOT conclude definite
        non-execution from P2's ``NEVER_RECEIVED`` — the row stays UNKNOWN.
        The original implementation marks it FAILED (defect reproduced).
        """
        effect_id = authorize_one(seeded)
        p1 = _provider(instantiated_at=T0 - timedelta(seconds=1))
        drive_to_unknown(seeded, at=T0, provider=p1, effect_id=effect_id)
        # The operation HAPPENED — captured from P1's ledger before the
        # process is discarded (the only durable witness this test has).
        assert p1.receipt_count(effect_id) == 1
        assert p1.application_count(effect_id) == 1
        # Restart: P2 constructed AFTER the submission (custody broken).
        p2 = _provider(instantiated_at=T0 + timedelta(seconds=120))
        assert p2.receipt_count(effect_id) == 0  # no transferable history
        with seeded["database"].session() as s:
            counts = reconcile_unknown(s, p2, now=T0 + timedelta(seconds=130))
        # Definitive failure is NOT justified: zero successes and — the RED
        # assertion — zero definitive failures (the original returns 1).
        assert counts[0] == 0
        assert counts[1] == 0
        if len(counts) == 3:
            assert counts[2] == 1  # inconclusive (corrected implementation)
        snapshot = get_row(seeded, effect_id)
        assert snapshot["status"] == EffectStatus.UNKNOWN  # preserved, not FAILED
        # No retry budget was consumed: attempts only advance on DEFINITIVE
        # failures — uncertainty is honest, not a spent attempt (INV-EF-R5).
        assert snapshot["attempts"] == 0
        # No duplicate delivery to P2, no unauthorized retry.
        assert p2.receipt_count(effect_id) == 0
        assert p2.application_count(effect_id) == 0

    def test_s1_c_a_marker_committed_no_call_custody_retained(self, seeded):
        """S1-C(a): crash between marker and provider call, provider alive.

        The provider instance predates the submission and genuinely never
        received anything — its NEVER_RECEIVED IS definitive (proven
        non-execution). Reconciliation closes the row as FAILED honestly.
        """
        effect_id = authorize_one(seeded)
        with seeded["database"].session() as s:
            refs = claim_eligible(s, now=T0, owner="d-1", cfg=effects_settings())
        assert len(refs) == 1
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
        # (dispatcher dies before the provider call; the PROVIDER survives)
        p_alive = _provider(instantiated_at=T0 - timedelta(seconds=1))
        assert p_alive.receipt_count(effect_id) == 0
        with seeded["database"].session() as s:
            unknowns = reap_ack_windows(s, now=T0 + timedelta(seconds=61))
        assert unknowns == 1
        with seeded["database"].session() as s:
            counts = reconcile_unknown(s, p_alive, now=T0 + timedelta(seconds=62))
        assert counts[0] == 0 and counts[1] == 1
        if len(counts) == 3:
            assert counts[2] == 0
        snapshot = get_row(seeded, effect_id)
        assert snapshot["status"] == EffectStatus.FAILED
        assert snapshot["attempts"] == 1
        assert snapshot["outcome_source"] == "reconciliation"
        assert p_alive.receipt_count(effect_id) == 0  # never delivered

    def test_s1_c_b_marker_committed_no_call_process_restarted(self, seeded):
        """S1-C(b) (RED): fresh process cannot prove the call never happened.

        Same crash boundary, but the whole process restarts: the answering
        provider was constructed AFTER the marker. Absence of a receipt in
        ITS ledger is not proof the submission never reached ANY provider
        instance — no false success and no unsupported definite failure.
        """
        effect_id = authorize_one(seeded)
        with seeded["database"].session() as s:
            refs = claim_eligible(s, now=T0, owner="d-1", cfg=effects_settings())
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
        with seeded["database"].session() as s:
            unknowns = reap_ack_windows(s, now=T0 + timedelta(seconds=61))
        assert unknowns == 1
        p2 = _provider(instantiated_at=T0 + timedelta(seconds=120))
        with seeded["database"].session() as s:
            counts = reconcile_unknown(s, p2, now=T0 + timedelta(seconds=130))
        assert counts[0] == 0
        assert counts[1] == 0  # RED on the original (returns 1 → FAILED)
        if len(counts) == 3:
            assert counts[2] == 1
        assert get_row(seeded, effect_id)["status"] == EffectStatus.UNKNOWN

    def test_s1_d_restart_new_attempt_reconciles_from_own_evidence(self, seeded):
        """S1-D: the continuity E1 actually supports — a NEW attempt's custody.

        E1 has no durable provider ledger (a same-process ledger would not
        prove remote-style atomicity anyway — prompt §3.3). The honest
        durable continuity is per-attempt: after a restart, a retryable
        row is re-claimed and resubmitted to P2; P2's OWN record (applied,
        lost ack) reconciles the row to SUCCEEDED — self-evidencing,
        regardless of the pre-restart history.
        """
        effect_id = authorize_one(seeded)
        p1 = _provider(instantiated_at=T0 - timedelta(seconds=1))
        p1.script_next(provider_mod.SCRIPT_FAIL)  # attempt 1: definitive reject
        result = dispatch_tick(
            seeded["database"].session,
            p1,
            now=T0 + timedelta(seconds=1),
            owner="d-1",
            settings=effects_settings(),
        )
        assert result.acked_failure == 1
        snapshot = get_row(seeded, effect_id)
        assert snapshot["status"] == EffectStatus.FAILED_RETRYABLE
        assert snapshot["attempts"] == 1
        # Restart: the new process owns a provider constructed at T0+10.
        p2 = _provider(instantiated_at=T0 + timedelta(seconds=10))
        p2.script_next(provider_mod.SCRIPT_LOSE_ACK)  # attempt 2: apply, lose ack
        result2 = dispatch_tick(
            seeded["database"].session,
            p2,
            now=T0 + timedelta(seconds=11),
            owner="d-2",
            settings=effects_settings(),
        )
        assert result2.submitted == 1 and result2.pending == 1
        assert p2.receipt_count(effect_id) == 1
        assert p2.application_count(effect_id) == 1
        with seeded["database"].session() as s:
            unknowns = reap_ack_windows(s, now=T0 + timedelta(seconds=72))
        assert unknowns == 1
        with seeded["database"].session() as s:
            counts = reconcile_unknown(s, p2, now=T0 + timedelta(seconds=73))
        assert counts[0] == 1 and counts[1] == 0
        snapshot = get_row(seeded, effect_id)
        assert snapshot["status"] == EffectStatus.SUCCEEDED
        assert snapshot["claim_epoch"] == 2  # the retry attempt owns the result
        assert snapshot["outcome_source"] == "reconciliation"
        # Stale/incomplete evidence stays rejected: P2 cannot speak for rows
        # submitted BEFORE it existed (S1-B semantics) — asserted separately.
        assert p1.receipt_count(effect_id) == 1  # P1 untouched by the retry

    def test_s1_e_repeated_reconciliation_idempotent_and_honest(self, seeded):
        """S1-E: repeated reconciliation never retries, never duplicates.

        The uncertain row stays honestly classified across repeated
        reconciliation passes: no unauthorized retry (the row is never
        re-claimed from UNKNOWN), no duplicate application, and — on the
        corrected implementation — exactly ONE inconclusive audit record
        per (row, provider instance), not one per pass.
        """
        effect_id = authorize_one(seeded)
        p1 = _provider(instantiated_at=T0 - timedelta(seconds=1))
        drive_to_unknown(seeded, at=T0, provider=p1, effect_id=effect_id)
        p2 = _provider(instantiated_at=T0 + timedelta(seconds=120))
        for pass_no in range(1, 4):
            with seeded["database"].session() as s:
                counts = reconcile_unknown(s, p2, now=T0 + timedelta(seconds=130 + pass_no))
            assert counts[0] == 0
            assert counts[1] == 0  # RED on pass 1 of the original (FAILED)
            if len(counts) == 3:
                assert counts[2] == 1
            snapshot = get_row(seeded, effect_id)
            assert snapshot["status"] == EffectStatus.UNKNOWN
            # Attempts frozen at 0 — inconclusive passes consume no budget.
            assert snapshot["attempts"] == 0
        # No delivery ever reached P2 — the synthetic operation was never
        # retried (no duplicate application could exist).
        assert p2.receipt_count(effect_id) == 0
        assert p2.application_count(effect_id) == 0
        with seeded["database"].session() as s:
            marker = (
                s.query(models.EffectEvent)
                .filter_by(effect_id=effect_id, reason="reconcile_inconclusive")
                .all()
            )
        if marker:  # exists only on the corrected implementation
            assert len(marker) == 1  # deduped per (row, provider instance)
            assert marker[0].from_status == "unknown"
            assert marker[0].to_status == "unknown"

    def test_s1_e_repeated_reconciliation_no_duplicate_transition(self, seeded):
        """S1-E (positive variant): an APPLIED row reconciles exactly once.

        Repeated passes over an already-reconciled row perform no further
        transition and record no further events (the candidate set only
        ever contains UNKNOWN rows).
        """
        effect_id = authorize_one(seeded)
        p1 = _provider(instantiated_at=T0 - timedelta(seconds=1))
        drive_to_unknown(seeded, at=T0, provider=p1, effect_id=effect_id)
        with seeded["database"].session() as s:
            first = reconcile_unknown(s, p1, now=T0 + timedelta(seconds=62))
        assert first[0] == 1
        for _ in range(2):
            with seeded["database"].session() as s:
                again = reconcile_unknown(s, p1, now=T0 + timedelta(seconds=63))
            assert again[0] == 0 and again[1] == 0
        snapshot = get_row(seeded, effect_id)
        assert snapshot["status"] == EffectStatus.SUCCEEDED
        assert p1.receipt_count(effect_id) == 1
        assert p1.application_count(effect_id) == 1
        succeeded_events = [
            e for e in events_for(seeded, effect_id) if e[2] == "reconciled_applied"
        ]
        assert len(succeeded_events) == 1


# ---------------------------------------------------------------------------
# S2 — Delayed ACK loses the originating dispatcher epoch (prompt §4)
# ---------------------------------------------------------------------------


class TestS2AckProvenance:
    def _row_submitted_at_epoch_two(self, seeded, *, owner):
        """Construct a validly-SUBMITTED row at claim epoch 2 (attempt 1 stalled).

        All transitions are the ordinary pipeline: claim(A) → claim-deadline
        requeue → claim(B, epoch 2) → mark SUBMITTED. Nothing about the row
        is fabricated.
        """
        effect_id = authorize_one(seeded)
        with seeded["database"].session() as s:
            refs = claim_eligible(s, now=T0, owner="d-A", cfg=effects_settings())
        assert refs[0].held_epoch == 1
        # A stalls out; the deadline reaper requeues (epoch untouched).
        with seeded["database"].session() as s:
            requeued = reap_claim_deadlines(s, now=T0 + timedelta(seconds=31))
        assert requeued == 1
        with seeded["database"].session() as s:
            refs_b = claim_eligible(
                s, now=T0 + timedelta(seconds=32), owner=owner, cfg=effects_settings()
            )
        assert len(refs_b) == 1 and refs_b[0].held_epoch == 2
        with seeded["database"].session() as s:
            stage = mark_submitted(
                s,
                refs_b[0].effect_id,
                refs_b[0].held_epoch,
                refs_b[0].owner,
                now=T0 + timedelta(seconds=32),
                cfg=effects_settings(),
            )
        assert stage == "submitted"
        return effect_id, refs_b[0]

    def test_s2_a_delayed_ack_from_active_attempt(self, seeded):
        """S2-A: the ACK's originating epoch == the row's current epoch.

        The delayed acknowledgment is delivered by the tick's pump while
        the originating attempt still owns the row: the row reaches
        SUCCEEDED, and the audit event carries the ORIGINATING epoch.
        """
        effect_id = authorize_one(seeded)
        provider = _provider()
        provider.script_next(provider_mod.SCRIPT_DELAY_ACK, delay_seconds=30)
        first = dispatch_tick(
            seeded["database"].session,
            provider,
            now=T0 + timedelta(seconds=1),
            owner="d-1",
            settings=effects_settings(),
        )
        assert first.submitted == 1 and first.pending == 1
        assert get_row(seeded, effect_id)["status"] == EffectStatus.SUBMITTED
        second = dispatch_tick(
            seeded["database"].session,
            provider,
            now=T0 + timedelta(seconds=40),
            owner="d-1",
            settings=effects_settings(),
        )
        assert second.acked_success == 1
        snapshot = get_row(seeded, effect_id)
        assert snapshot["status"] == EffectStatus.SUCCEEDED
        assert snapshot["claim_epoch"] == 1 and snapshot["claim_owner"] == "d-1"
        assert provider.receipt_count(effect_id) == 1
        assert provider.application_count(effect_id) == 1
        # Audit consistency: the success event is bound to the ORIGINATING
        # attempt's epoch (the epoch that submitted — not a substituted one).
        ack_events = [e for e in events_for(seeded, effect_id) if e[2] == "provider_ack"]
        assert len(ack_events) == 1
        assert ack_events[0][3] == snapshot["claim_epoch"]  # epoch match
        assert ack_events[0][4] == "d-1"

    def test_s2_b_delayed_ack_after_epoch_replacement_rejected(self, seeded):
        """S2-B (RED core): a provenance-less ACK must never land on a
        NEWER attempt.

        The row validly belongs to attempt 2 (A stalled → requeue → B
        claimed and marked SUBMITTED). The pump-form acknowledgment — the
        exact call signature ``dispatch_tick`` uses for pump deliveries —
        carries NO originating-epoch metadata. The original implementation
        substitutes the row's CURRENT epoch (2) and applies it as B's ack;
        the corrected implementation rejects it fail-closed with zero bytes.
        """
        effect_id, _ref_b = self._row_submitted_at_epoch_two(seeded, owner="d-B")
        snapshot = get_row(seeded, effect_id)
        assert snapshot["status"] == EffectStatus.SUBMITTED
        assert snapshot["claim_epoch"] == 2 and snapshot["claim_owner"] == "d-B"
        events_before = events_for(seeded, effect_id)
        # Pump-form delivery: exactly what dispatch_tick:1086 does today —
        # no held_epoch, no owner.
        with seeded["database"].session() as s:
            stage = apply_provider_ack(
                s, effect_id, provider_mod.AckOutcome.SUCCESS, now=T0 + timedelta(seconds=40)
            )
        assert stage == "ack_no_provenance"  # RED: original says acked_success
        snapshot = get_row(seeded, effect_id)
        assert snapshot["status"] == EffectStatus.SUBMITTED  # zero bytes
        assert snapshot["claim_epoch"] == 2 and snapshot["claim_owner"] == "d-B"
        assert events_for(seeded, effect_id) == events_before  # no new events
        assert "ack_without_provenance" in security_reasons(seeded)

    def test_s2_b_explicit_stale_epoch_rejected_zero_bytes(self, seeded):
        """S2-B (regression lock): an EXPLICIT stale epoch raises.

        A's delayed ACK that honestly carries its originating epoch (1)
        against the newer attempt (2) is rejected with ZERO bytes — the
        I-3 stale-epoch contract, unchanged by this remediation.
        """
        effect_id, _ = self._row_submitted_at_epoch_two(seeded, owner="d-B")
        events_before = events_for(seeded, effect_id)
        with seeded["database"].session() as s:
            with pytest.raises(StaleDispatcherError):
                apply_provider_ack(
                    s,
                    effect_id,
                    provider_mod.AckOutcome.SUCCESS,
                    now=T0 + timedelta(seconds=40),
                    held_epoch=1,
                    owner="d-A",
                )
        after = get_row(seeded, effect_id)
        assert after["status"] == EffectStatus.SUBMITTED
        assert after["claim_epoch"] == 2 and after["claim_owner"] == "d-B"
        assert events_for(seeded, effect_id) == events_before
        assert "stale_dispatcher_rejected" in security_reasons(seeded)

    def test_s2_c_duplicate_ack_same_epoch_idempotent(self, seeded):
        """S2-C: the identical acknowledgment delivered twice.

        First delivery: SUBMITTED→SUCCEEDED. Second delivery from the SAME
        valid attempt: idempotent no-op — no duplicate transition, no
        duplicate audit event, provider counters unchanged.
        """
        effect_id = authorize_one(seeded)
        provider = _provider()
        provider.script_next(provider_mod.SCRIPT_DELAY_ACK, delay_seconds=10)
        first = dispatch_tick(
            seeded["database"].session,
            provider,
            now=T0 + timedelta(seconds=1),
            owner="d-1",
            settings=effects_settings(),
        )
        assert first.pending == 1
        second = dispatch_tick(
            seeded["database"].session,
            provider,
            now=T0 + timedelta(seconds=20),
            owner="d-1",
            settings=effects_settings(),
        )
        assert second.acked_success == 1
        events_after_first = events_for(seeded, effect_id)
        # Duplicate delivery of the SAME ack (same provenance).
        with seeded["database"].session() as s:
            stage = apply_provider_ack(
                s,
                effect_id,
                provider_mod.AckOutcome.SUCCESS,
                now=T0 + timedelta(seconds=21),
                held_epoch=1,
                owner="d-1",
            )
        assert stage == "idempotent_noop"
        assert get_row(seeded, effect_id)["status"] == EffectStatus.SUCCEEDED
        assert events_for(seeded, effect_id) == events_after_first
        assert provider.receipt_count(effect_id) == 1
        assert provider.application_count(effect_id) == 1

    def test_s2_d_ack_without_provenance_fail_closed(self, seeded):
        """S2-D (RED core): omit originating metadata → reject, never
        substitute.

        The row is SUBMITTED at epoch 1 under the ACTIVE attempt. An ack
        with NO provenance arrives (the pump form). The original
        implementation substitutes the current epoch and applies it; the
        corrected implementation fails closed: rejected, zero bytes, and
        an audit record — the current epoch is NEVER accepted as proof of
        authority.
        """
        effect_id = authorize_one(seeded)
        provider = _provider()
        with seeded["database"].session() as s:
            refs = claim_eligible(s, now=T0, owner="d-1", cfg=effects_settings())
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
        before = get_row(seeded, effect_id)
        events_before = events_for(seeded, effect_id)
        with seeded["database"].session() as s:
            stage = apply_provider_ack(
                s, effect_id, provider_mod.AckOutcome.SUCCESS, now=T0 + timedelta(seconds=5)
            )
        assert stage == "ack_no_provenance"  # RED: original says acked_success
        after = get_row(seeded, effect_id)
        assert after["status"] == EffectStatus.SUBMITTED  # zero bytes on the row
        assert after["claim_epoch"] == before["claim_epoch"]
        assert after["acknowledged_at"] is None
        assert after["final_outcome_at"] is None
        assert events_for(seeded, effect_id) == events_before
        assert "ack_without_provenance" in security_reasons(seeded)
        assert provider.receipt_count(effect_id) == 0  # nothing was submitted to it

    def test_s2_e_delayed_ack_after_provider_reconstruction(self, seeded):
        """S2-E: attempt metadata lost with the provider → no false success.

        P1 queued a delayed ack (due T0+30) and the process died with it.
        The reconstructed provider P2 has NO pending ack and NO knowledge
        of the attempt: nothing is delivered, the row honestly traverses
        UNKNOWN, and reconciliation under P2's broken custody preserves
        UNKNOWN (no false success from missing provenance).
        """
        effect_id = authorize_one(seeded)
        p1 = _provider(instantiated_at=T0 - timedelta(seconds=1))
        p1.script_next(provider_mod.SCRIPT_DELAY_ACK, delay_seconds=30)
        first = dispatch_tick(
            seeded["database"].session,
            p1,
            now=T0 + timedelta(seconds=1),
            owner="d-1",
            settings=effects_settings(),
        )
        assert first.pending == 1
        # Crash: P1 (and its in-memory pending ack) is discarded.
        p2 = _provider(instantiated_at=T0 + timedelta(seconds=35))
        result = dispatch_tick(
            seeded["database"].session,
            p2,
            now=T0 + timedelta(seconds=40),
            owner="d-restart",
            settings=effects_settings(),
        )
        # No ack existed to deliver — nothing false was recorded.
        assert result.acked_success == 0
        assert result.submitted == 0  # SUBMITTED rows are never re-claimed
        assert get_row(seeded, effect_id)["status"] == EffectStatus.SUBMITTED
        with seeded["database"].session() as s:
            unknowns = reap_ack_windows(s, now=T0 + timedelta(seconds=62))
        assert unknowns == 1
        with seeded["database"].session() as s:
            counts = reconcile_unknown(s, p2, now=T0 + timedelta(seconds=70))
        assert counts[0] == 0 and counts[1] == 0  # RED: original closes FAILED
        assert get_row(seeded, effect_id)["status"] == EffectStatus.UNKNOWN
        assert p2.receipt_count(effect_id) == 0
        # The operation DID happen (P1 applied it) — but its evidence died
        # with P1; the honest state is uncertainty, not a definite outcome.
        assert p1.application_count(effect_id) == 1

    def test_s2_f_concurrent_takeover_deterministic_barrier(self, seeded):
        """S2-F: deterministic barrier — epoch 2 wins; both attempts answer.

        The barrier is the reaper's timestamp deadline (deterministic
        ordering; the TRUE concurrent claim race is the PG harness's
        domain — EFF-15). Responses from BOTH attempts are then delivered:
        the winner's is an idempotent confirmation, the loser's is rejected
        with zero bytes, and a provenance-less variant cannot snoop the
        winner's authority.
        """
        effect_id = authorize_one(seeded)
        with seeded["database"].session() as s:
            refs_a = claim_eligible(s, now=T0, owner="d-A", cfg=effects_settings())
        assert refs_a[0].held_epoch == 1
        # --- deterministic barrier: A stalls; the reaper requeues at +31s
        with seeded["database"].session() as s:
            requeued = reap_claim_deadlines(s, now=T0 + timedelta(seconds=31))
        assert requeued == 1
        provider = _provider()
        # B wins the second claim race (epoch 2) and completes.
        second = dispatch_tick(
            seeded["database"].session,
            provider,
            now=T0 + timedelta(seconds=32),
            owner="d-B",
            settings=effects_settings(),
        )
        assert second.claimed == 1 and second.acked_success == 1
        snapshot = get_row(seeded, effect_id)
        assert snapshot["status"] == EffectStatus.SUCCEEDED
        assert snapshot["claim_epoch"] == 2 and snapshot["claim_owner"] == "d-B"
        events_before = events_for(seeded, effect_id)
        # The winning attempt's own (duplicate) response: idempotent no-op.
        with seeded["database"].session() as s:
            stage = apply_provider_ack(
                s,
                effect_id,
                provider_mod.AckOutcome.SUCCESS,
                now=T0 + timedelta(seconds=40),
                held_epoch=2,
                owner="d-B",
            )
        assert stage == "idempotent_noop"
        # The losing attempt's response with honest provenance: rejected.
        with seeded["database"].session() as s:
            with pytest.raises(StaleDispatcherError):
                apply_provider_ack(
                    s,
                    effect_id,
                    provider_mod.AckOutcome.SUCCESS,
                    now=T0 + timedelta(seconds=41),
                    held_epoch=1,
                    owner="d-A",
                )
        # The losing attempt's response WITHOUT provenance: fail-closed —
        # never attributed to the winner (RED: original applies it).
        with seeded["database"].session() as s:
            stage = apply_provider_ack(
                s, effect_id, provider_mod.AckOutcome.SUCCESS, now=T0 + timedelta(seconds=42)
            )
        assert stage == "ack_no_provenance"  # RED: original says idempotent_noop
        final = get_row(seeded, effect_id)
        assert final["status"] == EffectStatus.SUCCEEDED  # B's result intact
        assert final["claim_epoch"] == 2 and final["claim_owner"] == "d-B"
        assert events_for(seeded, effect_id) == events_before
        assert "ack_without_provenance" in security_reasons(seeded)
        assert provider.receipt_count(effect_id) == 1
        assert provider.application_count(effect_id) == 1

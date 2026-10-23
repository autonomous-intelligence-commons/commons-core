"""Trusted fake-only dispatcher for the E1 effect outbox (R02-B2).

The dispatcher is the AUTHORITY for every outbound (simulated) submission.
It never invokes anything but the in-process :class:`FakeEffectProvider`
(pressed through :func:`dispatch_tick`'s provider argument — the ONLY
provider class that exists in this codebase).

Ownership model (Phase 0 P0-C4):

- ``claim_epoch`` — monotonic per-row counter, incremented ONLY inside the
  claim's conditional UPDATE; deadline recovery NEVER touches it (mirrors
  the R02-B1 ``lease_generation`` accounting);
- every dispatcher-owned write presents the epoch held at claim time; a
  conditional UPDATE matching zero rows raises
  :class:`StaleDispatcherError` — a superseded dispatcher writes ZERO
  bytes and its observation is discarded (never overwrites the newer
  authoritative local result), with an audit event (counts only).

Submission order (Phase 0 P0-C1 §2.3 — the point-of-no-return rule):

1. per-row revalidation (stop / agent state / lease snapshot / digest) —
   failures transition CLAIMED→CANCELLED or →EXPIRED with ZERO outbound;
2. the SUBMITTED marker commits FIRST (``submitted_at`` — a local intent
   record, NOT proof of provider receipt);
3. only THEN is the provider called; outcomes are applied in a separate
   short transaction (crash between mark and outcome → the row honestly
   traverses UNKNOWN → reconciliation).

System-owned transitions (claim-deadline requeue, authorization-TTL
expiry, ack-window UNKNOWN) are conditional UPDATEs evaluated by the
database — mirroring ``expire_leases``; they never touch the epoch and a
committed terminal state can never be flipped back.

The public stage functions are deliberately granular so tests can stop the
pipeline AT a crash boundary (B3'/B4'/B5'/B6') without monkeypatching.
``dispatch_tick`` orchestrates the stages over short per-step transactions
(the same two-phase discipline as ``run_worker``).
"""

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from commons_core.config.settings import Settings
from commons_core.database.enums import AgentStatus, EffectStatus, SecurityEventCategory
from commons_core.database.models import (
    Agent,
    EffectEvent,
    EffectRequest,
    ScheduledTask,
    SecurityEvent,
)
from commons_core.effects.capabilities import (
    CAPABILITY_ALLOWLIST,
    validate_operation,
)
from commons_core.effects.errors import (
    InvalidEffectOperationError,
    StaleDispatcherError,
    UnknownEffectError,
)
from commons_core.effects.provider import (
    AckOutcome,
    FakeEffectProvider,
    ProviderStatus,
    SubmitResult,
)
from commons_core.infrastructure.system_state import emergency_stop_active
from commons_core.infrastructure.time import as_utc, utc_now

#: Session-context factory type (mirrors ``scheduler.worker.SessionFactory``).
SessionFactory = Callable[[], AbstractContextManager[Session]]

#: Maximum rows claimed per tick (bounded batches, like ``_CLAIM_CANDIDATES``).
EFFECT_CLAIM_BATCH = 100

#: Statuses eligible for dispatcher claims (first-time + retry re-claims).
_CLAIMABLE_STATUSES = (EffectStatus.AUTHORIZED, EffectStatus.FAILED_RETRYABLE)


@dataclass
class ClaimedRef:
    """A won claim: the row id and the epoch/owner that now own it."""

    effect_id: str
    held_epoch: int
    owner: str


@dataclass
class DispatchTickResult:
    """Exact counts of one dispatcher tick (evidence-grade, no approximations)."""

    now: datetime
    owner: str
    #: AIC_EFFECTS_ENABLED=false — nothing scanned, rows frozen (INV-EF-R11).
    frozen: bool = False
    cancelled_stop_sweep: int = 0
    requeued: int = 0
    expired_ttl: int = 0
    unknown_reaped: int = 0
    claimed: int = 0
    submitted: int = 0
    cancelled_on_submit: int = 0
    expired_on_submit: int = 0
    acked_success: int = 0
    acked_failure: int = 0
    ambiguous: int = 0
    pending: int = 0
    idempotent_noop: int = 0
    stale_rejected: int = 0
    #: Pump acks rejected fail-closed for missing originating-attempt
    #: provenance (S2) — never attributed to the current attempt.
    ack_no_provenance: int = 0
    reconciled_success: int = 0
    reconciled_failed: int = 0
    #: UNKNOWN rows left in place because the answering provider instance
    #: cannot establish receipt custody (S1 — absence of evidence is not
    #: evidence of non-execution).
    reconciled_inconclusive: int = 0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _event(
    session: Session,
    effect_id: str,
    from_status: str,
    to_status: str,
    reason: str,
    *,
    epoch: int | None = None,
    owner: str | None = None,
    moment: datetime,
) -> None:
    """Append a transition audit event (same transaction as the transition)."""
    session.add(
        EffectEvent(
            effect_id=effect_id,
            from_status=from_status,
            to_status=to_status,
            reason=str(reason)[:64],
            claim_epoch=epoch,
            owner=owner,
            occurred_at=moment,
        )
    )


def _stop_engaged(session: Session) -> bool:
    return emergency_stop_active(session)


def _cas(
    session: Session,
    effect_id: str,
    from_status: EffectStatus,
    values: dict,
    *,
    held_epoch: int | None = None,
    owner: str | None = None,
    extra: list | None = None,
) -> int:
    """One conditional UPDATE; returns the rowcount (0 = predicate missed)."""
    predicates = [EffectRequest.id == effect_id, EffectRequest.status == from_status]
    if held_epoch is not None:
        predicates.append(EffectRequest.claim_epoch == held_epoch)
    if owner is not None:
        predicates.append(EffectRequest.claim_owner == owner)
    if extra:
        predicates.extend(extra)
    result = session.execute(
        update(EffectRequest)
        .where(*predicates)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount)


# ---------------------------------------------------------------------------
# System-owned reapers (conditional UPDATEs; never touch the epoch)
# ---------------------------------------------------------------------------


def reap_claim_deadlines(session: Session, *, now: datetime) -> int:
    """Requeue CLAIMED rows whose claim deadline passed (B4' crash recovery).

    The row returns to AUTHORIZED — re-claimable on a later tick with the
    NEXT epoch (the reaper itself never increments). A dispatcher that
    completed its submission between the scan and the CAS simply does not
    match the predicate (rowcount 0, not counted, not overwritten).
    """
    moment = as_utc(now)
    candidate_ids = list(
        session.scalars(
            select(EffectRequest.id).where(
                EffectRequest.status == EffectStatus.CLAIMED,
                EffectRequest.claim_deadline < moment,
            )
        )
    )
    recovered = 0
    for effect_id in candidate_ids:
        rowcount = _cas(
            session,
            effect_id,
            EffectStatus.CLAIMED,
            {
                "status": EffectStatus.AUTHORIZED,
                "claim_owner": None,
                "claimed_at": None,
                "claim_deadline": None,
                "updated_at": moment,
            },
            extra=[EffectRequest.claim_deadline < moment],
        )
        if rowcount == 1:
            recovered += 1
            _event(
                session,
                effect_id,
                EffectStatus.CLAIMED.value,
                EffectStatus.AUTHORIZED.value,
                "claim_deadline",
                moment=moment,
            )
    return recovered


def reap_authorization_ttl(session: Session, *, now: datetime, cfg: Settings) -> int:
    """Expire AUTHORIZED rows never claimed within the authorization TTL."""
    moment = as_utc(now)
    cutoff = moment - timedelta(seconds=int(cfg.effects_authorization_ttl_seconds))
    candidate_ids = list(
        session.scalars(
            select(EffectRequest.id).where(
                EffectRequest.status == EffectStatus.AUTHORIZED,
                EffectRequest.created_at < cutoff,
            )
        )
    )
    expired = 0
    for effect_id in candidate_ids:
        rowcount = _cas(
            session,
            effect_id,
            EffectStatus.AUTHORIZED,
            {
                "status": EffectStatus.EXPIRED,
                "cancel_reason": None,
                "final_outcome_at": moment,
                "outcome_source": "reaper",
                "updated_at": moment,
            },
            extra=[EffectRequest.created_at < cutoff],
        )
        if rowcount == 1:
            expired += 1
            _event(
                session,
                effect_id,
                EffectStatus.AUTHORIZED.value,
                EffectStatus.EXPIRED.value,
                "authorization_ttl",
                moment=moment,
            )
    return expired


def reap_ack_windows(session: Session, *, now: datetime) -> int:
    """Transition SUBMITTED rows whose ack window passed to UNKNOWN (never FAILED).

    The B4'/B5' iron rule: "no local success record" is NOT "did not
    happen". The ONLY legal transition is UNKNOWN — resolution belongs to
    reconciliation (:func:`reconcile_unknown`) or (test-only, labeled)
    manual marking. Attempts are frozen (INV-EF-R5).
    """
    moment = as_utc(now)
    candidate_ids = list(
        session.scalars(
            select(EffectRequest.id).where(
                EffectRequest.status == EffectStatus.SUBMITTED,
                EffectRequest.ack_deadline < moment,
            )
        )
    )
    unknowns = 0
    for effect_id in candidate_ids:
        rowcount = _cas(
            session,
            effect_id,
            EffectStatus.SUBMITTED,
            {
                "status": EffectStatus.UNKNOWN,
                "outcome_source": None,
                "updated_at": moment,
            },
            extra=[EffectRequest.ack_deadline < moment],
        )
        if rowcount == 1:
            unknowns += 1
            _event(
                session,
                effect_id,
                EffectStatus.SUBMITTED.value,
                EffectStatus.UNKNOWN.value,
                "ack_window_expired",
                moment=moment,
            )
    return unknowns


# ---------------------------------------------------------------------------
# Pre-submission cancellation sweep (emergency stop / revocation linkage)
# ---------------------------------------------------------------------------


def cancel_pending_effects(session: Session, *, reason: str, now: datetime) -> int:
    """Cancel every pre-submission row (AUTHORIZED or CLAIMED).

    P0-C1 atomic cancellation predicate — a single conditional UPDATE per
    row, STATUS-based: the CURRENT claim's submission state is the status
    column, not the historical ``submitted_at`` (a retry-claimed row is
    cancellable before ITS re-submission even though a previous attempt
    was submitted — discovered during implementation; INV-EF-R13
    amended in the Phase 0 report). Rows already SUBMITTED structurally
    cannot be cancelled (EFF-12 ban); they complete or reconcile
    honestly. The cancel-vs-submit race resolves via the database's
    row-level serialization of the two CAS predicates (exactly one wins;
    the loser's re-check misses).
    """
    moment = as_utc(now)
    candidate_rows = list(
        session.execute(
            select(EffectRequest.id, EffectRequest.status).where(
                EffectRequest.status.in_((EffectStatus.AUTHORIZED, EffectStatus.CLAIMED)),
            )
        )
    )
    cancelled = 0
    for effect_id, from_status in candidate_rows:
        rowcount = _cas(
            session,
            effect_id,
            from_status,
            {
                "status": EffectStatus.CANCELLED,
                "cancel_reason": str(reason)[:64],
                "final_outcome_at": moment,
                "outcome_source": "cancellation",
                "updated_at": moment,
            },
        )
        if rowcount == 1:
            cancelled += 1
            _event(
                session,
                effect_id,
                from_status.value,
                EffectStatus.CANCELLED.value,
                reason,
                moment=moment,
            )
    return cancelled


# ---------------------------------------------------------------------------
# Dispatcher-owned stages
# ---------------------------------------------------------------------------


def claim_eligible(
    session: Session,
    *,
    now: datetime,
    owner: str,
    cfg: Settings,
    limit: int | None = None,
) -> list[ClaimedRef]:
    """Atomically claim eligible rows (AUTHORIZED + retryable FAILED_RETRYABLE).

    Each claim is a conditional UPDATE that ALSO increments ``claim_epoch``
    on the database side — exactly one increment per WON claim; losing
    claims write nothing. On PostgreSQL the candidate scan takes
    ``FOR UPDATE SKIP LOCKED`` (same dialect switch as ``claim_next``).
    Retry re-claims are bounded by the attempts budget in the predicate.
    """
    moment = as_utc(now)
    stmt = (
        select(EffectRequest)
        .where(
            (EffectRequest.status == EffectStatus.AUTHORIZED)
            | (
                (EffectRequest.status == EffectStatus.FAILED_RETRYABLE)
                & (EffectRequest.attempts < EffectRequest.max_attempts)
            )
        )
        .order_by(EffectRequest.created_at, EffectRequest.id)
        .limit(int(limit or EFFECT_CLAIM_BATCH))
    )
    bind = session.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    candidates = list(session.scalars(stmt))
    deadline = moment + timedelta(seconds=int(cfg.effects_claim_deadline_seconds))
    won: list[ClaimedRef] = []
    for candidate in candidates:
        from_status = candidate.status
        result = session.execute(
            update(EffectRequest)
            .where(
                EffectRequest.id == candidate.id,
                EffectRequest.status == from_status,
                EffectRequest.claim_epoch == candidate.claim_epoch,
            )
            .values(
                status=EffectStatus.CLAIMED,
                claim_epoch=EffectRequest.claim_epoch + 1,
                claim_owner=owner,
                claimed_at=moment,
                claim_deadline=deadline,
                updated_at=moment,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 1:
            held_epoch = int(candidate.claim_epoch) + 1
            won.append(ClaimedRef(effect_id=candidate.id, held_epoch=held_epoch, owner=owner))
            _event(
                session,
                candidate.id,
                from_status.value,
                EffectStatus.CLAIMED.value,
                "claimed",
                epoch=held_epoch,
                owner=owner,
                moment=moment,
            )
        # A lost race (rowcount 0) writes nothing and moves to the next row.
    return won


def mark_submitted(
    session: Session,
    effect_id: str,
    held_epoch: int,
    owner: str,
    *,
    now: datetime,
    cfg: Settings,
) -> str:
    """Revalidate then commit the SUBMITTED marker — the point of no return.

    Revalidation (ALL before any provider call — zero outbound on failure):
    emergency stop → CANCELLED; agent not ACTIVE → CANCELLED (E1 synthetic
    revocation policy); lease snapshot mismatch → EXPIRED; operation
    descriptor/digest integrity failure → EXPIRED (frozen, never
    submitted). The marker CAS carries the full epoch/owner predicate — a
    superseded dispatcher raises StaleDispatcherError (zero bytes). The
    CURRENT-claim submission state is the STATUS column alone: a retry
    re-claim legitimately re-marks a row whose ``submitted_at`` still
    records a PREVIOUS attempt's submission (the durable "submitted at
    least once" fact is never cleared; full per-attempt history lives in
    ``effect_events`` — INV-EF-R13 amended).
    """
    moment = as_utc(now)
    row = session.get(EffectRequest, effect_id)
    if row is None:
        raise UnknownEffectError()

    def _transition(target: EffectStatus, reason: str, source: str | None) -> str:
        rowcount = _cas(
            session,
            effect_id,
            EffectStatus.CLAIMED,
            {
                "status": target,
                "cancel_reason": reason if target is EffectStatus.CANCELLED else None,
                "final_outcome_at": moment,
                "outcome_source": source,
                "updated_at": moment,
            },
            held_epoch=held_epoch,
            owner=owner,
        )
        if rowcount != 1:
            raise StaleDispatcherError(effect_id, held_epoch)
        _event(
            session,
            effect_id,
            EffectStatus.CLAIMED.value,
            target.value,
            reason,
            epoch=held_epoch,
            owner=owner,
            moment=moment,
        )
        return "cancelled" if target is EffectStatus.CANCELLED else "expired"

    if _stop_engaged(session):
        return _transition(EffectStatus.CANCELLED, "emergency_stop", "cancellation")

    agent = session.get(Agent, row.agent_id)
    if agent is None or agent.status != AgentStatus.ACTIVE:
        return _transition(EffectStatus.CANCELLED, "agent_not_active", "cancellation")

    task = session.get(ScheduledTask, row.task_id)
    if task is None or int(task.lease_generation) != int(row.claim_generation):
        # Defense-in-depth lease guard (Phase 0 §2.5): the natural interleaving
        # is structurally prevented by the task CAS semantics, but a snapshot
        # mismatch still means ZERO outbound — verified, never assumed.
        return _transition(EffectStatus.EXPIRED, "lease_superseded", "dispatcher")

    try:
        normalized, digest = validate_operation(row.operation)
    except InvalidEffectOperationError:
        return _transition(EffectStatus.EXPIRED, "operation_invalid", "dispatcher")
    if digest != row.request_digest:
        # Integrity freeze: the descriptor changed after authorization.
        session.add(
            SecurityEvent(
                category=SecurityEventCategory.POLICY_VIOLATION,
                principal=str(row.agent_id)[:64],
                details={"reason": "effect_digest_mismatch", "capability": row.capability[:64]},
            )
        )
        return _transition(EffectStatus.EXPIRED, "digest_mismatch", "dispatcher")

    ack_deadline = moment + timedelta(seconds=int(cfg.effects_ack_window_seconds))
    rowcount = _cas(
        session,
        effect_id,
        EffectStatus.CLAIMED,
        {
            "status": EffectStatus.SUBMITTED,
            "submitted_at": moment,
            "ack_deadline": ack_deadline,
            "updated_at": moment,
        },
        held_epoch=held_epoch,
        owner=owner,
    )
    if rowcount != 1:
        raise StaleDispatcherError(effect_id, held_epoch)
    _event(
        session,
        effect_id,
        EffectStatus.CLAIMED.value,
        EffectStatus.SUBMITTED.value,
        "submitted",
        epoch=held_epoch,
        owner=owner,
        moment=moment,
    )
    return "submitted"


def _failure_target(row: EffectRequest, spec) -> EffectStatus:
    """FAILED vs FAILED_RETRYABLE per policy (risk class + budget + capability)."""
    attempts_after = int(row.attempts) + 1
    if attempts_after >= int(row.max_attempts):
        return EffectStatus.FAILED
    if not spec.auto_retry:
        # Class-4 / high-impact policy: never automatically retryable.
        return EffectStatus.FAILED
    return EffectStatus.FAILED_RETRYABLE


def _apply_failure(
    session: Session,
    effect_id: str,
    held_epoch: int,
    owner: str,
    *,
    now: datetime,
    reason: str,
    from_status: EffectStatus,
) -> str:
    """Apply a DEFINITIVE provider failure (attempts+1 atomically in the CAS)."""
    moment = as_utc(now)
    row = session.get(EffectRequest, effect_id)
    if row is None:
        raise UnknownEffectError()
    spec = CAPABILITY_ALLOWLIST[row.capability]
    target = _failure_target(row, spec)
    rowcount = _cas(
        session,
        effect_id,
        from_status,
        {
            "status": target,
            "attempts": EffectRequest.attempts + 1,
            "final_outcome_at": moment,
            "outcome_source": "provider_ack",
            "updated_at": moment,
        },
        held_epoch=held_epoch,
        owner=owner,
    )
    if rowcount != 1:
        raise StaleDispatcherError(effect_id, held_epoch)
    _event(
        session,
        effect_id,
        from_status.value,
        target.value,
        reason,
        epoch=held_epoch,
        owner=owner,
        moment=moment,
    )
    return "ack_failure" if target is EffectStatus.FAILED_RETRYABLE else "failed"


def apply_submit_outcome(
    session: Session,
    effect_id: str,
    result: SubmitResult,
    *,
    held_epoch: int,
    owner: str,
    now: datetime,
    reason_code: str = "provider_rejected",
) -> str:
    """Apply the synchronous submit outcome in its OWN short transaction.

    - ACKED    → SUBMITTED→SUCCEEDED (duplicate success ack on an already
      SUCCEEDED row = idempotent no-op, EFF-06b);
    - REJECTED → definitive failure (attempts+1, FAILED vs FAILED_RETRYABLE
      per policy);
    - PENDING  → nothing (the ack window / pump owns the next transition);
    - AMBIGUOUS→ UNKNOWN immediately (semantically unclear non-2xx is NOT
      proof of non-application — EFF-14).
    """
    moment = as_utc(now)
    if result is SubmitResult.PENDING:
        return "pending"
    if result is SubmitResult.AMBIGUOUS:
        rowcount = _cas(
            session,
            effect_id,
            EffectStatus.SUBMITTED,
            {
                "status": EffectStatus.UNKNOWN,
                "outcome_source": None,
                "updated_at": moment,
            },
            held_epoch=held_epoch,
            owner=owner,
        )
        if rowcount != 1:
            raise StaleDispatcherError(effect_id, held_epoch)
        _event(
            session,
            effect_id,
            EffectStatus.SUBMITTED.value,
            EffectStatus.UNKNOWN.value,
            "ambiguous_response",
            epoch=held_epoch,
            owner=owner,
            moment=moment,
        )
        return "ambiguous"

    row = session.get(EffectRequest, effect_id)
    if row is None:
        raise UnknownEffectError()
    if result is SubmitResult.ACKED:
        if row.status == EffectStatus.SUCCEEDED:
            # Duplicate success on a terminal row — idempotent confirmation.
            return "idempotent_noop"
        rowcount = _cas(
            session,
            effect_id,
            EffectStatus.SUBMITTED,
            {
                "status": EffectStatus.SUCCEEDED,
                "acknowledged_at": moment,
                "final_outcome_at": moment,
                "outcome_source": "provider_ack",
                "updated_at": moment,
            },
            held_epoch=held_epoch,
            owner=owner,
        )
        if rowcount != 1:
            raise StaleDispatcherError(effect_id, held_epoch)
        _event(
            session,
            effect_id,
            EffectStatus.SUBMITTED.value,
            EffectStatus.SUCCEEDED.value,
            "provider_ack",
            epoch=held_epoch,
            owner=owner,
            moment=moment,
        )
        return "acked_success"
    if result is SubmitResult.REJECTED:
        return _apply_failure(
            session,
            effect_id,
            held_epoch,
            owner,
            now=moment,
            reason=reason_code,
            from_status=EffectStatus.SUBMITTED,
        )
    raise ValueError(f"unknown submit result: {result!r}")  # pragma: no cover


def apply_provider_ack(
    session: Session,
    effect_id: str,
    outcome: AckOutcome,
    *,
    now: datetime,
    held_epoch: int | None = None,
    owner: str | None = None,
) -> str:
    """Apply one asynchronous provider acknowledgment (pump-delivered).

    S2/S3 fail-closed provenance: every pump ack carries its ORIGINATING
    attempt — ``held_epoch`` AND ``owner`` (from
    :class:`~..provider.DelayedAck`). An ack missing EITHER element
    (provenance unavailable) is REJECTED — ``"ack_no_provenance"`` with
    zero effect-row bytes and ONE deduplicated audit marker per effect
    row (repeated invalid acks do not amplify the security-event log;
    every delivery is still individually rejected by the return value).
    NEITHER the row's CURRENT claim epoch NOR its CURRENT claim owner is
    EVER substituted as proof of authority (S3-A). An EXPLICIT epoch
    that no longer matches — or an owner that is not the authoritative
    attempt's owner — is the STALE/MISMATCH-DISPATCHER path (P0-C4,
    S3-B): a :class:`StaleDispatcherError` is raised with ZERO bytes
    written — on ANY row state, terminal included (a superseded or
    non-owning dispatcher never gets "idempotent confirmation"; its
    observation is rejected and discarded BEFORE any terminal-state
    shortcut). Duplicate acks by the CURRENT owner on terminal rows are
    idempotent no-ops (EFF-06b). A late ack from the OWNING attempt on
    a non-SUBMITTED row is a ``"not_submitted"`` no-op — acks arriving
    after UNKNOWN do NOT jump states; reconciliation owns the UNKNOWN
    exit, and terminal outcomes are final.

    Rejections are audited in the SAME session (counts/ids only); a caller
    that rolls back discards it, but the tick's catch layer re-records
    durably in a fresh transaction.
    """
    moment = as_utc(now)
    row = session.get(EffectRequest, effect_id)
    if row is None:
        return "unknown_effect"
    if held_epoch is None or owner is None:
        # S2/S3 fail-closed: an acknowledgment that cannot COMPLETELY
        # name its originating attempt (epoch or owner missing) is never
        # attributed to the current one — no substitution, zero bytes.
        # Bounded audit (S3): ONE marker per effect row, so repeated
        # invalid acks cannot amplify the security-event log.
        already_marked = (
            session.query(SecurityEvent.id)
            .filter(
                SecurityEvent.details["reason"].as_string() == "ack_without_provenance",
                SecurityEvent.details["effect_id"].as_string() == effect_id,
            )
            .first()
            is not None
        )
        if not already_marked:
            session.add(
                SecurityEvent(
                    category=SecurityEventCategory.POLICY_VIOLATION,
                    principal="unknown",
                    details={
                        "reason": "ack_without_provenance",
                        "effect_id": effect_id,
                        "row_claim_epoch": int(row.claim_epoch),
                    },
                )
            )
            session.flush()
        return "ack_no_provenance"
    epoch = int(held_epoch)
    row_owner = str(owner)
    if epoch != int(row.claim_epoch):
        _audit_stale_dispatcher(session, effect_id, epoch, row_owner)
        raise StaleDispatcherError(effect_id, epoch)
    if row_owner != row.claim_owner:
        # S3-B: the ack names an owner that is NOT the authoritative
        # attempt's owner. Same rejection contract as a stale epoch (and
        # as the owner CAS-miss on the SUBMITTED path), enforced BEFORE
        # any terminal-state shortcut — a wrong-owner ack never reaches
        # the idempotent branch.
        _audit_stale_dispatcher(session, effect_id, epoch, row_owner)
        raise StaleDispatcherError(effect_id, epoch)
    if row.status == EffectStatus.SUCCEEDED:
        return "idempotent_noop"
    if row.status != EffectStatus.SUBMITTED:
        # Late ack from the OWNING attempt after UNKNOWN/terminal — a no-op;
        # the honest resolution lives in reconciliation (query_status) or
        # manual marking. (A wrong-identity ack cannot reach here: the
        # identity gate above rejected it first.)
        return "not_submitted"
    if outcome is AckOutcome.SUCCESS:
        rowcount = _cas(
            session,
            effect_id,
            EffectStatus.SUBMITTED,
            {
                "status": EffectStatus.SUCCEEDED,
                "acknowledged_at": moment,
                "final_outcome_at": moment,
                "outcome_source": "provider_ack",
                "updated_at": moment,
            },
            held_epoch=epoch,
            owner=row_owner,
        )
        if rowcount != 1:
            _audit_stale_dispatcher(session, effect_id, epoch, row_owner)
            raise StaleDispatcherError(effect_id, epoch)
        _event(
            session,
            effect_id,
            EffectStatus.SUBMITTED.value,
            EffectStatus.SUCCEEDED.value,
            "provider_ack",
            epoch=epoch,
            owner=row_owner,
            moment=moment,
        )
        return "acked_success"
    if outcome is AckOutcome.FAILURE:
        try:
            return _apply_failure(
                session,
                effect_id,
                epoch,
                row_owner,
                now=moment,
                reason="provider_acked_failure",
                from_status=EffectStatus.SUBMITTED,
            )
        except StaleDispatcherError:
            _audit_stale_dispatcher(session, effect_id, epoch, row_owner)
            raise
    raise ValueError(f"unknown ack outcome: {outcome!r}")  # pragma: no cover


def _audit_stale_dispatcher(
    session: Session, effect_id: str, held_epoch: int, owner: str | None
) -> None:
    """Same-session audit of a superseded-dispatcher rejection (counts/ids only)."""
    session.add(
        SecurityEvent(
            category=SecurityEventCategory.POLICY_VIOLATION,
            principal=str(owner or "unknown")[:64],
            details={
                "reason": "stale_dispatcher_rejected",
                "effect_id": effect_id,
                "held_epoch": int(held_epoch),
            },
        )
    )
    session.flush()


def reconcile_unknown(
    session: Session,
    provider: FakeEffectProvider,
    *,
    now: datetime,
) -> tuple[int, int, int]:
    """Close UNKNOWN rows from provider evidence (low/medium + queryable only).

    High-impact rows are deliberately EXCLUDED from automatic
    reconciliation (they freeze until the labeled test-only manual path —
    design §4.5/EFF-10); non-queryable capabilities (Class-4) have no
    evidence source and also stay frozen. Evidence semantics (P0-C4,
    amended by the S1 remediation):

    - APPLIED        → SUCCEEDED (provenance ``reconciliation``);
    - REJECTED       → FAILED — a self-evidencing record held by the
      ANSWERING instance (only that instance could have produced it);
    - NEVER_RECEIVED → FAILED only under RECEIPT CUSTODY: the answering
      instance was alive at/before the row's submission
      (``provider.custody_covers``), so its empty ledger proves the
      delivery never reached it. WITHOUT custody (a provider
      reconstructed after a restart — its history is unrecoverable
      process-local state) the row is INCONCLUSIVE: it stays UNKNOWN,
      counted in the third return element, and marked with ONE deduped
      ``reconcile_inconclusive`` audit event per (row, provider
      instance) — absence of evidence is NOT evidence of non-execution.
      Durable receipt continuity (a persistent provider ledger) is an
      E2/owner decision; E1 fails safe instead of guessing.

    Returns ``(success, failed, inconclusive)`` — exact transition counts.
    """
    moment = as_utc(now)
    candidates = list(
        session.scalars(select(EffectRequest).where(EffectRequest.status == EffectStatus.UNKNOWN))
    )
    success = failed = inconclusive = 0
    for row in candidates:
        spec = CAPABILITY_ALLOWLIST.get(row.capability)
        if spec is None or not spec.queryable:
            continue
        if spec.risk in ("high", "irreversible"):
            continue  # manual reconciliation only (EFF-10)
        status = provider.query_status(row.id)
        if status is ProviderStatus.APPLIED:
            rowcount = _cas(
                session,
                row.id,
                EffectStatus.UNKNOWN,
                {
                    "status": EffectStatus.SUCCEEDED,
                    "final_outcome_at": moment,
                    "outcome_source": "reconciliation",
                    "updated_at": moment,
                },
            )
            if rowcount == 1:
                success += 1
                _event(
                    session,
                    row.id,
                    EffectStatus.UNKNOWN.value,
                    EffectStatus.SUCCEEDED.value,
                    "reconciled_applied",
                    moment=moment,
                )
        elif status is ProviderStatus.REJECTED:
            # Self-evidencing: only the answering instance could hold this
            # record — definitive failure regardless of custody.
            rowcount = _cas(
                session,
                row.id,
                EffectStatus.UNKNOWN,
                {
                    "status": EffectStatus.FAILED,
                    "attempts": EffectRequest.attempts + 1,
                    "final_outcome_at": moment,
                    "outcome_source": "reconciliation",
                    "updated_at": moment,
                },
            )
            if rowcount == 1:
                failed += 1
                _event(
                    session,
                    row.id,
                    EffectStatus.UNKNOWN.value,
                    EffectStatus.FAILED.value,
                    "reconciled_not_applied",
                    moment=moment,
                )
        elif provider.custody_covers(row.submitted_at):
            # NEVER_RECEIVED under receipt custody: this instance was alive
            # at/before the submission and genuinely never received it —
            # definitive non-execution (the B5'-a semantics, preserved).
            rowcount = _cas(
                session,
                row.id,
                EffectStatus.UNKNOWN,
                {
                    "status": EffectStatus.FAILED,
                    "attempts": EffectRequest.attempts + 1,
                    "final_outcome_at": moment,
                    "outcome_source": "reconciliation",
                    "updated_at": moment,
                },
            )
            if rowcount == 1:
                failed += 1
                _event(
                    session,
                    row.id,
                    EffectStatus.UNKNOWN.value,
                    EffectStatus.FAILED.value,
                    "reconciled_not_applied",
                    moment=moment,
                )
        else:
            # NEVER_RECEIVED without custody (S1): the answering instance
            # was constructed after the submission — its empty ledger cannot
            # prove the operation never reached a predecessor instance.
            # Fail safe: preserve UNKNOWN, count it, and record ONE deduped
            # marker per (row, provider instance) so the operator sees WHY
            # the row is still uncertain (bounded: never one event per pass).
            already_marked = (
                session.query(EffectEvent.id)
                .filter_by(
                    effect_id=row.id,
                    reason="reconcile_inconclusive",
                    owner=provider.instance_id,
                )
                .first()
                is not None
            )
            if not already_marked:
                _event(
                    session,
                    row.id,
                    EffectStatus.UNKNOWN.value,
                    EffectStatus.UNKNOWN.value,
                    "reconcile_inconclusive",
                    owner=provider.instance_id,
                    moment=moment,
                )
            inconclusive += 1
    return success, failed, inconclusive


def mark_unknown_outcome(
    session: Session,
    effect_id: str,
    outcome: str,
    *,
    now: datetime,
    actor: str = "manual:test-only",
) -> EffectRequest:
    """Manually resolve an UNKNOWN row — **E1 SYNTHETIC TEST-ONLY** (labeled).

    The real operational reconciliation interface (admin permission, audit
    SLA, dual control) is owner decision D7 and is NOT implemented in E1.
    This function exists so tests can exercise the manual exit path; the
    ``outcome_source`` is permanently labeled ``manual:test-only``.
    """
    moment = as_utc(now)
    target = {
        "succeeded": EffectStatus.SUCCEEDED,
        "failed": EffectStatus.FAILED,
    }.get(str(outcome).lower())
    if target is None:
        raise ValueError("outcome must be 'succeeded' or 'failed'")
    row = session.get(EffectRequest, effect_id)
    if row is None:
        raise UnknownEffectError()
    rowcount = _cas(
        session,
        effect_id,
        EffectStatus.UNKNOWN,
        {
            "status": target,
            "final_outcome_at": moment,
            "outcome_source": "manual:test-only",
            "updated_at": moment,
        },
    )
    if rowcount != 1:
        from commons_core.api.errors import InvalidStateError

        raise InvalidStateError(
            f"Only UNKNOWN effects can be manually marked (current status={row.status!s})."
        )
    _event(
        session,
        effect_id,
        EffectStatus.UNKNOWN.value,
        target.value,
        "manual_mark",
        owner=actor,
        moment=moment,
    )
    session.flush()
    return row


# ---------------------------------------------------------------------------
# The orchestrating tick
# ---------------------------------------------------------------------------


def submit_claimed(
    session_factory: SessionFactory,
    provider: FakeEffectProvider,
    ref: ClaimedRef,
    *,
    now: datetime,
    cfg: Settings,
) -> str:
    """Full submit pipeline for one claimed row, over SHORT transactions.

    Transaction 1 — :func:`mark_submitted` (revalidation + SUBMITTED
    marker commit). Provider call — OUTSIDE any transaction. Transaction 2
    — :func:`apply_submit_outcome`. A crash between 1 and the provider call
    leaves an honestly-SUBMITTED row that traverses UNKNOWN →
    reconciliation ("definitively not applied" → FAILED); a crash after
    the provider call but before 2 reconciles to SUCCEEDED from the
    receipt ledger (B5'/B6'). A superseded epoch surfaces as
    ``"stale_rejected"`` (zero bytes written; audit event recorded).
    """
    try:
        with session_factory() as session:
            stage = mark_submitted(
                session, ref.effect_id, ref.held_epoch, ref.owner, now=now, cfg=cfg
            )
    except StaleDispatcherError:
        _record_stale_rejection(session_factory, ref)
        return "stale_rejected"
    if stage != "submitted":
        return stage  # cancelled / expired — ZERO outbound

    with session_factory() as session:
        row = session.get(EffectRequest, ref.effect_id)
        if row is None:  # pragma: no cover - defensive
            raise UnknownEffectError()
        spec = CAPABILITY_ALLOWLIST[row.capability]
    result = provider.submit(
        ref.effect_id,
        row.capability,
        row.operation,
        as_utc(now),
        idempotent=spec.idempotent,
        claim_epoch=ref.held_epoch,
        claim_owner=ref.owner,
    )
    try:
        with session_factory() as session:
            return apply_submit_outcome(
                session, ref.effect_id, result, held_epoch=ref.held_epoch, owner=ref.owner, now=now
            )
    except StaleDispatcherError:
        _record_stale_rejection(session_factory, ref)
        return "stale_rejected"


def _record_stale_rejection(session_factory: SessionFactory, ref: ClaimedRef) -> None:
    """Audit a superseded-dispatcher rejection (counts/ids only — zero row bytes)."""
    with session_factory() as session:
        session.add(
            SecurityEvent(
                category=SecurityEventCategory.POLICY_VIOLATION,
                principal=str(ref.owner)[:64],
                details={
                    "reason": "stale_dispatcher_rejected",
                    "effect_id": ref.effect_id,
                    "held_epoch": ref.held_epoch,
                },
            )
        )
        session.flush()


def dispatch_tick(
    session_factory: SessionFactory,
    provider: FakeEffectProvider,
    *,
    now: datetime,
    owner: str,
    settings: Settings | None = None,
    limit: int | None = None,
) -> DispatchTickResult:
    """One deterministic dispatcher iteration (short per-step transactions).

    Ordering: feature gate (freeze) → emergency-stop sweep → system reapers
    → claims → per-row submit pipeline → asynchronous acks → automatic
    reconciliation. Every count in the result is an exact transition count.
    """
    from commons_core.config.settings import get_settings

    cfg = settings or get_settings()
    moment = as_utc(now or utc_now())
    result = DispatchTickResult(now=moment, owner=owner)

    if not cfg.effects_enabled:
        # INV-EF-R11: scan NOTHING, cancel NOTHING — rows are frozen.
        result.frozen = True
        return result

    with session_factory() as session:
        if _stop_engaged(session):
            result.cancelled_stop_sweep = cancel_pending_effects(
                session, reason="emergency_stop", now=moment
            )
            return result
        result.requeued = reap_claim_deadlines(session, now=moment)
        result.expired_ttl = reap_authorization_ttl(session, now=moment, cfg=cfg)
        result.unknown_reaped = reap_ack_windows(session, now=moment)
        claimed = claim_eligible(session, now=moment, owner=owner, cfg=cfg, limit=limit)
    result.claimed = len(claimed)

    for ref in claimed:
        stage = submit_claimed(session_factory, provider, ref, now=moment, cfg=cfg)
        if stage == "cancelled":
            result.cancelled_on_submit += 1
        elif stage == "expired":
            result.expired_on_submit += 1
        elif stage == "stale_rejected":
            result.stale_rejected += 1
        else:
            # The marker committed and the provider was called exactly once.
            result.submitted += 1
            if stage == "acked_success":
                result.acked_success += 1
            elif stage in ("ack_failure", "failed"):
                result.acked_failure += 1
            elif stage == "ambiguous":
                result.ambiguous += 1
            elif stage == "pending":
                result.pending += 1
            elif stage == "idempotent_noop":
                result.idempotent_noop += 1

    for ack in provider.pump(moment):
        # S2: every pump ack carries its ORIGINATING attempt; each ack
        # commits in its own short transaction so one bad ack can never
        # poison the others. A stale originating epoch raises
        # StaleDispatcherError — the catch layer below re-records the
        # rejection durably (the same-session audit rolled back) and the
        # tick continues; a provenance-less ack never reaches here
        # (``DelayedAck`` always names its attempt).
        try:
            with session_factory() as ack_session:
                stage = apply_provider_ack(
                    ack_session,
                    ack.effect_id,
                    ack.outcome,
                    now=moment,
                    held_epoch=ack.claim_epoch,
                    owner=ack.claim_owner,
                )
        except StaleDispatcherError:
            _record_stale_rejection(
                session_factory,
                ClaimedRef(
                    effect_id=ack.effect_id,
                    held_epoch=ack.claim_epoch,
                    owner=ack.claim_owner,
                ),
            )
            result.stale_rejected += 1
            continue
        if stage == "acked_success":
            result.acked_success += 1
        elif stage in ("ack_failure", "failed"):
            result.acked_failure += 1
        elif stage == "idempotent_noop":
            result.idempotent_noop += 1
        elif stage == "ack_no_provenance":
            result.ack_no_provenance += 1

    with session_factory() as session:
        (
            result.reconciled_success,
            result.reconciled_failed,
            result.reconciled_inconclusive,
        ) = reconcile_unknown(session, provider, now=moment)
    return result

"""Deterministic in-process FAKE provider (E1 — the ONLY provider that exists).

HARD BOUNDARY: there is no transport in this interface at all. ``submit``
RECORDS a receipt in an in-memory ledger; it never opens a socket, never
performs HTTP, never touches the filesystem, never sends a message and
never contacts any real system. A "real adapter" cannot even express its
semantics through this interface — implementing it would mean lying in a
docstring, which code review catches, and E2's separately reviewed adapter
interface is the only place a transport will ever exist (currently absent
by design).

Receipt-ledger semantics (P0-C4 — simulated duplicate receipt is RECORDED
and EVALUATED, never hidden):

- every ``submit`` call appends a :class:`ProviderReceipt`
  ``(effect_id, attempt_seq, received_at, collapsed)``;
- for IDEMPOTENT capabilities (Class-1 style) a repeated ``effect_id``
  collapses into the existing application: the receipt is recorded with
  ``collapsed=True`` and the effect is applied ONCE;
- for NON-IDEMPOTENT capabilities (Class-4 style, ``fx.nonidempotent``)
  every receipt is a NEW application (``collapsed=False``) — duplicate
  delivery is a real duplicate, honestly counted.

Custody and provenance (S1/S2 remediation):

- every instance carries ``instance_id`` (unique identity) and
  ``instantiated_at`` (the custody anchor). An instance constructed
  AFTER a row's submission cannot attest that no OTHER instance ever
  received that row: its ``NEVER_RECEIVED`` is instance-local absence,
  NOT global non-execution evidence (:meth:`custody_covers` — the
  reconciliation side refuses to turn missing receipt history into a
  definite failure);
- every delayed acknowledgment is queued WITH the originating attempt's
  ``claim_epoch``/``claim_owner`` and :meth:`pump` delivers it as a
  :class:`DelayedAck` — provenance carried unchanged to ACK processing.
  The epoch is CAPTURED from the trusted dispatcher's held claim (never
  supplied by an external actor); an ack without provenance is rejected
  fail-closed downstream, never attributed to the current attempt.

Scenario scripting is TEST-ONLY state on the provider instance (a queue of
behaviors consumed by the next ``submit`` calls): definitive failure, lost
ack, delayed ack, timeout (never applies, never answers), ambiguous 401 —
plus delayed acks delivered deterministically through :meth:`pump` with the
injected clock. Operations describe WHAT to do; scenario scripting belongs
to the test harness, never to the durable descriptor.
"""

import enum
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from commons_core.infrastructure.time import as_utc, utc_now


class AckOutcome(enum.StrEnum):
    """Kind of an asynchronous acknowledgment delivered by :meth:`pump`."""

    SUCCESS = "success"
    FAILURE = "failure"


@dataclass(frozen=True)
class ProviderReceipt:
    """One recorded submission receipt (the honest remote-outcome ledger)."""

    effect_id: str
    attempt_seq: int
    received_at: datetime
    #: True when the provider's idempotency layer collapsed this receipt into
    #: an existing application (Class-1); False means a NEW application.
    collapsed: bool
    capability: str


class ProviderStatus(enum.StrEnum):
    """Provider-side status answer for reconciliation queries."""

    APPLIED = "applied"
    REJECTED = "rejected"
    NEVER_RECEIVED = "never_received"


class SubmitResult(enum.Enum):
    """Deterministic outcome of one synchronous ``submit`` call.

    - ``ACKED``    — the provider applied the effect and acknowledges now.
    - ``REJECTED`` — the provider DEFINITIVELY rejected the request (the
      effect was NOT applied); retry policy applies.
    - ``PENDING``  — the provider received the request; an asynchronous ack
      will (or will not — lost ack / timeout scripts) arrive via ``pump``.
      The row waits in SUBMITTED for its ack window.
    - ``AMBIGUOUS``— a semantically unclear non-2xx answer (e.g. 401):
      NOT proof of non-application → maps to UNKNOWN, never FAILED.
    """

    ACKED = "acked"
    REJECTED = "rejected"
    PENDING = "pending"
    AMBIGUOUS = "ambiguous"


#: Scriptable behaviors (TEST-ONLY provider state, consumed in order).
SCRIPT_FAIL = "fail"
SCRIPT_FAIL_ONCE = "fail_once"
SCRIPT_LOSE_ACK = "lose_ack"
SCRIPT_DELAY_ACK = "delay_ack"
SCRIPT_TIMEOUT = "timeout"
SCRIPT_AMBIGUOUS_401 = "ambiguous_401"


@dataclass
class _PendingAck:
    effect_id: str
    outcome: AckOutcome
    due_at: datetime
    #: Originating attempt provenance (S2) — captured from the trusted
    #: dispatcher's held claim at submission time, carried to ACK processing.
    claim_epoch: int
    claim_owner: str


@dataclass(frozen=True)
class DelayedAck:
    """One due asynchronous acknowledgment WITH its originating attempt.

    S2 invariant: a delayed ack stays bound to the claim epoch/owner of the
    submission attempt that produced it. ``claim_epoch`` is captured from
    the dispatcher's held claim (trusted internal state — in a future real
    provider design an epoch supplied by an untrusted external actor must
    NEVER be accepted as authority).
    """

    effect_id: str
    outcome: AckOutcome
    claim_epoch: int
    claim_owner: str


@dataclass
class FakeEffectProvider:
    """In-process deterministic ledger provider (fake-by-construction).

    State is bounded and synthetic: applied/rejected effects by id, the
    receipt ledger, a script queue and pending delayed acks. Nothing ever
    leaves the process — which is precisely why instance identity and
    the custody anchor exist: process-local evidence CANNOT survive a
    restart, and reconciliation must never read an empty fresh ledger as
    proof that nothing ever happened (S1).
    """

    #: effect_id -> {"status": ProviderStatus, "applied_at": datetime}
    _applications: dict[str, dict] = field(default_factory=dict)
    _receipts: list[ProviderReceipt] = field(default_factory=list)
    _next_seq: int = field(default=1)
    _scripts: deque = field(default_factory=deque)
    _pending_acks: list[_PendingAck] = field(default_factory=list)
    #: Unique instance identity (receipt-continuity marker, S1).
    instance_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    #: When this instance came alive — the custody anchor for
    #: ``NEVER_RECEIVED`` evidence (injectable for deterministic tests).
    instantiated_at: datetime = field(default_factory=utc_now)

    # ------------------------------------------------------------------
    # Test-side scripting (never persists, never reaches the descriptor)
    # ------------------------------------------------------------------
    def script_next(self, behavior: str, *, delay_seconds: int = 5) -> None:
        """Queue the next submit behavior(s) — TEST-ONLY harness control."""
        if behavior not in {
            SCRIPT_FAIL,
            SCRIPT_FAIL_ONCE,
            SCRIPT_LOSE_ACK,
            SCRIPT_DELAY_ACK,
            SCRIPT_TIMEOUT,
            SCRIPT_AMBIGUOUS_401,
        }:
            raise ValueError(f"unknown provider script: {behavior!r}")
        self._scripts.append((behavior, int(delay_seconds)))

    # ------------------------------------------------------------------
    # The provider interface used by the dispatcher
    # ------------------------------------------------------------------
    def submit(
        self,
        effect_id: str,
        capability: str,
        operation: dict,
        now: datetime,
        *,
        idempotent: bool,
        claim_epoch: int,
        claim_owner: str,
    ) -> SubmitResult:
        """Record a receipt and behave per script/capability — no transport.

        EVERY submission attempt is recorded in the receipt ledger FIRST
        (the honest duplicate-delivery metric — P0-C4), then the scripted or
        deterministic application semantics decide the synchronous answer.

        ``claim_epoch``/``claim_owner`` are the ORIGINATING attempt's
        provenance (S2): captured from the dispatcher's held claim by the
        trusted caller and preserved on every delayed acknowledgment. They
        are REQUIRED — an acknowledgment that cannot name its attempt is
        rejected downstream, never attributed to the current one.
        """
        now = as_utc(now)
        behavior = None
        if self._scripts:
            behavior, delay = self._scripts.popleft()
        else:
            delay = 5

        # Scripted outcomes that never apply anything (receipt recorded).
        if behavior == SCRIPT_FAIL:
            self._record_receipt(effect_id, capability, now, collapsed=False)
            self._applications[effect_id] = {
                "status": ProviderStatus.REJECTED,
                "applied_at": now,
            }
            return SubmitResult.REJECTED
        if behavior == SCRIPT_FAIL_ONCE:
            self._scripts.clear()  # "once" — subsequent submits behave normally
            self._record_receipt(effect_id, capability, now, collapsed=False)
            self._applications[effect_id] = {
                "status": ProviderStatus.REJECTED,
                "applied_at": now,
            }
            return SubmitResult.REJECTED
        if behavior == SCRIPT_TIMEOUT:
            # Received (receipt) but never applied, never answered.
            self._record_receipt(effect_id, capability, now, collapsed=False)
            return SubmitResult.PENDING
        if behavior == SCRIPT_AMBIGUOUS_401:
            # Received (receipt); semantically unclear non-2xx — outcome NOT
            # definitive (could mean "refused" OR "applied, answer lost").
            self._record_receipt(effect_id, capability, now, collapsed=False)
            return SubmitResult.AMBIGUOUS

        # Deterministic application path.
        already_applied = self._applications.get(effect_id)
        if already_applied is not None and already_applied["status"] == ProviderStatus.APPLIED:
            if idempotent:
                # Class-1 collapse: duplicate receipt, ONE application.
                self._record_receipt(effect_id, capability, now, collapsed=True)
                return SubmitResult.ACKED
            # Class-4: every receipt is a new application.
            self._record_receipt(effect_id, capability, now, collapsed=False)
            return SubmitResult.ACKED

        self._record_receipt(effect_id, capability, now, collapsed=False)
        self._applications[effect_id] = {"status": ProviderStatus.APPLIED, "applied_at": now}

        if behavior == SCRIPT_LOSE_ACK:
            # Applied, but the acknowledgment is lost forever: PENDING with
            # NO queued ack. The row will traverse UNKNOWN and reconcile to
            # SUCCEEDED via the status query (EFF-09).
            return SubmitResult.PENDING
        if behavior == SCRIPT_DELAY_ACK:
            # Applied; ack delivered via pump() once the clock passes the
            # delay (deterministic asynchronous acknowledgment) — WITH the
            # originating attempt's provenance (S2).
            self._pending_acks.append(
                _PendingAck(
                    effect_id=effect_id,
                    outcome=AckOutcome.SUCCESS,
                    due_at=now + timedelta(seconds=delay),
                    claim_epoch=int(claim_epoch),
                    claim_owner=str(claim_owner),
                )
            )
            return SubmitResult.PENDING
        return SubmitResult.ACKED

    def pump(self, now: datetime) -> list[DelayedAck]:
        """Deliver asynchronous acks due at ``now``, provenance intact (S2).

        Each delivered :class:`DelayedAck` carries the originating attempt's
        claim epoch/owner unchanged — ACK processing enforces it against the
        authoritative row state and rejects stale or provenance-less acks.
        """
        now = as_utc(now)
        due, remaining = [], []
        for ack in self._pending_acks:
            if as_utc(ack.due_at) <= now:
                due.append(
                    DelayedAck(
                        effect_id=ack.effect_id,
                        outcome=ack.outcome,
                        claim_epoch=ack.claim_epoch,
                        claim_owner=ack.claim_owner,
                    )
                )
            else:
                remaining.append(ack)
        self._pending_acks = remaining
        return due

    def query_status(self, effect_id: str) -> ProviderStatus:
        """Provider-side reconciliation answer (Class-1/3 capabilities only).

        HONEST scope: the answer is INSTANCE-LOCAL. ``NEVER_RECEIVED``
        means "no delivery ever reached THIS instance" — it is definitive
        non-execution evidence ONLY when this instance holds receipt
        custody over the row's submission (:meth:`custody_covers`); an
        instance constructed after a restart cannot speak for its
        predecessors (S1).
        """
        record = self._applications.get(effect_id)
        if record is None:
            return ProviderStatus.NEVER_RECEIVED
        return record["status"]

    def custody_covers(self, submitted_at: datetime | None) -> bool:
        """True when THIS instance was alive at/before the row's submission.

        Receipt custody (S1): an instance constructed after ``submitted_at``
        cannot attest that no OTHER instance received the row — its empty
        ledger is instance-local absence, not global non-execution
        evidence. ``submitted_at is None`` (defensive — UNKNOWN rows always
        carry a submission marker) fails closed: no custody, no definitive
        negative evidence.
        """
        if submitted_at is None:
            return False
        return as_utc(submitted_at) >= as_utc(self.instantiated_at)

    # ------------------------------------------------------------------
    # Ledger inspection (tests / evidence)
    # ------------------------------------------------------------------
    def receipts(self, effect_id: str | None = None) -> list[ProviderReceipt]:
        """All receipts (optionally filtered by effect id), in arrival order."""
        if effect_id is None:
            return list(self._receipts)
        return [r for r in self._receipts if r.effect_id == effect_id]

    def receipt_count(self, effect_id: str | None = None) -> int:
        """Exact receipt count — the honest duplicate-delivery metric."""
        return len(self.receipts(effect_id))

    def application_count(self, effect_id: str | None = None) -> int:
        """Distinct APPLIED applications (collapsed receipts count once)."""
        if effect_id is not None:
            record = self._applications.get(effect_id)
            return 1 if record and record["status"] == ProviderStatus.APPLIED else 0
        return sum(
            1
            for record in self._applications.values()
            if record["status"] == ProviderStatus.APPLIED
        )

    def _record_receipt(
        self, effect_id: str, capability: str, now: datetime, *, collapsed: bool
    ) -> None:
        self._receipts.append(
            ProviderReceipt(
                effect_id=effect_id,
                attempt_seq=self._next_seq,
                received_at=now,
                collapsed=collapsed,
                capability=capability,
            )
        )
        self._next_seq += 1

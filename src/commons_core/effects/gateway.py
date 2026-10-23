"""Effect-request authorization gateway (R02-B2 E1 — authorization skeleton).

THE NARROW INTERFACE (:func:`request_effect`) is the ONLY way an effect row
comes into existence. Default-deny ordering (fail-closed on the FIRST
failing gate, exactly like the task pipeline):

1. feature gate — ``AIC_EFFECTS_ENABLED=false`` (the default) → 403
   ``EFFECTS_DISABLED`` (INV-EF-R11: rows frozen, never cleared);
2. emergency stop engaged → 503 ``SERVICE_PAUSED`` (symmetric with task
   creation);
3. capability allowlist (code constants, ``fx.*`` only) → deterministic
   422 for everything else — REAL capabilities are structurally
   unrepresentable in E1 (prompt §6.3);
4. operation descriptor validation (P0-C2 closed schema, versioned,
   size-bounded, forbidden keys);
5. authoritative task row checks (gateway re-reads the row in ITS
   transaction — the handler asserts NOTHING): task ``running`` + lease
   not expired (409 family). The lease-generation snapshot recorded on
   the row is FENCING freshness, never caller identity (P0-C3);
6. authoritative agent row check: ACTIVE only (E1 synthetic revocation
   policy — labeled test-only; the real policy is owner decision D1);
7. bounded outbox (429 when the global non-terminal bound is hit);
8. approval binding for high-impact capabilities (digest-bound, single
   use, expiring — EFF-16);
9. application dedup ``(task_id, capability, resource_key)`` → 409 conflict
   (never silent re-targeting).

TRANSACTION SEMANTICS (Phase 0 §2.5 — deliberate, documented): the
authorization INSERT joins the CALLER's ambient transaction (the handler
runs inside ``execute_claimed``'s open transaction and holds no other
session). Effects are therefore atomic with the task attempt's outcome: a
handler failure rolls the row back (no orphan authorizations), a task
commit publishes the row. The gateway NEVER commits, NEVER opens a second
connection, and records denial events via ``session.add`` (NOT
``security.events.record_event``, which commits — that would leak a
partial task transaction; see Phase 0 §6.2).

P0-C3: the :class:`EffectExecutionContext` is a typed, frozen, in-process
companion constructed from authoritative row values inside the
authorization call — not a cryptographic token. It carries no authority
across calls, is never persisted and never logged. In-process isolation
against compromised handlers is explicitly OUT of scope for E1 (E2).
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session

from commons_core.config.settings import Settings
from commons_core.database.enums import AgentStatus, EffectStatus, SecurityEventCategory, TaskStatus
from commons_core.database.models import (
    Agent,
    EffectApproval,
    EffectEvent,
    EffectRequest,
    ScheduledTask,
    SecurityEvent,
    new_uuid,
)
from commons_core.effects.capabilities import CAPABILITY_ALLOWLIST, validate_operation
from commons_core.effects.errors import (
    DuplicateEffectError,
    EffectAgentNotActiveError,
    EffectApprovalInvalidError,
    EffectApprovalRequiredError,
    EffectNotActiveError,
    EffectQueueFullError,
    EffectSchemaError,
    EffectsDisabledError,
    EffectServicePausedError,
    StaleEffectLeaseError,
    UnknownEffectCapabilityError,
)
from commons_core.infrastructure.system_state import emergency_stop_active
from commons_core.infrastructure.time import as_utc, utc_now

#: Statuses that still occupy the bounded outbox (non-terminal).
IN_FLIGHT_EFFECT_STATUSES = (
    EffectStatus.AUTHORIZED,
    EffectStatus.CLAIMED,
    EffectStatus.SUBMITTED,
    EffectStatus.FAILED_RETRYABLE,
    EffectStatus.UNKNOWN,
)


@dataclass(frozen=True)
class EffectExecutionContext:
    """Typed trusted-context companion (P0-C3) — gateway-internal.

    Constructed ONLY by :func:`request_effect` from authoritative row
    values; carries no authority across calls; never persisted, never
    logged. The durable authority is the effect row itself; dispatch-time
    freshness is re-derived from the database (lease generation, agent
    state, stop flag), never from this object.
    """

    task_id: str
    agent_id: str
    claim_generation: int
    capability: str
    effect_id: str
    created_at: datetime


def verify_effects_schema(session: Session) -> None:
    """Fail-closed startup gate: the E1 outbox tables must exist (EFF-19b).

    An effects-aware dispatcher must NOT run against a 0002-only schema:
    every dispatcher-owned transition depends on the ``effect_requests``
    table and its epoch column. Mirrors ``verify_fencing_schema`` (OG-1
    precedent); raises :class:`EffectSchemaError` when the table (or its
    fencing column) is missing.
    """
    bind = session.get_bind()
    if bind is None:  # pragma: no cover - defensive
        raise EffectSchemaError("no database bind available for the effects schema check")
    inspector = sa_inspect(bind)
    if not inspector.has_table(EffectRequest.__tablename__):
        raise EffectSchemaError(
            "effect_requests table is missing — the database is below migration "
            "0003 while effects-aware code is starting; refusing to dispatch "
            "(fail-closed)."
        )
    columns = {column["name"] for column in inspector.get_columns(EffectRequest.__tablename__)}
    if "claim_epoch" not in columns:
        raise EffectSchemaError(
            "effect_requests.claim_epoch is missing — the schema predates the "
            "Phase 0 P0-C4 ownership model; refusing to dispatch (fail-closed)."
        )


def _deny(session: Session, agent_id: str, capability: str, reason: str) -> None:
    """Record a same-transaction authorization-denial event (counts/ids only).

    Uses ``session.add`` deliberately: ``security.events.record_event`` COMMITS
    the session, which would commit the caller's half-finished task
    transaction (Phase 0 §6.2). Same-transaction semantics mean a denial
    recorded during a task attempt that later rolls back disappears with
    it — the task's ``last_error`` still carries the denial class.
    """
    session.add(
        SecurityEvent(
            category=SecurityEventCategory.AUTHZ_DENIAL,
            principal=str(agent_id)[:64],
            details={"capability": str(capability)[:64], "reason": str(reason)[:64]},
        )
    )
    session.flush()


def request_effect(
    session: Session,
    task: ScheduledTask,
    capability: str,
    operation: object,
    *,
    now: datetime | None = None,
    settings: Settings | None = None,
    resource_key: str | None = None,
    approval_id: str | None = None,
) -> EffectRequest:
    """Authorize and durably enqueue ONE synthetic effect (the only entry point).

    Runs inside the CALLER's transaction (Phase 0 §2.5) — atomic with the
    task attempt's outcome; the caller commits. On success the row is
    ``AUTHORIZED`` (queued, unclaimed, zero outbound). On failure raises a
    deterministic :class:`~commons_core.api.errors.DomainError` and NOTHING
    is written (fail-closed); every security-relevant denial is recorded
    with counts/identifiers only.
    """
    from commons_core.config.settings import get_settings

    moment = as_utc(now or utc_now())
    cfg = settings or get_settings()

    # --- 1. feature gate (fail-closed default) ---------------------------
    if not cfg.effects_enabled:
        _deny(session, "anonymous", str(capability), "effects_disabled")
        raise EffectsDisabledError()

    # --- 2. emergency stop (service-pause symmetry) ----------------------
    if emergency_stop_active(session):
        _deny(session, "anonymous", str(capability), "emergency_stop")
        raise EffectServicePausedError()

    # --- 3. capability allowlist (code constants only) -------------------
    spec = CAPABILITY_ALLOWLIST.get(capability)
    if spec is None:
        _deny(session, "anonymous", str(capability), "unknown_capability")
        raise UnknownEffectCapabilityError(str(capability))

    # --- 4. operation descriptor (P0-C2 closed schema) -------------------
    normalized, digest = validate_operation(operation)
    if normalized["capability"] != capability:
        # The descriptor names a different capability than requested —
        # deterministic schema failure (never silently re-target).
        _deny(session, "anonymous", capability, "operation_capability_mismatch")
        raise UnknownEffectCapabilityError(str(capability))

    # --- 5. authoritative task row (gateway re-reads; caller asserts 0) ---
    db_task = session.get(ScheduledTask, task.id)
    if db_task is None:
        _deny(session, "anonymous", capability, "task_missing")
        raise EffectNotActiveError("missing")
    if db_task.status != TaskStatus.RUNNING:
        _deny(session, str(db_task.agent_id), capability, "task_not_running")
        raise EffectNotActiveError(str(db_task.status))
    lease_expires = db_task.lease_expires_at
    if lease_expires is not None and as_utc(lease_expires) <= moment:
        _deny(session, str(db_task.agent_id), capability, "lease_expired")
        raise StaleEffectLeaseError(db_task.id, lease_expires)

    # --- 6. authoritative agent row ---------------------------------------
    agent = session.get(Agent, db_task.agent_id)
    if agent is None or agent.status != AgentStatus.ACTIVE:
        status_value = str(agent.status) if agent is not None else "missing"
        _deny(session, str(db_task.agent_id), capability, "agent_not_active")
        raise EffectAgentNotActiveError(status_value)

    agent_id = db_task.agent_id
    claim_generation = int(db_task.lease_generation)

    # --- 7. bounded outbox -------------------------------------------------
    in_flight = session.scalar(
        select(func.count())
        .select_from(EffectRequest)
        .where(EffectRequest.status.in_(IN_FLIGHT_EFFECT_STATUSES))
    )
    if int(in_flight or 0) >= int(cfg.effects_max_in_flight):
        _deny(session, agent_id, capability, "queue_full")
        raise EffectQueueFullError(int(cfg.effects_max_in_flight))

    # --- 8. approval binding (high-impact synthetic policy) ---------------
    approval = None
    if spec.requires_approval:
        if approval_id is None:
            _deny(session, agent_id, capability, "approval_required")
            raise EffectApprovalRequiredError(capability)
        approval = session.get(EffectApproval, approval_id)
        if approval is None:
            _deny(session, agent_id, capability, "approval_unknown")
            raise EffectApprovalInvalidError("unknown approval")
        if approval.capability != capability:
            _deny(session, agent_id, capability, "approval_capability_mismatch")
            raise EffectApprovalInvalidError("approval capability mismatch")
        if approval.bound_request_digest != digest:
            _deny(session, agent_id, capability, "approval_digest_mismatch")
            raise EffectApprovalInvalidError("approval digest mismatch")
        if as_utc(approval.expires_at) <= moment:
            _deny(session, agent_id, capability, "approval_expired")
            raise EffectApprovalInvalidError("approval expired")
        if approval.used_at is not None:
            _deny(session, agent_id, capability, "approval_used")
            raise EffectApprovalInvalidError("approval already used")

    # --- 9. application dedup ----------------------------------------------
    if resource_key is not None:
        existing = session.scalar(
            select(EffectRequest.id).where(
                EffectRequest.task_id == db_task.id,
                EffectRequest.capability == capability,
                EffectRequest.resource_key == resource_key,
            )
        )
        if existing is not None:
            _deny(session, agent_id, capability, "duplicate_effect")
            raise DuplicateEffectError()

    effect_id = new_uuid()
    row = EffectRequest(
        id=effect_id,
        task_id=db_task.id,
        agent_id=agent_id,
        claim_generation=claim_generation,
        capability=capability,
        risk_level=spec.risk,
        status=EffectStatus.AUTHORIZED,
        operation=normalized,
        request_digest=digest,
        resource_key=resource_key,
        approval_id=approval.id if approval is not None else None,
        attempts=0,
        max_attempts=int(spec.max_attempts),
        created_at=moment,
        updated_at=moment,
    )
    session.add(row)
    # Surface IntegrityError (unique dedup) inside the caller's transaction.
    session.flush()

    # Single-use approval consumption — atomic with the INSERT (same
    # transaction): a failed attempt above never reached this point, so a
    # failed request NEVER consumes its approval (EFF-16c).
    if approval is not None:
        approval.used_at = moment

    session.add(
        EffectEvent(
            effect_id=effect_id,
            from_status=None,
            to_status=EffectStatus.AUTHORIZED.value,
            reason="authorized",
            claim_epoch=0,
            owner=None,
            occurred_at=moment,
        )
    )
    session.flush()

    # P0-C3 typed context — internal companion, not persisted.
    _context = EffectExecutionContext(
        task_id=db_task.id,
        agent_id=agent_id,
        claim_generation=claim_generation,
        capability=capability,
        effect_id=effect_id,
        created_at=moment,
    )
    return row

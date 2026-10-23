"""Error vocabulary for the E1 effect gateway (R02-B2).

Conventions mirror ``scheduler/service.py`` errors: ``DomainError`` subclasses
carry a deterministic ``status_code``/``code`` pair (the E1 gateway is
in-process — handlers and tests see the typed exceptions; the HTTP mapping
exists so a future routed surface inherits the same codes). INTERNAL fencing
errors (``StaleDispatcherError``/``EffectSchemaError``) are plain
``RuntimeError`` subclasses like their R02-B1 counterparts
(``LeaseSupersededError``-adjacent paths never surface over HTTP as-is).

PRIVACY: messages carry identifiers, statuses and counts only — never
operation parameters or content.
"""

from commons_core.api.errors import DomainError


class EffectsDisabledError(DomainError):
    """The synthetic effects subsystem is disabled (fail-closed, INV-EF-R11).

    ``AIC_EFFECTS_ENABLED=false`` (the default) makes every gateway request
    fail closed BEFORE any authorization evaluation: 403 with a deterministic
    code and a recorded denial event. The dispatcher scans nothing and
    existing rows are FROZEN, not cleared.
    """

    status_code = 403
    code = "EFFECTS_DISABLED"

    def __init__(self) -> None:
        super().__init__("The effects subsystem is disabled (AIC_EFFECTS_ENABLED=false).")


class EffectServicePausedError(DomainError):
    """Emergency stop engaged — new effect requests are refused (503, symmetric).

    Mirrors the task-create behavior while an emergency stop is engaged
    (``SERVICE_PAUSED``). Pre-submission rows are cancelled by the dispatcher
    sweep (reason ``emergency_stop``); submitted rows complete or reconcile
    honestly.
    """

    status_code = 503
    code = "SERVICE_PAUSED"

    def __init__(self) -> None:
        super().__init__("Emergency stop is engaged; effect requests are paused.")


class UnknownEffectCapabilityError(DomainError):
    """The requested capability is not in the synthetic allowlist (422).

    DETERMINISTIC REJECTION of everything outside the code-registered
    ``fx.*`` allowlist — including every real-world capability string
    (``http:post``, ``smtp:send``, ``file:write`` …). Real capabilities do
    not exist in E1, so this rejection is also the structural no-bypass
    gate: there is nothing to enable (prompt §6.3/§6.6).
    """

    status_code = 422
    code = "UNKNOWN_EFFECT_CAPABILITY"

    def __init__(self, capability: str) -> None:
        super().__init__(
            "Capability is not a registered synthetic effect.",
            capability=str(capability)[:64],
        )


class InvalidEffectOperationError(DomainError):
    """The synthetic operation descriptor failed schema validation (422).

    P0-C2: the operation is a strictly bounded, versioned, allowlisted
    descriptor. Violations: missing/unknown ``schema_version``, keys outside
    the capability's fixed schema, non-string parameters, oversize values
    or descriptor, nesting beyond the limit, forbidden key names
    (``content``/``secret``/``token``/``credentials`` at any depth).
    """

    status_code = 422
    code = "INVALID_EFFECT_OPERATION"

    def __init__(self, reason: str) -> None:
        super().__init__(
            "Synthetic operation descriptor is invalid.",
            reason=str(reason)[:128],
        )


class EffectAgentNotActiveError(DomainError):
    """The task-owning agent is not ACTIVE (403, synthetic revocation policy).

    E1 SYNTHETIC TEST-ONLY policy (labeled — the real revocation policy is
    owner decision D1 / RV2-06, which stays OPEN): a SUSPENDED/REVOKED agent
    gets no new effect authorizations. The agent id is the authoritative one
    from the task row — never caller-asserted.
    """

    status_code = 403
    code = "EFFECT_AGENT_NOT_ACTIVE"

    def __init__(self, agent_status: str) -> None:
        super().__init__(
            "Task-owning agent is not active; effect denied.",
            agent_status=str(agent_status)[:32],
        )


class EffectNotActiveError(DomainError):
    """The task is not in the executing state required for effects (409).

    Effects may only be authorized while the authoritative task row is
    ``running`` (the handler executes inside that transaction —
    ``service.execute_claimed`` T2). Terminal/pending/leased tasks imply no
    legitimate live handler context.
    """

    status_code = 409
    code = "EFFECT_TASK_NOT_ACTIVE"

    def __init__(self, task_status: str) -> None:
        super().__init__(
            "Effects may only be requested while the task is executing.",
            task_status=str(task_status)[:32],
        )


class StaleEffectLeaseError(DomainError):
    """The task lease is expired — the execution context is stale (409).

    Fencing axis (R02-B1 semantics reused verbatim): an expired
    ``lease_expires_at`` means the claiming context can no longer produce
    authorized effects. A matching CURRENT generation is freshness, NOT
    identity (design §4.2 / C4) — the gateway never treats it as
    sufficient on its own, and never accepts caller-asserted values.
    """

    status_code = 409
    code = "LEASE_SUPERSEDED"

    def __init__(self, task_id: str, expires_at: object) -> None:
        super().__init__(
            "Task lease expired before this effect request; rejected.",
            task_id=str(task_id),
            lease_expires_at=str(expires_at),
        )


class EffectApprovalRequiredError(DomainError):
    """High-impact synthetic capability requires an approval binding (403)."""

    status_code = 403
    code = "EFFECT_APPROVAL_REQUIRED"

    def __init__(self, capability: str) -> None:
        super().__init__(
            "This capability requires an approval binding.",
            capability=str(capability)[:64],
        )


class EffectApprovalInvalidError(DomainError):
    """The approval binding does not match this request (403, EFF-16).

    Fails when the approval is unknown, bound to another capability, has a
    ``bound_request_digest`` mismatch (replay against different content),
    is expired, or was already used. Failed attempts NEVER consume the
    approval (``used_at`` is set only on successful authorization).
    """

    status_code = 403
    code = "EFFECT_APPROVAL_INVALID"

    def __init__(self, reason: str) -> None:
        super().__init__("Approval binding is invalid for this request.", reason=str(reason)[:64])


class DuplicateEffectError(DomainError):
    """An equal (task, capability, resource_key) row already exists (409).

    Conflict-rejection semantics (EFF-06a, INV-EF-R1): the application-level
    dedup key refuses a second row rather than silently re-targeting the
    first (contrast with task ``idempotency_key``'s return-existing
    semantics — the difference is deliberate and documented).
    """

    status_code = 409
    code = "EFFECT_DUPLICATE"

    def __init__(self) -> None:
        super().__init__("An effect with this (task, capability, resource_key) already exists.")


class EffectQueueFullError(DomainError):
    """Global non-terminal effect bound exceeded (429, bounded outbox)."""

    status_code = 429
    code = "EFFECT_QUEUE_FULL"

    def __init__(self, limit: int) -> None:
        super().__init__("Effect queue is full; retry after in-flight effects drain.", limit=limit)


class EffectAlreadySubmittedError(DomainError):
    """Cancellation/revocation hit a row that is already submitted (409).

    The structural ban (P0-C1/INV-EF-R7): after ``SUBMITTED`` there is NO
    path to ``CANCELLED``. The response states the honest fact — the effect
    may already have been applied and cannot be retracted locally.
    """

    status_code = 409
    code = "EFFECT_ALREADY_SUBMITTED"

    def __init__(self) -> None:
        super().__init__(
            "Effect was already submitted; it cannot be cancelled locally "
            "(it may already have been applied)."
        )


class UnknownEffectError(DomainError):
    """Effect id does not exist (404)."""

    status_code = 404
    code = "UNKNOWN_EFFECT"

    def __init__(self) -> None:
        super().__init__("Effect not found.")


class StaleDispatcherError(RuntimeError):
    """A superseded dispatcher presented a stale claim epoch (zero bytes).

    INTERNAL (P0-C4, mirrors R02-B1 ``LeaseSupersededError``'s zero-write
    guarantee at the effect-row level): every dispatcher-owned write presents
    the epoch held at claim time; a conditional UPDATE that matches zero
    rows means a newer dispatcher owns the row. NOTHING is written — the
    stale dispatcher's observation is discarded (never overwrites the
    newer authoritative local result) and an audit event is recorded
    (counts only).
    """

    def __init__(self, effect_id: str, held_epoch: int) -> None:
        super().__init__(
            f"Dispatcher claim epoch is superseded (effect={effect_id}, "
            f"held_epoch={held_epoch}); the transition was rejected and "
            "nothing was written."
        )
        self.effect_id = effect_id
        self.held_epoch = int(held_epoch)


class EffectSchemaError(RuntimeError):
    """The effect outbox schema is missing — effects-aware code refuses to run.

    Fail-closed startup gate (EFF-19b, mirrors ``FencingSchemaError``):
    a post-0003 dispatcher must NOT operate against a 0002-only schema.
    See :func:`commons_core.effects.gateway.verify_effects_schema`.
    """

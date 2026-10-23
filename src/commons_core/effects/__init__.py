"""External effect gateway — R02-B2 E1 (FAKE-ONLY INFRASTRUCTURE).

Phase-0-gated durable transactional outbox + authorization skeleton +
trusted fake-only dispatcher for SYNTHETIC effects (design review
"Option D", first stage). Hard boundaries (Phase 0 §9, prompt §6):

- REAL EXTERNAL EFFECTS ARE PROHIBITED. The capability allowlist contains
  ONLY ``fx.*`` synthetic code-registered constants; the only provider in
  this codebase is :class:`~commons_core.effects.provider.FakeEffectProvider`
  (an in-process ledger — its interface has NO transport abstraction).
  No live credential, no URL, no SMTP, no outbound call exists anywhere in
  this package, and NO environment variable can change that.
- E1 is NOT an isolation boundary against compromised in-process handlers
  (prompt §6.1): it enforces the SEMANTIC gates (default-deny allowlist,
  lease freshness, agent state, epoch fencing, retry budgets) against
  cooperative and stale callers. Process isolation, credential vaulting
  and egress enforcement are deferred to E2.
- The existing scheduler task lifecycle (R02-A / R02-B1) is untouched:
  this package only READS ``scheduled_tasks.lease_generation`` and
  ``agents.status`` as authorization inputs.
"""

from commons_core.effects.capabilities import (
    CAPABILITY_ALLOWLIST,
    CapabilitySpec,
    canonical_operation_json,
    operation_digest,
    validate_operation,
)
from commons_core.effects.errors import (
    DuplicateEffectError,
    EffectAgentNotActiveError,
    EffectAlreadySubmittedError,
    EffectApprovalInvalidError,
    EffectApprovalRequiredError,
    EffectNotActiveError,
    EffectQueueFullError,
    EffectSchemaError,
    EffectsDisabledError,
    EffectServicePausedError,
    InvalidEffectOperationError,
    StaleDispatcherError,
    StaleEffectLeaseError,
    UnknownEffectCapabilityError,
    UnknownEffectError,
)
from commons_core.effects.gateway import (
    EffectExecutionContext,
    request_effect,
    verify_effects_schema,
)
from commons_core.effects.provider import (
    AckOutcome,
    DelayedAck,
    FakeEffectProvider,
    ProviderReceipt,
    ProviderStatus,
    SubmitResult,
)

__all__ = [
    "CAPABILITY_ALLOWLIST",
    "CapabilitySpec",
    "canonical_operation_json",
    "operation_digest",
    "validate_operation",
    "DuplicateEffectError",
    "EffectAgentNotActiveError",
    "EffectAlreadySubmittedError",
    "EffectApprovalInvalidError",
    "EffectApprovalRequiredError",
    "EffectNotActiveError",
    "EffectQueueFullError",
    "EffectSchemaError",
    "EffectsDisabledError",
    "InvalidEffectOperationError",
    "StaleDispatcherError",
    "StaleEffectLeaseError",
    "UnknownEffectCapabilityError",
    "UnknownEffectError",
    "EffectServicePausedError",
    "EffectExecutionContext",
    "request_effect",
    "verify_effects_schema",
    "AckOutcome",
    "DelayedAck",
    "FakeEffectProvider",
    "ProviderReceipt",
    "ProviderStatus",
    "SubmitResult",
]

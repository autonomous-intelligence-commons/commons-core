"""Synthetic capability allowlist and bounded operation descriptors (P0-C2).

E1 capability surface — CODE-REGISTERED CONSTANTS ONLY. There is no
registration API, no configuration path, no environment variable and no
import mechanism that can add a capability to this allowlist: enabling a
REAL capability requires a separately reviewed code change (E2), which is
exactly the no-bypass property required by the prompt §6.3/§6.6.

Every allowlisted capability is a SYNTHETIC fake-provider capability
(consumed only by ``FakeEffectProvider``) with:

- a fixed risk level (E1 has NO ``irreversible`` member);
- a closed parameter schema (exact keys, string types, length bounds);
- provider semantics flags (idempotent / queryable / auto-retry policy).

The durable operation descriptor (Phase 0 §3) is::

    {"schema_version": 1, "capability": "fx.echo", "parameters": {...}}

It is validated against the allowlist at AUTHORIZATION time and
RE-validated + digest-verified at DISPATCH time (restart reconstruction —
the dispatcher needs nothing beyond the durable row). The descriptor is
DATA: it is never executed, imported, or interpreted as commands, and the
closed schemas have no slot in which URLs, paths, credentials or any real
content could even be expressed.
"""

import hashlib
import json
from dataclasses import dataclass

from commons_core.effects.errors import InvalidEffectOperationError

#: The only accepted operation descriptor version. Unknown versions fail
#: closed (422) — a future schema change ships a new validator with it.
OPERATION_SCHEMA_VERSION = 1

#: Hard byte bound on the canonical descriptor JSON (P0-C2 size limit).
OPERATION_MAX_BYTES = 512

#: Maximum nesting depth of ``parameters``.
_OPERATION_MAX_DEPTH = 4

#: Parameter string length bound.
_OPERATION_MAX_STR_LEN = 256

#: Forbidden parameter key names at ANY depth (same policy vocabulary as
#: task payloads — ``scheduler.service.FORBIDDEN_PAYLOAD_KEYS``; duplicated
#: as a module constant so the effects package has no import cycle with
#: the scheduler service and the policy travels with the schema).
FORBIDDEN_OPERATION_KEYS = frozenset({"content", "secret", "token", "credentials"})


@dataclass(frozen=True)
class CapabilitySpec:
    """Static definition of one synthetic capability (code-registered)."""

    name: str
    risk: str
    #: exact key -> (required, max_length) — closed schema, string-only values.
    parameters: dict[str, tuple[bool, int]]
    #: Class-1 style: the provider collapses duplicate receipts of the same
    #: effect id into ONE application (vs Class-4: every receipt applies).
    idempotent: bool
    #: Provider supports status queries (reconciliation can close UNKNOWN).
    queryable: bool
    #: Policy: automatic retry allowed after definitive provider failure.
    auto_retry: bool
    #: High-impact capabilities require an approval binding (E1 test policy).
    requires_approval: bool
    #: Retry budget (0 = no automatic retries, INV-EF-R5).
    max_attempts: int


#: The COMPLETE E1 capability allowlist. Adding a real capability here is a
#: separately reviewed code change (E2) — nothing else can extend it.
CAPABILITY_ALLOWLIST: dict[str, CapabilitySpec] = {
    "fx.echo": CapabilitySpec(
        name="fx.echo",
        risk="low",
        parameters={"message": (True, 256)},
        idempotent=True,
        queryable=True,
        auto_retry=True,
        requires_approval=False,
        max_attempts=3,
    ),
    "fx.counter": CapabilitySpec(
        name="fx.counter",
        risk="low",
        parameters={"counter_key": (True, 64)},
        idempotent=True,
        queryable=True,
        auto_retry=True,
        requires_approval=False,
        max_attempts=3,
    ),
    "fx.ambiguous": CapabilitySpec(
        name="fx.ambiguous",
        risk="medium",
        parameters={"note": (False, 128)},
        idempotent=True,
        queryable=True,
        auto_retry=True,
        requires_approval=False,
        max_attempts=3,
    ),
    "fx.nonidempotent": CapabilitySpec(
        name="fx.nonidempotent",
        risk="medium",
        parameters={"note": (False, 128)},
        idempotent=False,
        queryable=False,
        auto_retry=False,  # Class-4 policy: automatic retry disabled (EFF-18)
        requires_approval=False,
        max_attempts=3,
    ),
    "fx.requires_approval": CapabilitySpec(
        name="fx.requires_approval",
        risk="high",
        parameters={"note": (False, 128)},
        idempotent=True,
        queryable=True,
        auto_retry=False,  # high-impact: no automatic retries (EFF-10)
        requires_approval=True,
        max_attempts=0,  # INV-EF-R5: high-impact rows carry budget 0
    ),
}


def canonical_operation_json(operation: dict) -> str:
    """The ONE canonical JSON form of an operation descriptor.

    Digests are computed over this exact representation (sorted keys, tight
    separators, non-ASCII preserved, UTF-8) and re-verified at dispatch
    time against the stored column — any post-authorization mutation of
    ``operation`` breaks the digest match and freezes the row (zero
    outbound).
    """
    return json.dumps(operation, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def operation_digest(operation: dict) -> str:
    """SHA-256 hex digest of the canonical JSON form (exact representation)."""
    return hashlib.sha256(canonical_operation_json(operation).encode("utf-8")).hexdigest()


def _validate_parameters(spec: CapabilitySpec, parameters: object) -> dict:
    """Validate ``parameters`` against the capability's closed schema."""
    if not isinstance(parameters, dict):
        raise InvalidEffectOperationError("parameters must be an object")
    allowed = spec.parameters
    for key, value in parameters.items():
        if not isinstance(key, str):
            raise InvalidEffectOperationError("parameter keys must be strings")
        if key not in allowed:
            raise InvalidEffectOperationError(f"parameter {key!r} is not in the schema")
        required, max_len = allowed[key]
        if not isinstance(value, str):
            raise InvalidEffectOperationError(f"parameter {key!r} must be a string")
        if len(value) > max_len:
            raise InvalidEffectOperationError(f"parameter {key!r} exceeds {max_len} characters")
        if required and not value:
            raise InvalidEffectOperationError(f"parameter {key!r} must be non-empty")
    for key, (required, _max_len) in allowed.items():
        if required and key not in parameters:
            raise InvalidEffectOperationError(f"required parameter {key!r} is missing")

    def _scan(node: object, depth: int) -> None:
        if depth > _OPERATION_MAX_DEPTH:
            raise InvalidEffectOperationError("parameters exceed the nesting limit")
        if isinstance(node, dict):
            for key, child in node.items():
                if isinstance(key, str) and key.lower() in FORBIDDEN_OPERATION_KEYS:
                    raise InvalidEffectOperationError(
                        f"forbidden parameter key {key!r} at any depth"
                    )
                _scan(child, depth + 1)
        elif isinstance(node, list):
            for child in node:
                _scan(child, depth + 1)

    # The closed schema only admits flat string values; the recursive scan is
    # defense in depth for future schema evolution (fail-closed stays cheap).
    _scan(parameters, 1)
    return dict(parameters)


def validate_operation(operation: object) -> tuple[dict, str]:
    """Validate a raw operation descriptor; return (normalized, digest).

    Fail-closed on every violation (InvalidEffectOperationError, 422):
    unknown/missing schema version, non-allowlisted capability, schema
    violations, oversize descriptors. Returns the normalized descriptor
    ``{"schema_version": 1, "capability": ..., "parameters": {...}}`` and
    its SHA-256 digest over the canonical form.
    """
    if not isinstance(operation, dict):
        raise InvalidEffectOperationError("operation must be an object")
    version = operation.get("schema_version")
    if version != OPERATION_SCHEMA_VERSION:
        raise InvalidEffectOperationError(
            f"unsupported schema_version (expected {OPERATION_SCHEMA_VERSION})"
        )
    capability = operation.get("capability")
    if not isinstance(capability, str) or capability not in CAPABILITY_ALLOWLIST:
        # Capability allowlist membership is a separate gateway gate; reaching
        # this branch means the descriptor is malformed rather than merely
        # unauthorized — still a deterministic schema failure.
        raise InvalidEffectOperationError("capability missing or not allowlisted")
    spec = CAPABILITY_ALLOWLIST[capability]
    parameters = _validate_parameters(spec, operation.get("parameters", {}))
    normalized = {
        "schema_version": OPERATION_SCHEMA_VERSION,
        "capability": capability,
        "parameters": parameters,
    }
    encoded = canonical_operation_json(normalized).encode("utf-8")
    if len(encoded) > OPERATION_MAX_BYTES:
        raise InvalidEffectOperationError(
            f"operation descriptor exceeds {OPERATION_MAX_BYTES} bytes"
        )
    return normalized, operation_digest(normalized)

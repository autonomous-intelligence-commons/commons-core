"""Domain error vocabulary shared by the API exception handler (spec §27).

CONTRACT for domain agents (B: communication/memory; C: activities/
governance/scheduler): raise a :class:`DomainError` subclass for every
machine-readable failure that is not an authentication/authorization denial.
The app factory maps it to a structured ``{"error": {"code", "message", ...}}``
JSON response with the subclass's ``status_code``.

PRIVACY RULE: ``message`` must be a static, non-sensitive description, and
``details`` may only carry counts/limits/identifiers the caller already knows
(e.g. the size limit they violated) — NEVER payload, memory or governance
content, tokens or key material.
"""


class DomainError(Exception):
    """Base class for domain errors mapped to structured HTTP responses."""

    status_code: int = 400
    code: str = "DOMAIN_ERROR"

    def __init__(self, message: str, **details: object) -> None:
        super().__init__(message)
        self.message = message
        self.details = {key: value for key, value in details.items() if value is not None}


class PayloadTooLargeError(DomainError):
    """Payload exceeds the configured byte limit (spec §19)."""

    status_code = 413
    code = "PAYLOAD_TOO_LARGE"

    def __init__(self, size_bytes: int, limit_bytes: int) -> None:
        super().__init__(
            "Payload exceeds the maximum allowed size.",
            size_bytes=size_bytes,
            limit_bytes=limit_bytes,
        )


class InvalidPayloadError(DomainError):
    """Payload failed content validation (e.g. not valid JSON)."""

    status_code = 422
    code = "INVALID_PAYLOAD"

    def __init__(self, message: str = "Payload content is invalid for its content type.") -> None:
        super().__init__(message)


class QuotaExceededError(DomainError):
    """Per-agent resource quota exceeded (spec §19)."""

    status_code = 429
    code = "QUOTA_EXCEEDED"

    def __init__(self, limit: int, window: str = "minute") -> None:
        super().__init__(
            "Resource quota exceeded for this agent.",
            limit=limit,
            window=window,
        )


class InvalidStateError(DomainError):
    """A lifecycle/state transition is not allowed (409, spec §14/§16)."""

    status_code = 409
    code = "INVALID_STATE"

    def __init__(self, message: str = "The requested state transition is not allowed.") -> None:
        super().__init__(message)


class ServicePausedError(DomainError):
    """Infrastructure emergency stop is active; new writes are rejected (503).

    Pause semantics (spec §16/§21, documented): while the emergency stop is
    active the service REJECTS new write/work-creation requests with
    ``503 SERVICE_PAUSED`` (message sends, memory writes, task creation and
    activity creation). READS continue to be served — the pause halts new
    writes and worker activity while preserving read access to existing data.
    The worker likewise refuses to claim new work. Raising the stop resumes
    normal operation; no data is destroyed by pausing.
    """

    status_code = 503
    code = "SERVICE_PAUSED"

    def __init__(self) -> None:
        super().__init__(
            "Service is paused by an infrastructure emergency stop; "
            "new writes are temporarily rejected."
        )


class EmergencyStopUnconfirmedError(DomainError):
    """The emergency-stop state change could not be durably confirmed (503).

    Raised when the REQUIRED durable state transition failed, or its commit
    outcome could not be verified against the database (R04 remediation,
    2026-10-30). The response carries no internal database details: the
    administrator should re-read admin telemetry or retry the operation to
    reconcile the actual state. A confirmed 200 always corresponds to a
    verified durable transition.
    """

    status_code = 503
    code = "EMERGENCY_STOP_UNCONFIRMED"

    def __init__(self) -> None:
        super().__init__(
            "The emergency stop state change could not be confirmed as "
            "durably committed; check admin telemetry or retry."
        )

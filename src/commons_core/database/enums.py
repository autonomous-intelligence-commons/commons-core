"""Enumerated vocabularies stored as portable VARCHAR values.

These enums are the shared vocabulary between the database models, the
authorization engine and the API layer. ``str_enum`` builds a SQLAlchemy
``Enum`` type that persists the enum *values* (lowercase, e.g. ``"active"``)
as plain VARCHAR — compatible with SQLite locally and PostgreSQL in
production (``native_enum=False``).
"""

import enum

import sqlalchemy as sa


def _enum_values(enum_cls: type[enum.StrEnum]) -> list[str]:
    return [member.value for member in enum_cls]


def str_enum(enum_cls: type[enum.StrEnum], **kwargs: object) -> sa.Enum:
    """Build a portable SQLAlchemy Enum column type for a StrEnum class."""
    return sa.Enum(enum_cls, native_enum=False, values_callable=_enum_values, **kwargs)  # type: ignore[arg-type]


class AgentStatus(enum.StrEnum):
    """Lifecycle status of an agent identity."""

    ACTIVE = "active"
    SUSPENDED = "suspended"
    REVOKED = "revoked"


class CredentialStatus(enum.StrEnum):
    """Status of an agent credential."""

    ACTIVE = "active"
    REVOKED = "revoked"


class ChannelKind(enum.StrEnum):
    """Kind of communication channel."""

    DIRECT = "direct"
    GROUP = "group"


class ActivityStatus(enum.StrEnum):
    """Lifecycle status of an agent-created activity."""

    PROPOSED = "proposed"
    ACTIVE = "active"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class ActivityMemberState(enum.StrEnum):
    """Participation state of an activity member."""

    INVITED = "invited"
    JOINED = "joined"
    DECLINED = "declined"


class GovernanceKind(enum.StrEnum):
    """Kind of internal governance record."""

    PROPOSAL = "proposal"
    CONVENTION = "convention"
    AMENDMENT = "amendment"


class GovernanceStatus(enum.StrEnum):
    """Status of a governance record."""

    DRAFT = "draft"
    OPEN = "open"
    ACCEPTED = "accepted"
    SUPERSEDED = "superseded"


class TaskStatus(enum.StrEnum):
    """Lifecycle status of a scheduled task."""

    PENDING = "pending"
    LEASED = "leased"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SecurityEventCategory(enum.StrEnum):
    """Categories of privacy-preserving security events (spec §20).

    Details attached to events carry counts/limits only — never message,
    memory or governance content.
    """

    AUTH_FAILURE = "auth_failure"
    AUTHZ_DENIAL = "authz_denial"
    RATE_LIMITED = "rate_limited"
    QUOTA_EXCEEDED = "quota_exceeded"
    POLICY_VIOLATION = "policy_violation"
    EMERGENCY_STOP = "emergency_stop"
    CONTAINMENT = "containment"


class EffectStatus(enum.StrEnum):
    """Lifecycle status of a durable external-effect request (R02-B2 E1).

    Phase 0 (P0-C1) corrected lifecycle — the dispatcher claim, the submission
    intent and the provider outcome are DISTINCT durable facts:

    - ``AUTHORIZED``  — authorized and durably queued; NOT claimed.
    - ``CLAIMED``      — a dispatcher holds an atomic claim (``claim_epoch``);
                        NOTHING has been submitted (zero remote meaning).
    - ``SUBMITTED``    — the submission marker is committed (the point of no
                        return for cancellation); a LOCAL intent record, NOT
                        proof of provider receipt.
    - ``SUCCEEDED`` / ``FAILED`` / ``FAILED_RETRYABLE`` — provider-definitive.
    - ``UNKNOWN``      — submitted, outcome not established; NEVER implicitly
                        ``FAILED`` ("no local record" is not "did not happen").
    - ``CANCELLED``    — cancelled BEFORE submission (reachable only from
                        AUTHORIZED/CLAIMED with ``submitted_at IS NULL``).
    - ``EXPIRED``      — lease superseded / TTL expired / integrity freeze,
                        always before submission; terminal.

    Structural bans: no path leads SUBMITTED→CANCELLED; UNKNOWN leaves only
    via reconciliation or (test-only, labeled) manual marking; terminal
    states never flip. Compensation states are deferred to E2.
    """

    AUTHORIZED = "authorized"
    CLAIMED = "claimed"
    SUBMITTED = "submitted"
    SUCCEEDED = "succeeded"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED = "failed"
    UNKNOWN = "unknown"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class EffectRiskLevel(enum.StrEnum):
    """Risk classification of an effect capability (R02-B2 design §4.7).

    E1 registers ONLY synthetic fake-provider capabilities; ``irreversible``
    has NO members in the E1 allowlist (structurally impossible).
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    IRREVERSIBLE = "irreversible"

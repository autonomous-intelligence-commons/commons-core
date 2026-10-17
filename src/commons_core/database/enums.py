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

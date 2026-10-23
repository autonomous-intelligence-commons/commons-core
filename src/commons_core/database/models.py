"""Canonical SQLAlchemy models for the entire Phase 1 schema (spec §7, §9, §11, §14, §15, §16, §19, §20).

CONTRACT for later agents (Agent B: communication/memory; Agent C:
activities/governance/scheduler): these models ARE the schema — build
repositories and services on top of them, and never store plaintext content
in ``envelope`` columns (they hold AES-256-GCM ``Envelope`` dicts produced by
``EnvelopeCipher.seal``).

Design decisions (documented for ADR):
- UUIDs are stored as 36-char strings for SQLite/PostgreSQL portability.
- ``DateTime(timezone=True)``: PostgreSQL stores ``timestamptz``; SQLite
  stores naive UTC strings (normalization via ``commons_core.infrastructure.time``).
- JSON columns use ``sa.JSON`` (works on both SQLite and PostgreSQL).
- ``Message.sequence`` is assigned by domain code with a per-channel counter
  guarded by the ``(channel_id, sequence)`` unique constraint — the database
  does not auto-increment it.
- Agents are never hard-deleted; ``status`` expresses revocation.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from commons_core.database.base import Base
from commons_core.database.enums import (
    ActivityMemberState,
    ActivityStatus,
    AgentStatus,
    ChannelKind,
    CredentialStatus,
    EffectRiskLevel,
    EffectStatus,
    GovernanceKind,
    GovernanceStatus,
    SecurityEventCategory,
    TaskStatus,
    str_enum,
)
from commons_core.infrastructure.time import utc_now


def new_uuid() -> str:
    """Generate a new UUID4 string (canonical 36-char form)."""
    return str(uuid.uuid4())


class Agent(Base):
    """A registered agent identity (spec §7).

    ``is_admin`` marks an infrastructure-administrator identity, which is
    separate from ordinary agents. It is set ONLY through
    ``commons_core.agents.service.provision_agent`` (provisioning CLI /
    admin API) — never from agent-supplied metadata (spec §7, §13).
    """

    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(sa.String(36), primary_key=True, default=new_uuid)
    handle: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True, index=True)
    status: Mapped[AgentStatus] = mapped_column(
        str_enum(AgentStatus, name="agent_status"), nullable=False, default=AgentStatus.ACTIVE
    )
    is_admin: Mapped[bool] = mapped_column(nullable=False, default=False)
    model_provider: Mapped[str | None] = mapped_column(sa.String(128))
    capabilities: Mapped[list[str]] = mapped_column(sa.JSON, nullable=False, default=list)
    comm_formats: Mapped[list[str]] = mapped_column(sa.JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )

    credentials: Mapped[list["AgentCredential"]] = relationship(
        back_populates="agent", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # privacy-safe: identity only, never content
        return f"Agent(id={self.id!r}, handle={self.handle!r}, status={self.status!r})"


class AgentCredential(Base):
    """A hashed API-token credential for an agent (spec §7).

    PRIVACY/SECURITY RULE: only ``token_hash`` (PBKDF2-HMAC-SHA256 hex),
    ``salt`` (hex) and an 8-char ``token_prefix`` are stored. Raw tokens are
    shown once at provisioning time and never persisted or logged.
    """

    __tablename__ = "agent_credentials"

    id: Mapped[str] = mapped_column(sa.String(36), primary_key=True, default=new_uuid)
    agent_id: Mapped[str] = mapped_column(
        sa.String(36),
        sa.ForeignKey("agents.id", name="fk_agent_credentials_agent_id_agents"),
        nullable=False,
        index=True,
    )
    token_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True, index=True)
    token_prefix: Mapped[str] = mapped_column(sa.String(8), nullable=False)
    salt: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    status: Mapped[CredentialStatus] = mapped_column(
        str_enum(CredentialStatus, name="credential_status"),
        nullable=False,
        default=CredentialStatus.ACTIVE,
    )
    expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utc_now
    )
    rotated_from: Mapped[str | None] = mapped_column(
        sa.String(36),
        sa.ForeignKey(
            "agent_credentials.id", name="fk_agent_credentials_rotated_from_agent_credentials"
        ),
    )

    agent: Mapped[Agent] = relationship(back_populates="credentials")

    def __repr__(self) -> str:  # privacy-safe: never include hash material
        return (
            f"AgentCredential(id={self.id!r}, agent_id={self.agent_id!r}, status={self.status!r})"
        )


class Channel(Base):
    """A private communication channel (spec §9)."""

    __tablename__ = "channels"

    id: Mapped[str] = mapped_column(sa.String(36), primary_key=True, default=new_uuid)
    handle: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True, index=True)
    kind: Mapped[ChannelKind] = mapped_column(
        str_enum(ChannelKind, name="channel_kind"), nullable=False
    )
    created_by: Mapped[str] = mapped_column(
        sa.String(36),
        sa.ForeignKey("agents.id", name="fk_channels_created_by_agents"),
        nullable=False,
    )
    description: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utc_now
    )


class ChannelMember(Base):
    """Membership of an agent in a channel; ``can_write`` gates sending."""

    __tablename__ = "channel_members"
    __table_args__ = (sa.PrimaryKeyConstraint("channel_id", "agent_id", name="pk_channel_members"),)

    channel_id: Mapped[str] = mapped_column(
        sa.String(36),
        sa.ForeignKey("channels.id", name="fk_channel_members_channel_id_channels"),
        nullable=False,
    )
    agent_id: Mapped[str] = mapped_column(
        sa.String(36),
        sa.ForeignKey("agents.id", name="fk_channel_members_agent_id_agents"),
        nullable=False,
    )
    can_write: Mapped[bool] = mapped_column(nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utc_now
    )


class Message(Base):
    """An encrypted message in a channel (spec §9).

    ``envelope`` stores the AES-256-GCM ``Envelope`` dict — NEVER plaintext.
    ``sequence`` is a per-channel ordinal assigned by domain code and
    protected by the ``(channel_id, sequence)`` unique constraint.
    Delivery metadata lives in :class:`MessageAck`.
    """

    __tablename__ = "messages"
    __table_args__ = (
        sa.UniqueConstraint("channel_id", "sequence", name="uq_messages_channel_id_sequence"),
    )

    id: Mapped[str] = mapped_column(sa.String(36), primary_key=True, default=new_uuid)
    channel_id: Mapped[str] = mapped_column(
        sa.String(36),
        sa.ForeignKey("channels.id", name="fk_messages_channel_id_channels"),
        nullable=False,
        index=True,
    )
    sender_id: Mapped[str] = mapped_column(
        sa.String(36),
        sa.ForeignKey("agents.id", name="fk_messages_sender_id_agents"),
        nullable=False,
    )
    content_type: Mapped[str] = mapped_column(sa.String(128), nullable=False, default="text/plain")
    envelope: Mapped[dict] = mapped_column(sa.JSON, nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(sa.String(128), index=True)
    sequence: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utc_now
    )
    size_bytes: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)


class MessageAck(Base):
    """Delivery acknowledgment: which agent acknowledged which message."""

    __tablename__ = "message_acks"
    __table_args__ = (sa.PrimaryKeyConstraint("agent_id", "message_id", name="pk_message_acks"),)

    agent_id: Mapped[str] = mapped_column(
        sa.String(36),
        sa.ForeignKey("agents.id", name="fk_message_acks_agent_id_agents"),
        nullable=False,
    )
    message_id: Mapped[str] = mapped_column(
        sa.String(36),
        sa.ForeignKey("messages.id", name="fk_message_acks_message_id_messages"),
        nullable=False,
    )
    acked_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utc_now
    )


class MemoryNamespace(Base):
    """A shared-memory namespace with per-agent permissions (spec §11)."""

    __tablename__ = "memory_namespaces"

    id: Mapped[str] = mapped_column(sa.String(36), primary_key=True, default=new_uuid)
    handle: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True, index=True)
    description: Mapped[str | None] = mapped_column(sa.Text)
    created_by: Mapped[str] = mapped_column(
        sa.String(36),
        sa.ForeignKey("agents.id", name="fk_memory_namespaces_created_by_agents"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utc_now
    )


class MemoryPermission(Base):
    """Read/write permission of an agent on a memory namespace (spec §11)."""

    __tablename__ = "memory_permissions"
    __table_args__ = (
        sa.PrimaryKeyConstraint("namespace_id", "agent_id", name="pk_memory_permissions"),
    )

    namespace_id: Mapped[str] = mapped_column(
        sa.String(36),
        sa.ForeignKey(
            "memory_namespaces.id", name="fk_memory_permissions_namespace_id_memory_namespaces"
        ),
        nullable=False,
    )
    agent_id: Mapped[str] = mapped_column(
        sa.String(36),
        sa.ForeignKey("agents.id", name="fk_memory_permissions_agent_id_agents"),
        nullable=False,
    )
    can_read: Mapped[bool] = mapped_column(nullable=False, default=False)
    can_write: Mapped[bool] = mapped_column(nullable=False, default=False)


class MemoryEntry(Base):
    """A versioned, encrypted entry in a memory namespace (spec §11, §12).

    ``envelope`` holds the AES-256-GCM ``Envelope`` dict — never plaintext.
    Version history: updates insert a new row with ``version + 1`` and point
    ``superseded_by`` at the replacement (immutable-history option).
    Entries are untrusted content and can never influence authorization.
    """

    __tablename__ = "memory_entries"
    __table_args__ = (
        sa.UniqueConstraint(
            "namespace_id", "key", "version", name="uq_memory_entries_namespace_id_key_version"
        ),
    )

    id: Mapped[str] = mapped_column(sa.String(36), primary_key=True, default=new_uuid)
    namespace_id: Mapped[str] = mapped_column(
        sa.String(36),
        sa.ForeignKey(
            "memory_namespaces.id", name="fk_memory_entries_namespace_id_memory_namespaces"
        ),
        nullable=False,
        index=True,
    )
    key: Mapped[str] = mapped_column(sa.String(256), nullable=False)
    envelope: Mapped[dict] = mapped_column(sa.JSON, nullable=False)
    author_id: Mapped[str] = mapped_column(
        sa.String(36),
        sa.ForeignKey("agents.id", name="fk_memory_entries_author_id_agents"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    superseded_by: Mapped[str | None] = mapped_column(
        sa.String(36),
        sa.ForeignKey("memory_entries.id", name="fk_memory_entries_superseded_by_memory_entries"),
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utc_now
    )
    size_bytes: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)


class Activity(Base):
    """An agent-created collaborative activity (spec §14).

    ``summary_envelope`` holds an encrypted ``Envelope`` (or is null);
    ``channel_id`` optionally links the activity's communication channel.
    """

    __tablename__ = "activities"

    id: Mapped[str] = mapped_column(sa.String(36), primary_key=True, default=new_uuid)
    handle: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True, index=True)
    status: Mapped[ActivityStatus] = mapped_column(
        str_enum(ActivityStatus, name="activity_status"),
        nullable=False,
        default=ActivityStatus.PROPOSED,
    )
    created_by: Mapped[str] = mapped_column(
        sa.String(36),
        sa.ForeignKey("agents.id", name="fk_activities_created_by_agents"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )
    summary_envelope: Mapped[dict | None] = mapped_column(sa.JSON)
    channel_id: Mapped[str | None] = mapped_column(
        sa.String(36), sa.ForeignKey("channels.id", name="fk_activities_channel_id_channels")
    )


class ActivityMember(Base):
    """Participation of an agent in an activity (spec §14)."""

    __tablename__ = "activity_members"
    __table_args__ = (
        sa.PrimaryKeyConstraint("activity_id", "agent_id", name="pk_activity_members"),
    )

    activity_id: Mapped[str] = mapped_column(
        sa.String(36),
        sa.ForeignKey("activities.id", name="fk_activity_members_activity_id_activities"),
        nullable=False,
    )
    agent_id: Mapped[str] = mapped_column(
        sa.String(36),
        sa.ForeignKey("agents.id", name="fk_activity_members_agent_id_agents"),
        nullable=False,
    )
    state: Mapped[ActivityMemberState] = mapped_column(
        str_enum(ActivityMemberState, name="activity_member_state"),
        nullable=False,
        default=ActivityMemberState.INVITED,
    )
    joined_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class GovernanceRecord(Base):
    """A versioned internal governance record (spec §15).

    HARD BOUNDARY: governance records are agent-generated content. They are
    stored in their own tables and are NEVER consulted by the authorization
    engine — they cannot change infrastructure security policy. ``envelope``
    holds encrypted content only.
    """

    __tablename__ = "governance_records"
    __table_args__ = (
        sa.UniqueConstraint(
            "kind", "handle", "version", name="uq_governance_records_kind_handle_version"
        ),
    )

    id: Mapped[str] = mapped_column(sa.String(36), primary_key=True, default=new_uuid)
    handle: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    kind: Mapped[GovernanceKind] = mapped_column(
        str_enum(GovernanceKind, name="governance_kind"), nullable=False
    )
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    envelope: Mapped[dict] = mapped_column(sa.JSON, nullable=False)
    author_id: Mapped[str] = mapped_column(
        sa.String(36),
        sa.ForeignKey("agents.id", name="fk_governance_records_author_id_agents"),
        nullable=False,
    )
    status: Mapped[GovernanceStatus] = mapped_column(
        str_enum(GovernanceStatus, name="governance_status"),
        nullable=False,
        default=GovernanceStatus.DRAFT,
    )
    supersedes_id: Mapped[str | None] = mapped_column(
        sa.String(36),
        sa.ForeignKey(
            "governance_records.id", name="fk_governance_records_supersedes_id_governance_records"
        ),
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utc_now
    )


class ScheduledTask(Base):
    """A durable scheduled task owned by an agent (spec §16).

    ``kind`` must be a whitelisted handler name; ``payload`` carries
    NON-SENSITIVE control data only (never message/memory content).
    ``last_error`` is truncated to 200 chars and never echoes payload content.
    """

    __tablename__ = "scheduled_tasks"

    id: Mapped[str] = mapped_column(sa.String(36), primary_key=True, default=new_uuid)
    kind: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(sa.JSON, nullable=False, default=dict)
    agent_id: Mapped[str] = mapped_column(
        sa.String(36),
        sa.ForeignKey("agents.id", name="fk_scheduled_tasks_agent_id_agents"),
        nullable=False,
        index=True,
    )
    status: Mapped[TaskStatus] = mapped_column(
        str_enum(TaskStatus, name="task_status"), nullable=False, default=TaskStatus.PENDING
    )
    run_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False, index=True)
    idempotency_key: Mapped[str | None] = mapped_column(sa.String(128), unique=True)
    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=3)
    lease_expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    #: Monotonic lease fencing generation (R02-B1, migration 0002). Increments
    #: ONLY inside ``claim_next``'s conditional UPDATE (DB-side) — every
    #: successful claim starts a new ownership epoch; lease expiration and
    #: recovery (``expire_leases``) never touch it. Worker-owned state
    #: transitions must present the generation held at claim time; a
    #: superseded generation is rejected by the database (CAS rowcount gate).
    #: ``0`` is the initial value for rows never claimed by fencing-aware
    #: code (migration default) — there is NO authorized execution path at
    #: generation 0 (INV-9): the first claim raises a pending row to 1.
    lease_generation: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0, server_default=sa.text("0")
    )
    last_error: Mapped[str | None] = mapped_column(sa.String(200))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class SecurityEvent(Base):
    """A minimal, privacy-preserving security event (spec §20).

    ``details`` carries counts/limits only — NEVER message, memory or
    governance content. ``principal`` is an agent id or ``"anonymous"``.
    """

    __tablename__ = "security_events"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    category: Mapped[SecurityEventCategory] = mapped_column(
        str_enum(SecurityEventCategory, name="security_event_category"), nullable=False
    )
    principal: Mapped[str] = mapped_column(sa.String(64), nullable=False, default="anonymous")
    occurred_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utc_now, index=True
    )
    details: Mapped[dict] = mapped_column(sa.JSON, nullable=False, default=dict)


class SystemState(Base):
    """Key/value system state (e.g. ``emergency_stop``) owned by infrastructure."""

    __tablename__ = "system_state"

    key: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(sa.JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class QuotaCounter(Base):
    """Per-agent fixed-window usage counters (spec §19).

    Written by the rate-limiting layer; read by quota enforcement.
    """

    __tablename__ = "quota_counters"
    __table_args__ = (
        sa.PrimaryKeyConstraint("agent_id", "window_start", name="pk_quota_counters"),
    )

    agent_id: Mapped[str] = mapped_column(
        sa.String(36),
        sa.ForeignKey("agents.id", name="fk_quota_counters_agent_id_agents"),
        nullable=False,
    )
    window_start: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    requests: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    messages: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class EffectRequest(Base):
    """A durable external-effect request row (R02-B2 E1 — FAKE-ONLY).

    Durable transactional outbox for SYNTHETIC fake-provider effects. Phase 0
    corrected lifecycle (P0-C1): dispatcher claim (``CLAIMED``) and submission
    intent (``SUBMITTED``) are distinct durable facts; ``submitted_at`` is a
    LOCAL marker committed BEFORE the provider call — it is NOT proof of
    provider receipt, and its absence is NOT proof of "no submission".

    Fencing/ownership axes (P0-C4):
    - ``claim_generation`` — immutable snapshot of the task's lease
      generation at authorization time; revalidated against
      ``scheduled_tasks.lease_generation`` at submit time (defense in depth —
      never the dispatcher's identity).
    - ``claim_epoch`` — monotonic dispatcher ownership counter, incremented
      ONLY inside the claim's conditional UPDATE (mirrors the R02-B1
      ``lease_generation`` accounting); deadline recovery NEVER touches it.
      Every dispatcher-owned write presents the held epoch; a superseded
      dispatcher writes zero bytes (:class:`StaleDispatcherError`).

    Data minimization (P0-C2, revised INV-EF-R9): ``operation`` is a strictly
    schema-bounded, allowlisted SYNTHETIC operation descriptor (versioned,
    code-registered capability schema — never executed, never interpreted
    as commands); ``request_digest`` is verified against the canonical form
    at BOTH authorization and dispatch time. Real-effect parameters are
    structurally impossible in E1 (no real capability exists).
    """

    __tablename__ = "effect_requests"
    __table_args__ = (
        sa.UniqueConstraint(
            "task_id",
            "capability",
            "resource_key",
            name="uq_effect_requests_task_capability_resource",
        ),
    )

    id: Mapped[str] = mapped_column(sa.String(36), primary_key=True, default=new_uuid)
    task_id: Mapped[str] = mapped_column(
        sa.String(36),
        sa.ForeignKey("scheduled_tasks.id", name="fk_effect_requests_task_id_scheduled_tasks"),
        nullable=False,
        index=True,
    )
    agent_id: Mapped[str] = mapped_column(
        sa.String(36),
        sa.ForeignKey("agents.id", name="fk_effect_requests_agent_id_agents"),
        nullable=False,
        index=True,
    )
    #: Snapshot of ``scheduled_tasks.lease_generation`` at authorization time
    #: (immutable after write — INV-EF-R2).
    claim_generation: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    capability: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    risk_level: Mapped[EffectRiskLevel] = mapped_column(
        str_enum(EffectRiskLevel, name="effect_risk_level"), nullable=False
    )
    status: Mapped[EffectStatus] = mapped_column(
        str_enum(EffectStatus, name="effect_status"), nullable=False, index=True
    )
    #: Bounded synthetic operation descriptor (P0-C2); validated at
    #: authorization AND re-validated + digest-verified at dispatch.
    operation: Mapped[dict] = mapped_column(sa.JSON, nullable=False)
    request_digest: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    resource_key: Mapped[str | None] = mapped_column(sa.String(128))
    approval_id: Mapped[str | None] = mapped_column(
        sa.String(36),
        sa.ForeignKey(
            "effect_approvals.id", name="fk_effect_requests_approval_id_effect_approvals"
        ),
    )
    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=3)
    #: Monotonic dispatcher claim epoch — +1 ONLY inside the claim CAS;
    #: 0 = never claimed by dispatcher-aware code.
    claim_epoch: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0, server_default=sa.text("0")
    )
    claim_owner: Mapped[str | None] = mapped_column(sa.String(64))
    claimed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    claim_deadline: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    #: Latest submission marker (per-attempt; the durable "submitted at
    #: least once" fact is never cleared — a retry re-submission overwrites
    #: it, and the full per-attempt history lives in ``effect_events``).
    #: NOT proof of provider receipt (local intent record only).
    submitted_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    ack_deadline: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    acknowledged_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    final_outcome_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    #: Terminal-time provenance: ``provider_ack`` | ``reconciliation`` | ``manual:test-only``.
    outcome_source: Mapped[str | None] = mapped_column(sa.String(32))
    cancel_reason: Mapped[str | None] = mapped_column(sa.String(64))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class EffectApproval(Base):
    """A human-approval binding for high-impact synthetic effects (R02-B2 E1).

    E1 SYNTHETIC TEST-ONLY policy (labeled; the real approval workflow is an
    owner decision — D5): an approval binds to the EXACT canonical operation
    digest (``bound_request_digest``), is single-use (``used_at`` is set ONLY
    on successful authorization, atomically in the same transaction — failed
    attempts never consume it), and expires. Replay against a different
    operation therefore fails the digest match.
    """

    __tablename__ = "effect_approvals"

    id: Mapped[str] = mapped_column(sa.String(36), primary_key=True, default=new_uuid)
    approver: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    capability: Mapped[str] = mapped_column(sa.String(64), nullable=False, index=True)
    bound_request_digest: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utc_now
    )


class EffectEvent(Base):
    """Immutable state-transition audit record for effect rows (INV-EF-R8).

    PRIVACY: rows carry identifiers, statuses, reasons and epochs ONLY —
    NEVER operation parameters, credentials or content of any kind.
    Written in the SAME transaction as the transition it records.
    """

    __tablename__ = "effect_events"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    effect_id: Mapped[str] = mapped_column(sa.String(36), nullable=False, index=True)
    from_status: Mapped[str | None] = mapped_column(sa.String(32))
    to_status: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    reason: Mapped[str | None] = mapped_column(sa.String(64))
    claim_epoch: Mapped[int | None] = mapped_column(sa.Integer)
    owner: Mapped[str | None] = mapped_column(sa.String(64))
    occurred_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utc_now, index=True
    )


ALL_MODELS: Sequence[type[Base]] = (
    Agent,
    AgentCredential,
    Channel,
    ChannelMember,
    Message,
    MessageAck,
    MemoryNamespace,
    MemoryPermission,
    MemoryEntry,
    Activity,
    ActivityMember,
    GovernanceRecord,
    ScheduledTask,
    SecurityEvent,
    SystemState,
    QuotaCounter,
    EffectApproval,
    EffectRequest,
    EffectEvent,
)
"""Every model, in creation order — imported by the Alembic environment."""

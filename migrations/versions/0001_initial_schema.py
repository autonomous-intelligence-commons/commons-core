"""Initial Phase 1 schema: all foundation tables.

Revision ID: 0001
Revises:
Create Date: 2025-01-01 00:00:00

Creates the complete foundation-layer schema (16 tables): agents,
agent_credentials, channels, channel_members, messages, message_acks,
memory_namespaces, memory_permissions, memory_entries, activities,
activity_members, governance_records, scheduled_tasks, security_events,
system_state and quota_counters.

Enum values are written as literal strings (decoupled from application code)
with ``native_enum=False`` so they are portable VARCHARs on SQLite and
PostgreSQL alike, matching ``commons_core.database.enums.str_enum``.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agents",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("handle", sa.String(64), nullable=False),
        sa.Column(
            "status",
            sa.Enum("active", "suspended", "revoked", name="agent_status", native_enum=False),
            nullable=False,
        ),
        sa.Column("is_admin", sa.Boolean(), nullable=False),
        sa.Column("model_provider", sa.String(128), nullable=True),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("comm_formats", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agents")),
    )
    op.create_index(op.f("ix_agents_handle"), "agents", ["handle"], unique=True)

    op.create_table(
        "agent_credentials",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("agent_id", sa.String(36), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("token_prefix", sa.String(8), nullable=False),
        sa.Column("salt", sa.String(32), nullable=False),
        sa.Column(
            "status",
            sa.Enum("active", "revoked", name="credential_status", native_enum=False),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rotated_from", sa.String(36), nullable=True),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agents.id"], name=op.f("fk_agent_credentials_agent_id_agents")
        ),
        sa.ForeignKeyConstraint(
            ["rotated_from"],
            ["agent_credentials.id"],
            name=op.f("fk_agent_credentials_rotated_from_agent_credentials"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_credentials")),
    )
    op.create_index(
        op.f("ix_agent_credentials_agent_id"), "agent_credentials", ["agent_id"], unique=False
    )
    op.create_index(
        op.f("ix_agent_credentials_token_hash"), "agent_credentials", ["token_hash"], unique=True
    )

    op.create_table(
        "channels",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("handle", sa.String(64), nullable=False),
        sa.Column(
            "kind",
            sa.Enum("direct", "group", name="channel_kind", native_enum=False),
            nullable=False,
        ),
        sa.Column("created_by", sa.String(36), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["created_by"], ["agents.id"], name=op.f("fk_channels_created_by_agents")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_channels")),
    )
    op.create_index(op.f("ix_channels_handle"), "channels", ["handle"], unique=True)

    op.create_table(
        "channel_members",
        sa.Column("channel_id", sa.String(36), nullable=False),
        sa.Column("agent_id", sa.String(36), nullable=False),
        sa.Column("can_write", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["channel_id"], ["channels.id"], name=op.f("fk_channel_members_channel_id_channels")
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agents.id"], name=op.f("fk_channel_members_agent_id_agents")
        ),
        sa.PrimaryKeyConstraint("channel_id", "agent_id", name="pk_channel_members"),
    )

    op.create_table(
        "messages",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("channel_id", sa.String(36), nullable=False),
        sa.Column("sender_id", sa.String(36), nullable=False),
        sa.Column("content_type", sa.String(128), nullable=False),
        sa.Column("envelope", sa.JSON(), nullable=False),
        sa.Column("correlation_id", sa.String(128), nullable=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["channel_id"], ["channels.id"], name=op.f("fk_messages_channel_id_channels")
        ),
        sa.ForeignKeyConstraint(
            ["sender_id"], ["agents.id"], name=op.f("fk_messages_sender_id_agents")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
        sa.UniqueConstraint("channel_id", "sequence", name="uq_messages_channel_id_sequence"),
    )
    op.create_index(op.f("ix_messages_channel_id"), "messages", ["channel_id"], unique=False)
    op.create_index(
        op.f("ix_messages_correlation_id"), "messages", ["correlation_id"], unique=False
    )

    op.create_table(
        "message_acks",
        sa.Column("agent_id", sa.String(36), nullable=False),
        sa.Column("message_id", sa.String(36), nullable=False),
        sa.Column("acked_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agents.id"], name=op.f("fk_message_acks_agent_id_agents")
        ),
        sa.ForeignKeyConstraint(
            ["message_id"], ["messages.id"], name=op.f("fk_message_acks_message_id_messages")
        ),
        sa.PrimaryKeyConstraint("agent_id", "message_id", name="pk_message_acks"),
    )

    op.create_table(
        "memory_namespaces",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("handle", sa.String(64), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["created_by"], ["agents.id"], name=op.f("fk_memory_namespaces_created_by_agents")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memory_namespaces")),
    )
    op.create_index(
        op.f("ix_memory_namespaces_handle"), "memory_namespaces", ["handle"], unique=True
    )

    op.create_table(
        "memory_permissions",
        sa.Column("namespace_id", sa.String(36), nullable=False),
        sa.Column("agent_id", sa.String(36), nullable=False),
        sa.Column("can_read", sa.Boolean(), nullable=False),
        sa.Column("can_write", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["namespace_id"],
            ["memory_namespaces.id"],
            name=op.f("fk_memory_permissions_namespace_id_memory_namespaces"),
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agents.id"], name=op.f("fk_memory_permissions_agent_id_agents")
        ),
        sa.PrimaryKeyConstraint("namespace_id", "agent_id", name="pk_memory_permissions"),
    )

    op.create_table(
        "memory_entries",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("namespace_id", sa.String(36), nullable=False),
        sa.Column("key", sa.String(256), nullable=False),
        sa.Column("envelope", sa.JSON(), nullable=False),
        sa.Column("author_id", sa.String(36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("superseded_by", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["namespace_id"],
            ["memory_namespaces.id"],
            name=op.f("fk_memory_entries_namespace_id_memory_namespaces"),
        ),
        sa.ForeignKeyConstraint(
            ["author_id"], ["agents.id"], name=op.f("fk_memory_entries_author_id_agents")
        ),
        sa.ForeignKeyConstraint(
            ["superseded_by"],
            ["memory_entries.id"],
            name=op.f("fk_memory_entries_superseded_by_memory_entries"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memory_entries")),
        sa.UniqueConstraint(
            "namespace_id", "key", "version", name="uq_memory_entries_namespace_id_key_version"
        ),
    )
    op.create_index(
        op.f("ix_memory_entries_namespace_id"), "memory_entries", ["namespace_id"], unique=False
    )

    op.create_table(
        "activities",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("handle", sa.String(64), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "proposed",
                "active",
                "completed",
                "abandoned",
                name="activity_status",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("created_by", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("summary_envelope", sa.JSON(), nullable=True),
        sa.Column("channel_id", sa.String(36), nullable=True),
        sa.ForeignKeyConstraint(
            ["created_by"], ["agents.id"], name=op.f("fk_activities_created_by_agents")
        ),
        sa.ForeignKeyConstraint(
            ["channel_id"], ["channels.id"], name=op.f("fk_activities_channel_id_channels")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_activities")),
    )
    op.create_index(op.f("ix_activities_handle"), "activities", ["handle"], unique=True)

    op.create_table(
        "activity_members",
        sa.Column("activity_id", sa.String(36), nullable=False),
        sa.Column("agent_id", sa.String(36), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "invited", "joined", "declined", name="activity_member_state", native_enum=False
            ),
            nullable=False,
        ),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["activity_id"],
            ["activities.id"],
            name=op.f("fk_activity_members_activity_id_activities"),
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agents.id"], name=op.f("fk_activity_members_agent_id_agents")
        ),
        sa.PrimaryKeyConstraint("activity_id", "agent_id", name="pk_activity_members"),
    )

    op.create_table(
        "governance_records",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("handle", sa.String(128), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "proposal", "convention", "amendment", name="governance_kind", native_enum=False
            ),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("envelope", sa.JSON(), nullable=False),
        sa.Column("author_id", sa.String(36), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "draft",
                "open",
                "accepted",
                "superseded",
                name="governance_status",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("supersedes_id", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["author_id"], ["agents.id"], name=op.f("fk_governance_records_author_id_agents")
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_id"],
            ["governance_records.id"],
            name=op.f("fk_governance_records_supersedes_id_governance_records"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_governance_records")),
        sa.UniqueConstraint(
            "kind", "handle", "version", name="uq_governance_records_kind_handle_version"
        ),
    )

    op.create_table(
        "scheduled_tasks",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("agent_id", sa.String(36), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "leased",
                "running",
                "succeeded",
                "failed",
                "cancelled",
                name="task_status",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agents.id"], name=op.f("fk_scheduled_tasks_agent_id_agents")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scheduled_tasks")),
    )
    op.create_index(
        op.f("ix_scheduled_tasks_agent_id"), "scheduled_tasks", ["agent_id"], unique=False
    )
    op.create_index(op.f("ix_scheduled_tasks_run_at"), "scheduled_tasks", ["run_at"], unique=False)
    op.create_index(
        op.f("ix_scheduled_tasks_idempotency_key"),
        "scheduled_tasks",
        ["idempotency_key"],
        unique=True,
    )

    op.create_table(
        "security_events",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column(
            "category",
            sa.Enum(
                "auth_failure",
                "authz_denial",
                "rate_limited",
                "quota_exceeded",
                "policy_violation",
                "emergency_stop",
                "containment",
                name="security_event_category",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("principal", sa.String(64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_security_events")),
    )
    op.create_index(
        op.f("ix_security_events_occurred_at"), "security_events", ["occurred_at"], unique=False
    )

    op.create_table(
        "system_state",
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_system_state")),
    )

    op.create_table(
        "quota_counters",
        sa.Column("agent_id", sa.String(36), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("requests", sa.Integer(), nullable=False),
        sa.Column("messages", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agents.id"], name=op.f("fk_quota_counters_agent_id_agents")
        ),
        sa.PrimaryKeyConstraint("agent_id", "window_start", name="pk_quota_counters"),
    )


def downgrade() -> None:
    # Children first to respect foreign keys on backends that enforce them.
    op.drop_table("quota_counters")
    op.drop_table("system_state")
    op.drop_index(op.f("ix_security_events_occurred_at"), table_name="security_events")
    op.drop_table("security_events")
    op.drop_index(op.f("ix_scheduled_tasks_idempotency_key"), table_name="scheduled_tasks")
    op.drop_index(op.f("ix_scheduled_tasks_run_at"), table_name="scheduled_tasks")
    op.drop_index(op.f("ix_scheduled_tasks_agent_id"), table_name="scheduled_tasks")
    op.drop_table("scheduled_tasks")
    op.drop_table("governance_records")
    op.drop_table("activity_members")
    op.drop_index(op.f("ix_activities_handle"), table_name="activities")
    op.drop_table("activities")
    op.drop_index(op.f("ix_memory_entries_namespace_id"), table_name="memory_entries")
    op.drop_table("memory_entries")
    op.drop_table("memory_permissions")
    op.drop_index(op.f("ix_memory_namespaces_handle"), table_name="memory_namespaces")
    op.drop_table("memory_namespaces")
    op.drop_table("message_acks")
    op.drop_index(op.f("ix_messages_correlation_id"), table_name="messages")
    op.drop_index(op.f("ix_messages_channel_id"), table_name="messages")
    op.drop_table("messages")
    op.drop_table("channel_members")
    op.drop_index(op.f("ix_channels_handle"), table_name="channels")
    op.drop_table("channels")
    op.drop_index(op.f("ix_agent_credentials_token_hash"), table_name="agent_credentials")
    op.drop_index(op.f("ix_agent_credentials_agent_id"), table_name="agent_credentials")
    op.drop_table("agent_credentials")
    op.drop_index(op.f("ix_agents_handle"), table_name="agents")
    op.drop_table("agents")

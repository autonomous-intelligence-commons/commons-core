"""Create the external-effect outbox tables (R02-B2 E1 — FAKE-ONLY).

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-22

E1 durable transactional outbox for SYNTHETIC fake-provider effects
(design review Option D / E1, corrected by Phase 0 P0-C1..C4):

- ``effect_requests`` — the outbox row and its corrected 9-state lifecycle
  (AUTHORIZED → CLAIMED → SUBMITTED → SUCCEEDED/FAILED_RETRYABLE/FAILED/
  UNKNOWN, plus CANCELLED/EXPIRED terminal states). Carries the immutable
  lease-generation snapshot (fencing axis), the monotonic dispatcher
  ``claim_epoch`` (ownership axis, mirrors R02-B1 accounting), the bounded
  synthetic operation descriptor + request digest (P0-C2), and the
  application-level dedup key ``(task_id, capability, resource_key)``.
- ``effect_approvals`` — digest-bound, single-use, expiring approval
  records for high-impact SYNTHETIC capabilities (E1 test-only policy;
  the real approval workflow remains an owner decision — D5).
- ``effect_events`` — same-transaction state-transition audit records
  (identifiers/statuses/epochs only — never parameters).

Backwards compatibility (mixed-version coexistence, mirroring 0002):
pre-0003 binaries never import ``commons_core.effects``, so an upgraded
schema is inert to them — the task axis is untouched.

DOWNGRADE DOCTRINE (fail-closed, EFF-19c): the effect system rolls back by
FREEZING, not by dropping data. ``downgrade()`` REFUSES to run while
``effect_requests`` or ``effect_events`` contain rows (an empty outbox is
the only safe precondition); with empty tables the three tables are
dropped. ``verify_effects_schema`` makes a post-0003 binary refuse to
dispatch against a 0002-only schema (fail-closed startup gate, EFF-19b).
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "effect_approvals",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("approver", sa.String(length=64), nullable=False),
        sa.Column("capability", sa.String(length=64), nullable=False),
        sa.Column("bound_request_digest", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_effect_approvals"),
    )
    op.create_index("ix_effect_approvals_capability", "effect_approvals", ["capability"])

    op.create_table(
        "effect_requests",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("agent_id", sa.String(length=36), nullable=False),
        sa.Column("claim_generation", sa.Integer(), nullable=False),
        sa.Column("capability", sa.String(length=64), nullable=False),
        sa.Column("risk_level", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("operation", sa.JSON(), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("resource_key", sa.String(length=128), nullable=True),
        sa.Column("approval_id", sa.String(length=36), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("claim_epoch", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("claim_owner", sa.String(length=64), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ack_deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("final_outcome_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome_source", sa.String(length=32), nullable=True),
        sa.Column("cancel_reason", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_effect_requests"),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["scheduled_tasks.id"],
            name="fk_effect_requests_task_id_scheduled_tasks",
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agents.id"], name="fk_effect_requests_agent_id_agents"
        ),
        sa.ForeignKeyConstraint(
            ["approval_id"],
            ["effect_approvals.id"],
            name="fk_effect_requests_approval_id_effect_approvals",
        ),
        sa.UniqueConstraint(
            "task_id",
            "capability",
            "resource_key",
            name="uq_effect_requests_task_capability_resource",
        ),
        sa.CheckConstraint(
            "risk_level IN ('low','medium','high','irreversible')",
            name="ck_effect_requests_risk_level",
        ),
        sa.CheckConstraint(
            "status IN ('authorized','claimed','submitted','succeeded',"
            "'failed_retryable','failed','unknown','cancelled','expired')",
            name="ck_effect_requests_status",
        ),
    )
    op.create_index("ix_effect_requests_task_id", "effect_requests", ["task_id"])
    op.create_index("ix_effect_requests_agent_id", "effect_requests", ["agent_id"])
    op.create_index("ix_effect_requests_status", "effect_requests", ["status"])

    op.create_table(
        "effect_events",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
        ),
        sa.Column("effect_id", sa.String(length=36), nullable=False),
        sa.Column("from_status", sa.String(length=32), nullable=True),
        sa.Column("to_status", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=True),
        sa.Column("claim_epoch", sa.Integer(), nullable=True),
        sa.Column("owner", sa.String(length=64), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_effect_events"),
    )
    op.create_index("ix_effect_events_effect_id", "effect_events", ["effect_id"])
    op.create_index("ix_effect_events_occurred_at", "effect_events", ["occurred_at"])


def downgrade() -> None:
    # Fail-closed downgrade doctrine (EFF-19c): rolling the SCHEMA back while
    # effect rows exist would silently discard durable outbox state and audit
    # history. The ONLY safe precondition is an EMPTY effect system.
    connection = op.get_bind()
    requests_count = connection.execute(sa.text("SELECT COUNT(*) FROM effect_requests")).scalar()
    events_count = connection.execute(sa.text("SELECT COUNT(*) FROM effect_events")).scalar()
    if int(requests_count or 0) > 0 or int(events_count or 0) > 0:
        raise RuntimeError(
            "REFUSING to downgrade migration 0003: effect_requests/effect_events "
            "are non-empty. The effect system rolls back by FREEZING (disable "
            "AIC_EFFECTS_ENABLED and let rows drain or be reconciled) — dropping "
            "the schema with live rows would destroy durable outbox state and "
            "audit history. Re-run the downgrade only after the effect tables "
            "are verified empty."
        )
    op.drop_index("ix_effect_events_occurred_at", table_name="effect_events")
    op.drop_index("ix_effect_events_effect_id", table_name="effect_events")
    op.drop_table("effect_events")
    op.drop_index("ix_effect_requests_status", table_name="effect_requests")
    op.drop_index("ix_effect_requests_agent_id", table_name="effect_requests")
    op.drop_index("ix_effect_requests_task_id", table_name="effect_requests")
    op.drop_table("effect_requests")
    op.drop_index("ix_effect_approvals_capability", table_name="effect_approvals")
    op.drop_table("effect_approvals")

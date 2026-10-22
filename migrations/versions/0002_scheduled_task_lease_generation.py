"""Add monotonic lease fencing generation to scheduled_tasks (R02-B1).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-22

R02-B Phase-1 database-state fencing: ``scheduled_tasks.lease_generation`` is
a monotonic ownership epoch counter. It increments ONLY inside
``claim_next``'s conditional UPDATE (database-side, never a Python
read-modify-write); worker-owned state transitions must present the
generation held at claim time and are rejected by the database when
superseded (CAS rowcount gate → ``LeaseSupersededError``).

Migration semantics (design review §5.1 rev, C3):

- ``ADD COLUMN lease_generation INTEGER NOT NULL DEFAULT 0`` — existing rows
  are classified, not backfilled: pending rows claim 0→1 on first contact;
  terminal rows are inert; leased rows are drained via the deployment gates
  (OG-2) and recovered by ``expire_leases`` (generation unchanged), then
  claimed 0→1. There is NO authorized execution path at generation 0
  (INV-9) — fencing-aware workers always operate at generation >= 1.
- The server-side DEFAULT 0 keeps legacy (pre-0002) code writing against an
  upgraded schema safe: its ORM does not map the column, so INSERT/UPDATE
  statements simply leave it at the default (idempotent coexistence).
- Downgrade drops the column; SQLite requires batch mode (``render_as_batch``
  is enabled in ``migrations/env.py`` and used here explicitly for the drop).
  NOTE (design §5.6 rev, C5): reverting the SCHEMA is not a safety-preserving
  rollback by itself — running unfenced worker binaries reopens the original
  R02-B vulnerability. Rollback doctrine lives in the deployment gates
  (OG-8/DG-8), not in this migration.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

_TABLE = "scheduled_tasks"
_COLUMN = "lease_generation"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(_COLUMN, sa.Integer(), nullable=False, server_default=sa.text("0")),
    )


def downgrade() -> None:
    # SQLite cannot drop columns with a plain ALTER — batch mode recreates
    # the table (env.py render_as_batch=True; explicit here for clarity).
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_column(_COLUMN)

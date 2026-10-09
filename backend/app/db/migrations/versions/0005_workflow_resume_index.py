"""phase 5 schema: an index for finding the workflow that waits for a refund

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-10

Hand-written and hand-reviewed. Additive only: one partial expression index, no table or column
change and no enum, so there is no CHECK to review.

The resume consumer receives `refund.*` events and must find the chat turn that is waiting for that
refund: `status = 'waiting_approval' AND context ->> 'refund_id' = :id`. The checkpoint's
`refund_id` is written by the backend (never by the model). The index is partial, so it holds only
the few turns that are waiting, however many turns have ever run.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | None = None
depends_on: str | None = None

INDEX = "ix_workflow_runs_waiting_refund"


def upgrade() -> None:
    op.create_index(
        INDEX,
        "workflow_runs",
        [sa.text("(context ->> 'refund_id')")],
        postgresql_where=sa.text("status = 'waiting_approval'"),
    )


def downgrade() -> None:
    op.drop_index(INDEX, table_name="workflow_runs")

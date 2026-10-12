"""phase 6 schema: the refund status `expired` (nobody decided in time)

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-12

Hand-written and hand-reviewed (autogenerate does not see CHECK changes). The only change is the
`refunds.status` CHECK: it is dropped and recreated under the SAME name (`ck_refunds_refund_status`)
with `expired` added, so the enum still has exactly ONE named CHECK. `approvals.status` already
allowed `expired` since 0001; nothing else changes.

An expired refund never moved money. It is not in `reserving_statuses` or `velocity_counted_statuses`
of rules.yaml (those are inclusion lists), so expiring a refund releases the money it reserved
against the order and its slot in the customer's velocity count, with no change to the policy engine.

Downgrade: an `expired` refund cannot exist under the old CHECK, so such rows are mapped to
`rejected` (the closest old status: it also never paid and also releases the reservation) before the
old CHECK is restored. The downgrade loses the distinction, not any money.
"""

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | None = None
depends_on: str | None = None

NEW_REFUND_STATUSES = (
    "'pending_approval', 'processing', 'succeeded', 'failed', 'rejected', 'expired'"
)
OLD_REFUND_STATUSES = "'pending_approval', 'processing', 'succeeded', 'failed', 'rejected'"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_refunds_refund_status"), "refunds", type_="check")
    op.create_check_constraint(
        op.f("ck_refunds_refund_status"), "refunds", f"status IN ({NEW_REFUND_STATUSES})"
    )


def downgrade() -> None:
    op.execute("UPDATE refunds SET status = 'rejected' WHERE status = 'expired'")
    op.drop_constraint(op.f("ck_refunds_refund_status"), "refunds", type_="check")
    op.create_check_constraint(
        op.f("ck_refunds_refund_status"), "refunds", f"status IN ({OLD_REFUND_STATUSES})"
    )

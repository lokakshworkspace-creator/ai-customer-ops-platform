"""phase 2 schema: per-user refund idempotency, outbox ordering/parking, tool-call logging

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-07

Hand-written and hand-reviewed (autogenerate does not see CHECK changes and duplicates enum
CHECKs): every enum column ends up with exactly ONE named CHECK. Backfills run before
constraints tighten, so this is safe on a non-empty database.

Changes
- refunds:   UNIQUE(idempotency_key) -> UNIQUE(requested_by, idempotency_key); request_hash,
             reason_code; status values pending -> processing.
- approvals: refund_id FK + at most one *pending* approval per refund.
- tool_calls: status 'invalid'; policy_reason_code, error_code; stored-size caps.
- audit_logs: reason_code.
- outbox_events: seq (strict order), next_attempt_at (backoff), parked_at (retries exhausted);
             new partial indexes.
- processed_events: consumer dedupe ledger.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None

NEW_REFUND_STATUSES = "'pending_approval', 'processing', 'succeeded', 'failed', 'rejected'"
OLD_REFUND_STATUSES = "'pending', 'pending_approval', 'succeeded', 'failed', 'rejected'"
NEW_TOOL_CALL_STATUSES = (
    "'pending', 'invalid', 'succeeded', 'failed', 'denied', 'awaiting_approval'"
)
OLD_TOOL_CALL_STATUSES = "'pending', 'succeeded', 'failed', 'denied', 'awaiting_approval'"


def upgrade() -> None:
    # ---- refunds ---------------------------------------------------------------------
    # Order matters: the OLD check would reject 'processing', so drop it before backfilling.
    # Old 'pending' meant "created, not yet paid out" -> 'processing'.
    op.drop_constraint(op.f("ck_refunds_refund_status"), "refunds", type_="check")
    op.execute("UPDATE refunds SET status = 'processing' WHERE status = 'pending'")
    op.create_check_constraint(
        op.f("ck_refunds_refund_status"), "refunds", f"status IN ({NEW_REFUND_STATUSES})"
    )

    op.drop_constraint("uq_refunds_idempotency_key", "refunds", type_="unique")
    op.create_unique_constraint(
        op.f("uq_refunds_requested_by_idempotency_key"),
        "refunds",
        ["requested_by", "idempotency_key"],
    )

    op.add_column("refunds", sa.Column("request_hash", sa.String(length=64), nullable=True))
    op.add_column("refunds", sa.Column("reason_code", sa.String(length=32), nullable=True))
    op.execute(
        "UPDATE refunds SET request_hash = repeat('0', 64), reason_code = 'other' "
        "WHERE request_hash IS NULL"
    )
    op.alter_column("refunds", "request_hash", nullable=False)
    op.alter_column("refunds", "reason_code", nullable=False)

    # ---- approvals -------------------------------------------------------------------
    op.add_column("approvals", sa.Column("refund_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_approvals_refund_id_refunds"),
        "approvals",
        "refunds",
        ["refund_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "uq_approvals_refund_id_pending",
        "approvals",
        ["refund_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )

    # ---- tool_calls ------------------------------------------------------------------
    op.drop_constraint(op.f("ck_tool_calls_tool_call_status"), "tool_calls", type_="check")
    op.create_check_constraint(
        op.f("ck_tool_calls_tool_call_status"),
        "tool_calls",
        f"status IN ({NEW_TOOL_CALL_STATUSES})",
    )
    op.add_column(
        "tool_calls", sa.Column("policy_reason_code", sa.String(length=64), nullable=True)
    )
    op.add_column("tool_calls", sa.Column("error_code", sa.String(length=64), nullable=True))
    op.create_check_constraint(
        op.f("ck_tool_calls_arguments_size"),
        "tool_calls",
        "octet_length(arguments::text) <= 16384",
    )
    op.create_check_constraint(
        op.f("ck_tool_calls_result_size"),
        "tool_calls",
        "result IS NULL OR octet_length(result::text) <= 32768",
    )

    # ---- audit_logs ------------------------------------------------------------------
    op.add_column("audit_logs", sa.Column("reason_code", sa.String(length=64), nullable=True))

    # ---- outbox_events ---------------------------------------------------------------
    op.drop_index("ix_outbox_events_unpublished", table_name="outbox_events")
    # Identity backfills existing rows automatically.
    op.add_column(
        "outbox_events",
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=True), nullable=False),
    )
    op.add_column(
        "outbox_events",
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.add_column(
        "outbox_events", sa.Column("parked_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index("uq_outbox_events_seq", "outbox_events", ["seq"], unique=True)
    op.create_index(
        "ix_outbox_events_pending",
        "outbox_events",
        ["next_attempt_at", "seq"],
        postgresql_where=sa.text("published_at IS NULL AND parked_at IS NULL"),
    )
    op.create_index(
        "ix_outbox_events_aggregate_unpublished",
        "outbox_events",
        ["aggregate_id", "seq"],
        postgresql_where=sa.text("published_at IS NULL"),
    )

    # ---- processed_events ------------------------------------------------------------
    op.create_table(
        "processed_events",
        sa.Column("consumer", sa.String(length=100), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("consumer", "event_id", name=op.f("pk_processed_events")),
    )


def downgrade() -> None:
    """Lossy by nature: new columns and their data are dropped. Re-creating the old *global*
    UNIQUE(idempotency_key) fails if two users have used the same key."""
    op.drop_table("processed_events")

    op.drop_index("ix_outbox_events_aggregate_unpublished", table_name="outbox_events")
    op.drop_index("ix_outbox_events_pending", table_name="outbox_events")
    op.drop_index("uq_outbox_events_seq", table_name="outbox_events")
    op.drop_column("outbox_events", "parked_at")
    op.drop_column("outbox_events", "next_attempt_at")
    op.drop_column("outbox_events", "seq")
    op.create_index(
        "ix_outbox_events_unpublished",
        "outbox_events",
        ["created_at"],
        postgresql_where=sa.text("published_at IS NULL"),
    )

    op.drop_column("audit_logs", "reason_code")

    op.drop_constraint(op.f("ck_tool_calls_result_size"), "tool_calls", type_="check")
    op.drop_constraint(op.f("ck_tool_calls_arguments_size"), "tool_calls", type_="check")
    op.drop_column("tool_calls", "error_code")
    op.drop_column("tool_calls", "policy_reason_code")
    op.drop_constraint(op.f("ck_tool_calls_tool_call_status"), "tool_calls", type_="check")
    op.execute("UPDATE tool_calls SET status = 'failed' WHERE status = 'invalid'")
    op.create_check_constraint(
        op.f("ck_tool_calls_tool_call_status"),
        "tool_calls",
        f"status IN ({OLD_TOOL_CALL_STATUSES})",
    )

    op.drop_index("uq_approvals_refund_id_pending", table_name="approvals")
    op.drop_constraint("fk_approvals_refund_id_refunds", "approvals", type_="foreignkey")
    op.drop_column("approvals", "refund_id")

    op.drop_column("refunds", "reason_code")
    op.drop_column("refunds", "request_hash")
    op.drop_constraint("uq_refunds_requested_by_idempotency_key", "refunds", type_="unique")
    op.create_unique_constraint(op.f("uq_refunds_idempotency_key"), "refunds", ["idempotency_key"])
    # Drop the NEW check first: it would reject 'pending'.
    op.drop_constraint(op.f("ck_refunds_refund_status"), "refunds", type_="check")
    op.execute("UPDATE refunds SET status = 'pending' WHERE status = 'processing'")
    op.create_check_constraint(
        op.f("ck_refunds_refund_status"), "refunds", f"status IN ({OLD_REFUND_STATUSES})"
    )

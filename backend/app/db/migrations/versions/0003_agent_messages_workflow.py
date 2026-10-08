"""phase 3 schema: chat messages, and conversation / message identity on workflow_runs

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-08

Hand-written and hand-reviewed (autogenerate does not see CHECK changes): every enum column has
exactly ONE named CHECK.

Changes
- workflow_runs: status gains 'escalated' (CHECK dropped and re-created); conversation_id,
  message_id, response; UNIQUE (conversation_id, message_id), UNIQUE (user_id, message_id) and at
  most one 'running' run per conversation (partial unique index).
- messages: the chat transcript (role CHECK, content size cap, strict order by identity `seq`).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | None = None
depends_on: str | None = None

NEW_WORKFLOW_STATUSES = "'running', 'waiting_approval', 'completed', 'failed', 'escalated'"
OLD_WORKFLOW_STATUSES = "'running', 'waiting_approval', 'completed', 'failed'"


def upgrade() -> None:
    # ---- workflow_runs ---------------------------------------------------------------
    op.drop_constraint(op.f("ck_workflow_runs_workflow_status"), "workflow_runs", type_="check")
    op.create_check_constraint(
        op.f("ck_workflow_runs_workflow_status"),
        "workflow_runs",
        f"status IN ({NEW_WORKFLOW_STATUSES})",
    )
    op.add_column("workflow_runs", sa.Column("conversation_id", sa.Uuid(), nullable=True))
    op.add_column("workflow_runs", sa.Column("message_id", sa.Uuid(), nullable=True))
    op.add_column(
        "workflow_runs",
        sa.Column("response", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_index(
        op.f("ix_workflow_runs_conversation_id"),
        "workflow_runs",
        ["conversation_id"],
        unique=False,
    )
    # NULLs are distinct in a unique index, so runs that are not chat turns are unaffected.
    op.create_index(
        "uq_workflow_runs_conversation_id_message_id",
        "workflow_runs",
        ["conversation_id", "message_id"],
        unique=True,
    )
    op.create_index(
        "uq_workflow_runs_user_id_message_id",
        "workflow_runs",
        ["user_id", "message_id"],
        unique=True,
    )
    op.create_index(
        "uq_workflow_runs_conversation_running",
        "workflow_runs",
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
    )

    # ---- messages --------------------------------------------------------------------
    op.create_table(
        "messages",
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("workflow_run_id", sa.Uuid(), nullable=True),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("content", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "role IN ('user', 'assistant', 'tool')", name=op.f("ck_messages_message_role")
        ),
        sa.CheckConstraint(
            "octet_length(content::text) <= 32768", name=op.f("ck_messages_content_size")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_messages_user_id_users"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["workflow_run_id"],
            ["workflow_runs.id"],
            name=op.f("fk_messages_workflow_run_id_workflow_runs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
    )
    op.create_index("ix_messages_conversation_id_seq", "messages", ["conversation_id", "seq"])
    op.create_index(
        op.f("ix_messages_workflow_run_id"), "messages", ["workflow_run_id"], unique=False
    )


def downgrade() -> None:
    """Lossy by nature: the chat transcript and the new columns are dropped."""
    op.drop_table("messages")
    op.drop_index("uq_workflow_runs_conversation_running", table_name="workflow_runs")
    op.drop_index("uq_workflow_runs_user_id_message_id", table_name="workflow_runs")
    op.drop_index("uq_workflow_runs_conversation_id_message_id", table_name="workflow_runs")
    op.drop_index(op.f("ix_workflow_runs_conversation_id"), table_name="workflow_runs")
    op.drop_column("workflow_runs", "response")
    op.drop_column("workflow_runs", "message_id")
    op.drop_column("workflow_runs", "conversation_id")
    # Drop the NEW check first: re-creating the old one would reject 'escalated' rows.
    op.drop_constraint(op.f("ck_workflow_runs_workflow_status"), "workflow_runs", type_="check")
    op.execute("UPDATE workflow_runs SET status = 'failed' WHERE status = 'escalated'")
    op.create_check_constraint(
        op.f("ck_workflow_runs_workflow_status"),
        "workflow_runs",
        f"status IN ({OLD_WORKFLOW_STATUSES})",
    )

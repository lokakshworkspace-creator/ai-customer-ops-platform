"""phase 6 schema: revoked tokens (logout)

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-12

Hand-written and hand-reviewed. One new table, no enum and no change to an existing table, so there
is no CHECK to review.

revoked_tokens(jti PK, user_id FK users ON DELETE CASCADE, expires_at, revoked_at). A logged-out
token's `jti` goes here; get_current_user refuses it until its own expiry, after which the sweeper
purges the row (hence the index on expires_at). A Postgres table rather than a Redis denylist:
revocation is security state and Redis is disposable.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "revoked_tokens",
        sa.Column("jti", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_revoked_tokens_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("jti", name=op.f("pk_revoked_tokens")),
    )
    op.create_index("ix_revoked_tokens_expires_at", "revoked_tokens", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_revoked_tokens_expires_at", table_name="revoked_tokens")
    op.drop_table("revoked_tokens")

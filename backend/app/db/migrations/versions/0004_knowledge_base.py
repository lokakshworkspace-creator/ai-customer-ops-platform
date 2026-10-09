"""phase 4 schema: the knowledge base (documents, chunks, vector + full-text indexes)

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-09

Hand-written and hand-reviewed (autogenerate does not see CHECK changes or index operator
classes): the one enum column has exactly ONE named CHECK.

- kb_documents: namespace + source (unique together), title, audience (CHECK customer|internal),
  version, content_hash, embedding_model.
- kb_chunks: heading, content, embedding vector(768) (HARD-CODED: EMBEDDING_DIM must equal it and
  ingest/retrieval check), tsv (generated, 'english'), embedding_model, token_count;
  HNSW index (cosine) on the embedding and a GIN index on tsv. Chunks are deleted with their
  document.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.db.vector import Vector

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | None = None
depends_on: str | None = None

DIM = 768  # literal on purpose: the column type must not depend on a setting


def upgrade() -> None:
    op.create_table(
        "kb_documents",
        sa.Column("namespace", sa.String(length=64), server_default="default", nullable=False),
        sa.Column("source", sa.String(length=200), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("audience", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("embedding_model", sa.String(length=100), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "audience IN ('customer', 'internal')", name=op.f("ck_kb_documents_kb_audience")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_kb_documents")),
        sa.UniqueConstraint("namespace", "source", name=op.f("uq_kb_documents_namespace")),
    )
    op.create_table(
        "kb_chunks",
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("heading", sa.Text(), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(DIM), nullable=False),
        sa.Column(
            "tsv",
            postgresql.TSVECTOR(),
            sa.Computed(
                "to_tsvector('english', coalesce(heading, '') || ' ' || content)", persisted=True
            ),
            nullable=False,
        ),
        sa.Column("embedding_model", sa.String(length=100), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("token_count > 0", name=op.f("ck_kb_chunks_token_count_positive")),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["kb_documents.id"],
            name=op.f("fk_kb_chunks_document_id_kb_documents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_kb_chunks")),
        sa.UniqueConstraint("document_id", "ordinal", name=op.f("uq_kb_chunks_document_id")),
    )
    op.create_index(op.f("ix_kb_chunks_document_id"), "kb_chunks", ["document_id"], unique=False)
    op.create_index("ix_kb_chunks_tsv", "kb_chunks", ["tsv"], postgresql_using="gin")
    op.create_index(
        "ix_kb_chunks_embedding_hnsw",
        "kb_chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )


def downgrade() -> None:
    """Drops the knowledge base (it can be rebuilt from backend/app/data/kb with kb_ingest.py)."""
    op.drop_table("kb_chunks")
    op.drop_table("kb_documents")

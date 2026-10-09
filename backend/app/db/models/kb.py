import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Computed,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin
from app.db.models.enums import KbAudience, str_enum
from app.db.vector import Vector

# migration 0004 created the column as vector(768): EMBEDDING_DIM must equal this (rag/guards.py)
EMBEDDING_COLUMN_DIM = 768


class KbDocument(UUIDPrimaryKeyMixin, Base):
    """A knowledge-base document. Documents INFORM answers; they never authorize anything
    (golden rule 3): rules.yaml and the tool executor decide.

    `audience` is enforced inside the retrieval SQL: the agent only ever searches `customer`.
    `namespace` keeps separate indexes apart (the demo and evaluation runs use their own, so they
    cannot disturb a real one). `content_hash` is the hash of the RENDERED text plus the embedding
    model and dimension, so re-ingesting an unchanged document is a no-op."""

    __tablename__ = "kb_documents"
    __table_args__ = (UniqueConstraint("namespace", "source"),)

    namespace: Mapped[str] = mapped_column(String(64), server_default="default", default="default")
    source: Mapped[str] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(String(200))
    audience: Mapped[KbAudience] = mapped_column(str_enum(KbAudience, "kb_audience"))
    version: Mapped[int] = mapped_column(server_default="1", default=1)
    content_hash: Mapped[str] = mapped_column(String(64))
    embedding_model: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class KbChunk(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "kb_chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "ordinal"),
        Index("ix_kb_chunks_tsv", "tsv", postgresql_using="gin"),
        Index(
            "ix_kb_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        CheckConstraint("token_count > 0", name="token_count_positive"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("kb_documents.id", ondelete="CASCADE"), index=True
    )
    ordinal: Mapped[int]
    heading: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_COLUMN_DIM))
    tsv: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', coalesce(heading, '') || ' ' || content)", persisted=True),
    )
    embedding_model: Mapped[str] = mapped_column(String(100))
    token_count: Mapped[int]

"""Loud failures for knowledge-base configuration mismatches.

Two things must agree or retrieval returns nonsense (or errors deep inside pgvector):

* the EMBEDDING_DIM setting and the width of the `kb_chunks.embedding` column (migration 0004
  created it as vector(768)): checked here, by asking Postgres;
* the embedding model: vectors of different models are not comparable, so every chunk stores its
  model and a search only ever looks at the current model's chunks. If the knowledge base holds
  chunks but none from the current model, that is `KbNotIndexed` (a clear instruction to
  re-ingest), not an empty "nothing relevant" answer.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class KbConfigError(Exception):
    """A knowledge-base configuration mismatch. The message says how to fix it."""


class KbDimensionMismatch(KbConfigError):
    pass


class KbNotIndexed(KbConfigError):
    pass


async def column_dimension(session: AsyncSession) -> int:
    if await session.scalar(text("SELECT to_regclass('kb_chunks')")) is None:
        raise KbConfigError("The knowledge-base tables do not exist: run `alembic upgrade head`.")
    dim = await session.scalar(
        text(
            "SELECT atttypmod FROM pg_attribute "
            "WHERE attrelid = 'kb_chunks'::regclass AND attname = 'embedding'"
        )
    )
    if not dim or dim < 1:
        raise KbConfigError("kb_chunks.embedding is missing: run `alembic upgrade head`.")
    return int(dim)


async def check_dimension(session: AsyncSession, expected: int) -> None:
    actual = await column_dimension(session)
    if actual != expected:
        raise KbDimensionMismatch(
            f"EMBEDDING_DIM={expected} but kb_chunks.embedding is vector({actual}). "
            "Set EMBEDDING_DIM to match the column (a different width needs a new migration and a "
            "full re-ingest)."
        )


async def check_indexed(session: AsyncSession, namespace: str, model: str) -> None:
    """Raise KbNotIndexed when the namespace has chunks, but none made by `model`."""
    rows = (
        await session.execute(
            text(
                "SELECT c.embedding_model AS m, count(*) AS n FROM kb_chunks c "
                "JOIN kb_documents d ON d.id = c.document_id WHERE d.namespace = :ns GROUP BY 1"
            ),
            {"ns": namespace},
        )
    ).all()
    if rows and not any(r.m == model for r in rows):
        have = ", ".join(sorted(r.m for r in rows))
        raise KbNotIndexed(
            f"The knowledge base ({namespace!r}) was embedded with {have}, not {model!r}: "
            "run `python scripts/kb_ingest.py --apply` to re-embed it."
        )

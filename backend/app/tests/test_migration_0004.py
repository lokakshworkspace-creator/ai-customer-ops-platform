"""Migration 0004: the knowledge base tables, constraints and indexes, and the dimension guard."""

import asyncio
import uuid

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.db.vector import vector_literal
from app.rag.guards import (
    KbConfigError,
    KbDimensionMismatch,
    KbNotIndexed,
    check_dimension,
    check_indexed,
    column_dimension,
)
from app.tests.conftest import _alembic_config, _test_db_url, rebuild_schema, truncate_all

VEC = vector_literal([1.0] + [0.0] * 767)


async def rejected(session: AsyncSession, statement: str, **params) -> bool:
    try:
        async with session.begin_nested():
            await session.execute(text(statement), params)
    except Exception as exc:  # IntegrityError or the dimension DataError
        return (
            isinstance(exc, IntegrityError)
            or "dimension" in str(exc).lower()
            or "expected" in str(exc)
        )
    return False


async def add_document(session, source="returns.md", audience="customer", namespace="default"):
    doc_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO kb_documents (id, namespace, source, title, audience, content_hash, "
            "embedding_model) VALUES (:i, :n, :s, 'Returns', :a, repeat('a', 64), 'm1')"
        ),
        {"i": doc_id, "n": namespace, "s": source, "a": audience},
    )
    return doc_id


CHUNK = (
    "INSERT INTO kb_chunks (id, document_id, ordinal, heading, content, embedding, "
    "embedding_model, token_count) VALUES (gen_random_uuid(), :d, :o, 'Returns', :c, "
    "CAST(:v AS vector), 'm1', 12)"
)


async def test_a_document_and_a_chunk_round_trip_with_a_generated_search_vector(
    db_session: AsyncSession,
) -> None:
    doc = await add_document(db_session)
    await db_session.execute(
        text(CHUNK),
        {"d": doc, "o": 0, "c": "Electronics can be returned within thirty days.", "v": VEC},
    )
    hit = await db_session.scalar(
        text(
            "SELECT count(*) FROM kb_chunks "
            "WHERE tsv @@ to_tsquery('english', 'electronic & return')"
        )
    )
    assert hit == 1  # the generated tsvector is stemmed and searchable (heading is included too)
    near = await db_session.scalar(
        text("SELECT 1 - (embedding <=> CAST(:v AS vector)) FROM kb_chunks"), {"v": VEC}
    )
    assert abs(near - 1.0) < 1e-6


async def test_audience_is_one_of_customer_or_internal(db_session: AsyncSession) -> None:
    await add_document(db_session, "a.md", "customer")
    await add_document(db_session, "b.md", "internal")
    assert await rejected(
        db_session,
        "INSERT INTO kb_documents (id, source, title, audience, content_hash, embedding_model) "
        "VALUES (gen_random_uuid(), 'c.md', 't', 'public', repeat('a', 64), 'm')",
    )


async def test_a_source_is_unique_per_namespace_not_globally(db_session: AsyncSession) -> None:
    await add_document(db_session, "returns.md", namespace="default")
    await add_document(db_session, "returns.md", namespace="demo")  # another namespace: fine
    assert await rejected(
        db_session,
        "INSERT INTO kb_documents (id, namespace, source, title, audience, content_hash, "
        "embedding_model) VALUES (gen_random_uuid(), 'default', 'returns.md', 't', 'customer', "
        "repeat('a', 64), 'm')",
    )


async def test_chunk_ordinals_are_unique_per_document_and_chunks_cascade(
    db_session: AsyncSession,
) -> None:
    doc = await add_document(db_session)
    await db_session.execute(text(CHUNK), {"d": doc, "o": 0, "c": "one", "v": VEC})
    assert await rejected(db_session, CHUNK, d=doc, o=0, c="dup", v=VEC)
    await db_session.execute(text(CHUNK), {"d": doc, "o": 1, "c": "two", "v": VEC})
    await db_session.execute(text("DELETE FROM kb_documents WHERE id = :d"), {"d": doc})
    assert await db_session.scalar(text("SELECT count(*) FROM kb_chunks")) == 0


async def test_the_embedding_column_is_exactly_768_wide(db_session: AsyncSession) -> None:
    doc = await add_document(db_session)
    short = vector_literal([1.0] * 767)
    assert await rejected(db_session, CHUNK, d=doc, o=0, c="x", v=short)
    assert await column_dimension(db_session) == 768


async def test_token_count_must_be_positive(db_session: AsyncSession) -> None:
    doc = await add_document(db_session)
    assert await rejected(
        db_session,
        "INSERT INTO kb_chunks (id, document_id, ordinal, content, embedding, embedding_model, "
        "token_count) VALUES (gen_random_uuid(), :d, 0, 'x', CAST(:v AS vector), 'm', 0)",
        d=doc,
        v=VEC,
    )


async def test_the_hnsw_cosine_and_gin_indexes_exist(db_session: AsyncSession) -> None:
    rows = (
        await db_session.execute(
            text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'kb_chunks'")
        )
    ).all()
    defs = {r.indexname: r.indexdef for r in rows}
    assert "USING hnsw" in defs["ix_kb_chunks_embedding_hnsw"]
    assert "vector_cosine_ops" in defs["ix_kb_chunks_embedding_hnsw"]
    assert "USING gin" in defs["ix_kb_chunks_tsv"]


async def test_the_audience_enum_has_exactly_one_check(db_session: AsyncSession) -> None:
    n = await db_session.scalar(
        text(
            "SELECT count(*) FROM pg_constraint WHERE conrelid = 'kb_documents'::regclass "
            "AND contype = 'c'"
        )
    )
    assert n == 1


# --- the dimension / model guards ---------------------------------------------------------------


async def test_the_dimension_guard_passes_for_768_and_fails_loudly_otherwise(
    db_session: AsyncSession,
) -> None:
    await check_dimension(db_session, 768)
    with pytest.raises(KbDimensionMismatch, match=r"EMBEDDING_DIM=1536 .* vector\(768\)"):
        await check_dimension(db_session, 1536)


async def test_missing_knowledge_base_tables_are_a_clear_error_not_a_sql_error(
    db_session: AsyncSession,
) -> None:
    await db_session.execute(text("DROP TABLE kb_chunks CASCADE"))  # rolled back with the test
    with pytest.raises(KbConfigError, match="alembic upgrade head"):
        await check_dimension(db_session, 768)


async def test_chunks_from_another_model_are_a_loud_not_indexed_error(
    db_session: AsyncSession,
) -> None:
    await check_indexed(db_session, "default", "anything")  # an empty namespace is fine
    doc = await add_document(db_session)
    await db_session.execute(text(CHUNK), {"d": doc, "o": 0, "c": "x", "v": VEC})
    await check_indexed(db_session, "default", "m1")  # the model that made them
    with pytest.raises(KbNotIndexed, match="m1.*not 'm2'.*kb_ingest"):
        await check_indexed(db_session, "default", "m2")
    await check_indexed(db_session, "other-namespace", "m2")


# --- round trip ---------------------------------------------------------------------------------


async def _alembic(engine: AsyncEngine, fn, *args) -> None:
    await asyncio.to_thread(fn, _alembic_config(_test_db_url()), *args)
    await engine.dispose()


async def test_downgrade_drops_the_knowledge_base_and_upgrade_restores_it(
    engine: AsyncEngine,
) -> None:
    await truncate_all(engine)
    try:
        await _alembic(engine, command.downgrade, "0003")
        async with engine.connect() as c:
            assert await c.scalar(text("SELECT to_regclass('kb_chunks')")) is None
            assert await c.scalar(text("SELECT to_regclass('kb_documents')")) is None
        await _alembic(engine, command.upgrade, "head")
        async with engine.connect() as c:
            assert await c.scalar(text("SELECT count(*) FROM kb_chunks")) == 0
    finally:
        await rebuild_schema(engine)

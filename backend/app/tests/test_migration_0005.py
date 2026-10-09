"""Migration 0005: the partial index that lets the resume consumer find a waiting turn."""

import asyncio

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.tests.conftest import _alembic_config, _test_db_url, rebuild_schema, truncate_all

INDEX = "ix_workflow_runs_waiting_refund"


async def test_the_waiting_refund_index_is_partial_and_on_the_checkpoint_key(
    db_session: AsyncSession,
) -> None:
    definition = await db_session.scalar(
        text("SELECT indexdef FROM pg_indexes WHERE indexname = :n"), {"n": INDEX}
    )
    assert definition is not None
    assert "context ->> 'refund_id'" in definition
    assert "waiting_approval" in definition  # partial: only the turns that are waiting


async def test_the_planner_can_use_it_for_the_consumers_lookup(db_session: AsyncSession) -> None:
    await db_session.execute(text("SET LOCAL enable_seqscan = off"))
    plan = "\n".join(
        (
            await db_session.execute(
                text(
                    "EXPLAIN SELECT id FROM workflow_runs "
                    "WHERE status = 'waiting_approval' AND context ->> 'refund_id' = 'x'"
                )
            )
        ).scalars()
    )
    assert INDEX in plan


async def _alembic(engine: AsyncEngine, fn, *args) -> None:
    await asyncio.to_thread(fn, _alembic_config(_test_db_url()), *args)
    await engine.dispose()


async def test_downgrade_drops_the_index_and_upgrade_restores_it(engine: AsyncEngine) -> None:
    await truncate_all(engine)
    try:
        await _alembic(engine, command.downgrade, "0004")
        async with engine.connect() as c:
            assert await c.scalar(text("SELECT to_regclass(:n)"), {"n": INDEX}) is None
        await _alembic(engine, command.upgrade, "head")
        async with engine.connect() as c:
            assert await c.scalar(text("SELECT to_regclass(:n)"), {"n": INDEX}) is not None
    finally:
        await rebuild_schema(engine)


@pytest.mark.parametrize("revision", ["0005"])
def test_the_revision_chain_is_linear(revision: str) -> None:
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(_alembic_config(_test_db_url()))
    assert script.get_heads() == [revision]
    assert script.get_revision(revision).down_revision == "0004"

"""Migration 0006: the revoked_tokens table."""

import asyncio

from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.tests.conftest import _alembic_config, _test_db_url, rebuild_schema, truncate_all


async def test_the_table_has_the_expected_shape(db_session: AsyncSession) -> None:
    cols = dict(
        (
            await db_session.execute(
                text(
                    "SELECT column_name, is_nullable FROM information_schema.columns "
                    "WHERE table_name = 'revoked_tokens'"
                )
            )
        ).all()
    )
    assert cols == {
        "jti": "NO",
        "user_id": "NO",
        "expires_at": "NO",
        "revoked_at": "NO",
    }
    pk = await db_session.scalar(
        text(
            "SELECT string_agg(a.attname, ',') FROM pg_index i "
            "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
            "WHERE i.indrelid = 'revoked_tokens'::regclass AND i.indisprimary"
        )
    )
    assert pk == "jti"
    fk = await db_session.scalar(
        text(
            "SELECT confdeltype FROM pg_constraint "
            "WHERE conrelid = 'revoked_tokens'::regclass AND contype = 'f'"
        )
    )
    assert fk in ("c", b"c")  # ON DELETE CASCADE


async def test_the_purge_index_exists(db_session: AsyncSession) -> None:
    n = await db_session.scalar(
        text("SELECT count(*) FROM pg_indexes WHERE indexname = 'ix_revoked_tokens_expires_at'")
    )
    assert n == 1


async def test_no_enum_check_was_added(db_session: AsyncSession) -> None:
    n = await db_session.scalar(
        text(
            "SELECT count(*) FROM pg_constraint "
            "WHERE conrelid = 'revoked_tokens'::regclass AND contype = 'c'"
        )
    )
    assert n == 0


async def _alembic(engine: AsyncEngine, fn, *args) -> None:
    await asyncio.to_thread(fn, _alembic_config(_test_db_url()), *args)
    await engine.dispose()


async def test_downgrade_drops_the_table_and_upgrade_restores_it(engine: AsyncEngine) -> None:
    await truncate_all(engine)
    try:
        await _alembic(engine, command.downgrade, "0005")
        async with engine.connect() as c:
            assert await c.scalar(text("SELECT to_regclass('revoked_tokens')")) is None
        await _alembic(engine, command.upgrade, "head")
        async with engine.connect() as c:
            assert await c.scalar(text("SELECT to_regclass('revoked_tokens')")) is not None
    finally:
        await rebuild_schema(engine)


def test_0006_follows_0005_in_a_linear_chain() -> None:
    script = ScriptDirectory.from_config(_alembic_config(_test_db_url()))
    assert len(script.get_heads()) == 1
    assert script.get_revision("0006").down_revision == "0005"

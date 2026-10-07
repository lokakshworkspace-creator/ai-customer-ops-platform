import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from app.core.config import get_settings
from app.main import app

BACKEND_DIR = Path(__file__).resolve().parents[2]
TEST_DB_NAME = "ops_test"


def _test_db_url() -> URL:
    url = make_url(get_settings().database_url).set(database=TEST_DB_NAME)
    # Hard stop: migrations here drop and recreate the schema.
    assert url.database is not None and url.database.endswith("_test")
    return url


def _alembic_config(url: URL) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option(
        "sqlalchemy.url", url.render_as_string(hide_password=False).replace("%", "%%")
    )
    return cfg


async def _ensure_database(url: URL) -> None:
    admin = create_async_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            exists = await conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": url.database}
            )
            if not exists:
                await conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    finally:
        await admin.dispose()


@pytest.fixture(scope="session")
async def engine() -> AsyncIterator[AsyncEngine]:
    """A fresh `ops_test` database whose schema is built by running the real Alembic
    migrations (so the migration itself is under test)."""
    url = _test_db_url()
    await _ensure_database(url)

    eng = create_async_engine(url)
    async with eng.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))

    # Alembic's env.py calls asyncio.run(), so it must not run on the pytest event loop.
    await asyncio.to_thread(command.upgrade, _alembic_config(url), "head")
    yield eng
    await eng.dispose()


@pytest.fixture
async def db_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Each test runs inside an outer transaction that is rolled back afterwards.
    `commit()` inside the test only releases a SAVEPOINT."""
    async with engine.connect() as conn:
        outer = await conn.begin()
        session = AsyncSession(
            bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False
        )
        try:
            yield session
        finally:
            await session.close()
            await outer.rollback()


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
        app.dependency_overrides.clear()

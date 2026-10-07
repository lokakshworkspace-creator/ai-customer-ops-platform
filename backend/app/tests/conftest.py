import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.api.deps import get_executor, get_session
from app.core.config import get_settings
from app.db.base import Base
from app.main import app
from app.modules.refunds.payments import SimulatedPaymentProvider
from app.policy.rules import get_rules
from app.tools.default import build_default_registry
from app.tools.executor import ToolExecutor

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


async def rebuild_schema(engine: AsyncEngine) -> None:
    """Drop and recreate the schema of the TEST database, then migrate to head. Used at session
    start, and by tests that move the schema between revisions and must always leave it clean."""
    url = _test_db_url()
    async with engine.begin() as conn:
        dbname = await conn.scalar(text("SELECT current_database()"))
        assert dbname and dbname.endswith("_test"), f"refusing to rebuild {dbname!r}"
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    # Alembic's env.py calls asyncio.run(), so it must not run on the pytest event loop.
    await asyncio.to_thread(command.upgrade, _alembic_config(url), "head")
    await engine.dispose()  # pooled connections hold plans for the old schema


async def truncate_all(engine: AsyncEngine) -> None:
    """Empty every application table in the TEST database (for tests that really commit).

    Guards: refuses unless the connected database name ends in `_test`; only tables known to
    the models are touched, so `alembic_version` is never truncated.
    """
    async with engine.begin() as conn:
        dbname = await conn.scalar(text("SELECT current_database()"))
        assert dbname and dbname.endswith("_test"), f"refusing to truncate {dbname!r}"
        names = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
        assert "alembic_version" not in names
        await conn.execute(text(f"TRUNCATE TABLE {names} RESTART IDENTITY CASCADE"))


@pytest.fixture(scope="session")
async def engine() -> AsyncIterator[AsyncEngine]:
    """A fresh `ops_test` database whose schema is built by running the real Alembic
    migrations (so the migration itself is under test)."""
    url = _test_db_url()
    await _ensure_database(url)

    eng = create_async_engine(url)
    await rebuild_schema(eng)
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


@pytest.fixture
async def db_client(db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """The real app, with its DB session replaced by the rollback-per-test session."""

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_session] = _session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.pop(get_session, None)


@dataclass
class CommittedDB:
    """Real, committed data over a pool of SEPARATE connections, for concurrency tests."""

    engine: AsyncEngine
    maker: async_sessionmaker[AsyncSession]


@pytest.fixture
async def committed_db(engine: AsyncEngine) -> AsyncIterator[CommittedDB]:
    """Unlike `db_session` (one connection, rolled back), every session made from `maker` has its
    own connection and COMMITS, so concurrent flows really contend for locks. Pool of 12+.
    Tables are emptied before and after (test database only; see truncate_all's guards)."""
    await truncate_all(engine)
    pool = create_async_engine(_test_db_url(), pool_size=12, max_overflow=8)
    try:
        yield CommittedDB(pool, async_sessionmaker(pool, expire_on_commit=False))
    finally:
        await pool.dispose()
        await truncate_all(engine)


@pytest.fixture
def provider() -> SimulatedPaymentProvider:
    return SimulatedPaymentProvider()


@pytest.fixture
async def refund_client(
    db_session: AsyncSession, provider: SimulatedPaymentProvider
) -> AsyncIterator[AsyncClient]:
    """The real app and the REAL auth dependency (no override of get_current_user); only the
    database session (rollback-per-test) and the payment provider are swapped."""
    executor = ToolExecutor(build_default_registry(get_rules(), provider))

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_executor] = lambda: executor
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_executor, None)


@pytest.fixture
async def committed_client(
    committed_db: CommittedDB, provider: SimulatedPaymentProvider
) -> AsyncIterator[AsyncClient]:
    """The real app where EVERY REQUEST gets its own session and connection from the pool and
    commits for real: concurrent requests genuinely contend for row locks."""
    executor = ToolExecutor(build_default_registry(get_rules(), provider))

    async def _session() -> AsyncIterator[AsyncSession]:
        async with committed_db.maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_executor] = lambda: executor
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_executor, None)

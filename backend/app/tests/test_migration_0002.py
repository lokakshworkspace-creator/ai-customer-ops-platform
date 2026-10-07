"""Migration 0002 on a database that already holds Phase 1 data: backfills, and a downgrade /
upgrade round trip. Runs against the test database only (it really commits, then truncates)."""

import asyncio
import uuid

from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.tests.conftest import _alembic_config, _test_db_url, rebuild_schema, truncate_all


async def _alembic(engine: AsyncEngine, fn, *args) -> None:
    # Alembic's env.py calls asyncio.run(), so it runs in a thread; pooled connections hold
    # plans for the old schema, so drop them after each schema change.
    await asyncio.to_thread(fn, _alembic_config(_test_db_url()), *args)
    await engine.dispose()


async def _insert_phase1_rows(engine: AsyncEngine) -> dict[str, uuid.UUID]:
    ids = {k: uuid.uuid4() for k in ("user", "order", "payment", "refund", "tool_call")}
    async with engine.begin() as c:
        await c.execute(
            text(
                "INSERT INTO users (id, email, password_hash, full_name, role) "
                "VALUES (:u, 'legacy@example.com', 'x', 'Legacy', 'customer')"
            ),
            {"u": ids["user"]},
        )
        await c.execute(
            text(
                "INSERT INTO orders (id, user_id, status, total_amount, currency) "
                "VALUES (:o, :u, 'paid', 100.00, 'INR')"
            ),
            {"o": ids["order"], "u": ids["user"]},
        )
        await c.execute(
            text(
                "INSERT INTO payments (id, order_id, amount, currency, status, provider) "
                "VALUES (:p, :o, 100.00, 'INR', 'succeeded', 'simulated')"
            ),
            {"p": ids["payment"], "o": ids["order"]},
        )
        # Phase 1 shape: no request_hash / reason_code, old status vocabulary.
        await c.execute(
            text(
                "INSERT INTO refunds (id, idempotency_key, payment_id, order_id, requested_by,"
                " amount, currency, status) VALUES (:r, 'legacy-key', :p, :o, :u, 10.00, 'INR',"
                " 'pending')"
            ),
            {"r": ids["refund"], "p": ids["payment"], "o": ids["order"], "u": ids["user"]},
        )
        for i in range(3):
            await c.execute(
                text(
                    "INSERT INTO outbox_events (id, aggregate_type, aggregate_id, event_type) "
                    "VALUES (:id, 'refund', 'r1', :t)"
                ),
                {"id": uuid.uuid4(), "t": f"legacy.{i}"},
            )
        await c.execute(
            text(
                "INSERT INTO tool_calls (id, user_id, tool_name, status) "
                "VALUES (:t, :u, 'legacy_tool', 'succeeded')"
            ),
            {"t": ids["tool_call"], "u": ids["user"]},
        )
    return ids


async def _assert_upgraded(engine: AsyncEngine, ids: dict[str, uuid.UUID]) -> None:
    async with engine.connect() as c:
        refund = (
            await c.execute(
                text("SELECT status, request_hash, reason_code FROM refunds WHERE id = :r"),
                {"r": ids["refund"]},
            )
        ).one()
        assert refund.status == "processing"  # old 'pending' = "not yet paid out"
        assert refund.request_hash == "0" * 64
        assert refund.reason_code == "other"

        seqs = list(await c.scalars(text("SELECT seq FROM outbox_events ORDER BY seq")))
        assert len(seqs) == 3 and len(set(seqs)) == 3 and all(s is not None for s in seqs)

        assert await c.scalar(text("SELECT count(*) FROM tool_calls")) == 1
        # New per-user constraint exists; the old global one is gone.
        constraints = set(
            await c.scalars(
                text("SELECT conname FROM pg_constraint WHERE conrelid = 'refunds'::regclass")
            )
        )
        assert "uq_refunds_requested_by_idempotency_key" in constraints
        assert "uq_refunds_idempotency_key" not in constraints


async def test_upgrade_backfills_existing_rows_and_downgrade_round_trips(
    engine: AsyncEngine,
) -> None:
    await truncate_all(engine)
    try:
        await _alembic(engine, command.downgrade, "0001")
        ids = await _insert_phase1_rows(engine)

        await _alembic(engine, command.upgrade, "head")
        await _assert_upgraded(engine, ids)

        # Down again (lossy: new columns dropped) and back up: data survives and re-backfills.
        await _alembic(engine, command.downgrade, "0001")
        async with engine.connect() as c:
            assert (
                await c.scalar(
                    text("SELECT status FROM refunds WHERE id = :r"), {"r": ids["refund"]}
                )
                == "pending"
            )
        await _alembic(engine, command.upgrade, "head")
        await _assert_upgraded(engine, ids)
    finally:
        # Whatever state a failure left us in (even half-migrated), restore a clean head schema
        # so later tests are unaffected.
        await rebuild_schema(engine)

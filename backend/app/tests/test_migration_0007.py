"""Migration 0007: the refund status `expired`, and what an expired refund means for the rules."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.db.models.enums import RefundStatus
from app.modules.refunds import repo
from app.policy.rules import get_rules
from app.tests.conftest import _alembic_config, _test_db_url, rebuild_schema, truncate_all
from app.tests.factories import create_order, create_payment, create_refund, create_user

CK = "ck_refunds_refund_status"


async def test_the_refund_status_has_exactly_one_named_check_and_it_allows_expired(
    db_session: AsyncSession,
) -> None:
    rows = (
        await db_session.execute(
            text(
                "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid = 'refunds'::regclass AND contype = 'c' AND conname LIKE '%status%'"
            )
        )
    ).all()
    assert [r[0] for r in rows] == [CK]  # one CHECK for the enum, not two
    assert "'expired'" in rows[0][1]
    for status in RefundStatus:
        assert f"'{status.value}'" in rows[0][1]  # the enum and the CHECK agree


async def test_an_expired_refund_can_be_stored_and_an_unknown_status_cannot(
    db_session: AsyncSession,
) -> None:
    user = await create_user(db_session, email="alice@example.com")
    order = await create_order(db_session, user)
    payment = await create_payment(db_session, order)
    refund = await create_refund(
        db_session, payment, user, idempotency_key="k1", status=RefundStatus.expired
    )
    assert refund.status == RefundStatus.expired
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                text("UPDATE refunds SET status = 'bogus' WHERE id = :i"), {"i": refund.id}
            )


async def test_an_expired_refund_holds_no_money_and_no_velocity_slot(
    db_session: AsyncSession,
) -> None:
    """The reason no change to the policy engine is needed: rules.yaml lists the statuses that
    count, and `expired` is not one of them."""
    rules = get_rules().refund
    assert RefundStatus.expired not in rules.reserving_statuses
    assert RefundStatus.expired not in rules.velocity_counted_statuses

    user = await create_user(db_session, email="alice@example.com")
    order = await create_order(db_session, user)
    payment = await create_payment(db_session, order)
    await create_refund(
        db_session,
        payment,
        user,
        idempotency_key="k1",
        amount="20.00",
        status=RefundStatus.expired,
    )
    await create_refund(
        db_session,
        payment,
        user,
        idempotency_key="k2",
        amount="5.00",
        status=RefundStatus.pending_approval,
    )
    reserved = await repo.reserved_amount(db_session, order.id, rules.reserving_statuses)
    assert str(reserved) == "5.00"  # only the pending one holds money
    count = await repo.recent_count(
        db_session,
        user.id,
        rules.velocity_counted_statuses,
        since=datetime.now(UTC) - timedelta(days=30),
    )
    assert count == 1


async def _alembic(engine: AsyncEngine, fn, *args) -> None:
    await asyncio.to_thread(fn, _alembic_config(_test_db_url()), *args)
    await engine.dispose()


async def test_downgrade_maps_expired_to_rejected_and_upgrade_restores_the_check(
    engine: AsyncEngine,
) -> None:
    await truncate_all(engine)
    try:
        user, order, pay = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        async with engine.begin() as c:
            await c.execute(
                text(
                    "INSERT INTO users (id, email, password_hash, full_name, role) "
                    "VALUES (:u, 'a@example.com', 'x', 'A', 'customer')"
                ),
                {"u": user},
            )
            await c.execute(
                text(
                    "INSERT INTO orders (id, user_id, status, total_amount, currency) "
                    "VALUES (:o, :u, 'paid', 50.00, 'INR')"
                ),
                {"o": order, "u": user},
            )
            await c.execute(
                text(
                    "INSERT INTO payments (id, order_id, amount, currency, status, provider) "
                    "VALUES (:p, :o, 50.00, 'INR', 'succeeded', 'simulated')"
                ),
                {"p": pay, "o": order},
            )
            await c.execute(
                text(
                    "INSERT INTO refunds (id, idempotency_key, request_hash, payment_id, order_id, "
                    "requested_by, amount, currency, reason_code, status) VALUES "
                    "(gen_random_uuid(), 'k', :h, :p, :o, :u, 10.00, 'INR', 'damaged', 'expired')"
                ),
                {"h": "k" * 64, "p": pay, "o": order, "u": user},
            )
        await _alembic(engine, command.downgrade, "0006")
        async with engine.connect() as c:
            assert await c.scalar(text("SELECT status FROM refunds")) == "rejected"
            definition = await c.scalar(
                text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :n"),
                {"n": CK},
            )
            assert "'expired'" not in definition
        await _alembic(engine, command.upgrade, "head")
        async with engine.connect() as c:
            definition = await c.scalar(
                text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :n"),
                {"n": CK},
            )
            assert "'expired'" in definition
            assert await c.scalar(text("SELECT status FROM refunds")) == "rejected"  # stays mapped
    finally:
        await rebuild_schema(engine)


def test_0007_follows_0006_in_a_linear_chain() -> None:
    script = ScriptDirectory.from_config(_alembic_config(_test_db_url()))
    assert len(script.get_heads()) == 1
    assert script.get_revision("0007").down_revision == "0006"

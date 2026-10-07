"""Schema-level guarantees, tested against the real migrated Postgres."""

import asyncio
import uuid
from decimal import Decimal

import pytest
from alembic import command
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.db.base import Base
from app.db.models import Order, User
from app.db.models.enums import Role
from app.tests.conftest import _alembic_config, _test_db_url
from app.tests.factories import create_order, create_payment, create_refund, create_user


async def test_pgvector_extension_is_enabled(db_session: AsyncSession) -> None:
    name = await db_session.scalar(
        text("SELECT extname FROM pg_extension WHERE extname = 'vector'")
    )
    assert name == "vector"


async def test_all_model_tables_exist(db_session: AsyncSession) -> None:
    rows = await db_session.scalars(
        text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    )
    existing = set(rows)
    assert set(Base.metadata.tables) <= existing
    assert len(Base.metadata.tables) == 11


async def test_migration_matches_models(engine: AsyncEngine) -> None:
    """`alembic check` fails if the models have drifted from the migrations."""
    await asyncio.to_thread(command.check, _alembic_config(_test_db_url()))


def test_uuid_primary_key_is_generated_app_side() -> None:
    col = User.__table__.c.id
    assert col.server_default is None  # not the database
    assert col.default is not None
    assert isinstance(col.default.arg(None), uuid.UUID)


async def test_user_email_is_unique(db_session: AsyncSession) -> None:
    await create_user(db_session, email="dup@example.com")
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await create_user(db_session, email="dup@example.com")


async def test_money_roundtrips_as_exact_decimal(db_session: AsyncSession) -> None:
    user = await create_user(db_session)
    order = await create_order(db_session, user, total="0.10", items=[("S", 1, "0.10")])
    order_id = order.id
    db_session.expire_all()  # force a real round trip through the database
    loaded = await db_session.scalar(select(Order).where(Order.id == order_id))
    assert loaded is not None
    assert loaded.total_amount == Decimal("0.10")
    assert isinstance(loaded.total_amount, Decimal)


async def test_money_check_constraints(db_session: AsyncSession) -> None:
    user = await create_user(db_session)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await create_order(db_session, user, total="-1.00")
    order = await create_order(db_session, user)
    with pytest.raises(IntegrityError):  # amount must be > 0
        async with db_session.begin_nested():
            await create_payment(db_session, order, amount="0.00")


async def test_status_check_constraint_rejects_unknown_value(db_session: AsyncSession) -> None:
    user = await create_user(db_session)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "INSERT INTO orders (id, user_id, status, total_amount, currency) "
                    "VALUES (:id, :uid, 'bogus', 1.00, 'USD')"
                ),
                {"id": uuid.uuid4(), "uid": user.id},
            )


async def test_refund_idempotency_key_is_unique(db_session: AsyncSession) -> None:
    admin = await create_user(db_session, role=Role.admin)
    order = await create_order(db_session, admin)
    payment = await create_payment(db_session, order)

    await create_refund(db_session, payment, admin, idempotency_key="key-1")
    with pytest.raises(IntegrityError) as exc:
        async with db_session.begin_nested():
            await create_refund(db_session, payment, admin, idempotency_key="key-1")
    assert "uq_refunds_idempotency_key" in str(exc.value)

    # A different key is fine.
    await create_refund(db_session, payment, admin, idempotency_key="key-2")


async def test_refund_idempotency_key_is_not_null(db_session: AsyncSession) -> None:
    admin = await create_user(db_session, role=Role.admin)
    order = await create_order(db_session, admin)
    payment = await create_payment(db_session, order)
    with pytest.raises(IntegrityError) as exc:
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "INSERT INTO refunds (id, idempotency_key, payment_id, order_id,"
                    " requested_by, amount, currency, status)"
                    " VALUES (:id, NULL, :p, :o, :u, 5.00, 'USD', 'pending')"
                ),
                {"id": uuid.uuid4(), "p": payment.id, "o": order.id, "u": admin.id},
            )
    assert "idempotency_key" in str(exc.value)


async def test_deleting_order_with_payment_is_restricted(db_session: AsyncSession) -> None:
    user = await create_user(db_session)
    order = await create_order(db_session, user)
    await create_payment(db_session, order)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(text("DELETE FROM orders WHERE id = :id"), {"id": order.id})

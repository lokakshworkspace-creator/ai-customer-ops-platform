import importlib.util
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password, verify_password
from app.db.models import Order, OrderItem, Payment, User
from app.db.models.enums import OrderStatus, Role

SEED_PATH = Path(__file__).resolve().parents[3] / "scripts" / "seed.py"


@pytest.fixture(scope="module")
def seed_module():
    spec = importlib.util.spec_from_file_location("seed_script", SEED_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_password_hash_roundtrip() -> None:
    h = hash_password("s3cret-pass")
    assert h != "s3cret-pass"
    assert h.startswith("$argon2id$")
    assert verify_password("s3cret-pass", h)
    assert not verify_password("wrong", h)
    assert not verify_password("s3cret-pass", "not-a-valid-hash")


def test_password_hashes_are_salted() -> None:
    assert hash_password("same") != hash_password("same")


async def test_seed_creates_expected_data(db_session: AsyncSession, seed_module) -> None:
    assert await seed_module.seed(db_session, password="pw-for-tests") is True

    users = {u.email: u for u in await db_session.scalars(select(User))}
    assert set(users) == {"admin@example.com", "alice@example.com", "bob@example.com"}
    assert users["admin@example.com"].role == Role.admin
    assert users["alice@example.com"].role == Role.customer
    assert verify_password("pw-for-tests", users["alice@example.com"].password_hash)

    orders = list(await db_session.scalars(select(Order)))
    assert len(orders) == 10
    per_user = {e: sum(o.user_id == u.id for o in orders) for e, u in users.items()}
    assert per_user == {"admin@example.com": 0, "alice@example.com": 6, "bob@example.com": 4}


async def test_seed_order_totals_match_their_items(db_session: AsyncSession, seed_module) -> None:
    await seed_module.seed(db_session)
    rows = await db_session.execute(
        select(Order.id, Order.total_amount, func.sum(OrderItem.unit_price * OrderItem.quantity))
        .join(OrderItem)
        .group_by(Order.id)
    )
    rows = rows.all()
    assert len(rows) == 10
    for _id, total, items_total in rows:
        assert isinstance(total, Decimal)
        assert total == items_total


async def test_seed_creates_payments_only_for_paid_orders(
    db_session: AsyncSession, seed_module
) -> None:
    await seed_module.seed(db_session)
    paid_like = {OrderStatus.paid, OrderStatus.shipped, OrderStatus.delivered}
    orders = {o.id: o for o in await db_session.scalars(select(Order))}
    payments = list(await db_session.scalars(select(Payment)))
    assert len(payments) == sum(o.status in paid_like for o in orders.values())
    for p in payments:
        assert orders[p.order_id].status in paid_like
        assert p.amount == orders[p.order_id].total_amount


async def test_seed_is_idempotent(db_session: AsyncSession, seed_module) -> None:
    assert await seed_module.seed(db_session) is True
    assert await seed_module.seed(db_session) is False
    assert await db_session.scalar(select(func.count()).select_from(User)) == 3
    assert await db_session.scalar(select(func.count()).select_from(Order)) == 10

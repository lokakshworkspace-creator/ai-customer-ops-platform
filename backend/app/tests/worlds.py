"""A ready-made cast of users and orders for refund tests. Holds plain ids (never ORM objects):
the executor rolls back on denials, which expires loaded objects."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.enums import OrderStatus, Role
from app.tests.factories import create_order, create_payment, create_user


@dataclass(frozen=True)
class Person:
    id: uuid.UUID
    role: Role


@dataclass(frozen=True)
class World:
    alice: Person
    bob: Person
    admin: Person
    admin2: Person
    order: uuid.UUID  # alice: delivered, 1000.00, paid
    order2: uuid.UUID  # alice: delivered, 3000.00, paid
    old_order: uuid.UUID  # alice: delivered 45 days ago
    shipped_order: uuid.UUID  # alice: shipped (paid)
    unpaid_order: uuid.UUID  # alice: pending, no payment
    bob_order: uuid.UUID  # bob: delivered, 1000.00, paid


async def make_world(session: AsyncSession) -> World:
    alice = await create_user(session, email="alice@example.com")
    bob = await create_user(session, email="bob@example.com")
    admin = await create_user(session, email="admin@example.com", role=Role.admin)
    admin2 = await create_user(session, email="admin2@example.com", role=Role.admin)
    people = {
        n: Person(u.id, u.role)
        for n, u in dict(alice=alice, bob=bob, admin=admin, admin2=admin2).items()
    }

    async def paid_order(
        owner, total: str, status=OrderStatus.delivered, age_days: int = 2
    ) -> uuid.UUID:
        order = await create_order(
            session,
            owner,
            total=total,
            status=status,
            items=[("SKU-1", 1, total)],
            placed_at=datetime.now(UTC) - timedelta(days=age_days),
        )
        await create_payment(session, order)
        return order.id

    order = await paid_order(alice, "1000.00")
    order2 = await paid_order(alice, "3000.00")
    old_order = await paid_order(alice, "1000.00", age_days=45)
    shipped = await paid_order(alice, "1000.00", status=OrderStatus.shipped)
    unpaid = (
        await create_order(
            session, alice, total="1000.00", status=OrderStatus.pending, items=[("S", 1, "1000.00")]
        )
    ).id
    bob_order = await paid_order(bob, "1000.00")
    return World(
        alice=people["alice"],
        bob=people["bob"],
        admin=people["admin"],
        admin2=people["admin2"],
        order=order,
        order2=order2,
        old_order=old_order,
        shipped_order=shipped,
        unpaid_order=unpaid,
        bob_order=bob_order,
    )

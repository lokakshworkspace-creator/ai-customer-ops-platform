"""Seed dev data: 1 admin, 2 customers, 10 orders (with items and payments).

Usage (from the repo root, with the backend venv):
    backend/.venv/Scripts/python scripts/seed.py

Idempotent: does nothing if the seed admin already exists. Refuses to run when APP_ENV=prod.
Passwords come from SEED_PASSWORD (default below) and are for local development only.
"""

import asyncio
import os
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import hash_password
from app.db.models import Order, OrderItem, Payment, User
from app.db.models.enums import OrderStatus, PaymentStatus, Role
from app.db.session import get_engine, get_sessionmaker

DEFAULT_PASSWORD = "password123"

ADMIN = ("admin@example.com", "Ada Admin")
ALICE = ("alice@example.com", "Alice Customer")
BOB = ("bob@example.com", "Bob Customer")

CATALOG: dict[str, tuple[str, Decimal]] = {
    "KB-001": ("Mechanical Keyboard", Decimal("89.99")),
    "MS-002": ("Wireless Mouse", Decimal("29.50")),
    "HB-003": ("USB-C Hub", Decimal("45.00")),
    "MN-004": ("27in Monitor", Decimal("249.00")),
    "CB-005": ("HDMI Cable", Decimal("9.99")),
    "HP-006": ("Headphones", Decimal("129.00")),
    "WC-007": ("Webcam", Decimal("59.95")),
    "ST-008": ("Laptop Stand", Decimal("34.90")),
}

S = OrderStatus
# (customer, status, [(sku, qty), ...], days_ago)
ORDERS: list[tuple[tuple[str, str], OrderStatus, list[tuple[str, int]], int]] = [
    (ALICE, S.delivered, [("KB-001", 1), ("MS-002", 1)], 40),
    (ALICE, S.delivered, [("MN-004", 1), ("CB-005", 2)], 25),
    (ALICE, S.shipped, [("HP-006", 1)], 6),
    (ALICE, S.paid, [("WC-007", 1), ("ST-008", 1)], 2),
    (ALICE, S.pending, [("HB-003", 1)], 0),
    (ALICE, S.cancelled, [("CB-005", 3)], 12),
    (BOB, S.delivered, [("HP-006", 2)], 33),
    (BOB, S.shipped, [("KB-001", 1), ("HB-003", 1)], 4),
    (BOB, S.paid, [("MN-004", 2)], 1),
    (BOB, S.cancelled, [("MS-002", 1)], 18),
]

PAID_STATUSES = {S.paid, S.shipped, S.delivered}


async def seed(session: AsyncSession, password: str = DEFAULT_PASSWORD) -> bool:
    """Insert seed data. Returns False (and changes nothing) if it was already seeded."""
    existing = await session.scalar(select(User.id).where(User.email == ADMIN[0]))
    if existing is not None:
        return False

    password_hash = hash_password(password)
    users: dict[str, User] = {}
    for (email, name), role in ((ADMIN, Role.admin), (ALICE, Role.customer), (BOB, Role.customer)):
        users[email] = User(email=email, full_name=name, role=role, password_hash=password_hash)
    session.add_all(users.values())
    await session.flush()

    now = datetime.now(UTC)
    for n, ((email, _), status, lines, days_ago) in enumerate(ORDERS, start=1):
        items = [
            OrderItem(sku=sku, name=CATALOG[sku][0], quantity=qty, unit_price=CATALOG[sku][1])
            for sku, qty in lines
        ]
        total = sum((i.unit_price * i.quantity for i in items), Decimal("0.00"))
        order = Order(
            user_id=users[email].id,
            status=status,
            total_amount=total,
            currency="USD",
            placed_at=now - timedelta(days=days_ago),
            items=items,
        )
        session.add(order)
        await session.flush()
        if status in PAID_STATUSES:
            session.add(
                Payment(
                    order_id=order.id,
                    amount=total,
                    currency="USD",
                    status=PaymentStatus.succeeded,
                    provider="simulated",
                    provider_ref=f"sim_{n:04d}",
                )
            )
    await session.flush()
    return True


async def main() -> int:
    settings = get_settings()
    if settings.is_prod:
        print("Refusing to seed: APP_ENV=prod", file=sys.stderr)
        return 1

    password = os.environ.get("SEED_PASSWORD", DEFAULT_PASSWORD)
    async with get_sessionmaker()() as session:
        created = await seed(session, password)
        await session.commit()
    await get_engine().dispose()

    if not created:
        print("Already seeded (admin@example.com exists); nothing to do.")
        return 0
    print("Seeded 3 users and 10 orders. Login with:")
    for email, _ in (ADMIN, ALICE, BOB):
        print(f"  {email} / {password}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

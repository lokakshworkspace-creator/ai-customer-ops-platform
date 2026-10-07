"""Orders API + the central authorisation guarantee: a customer can only see their own orders."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.db.models import Order, User
from app.db.models.enums import Role
from app.tests.factories import create_order, create_user
from app.tests.tokens import bearer, forge_token

LIST_URL = "/api/v1/orders"


def auth(user: User) -> dict[str, str]:
    return bearer(create_access_token(user.id)[0])


@dataclass
class World:
    alice: User
    bob: User
    admin: User
    alice_orders: list[Order]
    bob_orders: list[Order]


@pytest.fixture
async def world(db_session: AsyncSession) -> World:
    alice = await create_user(db_session, email="alice@example.com", password="alice-pw-123")
    bob = await create_user(db_session, email="bob@example.com", password="bob-pw-12345")
    admin = await create_user(db_session, email="admin@example.com", role=Role.admin)
    now = datetime.now(UTC)
    alice_orders = [
        await create_order(
            db_session,
            alice,
            total="299.00",
            items=[("CB-005", 1, "299.00")],
            placed_at=now - timedelta(days=3),
        ),
        await create_order(
            db_session,
            alice,
            total="3499.00",
            items=[("HP-006", 1, "3499.00")],
            placed_at=now - timedelta(days=1),
        ),
    ]
    bob_orders = [
        await create_order(
            db_session,
            bob,
            total="4999.00",
            items=[("MN-004", 1, "4999.00")],
            placed_at=now - timedelta(days=5),
        ),
        await create_order(
            db_session,
            bob,
            total="799.00",
            items=[("MS-002", 1, "799.00")],
            placed_at=now - timedelta(days=2),
        ),
        await create_order(
            db_session,
            bob,
            total="1499.00",
            items=[("HB-003", 1, "1499.00")],
            placed_at=now - timedelta(hours=1),
        ),
    ]
    return World(alice, bob, admin, alice_orders, bob_orders)


def ids(orders: list[Order]) -> set[str]:
    return {str(o.id) for o in orders}


# --- the headline guarantee ----------------------------------------------------------------


async def test_customer_a_cannot_read_customer_bs_order(
    db_client: AsyncClient, world: World
) -> None:
    bobs_order = world.bob_orders[0]
    r = await db_client.get(f"{LIST_URL}/{bobs_order.id}", headers=auth(world.alice))
    assert r.status_code == 404
    # Nothing about the order leaks through the error body.
    assert str(bobs_order.id) not in r.text
    assert "4999" not in r.text
    assert "MN-004" not in r.text


async def test_and_the_reverse_direction(db_client: AsyncClient, world: World) -> None:
    r = await db_client.get(f"{LIST_URL}/{world.alice_orders[0].id}", headers=auth(world.bob))
    assert r.status_code == 404


async def test_another_customers_order_looks_exactly_like_a_nonexistent_one(
    db_client: AsyncClient, world: World
) -> None:
    not_yours = await db_client.get(
        f"{LIST_URL}/{world.bob_orders[0].id}", headers=auth(world.alice)
    )
    not_real = await db_client.get(f"{LIST_URL}/{uuid.uuid4()}", headers=auth(world.alice))

    assert not_yours.status_code == not_real.status_code == 404
    assert not_yours.json() == not_real.json()
    for h in ("content-type", "content-length"):
        assert not_yours.headers.get(h) == not_real.headers.get(h)


async def test_customer_as_list_contains_none_of_bs_orders(
    db_client: AsyncClient, world: World
) -> None:
    r = await db_client.get(LIST_URL, headers=auth(world.alice))
    assert r.status_code == 200
    body = r.json()
    returned = {o["id"] for o in body["items"]}
    assert returned == ids(world.alice_orders)
    assert returned.isdisjoint(ids(world.bob_orders))
    assert {o["user_id"] for o in body["items"]} == {str(world.alice.id)}
    assert body["total"] == 2  # the count doesn't leak B's orders either


async def test_each_customer_only_sees_their_own_list(db_client: AsyncClient, world: World) -> None:
    r = await db_client.get(LIST_URL, headers=auth(world.bob))
    body = r.json()
    assert {o["id"] for o in body["items"]} == ids(world.bob_orders)
    assert body["total"] == 3


async def test_customer_can_read_their_own_order(db_client: AsyncClient, world: World) -> None:
    order = world.alice_orders[1]
    r = await db_client.get(f"{LIST_URL}/{order.id}", headers=auth(world.alice))
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == str(order.id)
    assert body["status"] == "paid"
    assert body["currency"] == "INR"
    assert body["total_amount"] == "3499.00"  # exact decimal string, not a float
    assert len(body["items"]) == 1
    assert body["items"][0]["sku"] == "HP-006"
    assert body["items"][0]["unit_price"] == "3499.00"
    assert body["items"][0]["quantity"] == 1


# --- admin ---------------------------------------------------------------------------------


async def test_admin_can_read_any_order(db_client: AsyncClient, world: World) -> None:
    for order in (world.alice_orders[0], world.bob_orders[0]):
        r = await db_client.get(f"{LIST_URL}/{order.id}", headers=auth(world.admin))
        assert r.status_code == 200
        assert r.json()["id"] == str(order.id)


async def test_admin_sees_everyones_orders_in_the_list(
    db_client: AsyncClient, world: World
) -> None:
    r = await db_client.get(LIST_URL, headers=auth(world.admin))
    body = r.json()
    assert {o["id"] for o in body["items"]} == ids(world.alice_orders) | ids(world.bob_orders)
    assert body["total"] == 5


async def test_admin_gets_404_for_a_truly_nonexistent_order(
    db_client: AsyncClient, world: World
) -> None:
    r = await db_client.get(f"{LIST_URL}/{uuid.uuid4()}", headers=auth(world.admin))
    assert r.status_code == 404


# --- authentication / token edge cases -----------------------------------------------------


async def test_unauthenticated_requests_get_401(db_client: AsyncClient, world: World) -> None:
    assert (await db_client.get(LIST_URL)).status_code == 401
    assert (await db_client.get(f"{LIST_URL}/{world.alice_orders[0].id}")).status_code == 401
    assert (
        await db_client.get(LIST_URL, headers={"Authorization": "Bearer nope"})
    ).status_code == 401


async def test_forged_admin_claim_gives_no_extra_visibility(
    db_client: AsyncClient, world: World
) -> None:
    headers = bearer(forge_token(world.alice.id, extra={"role": "admin"}))
    one = await db_client.get(f"{LIST_URL}/{world.bob_orders[0].id}", headers=headers)
    assert one.status_code == 404
    listing = await db_client.get(LIST_URL, headers=headers)
    assert {o["id"] for o in listing.json()["items"]} == ids(world.alice_orders)


async def test_losing_admin_role_takes_effect_on_the_next_request(
    db_client: AsyncClient, db_session: AsyncSession, world: World
) -> None:
    headers = auth(world.admin)  # one token, reused
    url = f"{LIST_URL}/{world.bob_orders[0].id}"
    assert (await db_client.get(url, headers=headers)).status_code == 200

    world.admin.role = Role.customer
    await db_session.flush()
    assert (await db_client.get(url, headers=headers)).status_code == 404


async def test_deactivated_user_loses_access_immediately(
    db_client: AsyncClient, db_session: AsyncSession, world: World
) -> None:
    headers = auth(world.alice)
    assert (await db_client.get(LIST_URL, headers=headers)).status_code == 200
    world.alice.is_active = False
    await db_session.flush()
    assert (await db_client.get(LIST_URL, headers=headers)).status_code == 401


async def test_full_flow_login_then_list(db_client: AsyncClient, world: World) -> None:
    login = await db_client.post(
        "/api/v1/auth/login", json={"email": "alice@example.com", "password": "alice-pw-123"}
    )
    assert login.status_code == 200
    headers = bearer(login.json()["access_token"])

    r = await db_client.get(LIST_URL, headers=headers)
    assert r.status_code == 200
    assert {o["id"] for o in r.json()["items"]} == ids(world.alice_orders)


# --- input validation ----------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["not-a-uuid", "123", "00000000-0000-0000-0000-00000000000g"])
async def test_invalid_uuid_is_422(db_client: AsyncClient, world: World, bad: str) -> None:
    r = await db_client.get(f"{LIST_URL}/{bad}", headers=auth(world.alice))
    assert r.status_code == 422


async def test_invalid_uuid_without_a_token_is_still_401_not_422(
    db_client: AsyncClient, world: World
) -> None:
    # Authentication runs before anything about the resource is revealed or validated.
    assert (await db_client.get(f"{LIST_URL}/not-a-uuid")).status_code == 401


# --- pagination ----------------------------------------------------------------------------


@pytest.fixture
async def many(db_session: AsyncSession, world: World) -> list[Order]:
    base = datetime.now(UTC) - timedelta(days=100)
    orders = [
        await create_order(db_session, world.alice, placed_at=base + timedelta(hours=i))
        for i in range(25)
    ]
    # Plus alice's 2 from `world`: 27 in total. Bob's orders must never appear.
    return orders


async def test_default_limit_is_20_and_total_is_reported(
    db_client: AsyncClient, world: World, many: list[Order]
) -> None:
    body = (await db_client.get(LIST_URL, headers=auth(world.alice))).json()
    assert body["limit"] == 20
    assert body["offset"] == 0
    assert body["total"] == 27
    assert len(body["items"]) == 20


async def test_newest_first_ordering(
    db_client: AsyncClient, world: World, many: list[Order]
) -> None:
    body = (await db_client.get(f"{LIST_URL}?limit=100", headers=auth(world.alice))).json()
    placed = [datetime.fromisoformat(o["placed_at"]) for o in body["items"]]
    assert placed == sorted(placed, reverse=True)
    assert body["items"][0]["id"] == str(world.alice_orders[1].id)  # placed 1 day ago


async def test_pages_do_not_overlap_and_cover_everything(
    db_client: AsyncClient, world: World, many: list[Order]
) -> None:
    seen: list[str] = []
    for offset in (0, 10, 20):
        r = await db_client.get(f"{LIST_URL}?limit=10&offset={offset}", headers=auth(world.alice))
        seen += [o["id"] for o in r.json()["items"]]
    assert len(seen) == 27
    assert len(set(seen)) == 27
    assert set(seen) == ids(many) | ids(world.alice_orders)
    assert set(seen).isdisjoint(ids(world.bob_orders))


async def test_offset_past_the_end_is_an_empty_page(db_client: AsyncClient, world: World) -> None:
    body = (await db_client.get(f"{LIST_URL}?offset=1000", headers=auth(world.alice))).json()
    assert body["items"] == []
    assert body["total"] == 2


async def test_max_limit_is_accepted(
    db_client: AsyncClient, world: World, many: list[Order]
) -> None:
    r = await db_client.get(f"{LIST_URL}?limit=100", headers=auth(world.alice))
    assert r.status_code == 200
    assert len(r.json()["items"]) == 27


@pytest.mark.parametrize(
    "query", ["limit=101", "limit=1000", "limit=0", "limit=-1", "offset=-1", "limit=abc"]
)
async def test_pagination_limits_are_enforced(
    db_client: AsyncClient, world: World, query: str
) -> None:
    r = await db_client.get(f"{LIST_URL}?{query}", headers=auth(world.alice))
    assert r.status_code == 422

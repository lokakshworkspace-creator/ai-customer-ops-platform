"""The admin list's order: `pending` is a queue (oldest first), the decided tabs are a history
(newest decision first). Done in modules/refunds/approvals_view.py: the repository's own
`list_approvals` is untouched."""

import uuid

import pytest
from sqlalchemy import text

from app.modules.refunds import expiry
from app.tests.test_approvals_api import APPROVALS, decide, pending
from app.tests.tokens import auth_for
from app.tests.worlds import World, make_world


@pytest.fixture
async def world(db_session) -> World:
    return await make_world(db_session)


async def _ids(client, who, status: str, query: str = "") -> list[str]:
    r = await client.get(f"{APPROVALS}?status={status}{query}", headers=auth_for(who.id))
    assert r.status_code == 200, r.text
    return [i["id"] for i in r.json()["items"]]


async def _set(db_session, approval_id: str, **hours_ago: float) -> None:
    """created_at / decided_at = now() minus N hours, on this approval only."""
    for column, hours in hours_ago.items():
        await db_session.execute(
            text(
                f"UPDATE approvals SET {column} = now() - make_interval(secs => :s) WHERE id = :i"
            ),
            {"s": hours * 3600, "i": uuid.UUID(approval_id)},
        )
    await db_session.commit()  # only releases a savepoint in these tests


async def _three_pending(client, db_session, world: World) -> list[str]:
    """Three pending approvals on the 3000.00 order, 30, 20 and 10 hours old (oldest first)."""
    ids = []
    for hours in (30, 20, 10):
        _, approval_id = await pending(client, world.alice, world.order2, "700.00")
        await _set(db_session, approval_id, created_at=hours)
        ids.append(approval_id)
    return ids


async def test_pending_is_a_queue_oldest_first(refund_client, db_session, world) -> None:
    oldest, middle, newest = await _three_pending(refund_client, db_session, world)
    assert await _ids(refund_client, world.admin, "pending") == [oldest, middle, newest]


async def test_expired_is_a_history_newest_expiry_first(refund_client, db_session, world) -> None:
    a, b, c = await _three_pending(refund_client, db_session, world)
    for approval_id in (a, b, c):
        await _set(db_session, approval_id, created_at=60)  # all overdue
        assert await expiry.expire_one(db_session, uuid.UUID(approval_id)) is True
    # expired at different times: b first (oldest expiry), then c, then a (the newest)
    await _set(db_session, b, decided_at=5)
    await _set(db_session, c, decided_at=3)
    await _set(db_session, a, decided_at=1)
    assert await _ids(refund_client, world.admin, "expired") == [a, c, b]


@pytest.mark.parametrize(("verb", "status"), [("approve", "approved"), ("reject", "rejected")])
async def test_approved_and_rejected_are_newest_decision_first_too(
    refund_client, db_session, world, verb: str, status: str
) -> None:
    a, b, c = await _three_pending(refund_client, db_session, world)
    for approval_id in (a, b, c):
        assert (await decide(refund_client, world.admin, approval_id, verb)).status_code == 200
    await _set(db_session, a, decided_at=9)
    await _set(db_session, b, decided_at=2)
    await _set(db_session, c, decided_at=6)
    assert await _ids(refund_client, world.admin, status) == [b, c, a]


async def test_the_order_does_not_follow_creation_time_for_decided_tabs(
    refund_client, db_session, world
) -> None:
    """An approval created FIRST but decided LAST is on top of the history (not at the bottom)."""
    first, second, _ = await _three_pending(refund_client, db_session, world)
    assert (await decide(refund_client, world.admin, second, "reject")).status_code == 200
    assert (await decide(refund_client, world.admin, first, "reject")).status_code == 200
    await _set(db_session, second, decided_at=4)
    await _set(db_session, first, decided_at=1)
    assert await _ids(refund_client, world.admin, "rejected") == [first, second]


async def test_paging_covers_every_row_once_in_both_orders(
    refund_client, db_session, world
) -> None:
    ids = await _three_pending(refund_client, db_session, world)
    p1 = await _ids(refund_client, world.admin, "pending", "&limit=2&offset=0")
    p2 = await _ids(refund_client, world.admin, "pending", "&limit=2&offset=2")
    assert p1 + p2 == ids  # queue order, no row twice, none missing

    for approval_id in ids:
        await _set(db_session, approval_id, created_at=60)
        await expiry.expire_one(db_session, uuid.UUID(approval_id))
    full = await _ids(refund_client, world.admin, "expired")
    h1 = await _ids(refund_client, world.admin, "expired", "&limit=2&offset=0")
    h2 = await _ids(refund_client, world.admin, "expired", "&limit=2&offset=2")
    assert h1 + h2 == full and sorted(full) == sorted(ids)


async def test_the_total_and_the_other_tabs_are_unchanged(refund_client, db_session, world) -> None:
    ids = await _three_pending(refund_client, db_session, world)
    r = await refund_client.get(
        f"{APPROVALS}?status=pending&limit=1", headers=auth_for(world.admin.id)
    )
    assert r.json()["total"] == 3 and len(r.json()["items"]) == 1
    assert await _ids(refund_client, world.admin, "expired") == []
    assert await _ids(refund_client, world.admin, "approved") == []
    assert (await decide(refund_client, world.admin, ids[0], "approve")).status_code == 200
    assert await _ids(refund_client, world.admin, "pending") == ids[1:]

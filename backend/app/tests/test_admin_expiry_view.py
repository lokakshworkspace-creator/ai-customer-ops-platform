"""What the admin page sees of expiry: the deadline on a pending approval, the Expired tab, and the
backend's refusal when someone decides an expired (or overdue) approval over HTTP."""

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.modules.refunds import expiry
from app.tests.test_approvals_api import APPROVALS, decide, pending
from app.tests.tokens import auth_for
from app.tests.worlds import World, make_world


@pytest.fixture
async def world(db_session) -> World:
    return await make_world(db_session)


async def _backdate(db_session, approval_id: str, hours: float) -> None:
    await db_session.execute(
        text("UPDATE approvals SET created_at = now() - make_interval(secs => :s) WHERE id = :i"),
        {"s": hours * 3600, "i": uuid.UUID(approval_id)},
    )
    await db_session.commit()  # only releases a savepoint in these tests


async def _list(client, who, status: str):
    r = await client.get(f"{APPROVALS}?status={status}", headers=auth_for(who.id))
    assert r.status_code == 200
    return r.json()["items"]


async def test_a_pending_approval_shows_its_deadline(refund_client, world) -> None:
    _, approval_id = await pending(refund_client, world.alice, world.order, "700.00")
    (item,) = await _list(refund_client, world.admin, "pending")
    created = datetime.fromisoformat(item["created_at"])
    assert datetime.fromisoformat(item["expires_at"]) == created + timedelta(hours=48)
    assert item["id"] == approval_id


async def test_the_deadline_follows_the_setting(
    refund_client, world, monkeypatch: pytest.MonkeyPatch
) -> None:
    await pending(refund_client, world.alice, world.order, "700.00")
    monkeypatch.setattr(get_settings(), "approval_ttl_hours", 5)
    (item,) = await _list(refund_client, world.admin, "pending")
    created = datetime.fromisoformat(item["created_at"])
    assert datetime.fromisoformat(item["expires_at"]) == created + timedelta(hours=5)


async def test_an_expired_approval_moves_to_the_expired_tab_with_its_story(
    refund_client, db_session, world
) -> None:
    refund_id, approval_id = await pending(refund_client, world.alice, world.order, "700.00")
    await _backdate(db_session, approval_id, 50)
    assert await expiry.expire_one(db_session, uuid.UUID(approval_id)) is True

    assert await _list(refund_client, world.admin, "pending") == []
    (item,) = await _list(refund_client, world.admin, "expired")
    assert item["id"] == approval_id
    assert item["status"] == "expired"
    assert item["decided_by"] is None  # nobody decided
    assert item["decided_at"] is not None
    assert "48 hours" in item["decision_reason"]
    assert item["refund"]["status"] == "expired"
    created = datetime.fromisoformat(item["created_at"])
    assert datetime.fromisoformat(item["expires_at"]) == created + timedelta(hours=48)
    assert item["review_reasons"]  # why it had needed a person is still on the card
    assert await _list(refund_client, world.admin, "approved") == []
    assert await _list(refund_client, world.admin, "rejected") == []


async def test_a_decided_approval_has_no_deadline_to_show(refund_client, world) -> None:
    _, approval_id = await pending(refund_client, world.alice, world.order, "700.00")
    assert (await decide(refund_client, world.admin, approval_id, "approve")).status_code == 200
    (item,) = await _list(refund_client, world.admin, "approved")
    assert item["expires_at"] is None


@pytest.mark.parametrize("verb", ["approve", "reject"])
async def test_deciding_an_expired_approval_over_http_is_a_409_and_pays_nothing(
    refund_client, db_session, world, provider, verb: str
) -> None:
    _, approval_id = await pending(refund_client, world.alice, world.order, "700.00")
    await _backdate(db_session, approval_id, 50)
    await expiry.expire_one(db_session, uuid.UUID(approval_id))

    r = await decide(refund_client, world.admin, approval_id, verb)
    assert r.status_code == 409
    assert r.json()["code"] == "APPROVAL_EXPIRED"
    assert "expired" in r.json()["detail"]
    assert provider.calls == 0 and provider.ledger == {}  # nothing was paid out
    (item,) = await _list(refund_client, world.admin, "expired")
    assert item["status"] == "expired"


@pytest.mark.parametrize("verb", ["approve", "reject"])
async def test_deciding_an_overdue_approval_the_sweeper_has_not_reached_is_also_a_409(
    refund_client, db_session, world, verb: str
) -> None:
    _, approval_id = await pending(refund_client, world.alice, world.order, "700.00")
    await _backdate(db_session, approval_id, 49)
    r = await decide(refund_client, world.admin, approval_id, verb)
    assert r.status_code == 409 and r.json()["code"] == "APPROVAL_EXPIRED"
    (item,) = await _list(refund_client, world.admin, "pending")  # still pending until swept
    assert item["id"] == approval_id


async def test_a_customer_still_cannot_decide_an_approval(refund_client, world) -> None:
    _, approval_id = await pending(refund_client, world.alice, world.order, "700.00")
    r = await decide(refund_client, world.alice, approval_id, "approve")
    assert r.status_code == 403

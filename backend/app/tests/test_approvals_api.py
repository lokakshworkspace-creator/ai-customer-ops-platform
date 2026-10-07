"""Admin approve / reject endpoints and the refund state transitions behind them. Real auth."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Approval, AuditLog, Order, OutboxEvent, Refund, ToolCall
from app.db.models.enums import ApprovalStatus, RefundStatus, ToolCallStatus
from app.tests.tokens import auth_for
from app.tests.worlds import Person, World, make_world

REFUNDS = "/api/v1/refunds"
APPROVALS = "/api/v1/approvals"


@pytest.fixture
async def world(db_session: AsyncSession) -> World:
    return await make_world(db_session)


async def request_refund(
    client: AsyncClient,
    who: Person,
    order: uuid.UUID,
    amount: str = "800.00",
    reason: str = "damaged",
) -> Response:
    h = {**auth_for(who.id), "Idempotency-Key": f"key-{uuid.uuid4().hex}"}
    payload = {"order_id": str(order), "amount": amount, "reason_code": reason}
    return await client.post(REFUNDS, json=payload, headers=h)


async def pending(client: AsyncClient, who: Person, order: uuid.UUID, amount: str = "800.00"):
    r = await request_refund(client, who, order, amount)
    assert r.status_code == 202, r.text
    return r.json()["refund"]["id"], r.json()["approval_id"]


async def decide(client: AsyncClient, who: Person, approval_id: str, verb: str, **json) -> Response:
    return await client.post(
        f"{APPROVALS}/{approval_id}/{verb}", json=json or None, headers=auth_for(who.id)
    )


async def rows(session: AsyncSession, model: type) -> list:
    return list(await session.scalars(select(model).execution_options(populate_existing=True)))


async def event_types(session: AsyncSession) -> list[str]:
    return list(await session.scalars(select(OutboxEvent.event_type).order_by(OutboxEvent.seq)))


async def refund_status(session: AsyncSession, refund_id: str) -> RefundStatus:
    r = await session.scalar(
        select(Refund)
        .where(Refund.id == uuid.UUID(refund_id))
        .execution_options(populate_existing=True)
    )
    return r.status


# --- approve -------------------------------------------------------------------------------


async def test_approve_pays_out_and_records_everything(
    refund_client, db_session, world, provider
) -> None:
    refund_id, approval_id = await pending(refund_client, world.alice, world.order)
    assert provider.calls == 0

    r = await decide(
        refund_client, world.admin, approval_id, "approve", note="looked at the photos"
    )
    assert r.status_code == 200
    j = r.json()
    assert j["refund"]["status"] == "succeeded" and j["replayed"] is False
    assert (j["policy"]["decision"], j["policy"]["reason_code"]) == (
        "ALLOW",
        "ADMIN_ACTION_ALLOWED",
    )

    (approval,) = await rows(db_session, Approval)
    assert approval.status == ApprovalStatus.approved and approval.decided_by == world.admin.id
    assert approval.decided_at is not None and approval.decision_reason == "looked at the photos"
    assert list(provider.ledger) == [f"refund-{refund_id}"]
    assert await event_types(db_session) == [
        "refund.requested",
        "refund.approved",
        "refund.completed",
    ]
    approved = (await rows(db_session, OutboxEvent))[1]
    assert approved.payload["data"]["auto"] is False
    assert approved.payload["data"]["approved_by"] == str(world.admin.id)
    audits = {(a.action, a.actor_user_id) for a in await rows(db_session, AuditLog)}
    assert ("refund.approved", world.admin.id) in audits
    call = [c for c in await rows(db_session, ToolCall) if c.tool_name == "approve_refund"][0]
    assert call.status == ToolCallStatus.succeeded and call.user_id == world.admin.id


async def test_approving_a_payout_that_is_declined_keeps_the_decision_and_shows_failed(
    refund_client, db_session, world, provider
) -> None:
    refund_id, approval_id = await pending(refund_client, world.alice, world.order)
    provider.fail_next()
    r = await decide(refund_client, world.admin, approval_id, "approve")
    assert r.status_code == 200 and r.json()["refund"]["status"] == "failed"
    assert r.json()["error"]["code"] == "PAYOUT_FAILED"
    (approval,) = await rows(db_session, Approval)
    assert approval.status == ApprovalStatus.approved  # the human decision stands
    assert (await event_types(db_session))[-1] == "refund.failed"


async def test_approving_with_an_unconfirmed_payout_is_202_and_leaves_processing(
    refund_client, db_session, world, provider
) -> None:
    refund_id, approval_id = await pending(refund_client, world.alice, world.order)
    provider.unavailable_next()
    r = await decide(refund_client, world.admin, approval_id, "approve")
    assert r.status_code == 202 and r.json()["refund"]["status"] == "processing"
    assert await refund_status(db_session, refund_id) == RefundStatus.processing


# --- reject ------------------------------------------------------------------------------


async def test_reject_closes_the_refund_without_paying_and_frees_the_amount(
    refund_client, db_session, world, provider
) -> None:
    refund_id, approval_id = await pending(refund_client, world.alice, world.order)
    r = await decide(refund_client, world.admin, approval_id, "reject", note="not eligible")
    assert r.status_code == 200 and r.json()["refund"]["status"] == "rejected"

    (approval,) = await rows(db_session, Approval)
    assert approval.status == ApprovalStatus.rejected and approval.decision_reason == "not eligible"
    assert provider.calls == 0
    assert await event_types(db_session) == ["refund.requested", "refund.rejected"]
    # the rejected 800 no longer reserves the order: a new 900 request is not "over remaining"
    again = await request_refund(refund_client, world.alice, world.order, "900.00")
    assert again.status_code == 202


# --- state machine ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("first", "second"),
    [("approve", "approve"), ("approve", "reject"), ("reject", "approve"), ("reject", "reject")],
)
async def test_an_approval_can_only_be_decided_once(
    refund_client, world, provider, first, second
) -> None:
    refund_id, approval_id = await pending(refund_client, world.alice, world.order)
    assert (await decide(refund_client, world.admin, approval_id, first)).status_code == 200
    r = await decide(refund_client, world.admin2, approval_id, second)
    assert r.status_code == 409 and r.json()["code"] == "ALREADY_DECIDED"
    assert provider.calls <= 1 and len(provider.ledger) <= 1


async def test_unknown_and_malformed_approvals(refund_client, world) -> None:
    assert (
        await decide(refund_client, world.admin, str(uuid.uuid4()), "approve")
    ).status_code == 404
    bad = await refund_client.post(
        f"{APPROVALS}/not-a-uuid/approve", headers=auth_for(world.admin.id)
    )
    assert bad.status_code == 422


async def test_unknown_body_fields_are_rejected(refund_client, world) -> None:
    _, approval_id = await pending(refund_client, world.alice, world.order)
    r = await decide(
        refund_client, world.admin, approval_id, "approve", approved_by=str(world.bob.id)
    )
    assert r.status_code == 422


# --- who may decide -----------------------------------------------------------------------------


@pytest.mark.parametrize("verb", ["approve", "reject"])
async def test_a_customer_cannot_decide_and_the_attempt_is_logged(
    refund_client, db_session, world, provider, verb
) -> None:
    refund_id, approval_id = await pending(refund_client, world.alice, world.order)
    r = await decide(refund_client, world.alice, approval_id, verb)  # her own refund, still no
    assert r.status_code == 403 and r.json()["code"] == "ROLE_NOT_PERMITTED"
    assert await refund_status(db_session, refund_id) == RefundStatus.pending_approval
    assert provider.calls == 0
    calls = [
        c
        for c in await rows(db_session, ToolCall)
        if c.tool_name.endswith("_refund") and c.tool_name != "request_refund"
    ]
    assert len(calls) == 1 and calls[0].status == ToolCallStatus.denied
    assert (
        calls[0].policy_reason_code == "ROLE_NOT_PERMITTED" and calls[0].user_id == world.alice.id
    )


async def test_an_admin_cannot_approve_their_own_request(
    refund_client, db_session, world, provider
) -> None:
    refund_id, approval_id = await pending(
        refund_client, world.admin, world.order
    )  # admin files it
    r = await decide(refund_client, world.admin, approval_id, "approve")
    assert (r.status_code, r.json()["code"]) == (403, "SELF_APPROVAL")
    assert await refund_status(db_session, refund_id) == RefundStatus.pending_approval
    assert provider.calls == 0
    # ...but a DIFFERENT admin can
    ok = await decide(refund_client, world.admin2, approval_id, "approve")
    assert ok.status_code == 200 and ok.json()["refund"]["status"] == "succeeded"


async def test_an_admin_may_withdraw_their_own_request_by_rejecting_it(
    refund_client, world
) -> None:
    _, approval_id = await pending(refund_client, world.admin, world.order)
    assert (await decide(refund_client, world.admin, approval_id, "reject")).status_code == 200


async def test_unauthenticated_cannot_decide(refund_client, world) -> None:
    _, approval_id = await pending(refund_client, world.alice, world.order)
    assert (await refund_client.post(f"{APPROVALS}/{approval_id}/approve")).status_code == 401


# --- hard rules are re-checked at approval time -----------------------------------------------


async def test_approval_is_denied_if_the_order_has_since_aged_out_of_the_window(
    refund_client, db_session, world, provider
) -> None:
    refund_id, approval_id = await pending(refund_client, world.alice, world.order)
    await db_session.execute(
        update(Order)
        .where(Order.id == world.order)
        .values(placed_at=datetime.now(UTC) - timedelta(days=31))
    )
    r = await decide(refund_client, world.admin, approval_id, "approve")
    assert (r.status_code, r.json()["code"]) == (403, "ORDER_TOO_OLD")
    assert provider.calls == 0
    assert await refund_status(db_session, refund_id) == RefundStatus.pending_approval  # still open
    (approval,) = await rows(db_session, Approval)
    assert approval.status == ApprovalStatus.pending
    # the admin can still close it out
    assert (await decide(refund_client, world.admin, approval_id, "reject")).status_code == 200


# --- listing -----------------------------------------------------------------------------------


async def test_admin_lists_pending_approvals_and_customers_cannot(refund_client, world) -> None:
    _, approval_id = await pending(refund_client, world.alice, world.order)
    await pending(refund_client, world.bob, world.bob_order)
    r = await refund_client.get(APPROVALS, headers=auth_for(world.admin.id))
    assert r.status_code == 200 and r.json()["total"] == 2
    assert approval_id in {i["id"] for i in r.json()["items"]}
    assert (await refund_client.get(APPROVALS, headers=auth_for(world.alice.id))).status_code == 403
    assert (await refund_client.get(APPROVALS)).status_code == 401


async def test_listing_filters_by_status_and_validates_paging(refund_client, world) -> None:
    _, approval_id = await pending(refund_client, world.alice, world.order)
    await decide(refund_client, world.admin, approval_id, "reject")
    h = auth_for(world.admin.id)
    assert (await refund_client.get(APPROVALS, headers=h)).json()["total"] == 0
    assert (await refund_client.get(f"{APPROVALS}?status=rejected", headers=h)).json()["total"] == 1
    for q in ("limit=0", "limit=101", "offset=-1", "status=bogus"):
        assert (await refund_client.get(f"{APPROVALS}?{q}", headers=h)).status_code == 422


async def test_every_decision_attempt_is_logged(refund_client, db_session, world) -> None:
    _, approval_id = await pending(refund_client, world.alice, world.order)
    await decide(refund_client, world.alice, approval_id, "approve")  # denied
    await decide(refund_client, world.admin, approval_id, "approve")  # ok
    await decide(refund_client, world.admin, approval_id, "approve")  # 409
    statuses = [
        c.status for c in await rows(db_session, ToolCall) if c.tool_name == "approve_refund"
    ]
    assert sorted(s.value for s in statuses) == ["denied", "failed", "succeeded"]
    assert await db_session.scalar(select(func.count()).select_from(AuditLog)) >= 4

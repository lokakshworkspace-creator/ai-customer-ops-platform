"""POST /api/v1/refunds and GET /api/v1/refunds/{id}, end to end: the real app, the real
get_current_user dependency, the executor, policy, the outbox and a simulated provider."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Approval, AuditLog, Order, OutboxEvent, Refund, ToolCall, User
from app.db.models.enums import RefundStatus, ToolCallStatus
from app.tests.tokens import auth_for, bearer, forge_token
from app.tests.worlds import Person, World, make_world

URL = "/api/v1/refunds"


@pytest.fixture
async def world(db_session: AsyncSession) -> World:
    return await make_world(db_session)


def body(order: uuid.UUID, amount: str = "100.00", reason: str = "damaged", **extra: Any) -> dict:
    return {"order_id": str(order), "amount": amount, "reason_code": reason, **extra}


async def post(
    client: AsyncClient, who: Person, payload: dict, key: str | None = None, **headers: str
) -> Response:
    h = {**auth_for(who.id), "Idempotency-Key": key or f"key-{uuid.uuid4().hex}", **headers}
    return await client.post(URL, json=payload, headers=h)


async def rows(session: AsyncSession, model: type) -> list:
    return list(await session.scalars(select(model).execution_options(populate_existing=True)))


async def count(session: AsyncSession, model: type) -> int:
    return await session.scalar(select(func.count()).select_from(model)) or 0


async def event_types(session: AsyncSession, aggregate_id: str | None = None) -> list[str]:
    q = select(OutboxEvent.event_type).order_by(OutboxEvent.seq)
    if aggregate_id:
        q = q.where(OutboxEvent.aggregate_id == aggregate_id)
    return list(await session.scalars(q))


# --- ALLOW: paid immediately -----------------------------------------------------------


async def test_small_refund_is_paid_immediately(refund_client, db_session, world, provider) -> None:
    r = await post(refund_client, world.alice, body(world.order, "250.00"))
    assert r.status_code == 201
    j = r.json()
    assert j["replayed"] is False and j["approval_id"] is None
    assert j["refund"]["status"] == "succeeded" and j["refund"]["amount"] == "250.00"
    assert j["refund"]["currency"] == "INR" and j["refund"]["processed_at"]
    assert (j["policy"]["decision"], j["policy"]["reason_code"]) == ("ALLOW", "REFUND_ALLOWED")
    assert "Idempotent-Replayed" not in r.headers

    (refund,) = await rows(db_session, Refund)
    assert refund.status == RefundStatus.succeeded and refund.requested_by == world.alice.id
    assert len(refund.request_hash) == 64 and refund.reason_code == "damaged"
    # the provider was paid once, keyed by the refund id (never the user's own key)
    assert list(provider.ledger) == [f"refund-{refund.id}"]
    assert len(provider.succeeded_entries) == 1
    # outbox: written with the data change, in order
    assert await event_types(db_session) == [
        "refund.requested",
        "refund.approved",
        "refund.completed",
    ]
    approved = (await rows(db_session, OutboxEvent))[1]
    assert approved.payload["data"]["auto"] is True
    # audit and tool-call log
    actions = {a.action for a in await rows(db_session, AuditLog)}
    assert {"refund.requested", "refund.completed", "tool.request_refund"} <= actions
    (call,) = await rows(db_session, ToolCall)
    assert call.tool_name == "request_refund" and call.status == ToolCallStatus.succeeded
    assert call.policy_reason_code == "REFUND_ALLOWED" and call.user_id == world.alice.id
    assert "user_id" not in call.arguments


@pytest.mark.parametrize(
    ("amount", "status", "refund_status"),
    [("500.00", 201, "succeeded"), ("500", 201, "succeeded"), ("500.01", 202, "pending_approval")],
)
async def test_the_automatic_limit_boundary(
    refund_client, world, amount, status, refund_status
) -> None:
    r = await post(refund_client, world.alice, body(world.order, amount))
    assert (r.status_code, r.json()["refund"]["status"]) == (status, refund_status)


# --- REQUIRE_APPROVAL ----------------------------------------------------------------------


async def test_large_refund_waits_for_approval_and_does_not_touch_the_provider(
    refund_client, db_session, world, provider
) -> None:
    r = await post(refund_client, world.alice, body(world.order, "800.00"))
    assert r.status_code == 202
    j = r.json()
    assert j["refund"]["status"] == "pending_approval" and j["approval_id"]
    assert j["policy"]["decision"] == "REQUIRE_APPROVAL"
    assert j["policy"]["reason_code"] == "REFUND_ABOVE_AUTO_LIMIT"

    assert provider.calls == 0 and provider.ledger == {}
    assert await event_types(db_session) == ["refund.requested"]
    (approval,) = await rows(db_session, Approval)
    assert str(approval.id) == j["approval_id"] and approval.status.value == "pending"
    assert approval.requested_by == world.alice.id and approval.payload["amount"] == "800.00"
    assert approval.tool_call_id is not None
    (call,) = await rows(db_session, ToolCall)
    assert call.status == ToolCallStatus.awaiting_approval


async def test_a_reason_that_needs_review_waits_for_approval(refund_client, world) -> None:
    r = await post(refund_client, world.alice, body(world.order, "100.00", "changed_mind"))
    assert r.status_code == 202 and r.json()["policy"]["reason_code"] == "REASON_NEEDS_REVIEW"


# --- DENY ------------------------------------------------------------------------------------


async def test_a_50000_rupee_refund_is_denied_and_creates_nothing(
    refund_client, db_session, world, provider
) -> None:
    r = await post(refund_client, world.alice, body(world.order, "50000.00"))
    assert r.status_code == 403
    j = r.json()
    assert j["code"] == "AMOUNT_EXCEEDS_ORDER" and j["decision"] == "DENY"
    assert "exceeds the order total" in j["detail"] and j["tool_call_id"]
    assert await count(db_session, Refund) == 0 and await count(db_session, Approval) == 0
    assert await count(db_session, OutboxEvent) == 0 and provider.calls == 0
    (call,) = await rows(db_session, ToolCall)  # the denial is on record
    assert call.status == ToolCallStatus.denied
    assert (
        call.policy_reason_code == "AMOUNT_EXCEEDS_ORDER" and call.policy_decision.value == "DENY"
    )
    audit = [a for a in await rows(db_session, AuditLog) if a.action == "tool.request_refund"]
    assert audit and audit[0].decision.value == "DENY"


@pytest.mark.parametrize(
    ("which", "overrides", "code"),
    [
        ("old_order", {}, "ORDER_TOO_OLD"),
        ("shipped_order", {}, "ORDER_NOT_REFUNDABLE"),
        ("unpaid_order", {}, "ORDER_NOT_REFUNDABLE"),
        ("order", {"reason": "because"}, "INVALID_REASON"),
        ("order", {"amount": "1000.01"}, "AMOUNT_EXCEEDS_ORDER"),
    ],
)
async def test_policy_denials(refund_client, db_session, world, which, overrides, code) -> None:
    payload = body(
        getattr(world, which), overrides.get("amount", "100.00"), overrides.get("reason", "damaged")
    )
    r = await post(refund_client, world.alice, payload)
    assert (r.status_code, r.json()["code"]) == (403, code)
    assert await count(db_session, Refund) == 0


# --- authorization / authentication -------------------------------------------------------


async def test_another_customers_order_is_404_identical_to_a_missing_one(
    refund_client, db_session, world, provider
) -> None:
    theirs = await post(refund_client, world.alice, body(world.bob_order))
    nothing = await post(refund_client, world.alice, body(uuid.uuid4()))
    for r in (theirs, nothing):
        assert (r.status_code, r.json()["code"], r.json()["detail"]) == (
            404,
            "NOT_FOUND",
            "Order not found",
        )
    assert await count(db_session, Refund) == 0 and provider.calls == 0
    calls = await rows(db_session, ToolCall)
    assert [c.status for c in calls] == [ToolCallStatus.failed] * 2
    assert {c.error_code for c in calls} == {"NOT_FOUND"}


async def test_unauthenticated_requests_get_401(refund_client, world) -> None:
    payload, h = body(world.order), {"Idempotency-Key": "key-unauth-0001"}
    assert (await refund_client.post(URL, json=payload, headers=h)).status_code == 401
    bad = {**h, **bearer("garbage")}
    assert (await refund_client.post(URL, json=payload, headers=bad)).status_code == 401
    forged = {**h, **bearer(forge_token(world.alice.id, secret="x" * 40))}
    assert (await refund_client.post(URL, json=payload, headers=forged)).status_code == 401
    assert (await refund_client.get(f"{URL}/{uuid.uuid4()}")).status_code == 401


async def test_deactivated_user_is_rejected_by_the_real_auth_dependency(
    refund_client, db_session, world
) -> None:
    await db_session.execute(update(User).where(User.id == world.alice.id).values(is_active=False))
    r = await post(refund_client, world.alice, body(world.order))
    assert r.status_code == 401


async def test_admin_can_file_a_refund_for_a_customers_order(
    refund_client, db_session, world
) -> None:
    r = await post(refund_client, world.admin, body(world.order, "100.00"))
    assert r.status_code == 201
    (refund,) = await rows(db_session, Refund)
    assert refund.requested_by == world.admin.id  # the admin is the requester...
    order = await db_session.get(Order, world.order)
    assert order.user_id == world.alice.id  # ...on the customer's order


# --- request validation -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        None,
        "",
        "short",
        "a" * 256,
        "has space in it",
        "semi;colon-key",
        "slash/in-the-key",
        "ünï-çode-key1",
    ],
)
async def test_idempotency_key_is_required_and_validated(
    refund_client, db_session, world, key
) -> None:
    h: dict = auth_for(world.alice.id)
    if key is not None:
        h["Idempotency-Key"] = key.encode("latin-1")  # httpx refuses non-ASCII str headers
    r = await refund_client.post(URL, json=body(world.order), headers=h)
    assert r.status_code == 422
    assert await count(db_session, Refund) == 0


@pytest.mark.parametrize(
    "payload",
    [
        {"amount": 100.5},  # a JSON float
        {"amount": "100.001"},
        {"amount": "0"},
        {"amount": "0.00"},
        {"amount": "-5.00"},
        {"amount": "NaN"},
        {"amount": "abc"},
        {"amount": "10000000000.00"},
        {"amount": True},
        {"reason_code": "Damaged"},
        {"reason_code": ""},
        {"reason_code": "x" * 40},
        {"note": ""},
        {"note": "n" * 501},
        {"user_id": str(uuid.uuid4())},
        {"requested_by": str(uuid.uuid4())},
        {"idempotency_key": "smuggled-in-body"},
    ],
)
async def test_invalid_bodies_are_rejected_before_anything_happens(
    refund_client, db_session, world, payload
) -> None:
    r = await post(refund_client, world.alice, {**body(world.order), **payload})
    assert r.status_code == 422
    assert await count(db_session, Refund) == 0 and await count(db_session, ToolCall) == 0


async def test_an_integer_amount_is_accepted(refund_client, world) -> None:
    r = await post(refund_client, world.alice, {**body(world.order), "amount": 100})
    assert r.status_code == 201 and r.json()["refund"]["amount"] == "100.00"


@pytest.mark.parametrize("missing", ["order_id", "amount", "reason_code"])
async def test_missing_fields_are_422(refund_client, world, missing) -> None:
    payload = body(world.order)
    payload.pop(missing)
    assert (await post(refund_client, world.alice, payload)).status_code == 422


# --- idempotency ---------------------------------------------------------------------------------


async def test_same_key_ten_times_sequentially_makes_one_refund(
    refund_client, db_session, world, provider
) -> None:
    key = "retry-me-0001"
    responses = [
        await post(refund_client, world.alice, body(world.order, "100.00"), key) for _ in range(10)
    ]
    assert [r.status_code for r in responses] == [201] + [200] * 9
    assert len({r.json()["refund"]["id"] for r in responses}) == 1
    assert all(r.json()["replayed"] for r in responses[1:])
    assert all(r.headers.get("Idempotent-Replayed") == "true" for r in responses[1:])
    assert await count(db_session, Refund) == 1
    assert len(provider.ledger) == 1 and provider.calls == 1  # replays never reach the provider
    assert await event_types(db_session) == [
        "refund.requested",
        "refund.approved",
        "refund.completed",
    ]
    assert await count(db_session, ToolCall) == 10  # but every attempt is logged


@pytest.mark.parametrize("equivalent", ["100", "100.0", "100.00", "0100.00"])
async def test_equivalent_amount_spellings_are_the_same_request(
    refund_client, db_session, world, equivalent
) -> None:
    key = "same-amount-key1"
    first = await post(refund_client, world.alice, body(world.order, "100.00"), key)
    again = await post(refund_client, world.alice, body(world.order, equivalent), key)
    assert (first.status_code, again.status_code) == (201, 200)
    assert await count(db_session, Refund) == 1


@pytest.mark.parametrize(
    "variant",
    [
        {"amount": "100.01"},
        {"reason_code": "defective"},
        {"note": "now with a note"},
        {"order_id": "ORDER2"},
    ],
)
async def test_same_key_different_payload_is_409(
    refund_client, db_session, world, provider, variant
) -> None:
    key = "reused-key-0001"
    first = await post(refund_client, world.alice, body(world.order, "100.00"), key)
    assert first.status_code == 201
    changed = {**body(world.order, "100.00"), **variant}
    if changed["order_id"] == "ORDER2":
        changed["order_id"] = str(world.order2)
    r = await post(refund_client, world.alice, changed, key)
    assert r.status_code == 409 and r.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert await count(db_session, Refund) == 1 and len(provider.ledger) == 1
    (refund,) = await rows(db_session, Refund)
    assert refund.amount == 100 and refund.reason_code == "damaged"  # the original is untouched


async def test_a_replay_of_a_pending_approval_shows_the_same_approval(
    refund_client, db_session, world
) -> None:
    key = "pending-replay-1"
    first = await post(refund_client, world.alice, body(world.order, "800.00"), key)
    again = await post(refund_client, world.alice, body(world.order, "800.00"), key)
    assert (first.status_code, again.status_code) == (202, 200)
    assert again.json()["refund"]["status"] == "pending_approval"
    assert again.json()["approval_id"] == first.json()["approval_id"]
    assert await count(db_session, Approval) == 1


async def test_keys_are_scoped_per_user_and_provider_keys_are_not_the_users_key(
    refund_client, db_session, world, provider
) -> None:
    same_key = "shared-key-00001"
    a = await post(refund_client, world.alice, body(world.order, "100.00"), same_key)
    b = await post(refund_client, world.bob, body(world.bob_order, "100.00"), same_key)
    assert (a.status_code, b.status_code) == (201, 201)
    assert a.json()["refund"]["id"] != b.json()["refund"]["id"]
    assert await count(db_session, Refund) == 2
    # two refunds -> two provider ledger entries, each keyed by ITS refund id
    ids = {a.json()["refund"]["id"], b.json()["refund"]["id"]}
    assert set(provider.ledger) == {f"refund-{i}" for i in ids}
    assert same_key not in provider.ledger and len(provider.succeeded_entries) == 2
    # and each user's replay still returns their own
    again = await post(refund_client, world.bob, body(world.bob_order, "100.00"), same_key)
    assert again.json()["refund"]["id"] == b.json()["refund"]["id"]


# --- payout outcomes -----------------------------------------------------------------------------


async def test_a_declined_payout_is_a_201_with_status_failed_and_is_terminal_for_the_key(
    refund_client, db_session, world, provider
) -> None:
    provider.fail_next()
    key = "will-be-declined1"
    r = await post(refund_client, world.alice, body(world.order, "100.00"), key)
    assert r.status_code == 201  # accepted and recorded: the refund exists
    j = r.json()
    assert j["refund"]["status"] == "failed"
    assert j["error"]["code"] == "PAYOUT_FAILED" and "declined" in j["error"]["message"]
    (call,) = await rows(db_session, ToolCall)
    assert (call.status, call.error_code) == (ToolCallStatus.failed, "PAYOUT_FAILED")
    assert await event_types(db_session) == ["refund.requested", "refund.approved", "refund.failed"]

    calls_before = provider.calls
    again = await post(refund_client, world.alice, body(world.order, "100.00"), key)
    assert again.status_code == 200 and again.json()["refund"]["status"] == "failed"  # terminal
    assert provider.calls == calls_before  # the provider was not contacted again

    fresh = await post(refund_client, world.alice, body(world.order, "100.00"), "a-brand-new-key1")
    assert fresh.status_code == 201 and fresh.json()["refund"]["status"] == "succeeded"
    assert await count(db_session, Refund) == 2


async def test_an_unconfirmed_payout_is_202_processing_and_not_failed(
    refund_client, db_session, world, provider
) -> None:
    provider.unavailable_next()
    r = await post(refund_client, world.alice, body(world.order, "100.00"), "provider-timeout-1")
    assert r.status_code == 202
    assert r.json()["refund"]["status"] == "processing"
    assert r.json()["error"]["code"] == "PAYOUT_UNCONFIRMED"
    (refund,) = await rows(db_session, Refund)
    assert refund.status == RefundStatus.processing and refund.processed_at is None
    assert await event_types(db_session) == [
        "refund.requested",
        "refund.approved",
    ]  # no completed yet


# --- remaining refundable and velocity -------------------------------------------------------


async def test_the_order_total_cannot_be_over_refunded(refund_client, world) -> None:
    first = await post(refund_client, world.alice, body(world.order, "400.00"))
    second = await post(refund_client, world.alice, body(world.order, "400.00"))
    third = await post(refund_client, world.alice, body(world.order, "400.00"))  # only 200 left
    assert (first.status_code, second.status_code) == (201, 201)
    assert (third.status_code, third.json()["code"]) == (403, "AMOUNT_EXCEEDS_REMAINING")
    assert "200.00 still refundable" in third.json()["detail"]


async def test_a_pending_approval_reserves_its_amount(refund_client, world) -> None:
    assert (await post(refund_client, world.alice, body(world.order, "800.00"))).status_code == 202
    r = await post(refund_client, world.alice, body(world.order, "300.00"))  # 200 left
    assert (r.status_code, r.json()["code"]) == (403, "AMOUNT_EXCEEDS_REMAINING")


async def test_the_fourth_refund_in_30_days_needs_approval(refund_client, world) -> None:
    for _ in range(3):
        assert (
            await post(refund_client, world.alice, body(world.order2, "100.00"))
        ).status_code == 201
    r = await post(refund_client, world.alice, body(world.order2, "100.00"))
    assert r.status_code == 202
    assert r.json()["policy"]["reason_code"] == "REFUND_VELOCITY"


async def test_failed_refunds_do_not_count_toward_velocity(refund_client, world, provider) -> None:
    provider.fail_next(3)
    for _ in range(3):
        r = await post(refund_client, world.alice, body(world.order2, "100.00"))
        assert r.json()["refund"]["status"] == "failed"
    r = await post(refund_client, world.alice, body(world.order2, "100.00"))  # nothing counted
    assert (r.status_code, r.json()["refund"]["status"]) == (201, "succeeded")


# --- reading ---


async def test_get_refund_is_scoped_to_the_order_owner(refund_client, world) -> None:
    created = await post(refund_client, world.alice, body(world.order, "100.00"))
    rid = created.json()["refund"]["id"]
    mine = await refund_client.get(f"{URL}/{rid}", headers=auth_for(world.alice.id))
    admin = await refund_client.get(f"{URL}/{rid}", headers=auth_for(world.admin.id))
    theirs = await refund_client.get(f"{URL}/{rid}", headers=auth_for(world.bob.id))
    nothing = await refund_client.get(f"{URL}/{uuid.uuid4()}", headers=auth_for(world.bob.id))
    assert (mine.status_code, admin.status_code) == (200, 200)
    assert mine.json()["refund"]["id"] == rid
    assert (theirs.status_code, nothing.status_code) == (404, 404)
    assert theirs.json() == nothing.json()
    bad = await refund_client.get(f"{URL}/not-a-uuid", headers=auth_for(world.alice.id))
    assert bad.status_code == 422


async def test_the_request_id_is_carried_into_the_logs(refund_client, db_session, world) -> None:
    r = await post(
        refund_client, world.alice, body(world.order), **{"X-Request-ID": "trace-abcdef123"}
    )
    assert r.headers["x-request-id"] == "trace-abcdef123"
    (call,) = await rows(db_session, ToolCall)
    assert call.request_id == "trace-abcdef123"
    assert all(
        e.payload["meta"]["request_id"] == "trace-abcdef123"
        for e in await rows(db_session, OutboxEvent)
    )


async def test_nothing_depends_on_the_age_of_the_clock_for_fresh_orders(
    refund_client, db_session, world
) -> None:
    """Sanity: an order placed 29 days ago is refundable, one placed 31 days ago is not."""
    for days, expected in ((29, 201), (31, 403)):
        order = await db_session.get(Order, world.order2)
        await db_session.execute(
            update(Order)
            .where(Order.id == order.id)
            .values(placed_at=datetime.now(UTC) - timedelta(days=days))
        )
        r = await post(refund_client, world.alice, body(world.order2, "10.00"))
        assert r.status_code == expected, days

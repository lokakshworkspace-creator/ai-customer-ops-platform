"""The facts the policy engine is given: what counts toward 'remaining refundable' and toward the
velocity limit. Which refund statuses count is configuration (rules.yaml), tested both ways."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.enums import RefundStatus as S
from app.modules.refunds import repo
from app.modules.refunds import service as refund_service
from app.policy import evaluate_refund_request
from app.policy.types import Actor, Role
from app.tests.factories import create_order, create_payment, create_refund, create_user
from app.tests.policy_helpers import make_rules

NOW = datetime.now(UTC)
ALL = list(S)
DEFAULT_COUNTED = {S.pending_approval, S.processing, S.succeeded}


async def seed_one_refund_per_status(session: AsyncSession):
    customer = await create_user(session)
    order = await create_order(session, customer, total="1000.00")
    payment = await create_payment(session, order)
    for st in ALL:
        await create_refund(
            session,
            payment,
            customer,
            idempotency_key=f"key-{st.value}-xx",
            amount="10.00",
            status=st,
        )
    return customer, order


# --- reserved amount (remaining refundable) ----------------------------------------------


async def test_only_money_holding_statuses_reserve_the_order_total(db_session) -> None:
    _, order = await seed_one_refund_per_status(db_session)
    got = await repo.reserved_amount(db_session, order.id, DEFAULT_COUNTED)
    assert got == Decimal("30.00")  # pending_approval + processing + succeeded; NOT failed/rejected


@pytest.mark.parametrize("released", [S.failed, S.rejected])
async def test_failed_and_rejected_refunds_release_their_amount(db_session, released) -> None:
    _, order = await seed_one_refund_per_status(db_session)
    only = await repo.reserved_amount(db_session, order.id, {released})
    assert only == Decimal("10.00")  # they exist...
    assert released not in DEFAULT_COUNTED  # ...but the default rules do not count them


async def test_reserved_amount_can_exclude_the_refund_being_evaluated(db_session) -> None:
    customer = await create_user(db_session)
    order = await create_order(db_session, customer)
    payment = await create_payment(db_session, order)
    r1 = await create_refund(
        db_session, payment, customer, idempotency_key="key-one-xx", amount="10.00"
    )
    await create_refund(db_session, payment, customer, idempotency_key="key-two-xx", amount="5.00")
    assert await repo.reserved_amount(db_session, order.id, DEFAULT_COUNTED) == Decimal("15.00")
    assert await repo.reserved_amount(
        db_session, order.id, DEFAULT_COUNTED, exclude_refund_id=r1.id
    ) == Decimal("5.00")


async def test_reserved_amount_is_zero_with_no_refunds(db_session) -> None:
    customer = await create_user(db_session)
    order = await create_order(db_session, customer)
    assert await repo.reserved_amount(db_session, order.id, DEFAULT_COUNTED) == Decimal("0")


# --- velocity count --------------------------------------------------------------------------


async def test_velocity_counts_pending_processing_succeeded_but_not_rejected_or_failed(
    db_session,
) -> None:
    customer, _ = await seed_one_refund_per_status(db_session)
    n = await repo.recent_count(
        db_session, customer.id, DEFAULT_COUNTED, since=NOW - timedelta(days=30)
    )
    assert n == 3


@pytest.mark.parametrize(
    ("status", "counts"),
    [
        (S.pending_approval, True),
        (S.processing, True),
        (S.succeeded, True),
        (S.failed, False),
        (S.rejected, False),
        (S.expired, False),  # Phase 6: nobody decided, nothing was paid, nothing is held
    ],
)
async def test_each_status_individually_against_the_default_rules(
    db_session, status, counts
) -> None:
    customer = await create_user(db_session)
    payment = await create_payment(db_session, await create_order(db_session, customer))
    await create_refund(
        db_session, payment, customer, idempotency_key="the-only-key", status=status
    )
    rules = make_rules()
    n = await repo.recent_count(
        db_session,
        customer.id,
        rules.refund.velocity_counted_statuses,
        since=NOW - timedelta(days=30),
    )
    assert n == (1 if counts else 0)


async def test_which_statuses_count_is_configuration(db_session) -> None:
    customer, _ = await seed_one_refund_per_status(db_session)
    since = NOW - timedelta(days=30)
    everything = make_rules(**{"refund.velocity_counted_statuses": [s.value for s in ALL]})
    only_succeeded = make_rules(**{"refund.velocity_counted_statuses": ["succeeded"]})
    assert await repo.recent_count(
        db_session, customer.id, everything.refund.velocity_counted_statuses, since=since
    ) == len(ALL)
    assert (
        await repo.recent_count(
            db_session, customer.id, only_succeeded.refund.velocity_counted_statuses, since=since
        )
        == 1
    )


async def test_velocity_window_boundary(db_session) -> None:
    customer = await create_user(db_session)
    payment = await create_payment(db_session, await create_order(db_session, customer))
    since = NOW - timedelta(days=30)
    await create_refund(
        db_session, payment, customer, idempotency_key="exactly-30d-ago", created_at=since
    )
    await create_refund(
        db_session,
        payment,
        customer,
        idempotency_key="just-inside-win",
        created_at=since + timedelta(seconds=1),
    )
    await create_refund(
        db_session,
        payment,
        customer,
        idempotency_key="just-outside-win",
        created_at=since - timedelta(seconds=1),
    )
    # exactly at the window start counts; one second older does not
    assert await repo.recent_count(db_session, customer.id, DEFAULT_COUNTED, since=since) == 2


async def test_velocity_is_per_customer_and_follows_the_order_owner_not_the_requester(
    db_session,
) -> None:
    alice = await create_user(db_session)
    bob = await create_user(db_session)
    admin = await create_user(db_session, role=Role.admin)
    a_pay = await create_payment(db_session, await create_order(db_session, alice))
    b_pay = await create_payment(db_session, await create_order(db_session, bob))
    await create_refund(db_session, a_pay, alice, idempotency_key="alice-own-key")
    await create_refund(
        db_session, a_pay, admin, idempotency_key="admin-for-alice"
    )  # on alice's order
    await create_refund(db_session, b_pay, bob, idempotency_key="bobs-own-key-1")
    since = NOW - timedelta(days=30)
    assert await repo.recent_count(db_session, alice.id, DEFAULT_COUNTED, since=since) == 2
    assert await repo.recent_count(db_session, bob.id, DEFAULT_COUNTED, since=since) == 1
    assert await repo.recent_count(db_session, admin.id, DEFAULT_COUNTED, since=since) == 0


async def test_velocity_count_spans_all_of_a_customers_orders(db_session) -> None:
    customer = await create_user(db_session)
    for i in range(3):
        pay = await create_payment(db_session, await create_order(db_session, customer))
        await create_refund(db_session, pay, customer, idempotency_key=f"order-{i}-key-xx")
    assert (
        await repo.recent_count(
            db_session, customer.id, DEFAULT_COUNTED, since=NOW - timedelta(days=30)
        )
        == 3
    )


# --- end to end through build_facts + the engine ----------------------------------------------


async def policy_for_fourth_refund(session: AsyncSession, prior_statuses: list[S], rules=None):
    customer = await create_user(session)
    order = await create_order(session, customer, total="5000.00")
    payment = await create_payment(session, order)
    for i, st in enumerate(prior_statuses):
        await create_refund(
            session,
            payment,
            customer,
            idempotency_key=f"prior-{i}-key-xx",
            amount="10.00",
            status=st,
        )
    rules = rules or make_rules()
    from types import SimpleNamespace

    args = SimpleNamespace(amount=Decimal("100.00"), reason_code="damaged")
    facts = await refund_service.build_facts(
        session,
        rules,
        order,
        has_payment=True,
        amount=args.amount,
        reason_code=args.reason_code,
        now=NOW,
    )
    return evaluate_refund_request(rules, Actor(customer.id, Role.customer), facts, NOW)


async def test_fourth_refund_escalates_only_if_three_prior_ones_count(db_session) -> None:
    three_live = await policy_for_fourth_refund(
        db_session, [S.succeeded, S.processing, S.pending_approval]
    )
    assert three_live.reason_code.value == "REFUND_VELOCITY" and three_live.needs_approval

    # same number of rows, but two of them never moved money: only 1 counts -> auto-approved
    dead = await policy_for_fourth_refund(db_session, [S.succeeded, S.failed, S.rejected])
    assert dead.is_allow

    # three prior but only two count: this is the 3rd -> allowed
    two = await policy_for_fourth_refund(db_session, [S.succeeded, S.succeeded, S.rejected])
    assert two.is_allow


async def test_remaining_refundable_ignores_failed_and_rejected_in_the_policy(db_session) -> None:
    customer = await create_user(db_session)
    order = await create_order(db_session, customer, total="1000.00")
    payment = await create_payment(db_session, order)
    await create_refund(
        db_session,
        payment,
        customer,
        idempotency_key="dead-failed-xx",
        amount="900.00",
        status=S.failed,
    )
    await create_refund(
        db_session,
        payment,
        customer,
        idempotency_key="dead-reject-xx",
        amount="900.00",
        status=S.rejected,
    )
    rules = make_rules()
    facts = await refund_service.build_facts(
        db_session,
        rules,
        order,
        has_payment=True,
        amount=Decimal("400.00"),
        reason_code="damaged",
        now=NOW,
    )
    assert facts.already_refunded == Decimal("0")
    assert evaluate_refund_request(rules, Actor(customer.id, Role.customer), facts, NOW).is_allow

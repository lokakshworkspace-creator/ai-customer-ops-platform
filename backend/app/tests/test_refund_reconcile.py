"""Refunds stuck in `processing` are resolved exactly once, safely, via the provider key."""

import asyncio
import uuid
from collections import Counter
from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text

from app.db import locking
from app.db.models import AuditLog, OutboxEvent, Refund
from app.db.models.enums import RefundStatus
from app.modules.refunds.payments import SimulatedPaymentProvider
from app.modules.refunds.reconcile import find_stuck, reconcile_stuck_refunds
from app.modules.refunds.service import payout_flow
from app.tests.conftest import CommittedDB
from app.tests.tokens import auth_for
from app.tests.worlds import World, make_world

REFUNDS = "/api/v1/refunds"
FIVE_MIN = timedelta(minutes=5)


@pytest.fixture
async def world(committed_db: CommittedDB) -> World:
    async with committed_db.maker() as s:
        w = await make_world(s)
        await s.commit()
    return w


async def request(client: AsyncClient, world: World, key: str, amount: str = "100.00"):
    body = {"order_id": str(world.order), "amount": amount, "reason_code": "damaged"}
    return await client.post(
        REFUNDS, json=body, headers={**auth_for(world.alice.id), "Idempotency-Key": key}
    )


async def age_everything(db: CommittedDB, interval: str = "1 hour") -> None:
    async with db.maker() as s:
        await s.execute(text(f"UPDATE refunds SET updated_at = now() - interval '{interval}'"))
        await s.commit()


async def the_refund(db: CommittedDB) -> Refund:
    async with db.maker() as s:
        return (await s.execute(select(Refund))).scalar_one()


async def events(db: CommittedDB) -> Counter:
    async with db.maker() as s:
        return Counter(await s.scalars(select(OutboxEvent.event_type)))


async def test_crash_after_the_payout_resolves_exactly_once(
    committed_client, committed_db, world, provider: SimulatedPaymentProvider
) -> None:
    """The provider paid, but its response never reached us: the refund is stuck `processing`."""
    provider.lose_response_next()
    r = await request(committed_client, world, "lost-response-001")
    assert r.status_code == 202 and r.json()["refund"]["status"] == "processing"
    assert len(provider.succeeded_entries) == 1  # the money DID move
    stuck = await the_refund(committed_db)
    assert stuck.status == RefundStatus.processing
    assert await events(committed_db) == {"refund.requested": 1, "refund.approved": 1}

    await age_everything(committed_db)
    results = await reconcile_stuck_refunds(committed_db.maker, provider, older_than=FIVE_MIN)

    assert [(x.refund_id, x.outcome) for x in results] == [(stuck.id, "succeeded")]
    done = await the_refund(committed_db)
    assert done.status == RefundStatus.succeeded and done.processed_at is not None
    assert len(provider.ledger) == 1 and len(provider.succeeded_entries) == 1  # NOT paid twice
    assert await events(committed_db) == {
        "refund.requested": 1,
        "refund.approved": 1,
        "refund.completed": 1,
    }
    async with committed_db.maker() as s:
        completed = list(
            await s.scalars(select(AuditLog).where(AuditLog.action == "refund.completed"))
        )
    assert len(completed) == 1

    # running it again changes nothing
    assert await reconcile_stuck_refunds(committed_db.maker, provider, older_than=FIVE_MIN) == []
    again = await payout_flow_once(committed_db, provider, stuck.id)
    assert again == "already_final"
    assert (await events(committed_db))["refund.completed"] == 1 and len(provider.ledger) == 1


async def payout_flow_once(db: CommittedDB, provider, refund_id: uuid.UUID) -> str:
    async with db.maker() as s:
        return (await payout_flow(s, provider, refund_id, request_id=None)).kind


async def test_crash_between_the_provider_answer_and_the_finalise_resolves_exactly_once(
    committed_client, committed_db, world, provider, monkeypatch
) -> None:
    """The process dies AFTER the provider answered but BEFORE the result was recorded."""
    real_lock_refund = locking.lock_refund
    calls = {"n": 0}

    async def dying_lock_refund(session, refund_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("process died before recording the payout")
        return await real_lock_refund(session, refund_id)

    monkeypatch.setattr(locking, "lock_refund", dying_lock_refund)
    r = await request(committed_client, world, "died-before-record")
    assert r.status_code == 201  # accepted ...
    assert r.json()["error"]["code"] == "POST_COMMIT_ERROR"  # ... but the post-commit step blew up
    assert (await the_refund(committed_db)).status == RefundStatus.processing
    assert len(provider.succeeded_entries) == 1

    await age_everything(committed_db)
    results = await reconcile_stuck_refunds(committed_db.maker, provider, older_than=FIVE_MIN)
    assert [x.outcome for x in results] == ["succeeded"]
    assert (await the_refund(committed_db)).status == RefundStatus.succeeded
    assert len(provider.ledger) == 1 and (await events(committed_db))["refund.completed"] == 1


async def test_two_reconcilers_at_once_complete_a_refund_once(
    committed_client, committed_db, world, provider
) -> None:
    provider.unavailable_next()  # the original request never got through
    await request(committed_client, world, "two-reconcilers-1")
    await age_everything(committed_db)
    provider.latency_ms = 60
    a, b = await asyncio.gather(
        reconcile_stuck_refunds(committed_db.maker, provider, older_than=FIVE_MIN),
        reconcile_stuck_refunds(committed_db.maker, provider, older_than=FIVE_MIN),
    )
    outcomes = [x.outcome for x in a + b]
    # one run finishes it; the other either finds it already final or no longer finds it stuck
    assert outcomes.count("succeeded") == 1 and set(outcomes) <= {"succeeded", "already_final"}
    assert len(provider.ledger) == 1 and len(provider.succeeded_entries) == 1
    assert (await events(committed_db))["refund.completed"] == 1
    assert (await the_refund(committed_db)).status == RefundStatus.succeeded


async def test_a_recent_processing_refund_is_left_alone(
    committed_client, committed_db, world, provider
) -> None:
    provider.unavailable_next()
    await request(committed_client, world, "too-fresh-to-touch")  # stuck, but only just
    calls = provider.calls
    assert await reconcile_stuck_refunds(committed_db.maker, provider, older_than=FIVE_MIN) == []
    assert (
        provider.calls == calls
        and (await the_refund(committed_db)).status == RefundStatus.processing
    )
    await age_everything(committed_db, "10 minutes")
    assert [
        x.outcome
        for x in await reconcile_stuck_refunds(committed_db.maker, provider, older_than=FIVE_MIN)
    ] == ["succeeded"]


async def test_other_statuses_are_never_touched(
    committed_client, committed_db, world, provider
) -> None:
    await request(committed_client, world, "ok-succeeded-001")  # succeeded
    await request(committed_client, world, "needs-approval-1", "800.00")  # pending_approval
    await age_everything(committed_db)
    async with committed_db.maker() as s:
        assert await find_stuck(s, older_than=FIVE_MIN) == []
    calls = provider.calls
    assert await reconcile_stuck_refunds(committed_db.maker, provider, older_than=FIVE_MIN) == []
    assert provider.calls == calls


async def test_a_declined_payout_found_by_the_reconciler_ends_failed(
    committed_client, committed_db, world, provider
) -> None:
    provider.unavailable_next()
    await request(committed_client, world, "declined-on-retry")
    await age_everything(committed_db)
    provider.fail_next()
    results = await reconcile_stuck_refunds(committed_db.maker, provider, older_than=FIVE_MIN)
    assert [x.outcome for x in results] == ["failed"]
    assert (await the_refund(committed_db)).status == RefundStatus.failed
    assert (await events(committed_db))["refund.failed"] == 1


async def test_a_provider_that_is_still_down_leaves_the_refund_for_the_next_run(
    committed_client, committed_db, world, provider
) -> None:
    provider.unavailable_next(2)  # the original attempt AND the first reconcile attempt time out
    await request(committed_client, world, "still-down-xxxxx")
    await age_everything(committed_db)
    first = await reconcile_stuck_refunds(committed_db.maker, provider, older_than=FIVE_MIN)
    assert [x.outcome for x in first] == ["unconfirmed"]
    assert (await the_refund(committed_db)).status == RefundStatus.processing
    second = await reconcile_stuck_refunds(committed_db.maker, provider, older_than=FIVE_MIN)
    assert [x.outcome for x in second] == ["succeeded"]
    assert len(provider.ledger) == 1


async def test_an_error_on_one_refund_does_not_stop_the_others(
    committed_client, committed_db, world, provider, monkeypatch
) -> None:
    provider.unavailable_next(2)
    await request(committed_client, world, "batch-one-xxxxx")
    body = {"order_id": str(world.order2), "amount": "100.00", "reason_code": "damaged"}
    await committed_client.post(
        REFUNDS,
        json=body,
        headers={**auth_for(world.alice.id), "Idempotency-Key": "batch-two-xxxxx"},
    )
    await age_everything(committed_db)

    real = locking.lock_refund
    seen = {"n": 0}

    async def flaky(session, refund_id):
        seen["n"] += 1
        if seen["n"] == 1:
            raise RuntimeError("boom")
        return await real(session, refund_id)

    monkeypatch.setattr(locking, "lock_refund", flaky)
    results = await reconcile_stuck_refunds(committed_db.maker, provider, older_than=FIVE_MIN)
    assert sorted(x.outcome for x in results) == ["error", "succeeded"]

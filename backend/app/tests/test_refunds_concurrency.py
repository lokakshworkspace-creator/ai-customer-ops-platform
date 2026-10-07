"""Concurrency: real commits over separate connections (pool of 12), real lock contention.

Where a race window would otherwise be too narrow to hit reliably, the test widens it with a
small sleep placed exactly where a missing lock would let two requests interleave."""

import asyncio
import uuid
from collections import Counter
from decimal import Decimal

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import locking
from app.db.models import Approval, OutboxEvent, Refund, ToolCall
from app.db.models.enums import RefundStatus
from app.modules.refunds import repo as refunds_repo
from app.modules.refunds import service as refund_service
from app.modules.refunds.payments import SimulatedPaymentProvider
from app.tests.conftest import CommittedDB
from app.tests.factories import create_order, create_payment, create_refund
from app.tests.tokens import auth_for
from app.tests.worlds import Person, World, make_world

REFUNDS = "/api/v1/refunds"
APPROVALS = "/api/v1/approvals"
TIMEOUT = 30


@pytest.fixture
async def world(committed_db: CommittedDB) -> World:
    async with committed_db.maker() as s:
        w = await make_world(s)
        await s.commit()
    return w


def payload(order: uuid.UUID, amount: str = "100.00", reason: str = "damaged") -> dict:
    return {"order_id": str(order), "amount": amount, "reason_code": reason}


async def post(c: AsyncClient, who: Person, body: dict, key: str) -> Response:
    return await c.post(REFUNDS, json=body, headers={**auth_for(who.id), "Idempotency-Key": key})


async def run_all(*coros):
    return await asyncio.wait_for(asyncio.gather(*coros), timeout=TIMEOUT)


async def scalar(db: CommittedDB, sql: str, **params):
    async with db.maker() as s:
        return await s.scalar(text(sql), params)


async def refund_rows(db: CommittedDB) -> list[Refund]:
    async with db.maker() as s:
        return list(await s.scalars(select(Refund)))


async def event_counts(db: CommittedDB) -> Counter:
    async with db.maker() as s:
        return Counter(await s.scalars(select(OutboxEvent.event_type)))


# --- identical requests ---


async def test_ten_concurrent_identical_requests_make_exactly_one_refund(
    committed_client, committed_db, world
) -> None:
    key = "ten-at-once-0001"
    rs = await run_all(
        *[post(committed_client, world.alice, payload(world.order), key) for _ in range(10)]
    )

    assert sorted(r.status_code for r in rs) == [200] * 9 + [201]  # 1 created + 9 replays
    created = [r for r in rs if r.status_code == 201]
    replays = [r for r in rs if r.status_code == 200]
    assert created[0].json()["replayed"] is False
    assert all(r.json()["replayed"] is True for r in replays)
    assert all(r.headers["Idempotent-Replayed"] == "true" for r in replays)
    ids = {r.json()["refund"]["id"] for r in rs}
    assert len(ids) == 1  # consistent: everyone is told about the same refund
    assert {r.json()["refund"]["amount"] for r in rs} == {"100.00"}

    (refund,) = await refund_rows(committed_db)
    assert str(refund.id) in ids and refund.status == RefundStatus.succeeded
    assert await event_counts(committed_db) == {
        "refund.requested": 1,
        "refund.approved": 1,
        "refund.completed": 1,
    }
    assert (
        await scalar(committed_db, "SELECT count(*) FROM tool_calls") == 10
    )  # all attempts logged


async def test_ten_concurrent_identical_requests_make_exactly_one_provider_payout(
    committed_client, committed_db, world, provider
) -> None:
    provider.latency_ms = 80  # payout in flight while the replays arrive
    rs = await run_all(
        *[
            post(committed_client, world.alice, payload(world.order), "one-payout-key1")
            for _ in range(10)
        ]
    )
    assert Counter(r.status_code for r in rs) == {201: 1, 200: 9}
    assert len(provider.ledger) == 1 and len(provider.succeeded_entries) == 1
    assert provider.calls == 1  # replays never even reach the provider
    (refund,) = await refund_rows(committed_db)
    assert list(provider.ledger) == [f"refund-{refund.id}"]
    # while the payout was in flight, replays may legitimately see `processing`
    assert {r.json()["refund"]["status"] for r in rs} <= {"processing", "succeeded"}


async def test_concurrent_requests_needing_approval_make_one_refund_and_one_approval(
    committed_client, committed_db, world
) -> None:
    rs = await run_all(
        *[
            post(committed_client, world.alice, payload(world.order, "800.00"), "approval-race-01")
            for _ in range(10)
        ]
    )
    assert Counter(r.status_code for r in rs) == {202: 1, 200: 9}
    assert len({r.json()["approval_id"] for r in rs}) == 1
    async with committed_db.maker() as s:
        assert await s.scalar(select(func.count()).select_from(Approval)) == 1
    assert len(await refund_rows(committed_db)) == 1


# --- one key, different payloads ---


async def test_one_key_with_ten_different_payloads_admits_exactly_one(
    committed_client, committed_db, world
) -> None:
    key = "one-key-ten-bodies"
    rs = await run_all(
        *[
            post(committed_client, world.alice, payload(world.order, f"{10 + i}.00"), key)
            for i in range(10)
        ]
    )
    assert Counter(r.status_code for r in rs) == {201: 1, 409: 9}
    assert {r.json()["code"] for r in rs if r.status_code == 409} == {"IDEMPOTENCY_KEY_REUSED"}
    assert len(await refund_rows(committed_db)) == 1


def slow_build_facts(monkeypatch) -> None:
    """Both requests are past their pre-check before either one inserts."""
    real = refund_service.build_facts

    async def slow(*a, **kw):
        out = await real(*a, **kw)
        await asyncio.sleep(0.15)
        return out

    monkeypatch.setattr(refund_service, "build_facts", slow)


async def test_one_key_on_two_orders_of_the_same_customer_is_serialised_by_the_locks(
    committed_client, committed_db, world, monkeypatch
) -> None:
    slow_build_facts(monkeypatch)
    key = "same-key-two-orders"
    rs = await run_all(
        post(committed_client, world.alice, payload(world.order, "100.00"), key),
        post(committed_client, world.alice, payload(world.order2, "100.00"), key),
    )
    assert Counter(r.status_code for r in rs) == {201: 1, 409: 1}
    assert len(await refund_rows(committed_db)) == 1


async def test_only_the_database_constraint_can_stop_one_admin_key_used_for_two_customers(
    committed_client, committed_db, world, monkeypatch
) -> None:
    """One admin, ONE Idempotency-Key, orders of two DIFFERENT customers: the two requests share
    no order lock and no customer lock, so nothing but UNIQUE (requested_by, idempotency_key)
    can arbitrate. This is the test that fails if that constraint is removed."""
    slow_build_facts(monkeypatch)
    key = "admin-key-two-people"
    rs = await run_all(
        post(committed_client, world.admin, payload(world.order, "100.00"), key),  # alice's order
        post(committed_client, world.admin, payload(world.bob_order, "100.00"), key),  # bob's
    )
    assert Counter(r.status_code for r in rs) == {201: 1, 409: 1}
    assert {r.json().get("code") for r in rs if r.status_code == 409} == {"IDEMPOTENCY_KEY_REUSED"}
    assert len(await refund_rows(committed_db)) == 1


# --- order lock: no over-refunding ---


async def test_concurrent_refunds_cannot_exceed_the_order_total(
    committed_client, committed_db, world, provider, monkeypatch
) -> None:
    real = refunds_repo.reserved_amount

    async def slow(*a, **kw):
        out = await real(*a, **kw)
        await asyncio.sleep(0.15)  # without the order lock, everyone computes `remaining` now
        return out

    monkeypatch.setattr(refunds_repo, "reserved_amount", slow)
    rs = await run_all(
        *[
            post(
                committed_client,
                world.alice,
                payload(world.order, "400.00"),
                f"over-refund-{i:03d}",
            )
            for i in range(6)
        ]
    )
    assert Counter(r.status_code for r in rs) == {201: 2, 403: 4}  # 2 x 400 fits in 1000; 3 do not
    assert {r.json()["code"] for r in rs if r.status_code == 403} == {"AMOUNT_EXCEEDS_REMAINING"}
    refunds = await refund_rows(committed_db)
    assert sum((r.amount for r in refunds), Decimal(0)) == Decimal("800.00") <= Decimal("1000.00")
    assert len(provider.succeeded_entries) == 2


# --- user lock: velocity ---


async def test_concurrent_requests_cannot_slip_past_the_velocity_limit(
    committed_client, committed_db, world, monkeypatch
) -> None:
    """Two refunds already in the window; five more arrive at once on five DIFFERENT orders (so the
    order locks do not serialise them). Only the customer lock keeps the count honest: exactly one
    is the 3rd (auto-approved), the rest are 4th-or-later (need approval)."""
    async with committed_db.maker() as s:
        from app.db.models import User

        alice = await s.get(User, world.alice.id)
        orders = [
            await create_order(s, alice, total="1000.00", items=[("S", 1, "1000.00")])
            for _ in range(5)
        ]
        pay = [await create_payment(s, o) for o in orders]
        order_ids = [o.id for o in orders]
        prior_pay = await create_payment(s, await create_order(s, alice, total="1000.00"))
        for i in range(2):
            await create_refund(
                s,
                prior_pay,
                alice,
                idempotency_key=f"prior-live-{i}-xx",
                status=RefundStatus.succeeded,
            )
        await s.commit()
    del pay

    real = refunds_repo.recent_count

    async def slow(*a, **kw):
        out = await real(*a, **kw)
        await asyncio.sleep(0.15)
        return out

    monkeypatch.setattr(refunds_repo, "recent_count", slow)
    rs = await run_all(
        *[
            post(committed_client, world.alice, payload(o, "100.00"), f"velocity-key-{i}")
            for i, o in enumerate(order_ids)
        ]
    )
    assert Counter(r.status_code for r in rs) == {201: 1, 202: 4}
    assert {r.json()["policy"]["reason_code"] for r in rs if r.status_code == 202} == {
        "REFUND_VELOCITY"
    }


# --- lock order: no deadlocks ---


async def test_a_request_and_an_approve_overlap_without_deadlock(
    committed_client, committed_db, world, monkeypatch
) -> None:
    """Deterministic overlap. Flow A (a new refund request) takes the ORDER lock and is then held
    just before taking the USER lock. Flow B (approving an earlier refund on the same order) must
    wait on the order lock; releasing A lets both finish. If the two flows took their locks in
    different orders this would deadlock (or Postgres would abort one)."""
    first = await post(
        committed_client, world.alice, payload(world.order, "800.00"), "overlap-pending"
    )
    approval_id = first.json()["approval_id"]

    holding_order, release = asyncio.Event(), asyncio.Event()
    paused = {"done": False}
    real_lock_user = locking.lock_user

    async def gated_lock_user(session: AsyncSession, user_id):
        if not paused["done"]:
            paused["done"] = True
            holding_order.set()  # A now holds the order lock
            await release.wait()
        return await real_lock_user(session, user_id)

    monkeypatch.setattr(locking, "lock_user", gated_lock_user)

    task_a = asyncio.create_task(
        post(committed_client, world.alice, payload(world.order, "100.00"), "overlap-new-req")
    )
    task_b = None
    try:
        await asyncio.wait_for(holding_order.wait(), 10)
        task_b = asyncio.create_task(
            committed_client.post(
                f"{APPROVALS}/{approval_id}/approve", headers=auth_for(world.admin.id)
            )
        )
        await asyncio.sleep(0.4)
        assert not task_b.done(), (
            "approve should be queued behind the order lock held by the request"
        )
        assert not task_a.done()
        release.set()
        a, b = await asyncio.wait_for(asyncio.gather(task_a, task_b), TIMEOUT)
    finally:
        # Never leave a paused request holding row locks: it would block the fixture's cleanup
        # (TRUNCATE) forever if an assertion above failed.
        release.set()
        await asyncio.gather(*[t for t in (task_a, task_b) if t], return_exceptions=True)

    assert a.status_code in (201, 202) and b.status_code == 200
    assert b.json()["refund"]["status"] == "succeeded"
    assert await scalar(committed_db, "SELECT count(*) FROM refunds") == 2


async def test_overlapping_flows_stress_never_deadlock(
    committed_client, committed_db, world
) -> None:
    """Many overlaps of request / approve / reject on the same order and customer."""
    outcomes: list[int] = []
    for i in range(6):
        first = await post(
            committed_client,
            world.alice,
            payload(world.order2, "600.00"),
            f"stress-pending-{i:02d}",
        )
        approval_id = first.json().get("approval_id")
        if not approval_id:  # velocity/limits changed the path: nothing to approve this round
            continue
        verb = "approve" if i % 2 == 0 else "reject"
        rs = await run_all(
            committed_client.post(
                f"{APPROVALS}/{approval_id}/{verb}", headers=auth_for(world.admin.id)
            ),
            committed_client.post(
                f"{APPROVALS}/{approval_id}/{verb}", headers=auth_for(world.admin2.id)
            ),
            post(
                committed_client,
                world.alice,
                payload(world.order2, "50.00"),
                f"stress-new-a-{i:02d}",
            ),
            post(
                committed_client,
                world.alice,
                payload(world.order, "50.00"),
                f"stress-new-b-{i:02d}",
            ),
            post(
                committed_client,
                world.bob,
                payload(world.bob_order, "50.00"),
                f"stress-bob-{i:02d}",
            ),
        )
        outcomes += [r.status_code for r in rs]
    assert outcomes, "the stress loop did not run"
    # No 5xx anywhere: a deadlock or a lock timeout would surface as a 500.
    assert all(code < 500 for code in outcomes), Counter(outcomes)
    async with committed_db.maker() as s:
        calls = list(
            await s.scalars(select(ToolCall.error_code).where(ToolCall.error_code.is_not(None)))
        )
    assert "INTERNAL_ERROR" not in calls


async def test_two_admins_approving_at_once_decide_exactly_once(
    committed_client, committed_db, world, provider
) -> None:
    first = await post(
        committed_client, world.alice, payload(world.order, "800.00"), "double-approve-1"
    )
    approval_id = first.json()["approval_id"]
    rs = await run_all(
        *[
            committed_client.post(f"{APPROVALS}/{approval_id}/approve", headers=auth_for(a.id))
            for a in (world.admin, world.admin2) * 3
        ]
    )
    assert Counter(r.status_code for r in rs) == {200: 1, 409: 5}
    assert len(provider.ledger) == 1
    counts = await event_counts(committed_db)
    assert counts["refund.approved"] == 1 and counts["refund.completed"] == 1


# --- the provider call holds nothing ---


class ProbeProvider(SimulatedPaymentProvider):
    """Checks, DURING the provider call and from separate connections, that the application holds
    no open transaction and no row lock on the order, the customer or the refund."""

    def __init__(self, db: CommittedDB, order_id: uuid.UUID, user_id: uuid.UUID) -> None:
        super().__init__()
        self.db, self.order_id, self.user_id = db, order_id, user_id
        self.observations: list[dict] = []

    async def refund(self, *, payment_ref, amount, currency, idempotency_key):
        refund_id = uuid.UUID(idempotency_key.removeprefix("refund-"))
        obs: dict = {}
        async with self.db.engine.connect() as conn:
            obs["idle_in_transaction"] = await conn.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                    "AND pid <> pg_backend_pid() AND state LIKE 'idle in transaction%'"
                )
            )
            await conn.commit()  # end the implicit transaction of the query above
            for label, table, key in (
                ("order", "orders", self.order_id),
                ("user", "users", self.user_id),
                ("refund", "refunds", refund_id),
            ):
                try:
                    async with conn.begin():
                        # NOWAIT: fails immediately if anyone holds a conflicting row lock
                        await conn.execute(
                            text(f"SELECT 1 FROM {table} WHERE id = :id FOR UPDATE NOWAIT"),
                            {"id": key},
                        )
                    obs[f"{label}_locked"] = False
                except DBAPIError as exc:
                    if "could not obtain lock" not in str(exc):
                        raise  # a real error, not "someone holds the lock"
                    obs[f"{label}_locked"] = True
        # and a real concurrent write to the same order row must not wait
        async with self.db.engine.begin() as conn:
            await conn.execute(text("SET LOCAL lock_timeout = '300ms'"))
            await conn.execute(
                text("UPDATE orders SET updated_at = now() WHERE id = :id"), {"id": self.order_id}
            )
        obs["concurrent_write_ok"] = True
        self.observations.append(obs)
        return await super().refund(
            payment_ref=payment_ref,
            amount=amount,
            currency=currency,
            idempotency_key=idempotency_key,
        )


CLEAN = {
    "idle_in_transaction": 0,
    "order_locked": False,
    "user_locked": False,
    "refund_locked": False,
    "concurrent_write_ok": True,
}


@pytest.fixture
def probe(committed_db, world, committed_client) -> ProbeProvider:
    p = ProbeProvider(committed_db, world.order, world.alice.id)
    from app.api.deps import get_executor
    from app.main import app
    from app.policy.rules import get_rules
    from app.tools.default import build_default_registry
    from app.tools.executor import ToolExecutor

    ex = ToolExecutor(build_default_registry(get_rules(), p))
    app.dependency_overrides[get_executor] = lambda: ex
    return p


async def test_the_provider_runs_outside_any_transaction_and_holds_no_locks_on_a_new_refund(
    committed_client, world, probe
) -> None:
    r = await post(
        committed_client, world.alice, payload(world.order, "100.00"), "probe-new-refund1"
    )
    assert r.status_code == 201
    assert probe.observations == [CLEAN]


async def test_the_provider_runs_outside_any_transaction_and_holds_no_locks_on_an_approval(
    committed_client, world, probe
) -> None:
    first = await post(
        committed_client, world.alice, payload(world.order, "800.00"), "probe-approval-01"
    )
    approval_id = first.json()["approval_id"]
    assert probe.observations == []  # nothing was paid yet
    r = await committed_client.post(
        f"{APPROVALS}/{approval_id}/approve", headers=auth_for(world.admin.id)
    )
    assert r.status_code == 200
    assert probe.observations == [CLEAN]


async def test_the_provider_runs_outside_any_transaction_and_holds_no_locks_when_reconciling(
    committed_client, committed_db, world, probe
) -> None:
    from datetime import timedelta

    from app.modules.refunds.reconcile import reconcile_stuck_refunds

    probe.lose_response_next()  # the payout happens, the response is lost -> refund stuck
    r = await post(
        committed_client, world.alice, payload(world.order, "100.00"), "probe-reconcile-1"
    )
    assert r.status_code == 202
    async with committed_db.maker() as s:
        await s.execute(text("UPDATE refunds SET updated_at = now() - interval '1 hour'"))
        await s.commit()
    results = await reconcile_stuck_refunds(
        committed_db.maker, probe, older_than=timedelta(minutes=5)
    )
    assert [x.outcome for x in results] == ["succeeded"]
    assert len(probe.observations) == 2 and all(o == CLEAN for o in probe.observations)


# --- outbox atomicity with REAL commits ---


async def test_a_failed_request_leaves_neither_a_refund_nor_an_outbox_event(
    committed_client, committed_db, world, provider, monkeypatch
) -> None:
    """With real commits on separate connections: if the unit of work fails after the event was
    added, the event must vanish with the refund (it may never be committed on its own)."""
    from app.tools.handlers import refunds as refund_handlers

    def explode(*a, **kw):
        raise RuntimeError("audit write failed")

    monkeypatch.setattr(refund_handlers.audit, "record", explode)
    r = await post(committed_client, world.alice, payload(world.order), "atomic-real-0001")
    assert r.status_code == 500
    assert await scalar(committed_db, "SELECT count(*) FROM refunds") == 0
    assert await scalar(committed_db, "SELECT count(*) FROM outbox_events") == 0
    assert await scalar(committed_db, "SELECT count(*) FROM approvals") == 0
    assert provider.calls == 0
    assert (
        await scalar(committed_db, "SELECT count(*) FROM tool_calls") == 1
    )  # the attempt is logged

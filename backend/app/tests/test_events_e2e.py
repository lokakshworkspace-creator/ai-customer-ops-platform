"""End to end on the REAL stack: HTTP -> refund/ticket -> outbox -> relay -> Redpanda topic ->
consumer -> notification. Asserts the exact event sequence at every hop."""

import asyncio
import logging
import re
import uuid
from collections import defaultdict
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.db.models import OutboxEvent
from app.events.consumers.base import ConsumerConfig, EventConsumer
from app.events.consumers.notifications import notify
from app.events.envelope import EventEnvelope
from app.events.relay import Relay
from app.tests.conftest import CommittedDB
from app.tests.event_helpers import KafkaEnv, envelopes, outbox_rows, read_topic
from app.tests.tokens import auth_for
from app.tests.worlds import World, make_world

pytestmark = pytest.mark.usefixtures("committed_db")


@pytest.fixture
async def world(committed_db: CommittedDB) -> World:
    async with committed_db.maker() as s:
        w = await make_world(s)
        await s.commit()
    return w


class Notifications:
    """Wraps the real `notify` effect and records every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, session: Any, env: EventEnvelope) -> None:
        await notify(session, env)
        self.calls.append((env.event_type, env.aggregate_id))


async def pipeline(
    db: CommittedDB, env: KafkaEnv, expected: int
) -> tuple[list[EventEnvelope], Notifications]:
    """Run the relay, read what reached the topics, then run the consumer."""
    producer = env.producer()
    try:
        await Relay(db.maker, producer, topic_prefix=env.prefix, batch_size=50).drain()
    finally:
        await producer.stop()
    on_topics = envelopes(
        await read_topic(env, env.refund_topic) + await read_topic(env, env.ticket_topic)
    )
    assert len(on_topics) == expected
    notifications = Notifications()
    cfg = ConsumerConfig(
        name="notifications",
        bootstrap=env.bootstrap,
        topics=[env.refund_topic, env.ticket_topic],
        group_id=env.group,
        dlq_topic=env.dlq_topic,
        poll_timeout_ms=500,
        handler_backoff_s=0.05,
    )
    await asyncio.wait_for(
        EventConsumer(db.maker, notifications, cfg, env.producer()).run(once=True), 90
    )
    return on_topics, notifications


def by_aggregate(items: list[tuple[str, str]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = defaultdict(list)
    for event_type, agg in items:
        out[agg].append(event_type)
    return out


async def post_refund(c: AsyncClient, who, order, amount: str, key: str, reason: str = "damaged"):
    body = {"order_id": str(order), "amount": amount, "reason_code": reason}
    return await c.post(
        "/api/v1/refunds", json=body, headers={**auth_for(who.id), "Idempotency-Key": key}
    )


async def test_an_auto_approved_refund_travels_from_request_to_notification(
    committed_client, committed_db, kafka_env, world, caplog
) -> None:
    r = await post_refund(committed_client, world.alice, world.order, "250.00", "e2e-auto-0001")
    assert r.status_code == 201 and r.json()["refund"]["status"] == "succeeded"
    refund_id = r.json()["refund"]["id"]

    # 1. the outbox, written in the request's transaction
    rows = await outbox_rows(committed_db.maker)
    assert [x["event_type"] for x in rows] == [
        "refund.requested",
        "refund.approved",
        "refund.completed",
    ]
    assert all(x["published_at"] is None for x in rows)

    caplog.set_level(logging.INFO)
    on_topics, notifications = await pipeline(committed_db, kafka_env, expected=3)

    # 2. the topic: exact sequence, keyed by the refund
    assert [e.event_type for e in on_topics] == [
        "refund.requested",
        "refund.approved",
        "refund.completed",
    ]
    assert {e.aggregate_id for e in on_topics} == {refund_id}
    assert [e.event_id for e in on_topics] == [x["id"] for x in rows]
    assert {e.request_id for e in on_topics} == {r.headers["x-request-id"]}
    completed = on_topics[2]
    assert completed.data["status"] == "succeeded" and completed.data["amount"] == "250.00"
    assert completed.data["customer_id"] == str(world.alice.id)

    # 3. the consumer: the same sequence, once each, and a notification was logged
    assert notifications.calls == [(e.event_type, refund_id) for e in on_topics]
    texts = [rec.getMessage() for rec in caplog.records if "notification" in rec.getMessage()]
    assert any("was paid out" in t for t in texts)

    # 4. the outbox is fully published
    assert all(x["published_at"] is not None for x in await outbox_rows(committed_db.maker))


async def test_the_approval_path_declined_payouts_rejections_and_tickets_all_flow(
    committed_client, committed_db, kafka_env, world, provider
) -> None:
    # (a) needs approval, then approved by another admin, paid out
    a = await post_refund(committed_client, world.alice, world.order, "800.00", "e2e-approve-001")
    approval_a = a.json()["approval_id"]
    await committed_client.post(
        f"/api/v1/approvals/{approval_a}/approve", headers=auth_for(world.admin.id)
    )
    # (b) needs approval, then rejected
    b = await post_refund(committed_client, world.alice, world.order2, "900.00", "e2e-reject-0001")
    await committed_client.post(
        f"/api/v1/approvals/{b.json()['approval_id']}/reject", headers=auth_for(world.admin.id)
    )
    # (c) the provider declines the payout
    provider.fail_next()
    c = await post_refund(
        committed_client, world.bob, world.bob_order, "100.00", "e2e-declined-001"
    )
    assert c.json()["refund"]["status"] == "failed"
    # (d) a support ticket (through the tool executor, as the agent will)
    from app.policy.rules import get_rules
    from app.policy.types import Actor
    from app.tools.context import ToolContext
    from app.tools.default import build_default_registry
    from app.tools.executor import ToolExecutor

    async with committed_db.maker() as s:
        out = await ToolExecutor(build_default_registry(get_rules(), provider)).run(
            s,
            ToolContext(
                actor=Actor(world.alice.id, world.alice.role), request_id="req-ticket-0001"
            ),
            "create_ticket",
            {"subject": "Where is my parcel? it is urgent", "body": "x", "priority": "high"},
        )
    ticket_id = out.data["ticket"]["id"]

    _, notifications = await pipeline(committed_db, kafka_env, expected=3 + 2 + 3 + 1)
    seq = by_aggregate(notifications.calls)
    assert seq[a.json()["refund"]["id"]] == [
        "refund.requested",
        "refund.approved",
        "refund.completed",
    ]
    assert seq[b.json()["refund"]["id"]] == ["refund.requested", "refund.rejected"]
    assert seq[c.json()["refund"]["id"]] == ["refund.requested", "refund.approved", "refund.failed"]
    assert seq[ticket_id] == ["ticket.created"]


async def test_events_carry_ids_and_state_only_no_personal_data(
    committed_client, committed_db, kafka_env, world
) -> None:
    await post_refund(committed_client, world.alice, world.order, "100.00", "e2e-pii-000001")
    await post_refund(
        committed_client, world.alice, world.order2, "100.00", "e2e-pii-000002", "changed_mind"
    )
    on_topics, _ = await pipeline(committed_db, kafka_env, expected=3 + 1)
    blob = " ".join(e.model_dump_json() for e in on_topics)
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", blob), "an email address leaked into an event"
    assert "alice" not in blob.lower() and "password" not in blob.lower()
    uuid_fields = {"refund_id", "order_id", "customer_id", "requested_by"}
    for e in on_topics:
        for field in uuid_fields & set(e.data):
            uuid.UUID(e.data[field])  # ids only
    async with committed_db.maker() as s:
        for row in await s.scalars(select(OutboxEvent)):
            assert set(row.payload) == {"data", "meta"} and set(row.payload["meta"]) == {
                "request_id"
            }

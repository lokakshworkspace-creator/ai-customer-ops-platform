"""The resume consumer on the REAL stack (these two tests carry the `kafka` marker through the
`kafka_env` fixture): approval -> outbox -> relay -> Redpanda topic -> resume consumer -> the
waiting turn is finished, with NO read-time sync involved (nothing here calls GET /workflows)."""

import asyncio
import uuid

import pytest
from sqlalchemy import select

from app.db.models import Message, ProcessedEvent
from app.db.models.enums import WorkflowStatus
from app.events.consumers.base import ConsumerConfig, EventConsumer
from app.events.consumers.resume import CONSUMER_NAME, resume_waiting_turn
from app.events.relay import Relay
from app.modules.refunds.payments import SimulatedPaymentProvider
from app.tests.agent_helpers import all_rows
from app.tests.conftest import CommittedDB
from app.tests.event_helpers import KafkaEnv, read_topic
from app.tests.resume_helpers import decide, run_of, waiting_turn
from app.tests.worlds import World, make_world

pytestmark = pytest.mark.usefixtures("committed_db")


@pytest.fixture
async def world(committed_db: CommittedDB) -> World:
    async with committed_db.maker() as s:
        w = await make_world(s)
        await s.commit()
    return w


async def relay_then_resume(db: CommittedDB, env: KafkaEnv) -> None:
    producer = env.producer()
    try:
        await Relay(db.maker, producer, topic_prefix=env.prefix, batch_size=50).drain()
    finally:
        await producer.stop()
    cfg = ConsumerConfig(
        name=CONSUMER_NAME,
        bootstrap=env.bootstrap,
        topics=[env.refund_topic],
        group_id=f"{env.prefix}-resume",
        dlq_topic=env.dlq_topic,
        poll_timeout_ms=500,
        handler_backoff_s=0.05,
    )
    consumer = EventConsumer(db.maker, resume_waiting_turn, cfg, env.producer())
    await asyncio.wait_for(consumer.run(once=True), 90)


async def last_text(db: CommittedDB, run_id: uuid.UUID) -> str:
    async with db.maker() as s:
        msgs = sorted(
            await all_rows(s, Message, Message.workflow_run_id == run_id), key=lambda m: m.seq
        )
        return msgs[-1].content[0]["text"]


async def test_an_approval_travels_through_redpanda_and_finishes_the_waiting_turn(
    committed_db, kafka_env, world
) -> None:
    turn = await waiting_turn(committed_db, world, SimulatedPaymentProvider())
    await decide(committed_db, turn, world)  # an admin approves; the payout succeeds
    assert (await run_of(committed_db, turn.run_id)).status == WorkflowStatus.waiting_approval

    await relay_then_resume(committed_db, kafka_env)

    # read straight from the database: no GET, no sync
    run = await run_of(committed_db, turn.run_id)
    assert (run.state, run.status) == ("DONE", WorkflowStatus.completed)
    assert "approved and has been paid" in await last_text(committed_db, turn.run_id)
    async with committed_db.maker() as s:
        claimed = list(
            await s.scalars(
                select(ProcessedEvent.consumer).where(ProcessedEvent.consumer == CONSUMER_NAME)
            )
        )
    assert len(claimed) == 3  # requested, approved and completed were each claimed exactly once


async def test_a_poison_message_is_dead_lettered_and_the_real_event_still_resumes_the_turn(
    committed_db, kafka_env, world
) -> None:
    producer = kafka_env.producer()
    await producer.start()
    try:
        await producer.publish(kafka_env.refund_topic, b"junk", b"this is not json")
    finally:
        await producer.stop()
    turn = await waiting_turn(committed_db, world, SimulatedPaymentProvider())
    await decide(committed_db, turn, world)

    await relay_then_resume(committed_db, kafka_env)

    assert (await run_of(committed_db, turn.run_id)).status == WorkflowStatus.completed
    dead = await read_topic(kafka_env, kafka_env.dlq_topic)
    assert len(dead) == 1 and dead[0].value == b"this is not json"

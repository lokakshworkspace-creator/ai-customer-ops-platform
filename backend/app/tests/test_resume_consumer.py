"""The workflow-resume consumer's effect, through the REAL consumer base class (its dedupe
transaction), without Redpanda: events are handed to `EventConsumer._apply_effect` exactly as the
poll loop would. (The Redpanda-backed tests are in test_resume_consumer_kafka.py.)"""

import uuid
from types import SimpleNamespace

import pytest

from app.db.models import Message, ProcessedEvent, Refund, ToolCall
from app.db.models.enums import WorkflowStatus
from app.events.consumers.base import ConsumerConfig, EventConsumer
from app.events.consumers.resume import CONSUMER_NAME, resume_waiting_turn
from app.modules.refunds.payments import SimulatedPaymentProvider
from app.modules.refunds.service import payout_flow
from app.tests.agent_helpers import all_rows
from app.tests.conftest import CommittedDB
from app.tests.event_helpers import RecordingProducer
from app.tests.resume_helpers import decide, event, n, run_of, waiting_turn
from app.tests.worlds import World, make_world

pytestmark = pytest.mark.usefixtures("committed_db")


@pytest.fixture
async def world(committed_db: CommittedDB) -> World:
    async with committed_db.maker() as s:
        w = await make_world(s)
        await s.commit()
    return w


def consumer(db: CommittedDB, dlq=None) -> EventConsumer:
    cfg = ConsumerConfig(
        name=CONSUMER_NAME,
        bootstrap="unused",
        topics=["unused"],
        group_id="unused",
        dlq_topic="dlq",
        handler_backoff_s=0.01,
        max_handler_attempts=2,
    )
    return EventConsumer(db.maker, resume_waiting_turn, cfg, dlq or RecordingProducer())


async def deliver(c: EventConsumer, ev) -> str:
    msg = SimpleNamespace(topic="t", partition=0, offset=0, key=b"k", value=ev.to_bytes())
    return await c._apply_effect(ev, msg)  # type: ignore[arg-type]


async def claims(db: CommittedDB) -> int:
    return await n(db, ProcessedEvent)


async def last_text(db: CommittedDB, run_id: uuid.UUID) -> str:
    async with db.maker() as s:
        msgs = sorted(
            await all_rows(s, Message, Message.workflow_run_id == run_id), key=lambda m: m.seq
        )
        return msgs[-1].content[0]["text"]


async def test_a_completed_event_finishes_the_turn_once_and_a_redelivery_adds_nothing(
    committed_db, world
) -> None:
    turn = await waiting_turn(committed_db, world, SimulatedPaymentProvider())
    await decide(committed_db, turn, world)  # approved, and paid in the same call
    assert (await run_of(committed_db, turn.run_id)).status == WorkflowStatus.waiting_approval
    messages, tool_calls = await n(committed_db, Message), await n(committed_db, ToolCall)

    ev = event("refund.completed", turn.refund_id, world.alice.id)
    c = consumer(committed_db)
    assert await deliver(c, ev) == "processed"

    run = await run_of(committed_db, turn.run_id)
    assert (run.state, run.status) == ("DONE", WorkflowStatus.completed)
    assert "approved and has been paid" in await last_text(committed_db, turn.run_id)
    assert await n(committed_db, Message) == messages + 1
    assert await n(committed_db, ToolCall) == tool_calls  # no tool ran
    assert await claims(committed_db) == 1

    assert await deliver(c, ev) == "duplicate"  # the SAME event again
    other = event("refund.completed", turn.refund_id, world.alice.id)  # a new event id
    assert await deliver(c, other) == "processed"
    assert await n(committed_db, Message) == messages + 1  # still exactly one message


async def test_a_rejected_event_finishes_the_turn(committed_db, world) -> None:
    turn = await waiting_turn(committed_db, world, SimulatedPaymentProvider())
    await decide(committed_db, turn, world, tool="reject_refund")
    ev = event("refund.rejected", turn.refund_id, world.alice.id, status="rejected")
    await deliver(consumer(committed_db), ev)
    assert "did not approve" in await last_text(committed_db, turn.run_id)
    assert (await run_of(committed_db, turn.run_id)).status == WorkflowStatus.completed


async def test_an_approved_event_while_the_payout_is_processing_waits_for_the_payout(
    committed_db, world
) -> None:
    provider = SimulatedPaymentProvider()
    turn = await waiting_turn(committed_db, world, provider)
    provider.unavailable_next()
    await decide(committed_db, turn, world)  # approved; the provider did not answer
    c = consumer(committed_db)

    approved = event("refund.approved", turn.refund_id, world.alice.id, status="processing")
    assert await deliver(c, approved) == "processed"  # claimed, but the turn is not final
    assert (await run_of(committed_db, turn.run_id)).status == WorkflowStatus.waiting_approval

    async with committed_db.maker() as s:  # the reconcile path finishes the payout
        out = await payout_flow(s, provider, turn.refund_id, request_id="reconcile")
    assert out.kind == "succeeded"
    done = event("refund.completed", turn.refund_id, world.alice.id)
    assert await deliver(c, done) == "processed"
    assert (await run_of(committed_db, turn.run_id)).status == WorkflowStatus.completed
    assert "approved and has been paid" in await last_text(committed_db, turn.run_id)


async def test_an_event_for_a_refund_nobody_waits_for_is_claimed_and_changes_nothing(
    committed_db, world
) -> None:
    messages = await n(committed_db, Message)
    ev = event("refund.completed", uuid.uuid4(), world.alice.id)  # e.g. a refund made over the API
    assert await deliver(consumer(committed_db), ev) == "processed"
    assert await claims(committed_db) == 1 and await n(committed_db, Message) == messages


async def test_an_event_about_another_customer_never_finishes_the_turn(committed_db, world) -> None:
    turn = await waiting_turn(committed_db, world, SimulatedPaymentProvider())
    await decide(committed_db, turn, world)
    await deliver(consumer(committed_db), event("refund.completed", turn.refund_id, world.bob.id))
    assert (await run_of(committed_db, turn.run_id)).status == WorkflowStatus.waiting_approval


async def test_an_event_type_that_decides_nothing_is_ignored(committed_db, world) -> None:
    turn = await waiting_turn(committed_db, world, SimulatedPaymentProvider())
    await decide(committed_db, turn, world)  # the turn COULD be finished, but this is no trigger
    ev = event("refund.requested", turn.refund_id, world.alice.id)
    await deliver(consumer(committed_db), ev)
    assert (await run_of(committed_db, turn.run_id)).status == WorkflowStatus.waiting_approval


async def test_an_event_whose_data_names_another_refund_than_its_key_is_ignored(
    committed_db, world
) -> None:
    turn = await waiting_turn(committed_db, world, SimulatedPaymentProvider())
    await decide(committed_db, turn, world)
    ev = event("refund.completed", turn.refund_id, world.alice.id)
    forged = ev.model_copy(update={"aggregate_id": str(uuid.uuid4())})
    await deliver(consumer(committed_db), forged)
    assert (await run_of(committed_db, turn.run_id)).status == WorkflowStatus.waiting_approval


async def test_an_unusable_event_is_dead_lettered_and_leaves_no_claim(committed_db, world) -> None:
    dlq = RecordingProducer()
    bad = event("refund.completed", uuid.uuid4(), world.alice.id)
    bad = bad.model_copy(update={"data": {**bad.data, "refund_id": "not-a-uuid"}})
    assert await deliver(consumer(committed_db, dlq), bad) == "dead_lettered"
    assert len(dlq.published) == 1
    assert await claims(committed_db) == 0  # rolled back: a fixed handler could try it again


async def test_finishing_a_turn_never_touches_the_refund(committed_db, world) -> None:
    turn = await waiting_turn(committed_db, world, SimulatedPaymentProvider())
    await decide(committed_db, turn, world)
    async with committed_db.maker() as s:
        (before,) = await all_rows(s, Refund)
        state = (before.status, before.updated_at)
    await deliver(consumer(committed_db), event("refund.completed", turn.refund_id, world.alice.id))
    async with committed_db.maker() as s:
        (after,) = await all_rows(s, Refund)
        assert (after.status, after.updated_at) == state

"""The outbox relay against the REAL Redpanda (unique topics per test) and real Postgres commits."""

import asyncio
import logging
from collections import defaultdict

import pytest
from sqlalchemy import text

from app.events import admin
from app.events.kafka import KafkaEventProducer, producer_kwargs
from app.events.relay import Relay, backoff_seconds
from app.events.types import EventType
from app.tests.conftest import CommittedDB
from app.tests.event_helpers import (
    REFUND_SEQUENCE,
    FlakyProducer,
    GatedProducer,
    KafkaEnv,
    RecordingProducer,
    SlowProducer,
    UnreachableProducer,
    envelopes,
    outbox_rows,
    read_topic,
    seed_events,
)

pytestmark = pytest.mark.usefixtures("committed_db")


def make_relay(db: CommittedDB, env: KafkaEnv, producer, **kw) -> Relay:
    kw.setdefault("batch_size", 50)
    return Relay(db.maker, producer, topic_prefix=env.prefix, **kw)


async def set_due_now(db: CommittedDB) -> None:
    async with db.maker() as s:
        await s.execute(
            text("UPDATE outbox_events SET next_attempt_at = now() - interval '1 second'")
        )
        await s.commit()


# --- pure ---


def test_backoff_doubles_and_is_capped() -> None:
    assert [backoff_seconds(n, 1.0, 300.0) for n in range(0, 6)] == [1, 2, 4, 8, 16, 32]
    assert backoff_seconds(20, 1.0, 300.0) == 300.0
    assert backoff_seconds(0, 5.0, 60.0) == 5.0


def test_the_producer_is_idempotent_and_waits_for_all_replicas() -> None:
    kw = producer_kwargs("127.0.0.1:9092", "c", 5000)
    assert kw["acks"] == "all" and kw["enable_idempotence"] is True


# --- publishing ---


async def test_events_are_published_in_order_keyed_by_aggregate_and_marked_published(
    committed_db, kafka_env
) -> None:
    (agg,) = await seed_events(committed_db.maker, 1)
    producer = kafka_env.producer()
    try:
        stats = await make_relay(committed_db, kafka_env, producer).drain()
    finally:
        await producer.stop()
    assert (stats.published, stats.failed, stats.parked) == (3, 0, 0)

    records = await read_topic(kafka_env, kafka_env.refund_topic, expected=3)
    assert [r.key for r in records] == [str(agg).encode()] * 3  # key = aggregate id
    envs = envelopes(records)
    assert [e.event_type for e in envs] == [t.value for t in REFUND_SEQUENCE]
    assert {e.aggregate_id for e in envs} == {str(agg)} and {e.version for e in envs} == {1}
    assert {e.request_id for e in envs} == {"req-seeded-0001"}
    headers = dict(records[0].headers)
    assert headers["event_type"] == b"refund.requested" and headers["event_id"]

    rows = await outbox_rows(committed_db.maker)
    assert all(r["published_at"] is not None and r["attempts"] == 0 for r in rows)
    assert [str(r["id"]) for r in rows] == [str(e.event_id) for e in envs]  # event_id = row id


async def test_an_idle_relay_never_contacts_the_broker(committed_db, kafka_env) -> None:
    stats = await make_relay(committed_db, kafka_env, UnreachableProducer()).run_once()
    assert stats.selected == 0  # start() would have raised if it had been called


# --- failure, backoff, parking ---


async def test_an_unreachable_broker_leaves_the_row_unpublished_with_attempts_and_backoff(
    committed_db, kafka_env
) -> None:
    await seed_events(committed_db.maker, 1, [EventType.REFUND_REQUESTED])
    dead = KafkaEventProducer("127.0.0.1:1", startup_timeout_s=3)  # nothing listens on port 1
    relay = make_relay(committed_db, kafka_env, dead, backoff_base_s=30.0, backoff_max_s=3600.0)

    stats = await relay.run_once()
    assert (stats.selected, stats.published, stats.failed, stats.parked) == (1, 0, 1, 0)
    (row,) = await outbox_rows(committed_db.maker)
    assert row["published_at"] is None and row["parked_at"] is None
    assert row["attempts"] == 1 and row["last_error"]
    async with committed_db.maker() as s:
        delay = await s.scalar(
            text("SELECT extract(epoch FROM next_attempt_at - now()) FROM outbox_events")
        )
    assert 25 <= delay <= 31, f"first backoff should be ~30s, got {delay}"

    # not due yet: the next cycle leaves it alone (no hot retry loop)
    again = await relay.run_once()
    assert again.selected == 0
    assert (await outbox_rows(committed_db.maker))[0]["attempts"] == 1

    # due again: a second failure doubles the delay
    await set_due_now(committed_db)
    await relay.run_once()
    (row,) = await outbox_rows(committed_db.maker)
    assert row["attempts"] == 2
    async with committed_db.maker() as s:
        delay = await s.scalar(
            text("SELECT extract(epoch FROM next_attempt_at - now()) FROM outbox_events")
        )
    assert 55 <= delay <= 61, f"second backoff should be ~60s, got {delay}"


async def test_after_max_attempts_the_event_is_parked_and_an_error_is_logged(
    committed_db, kafka_env, caplog
) -> None:
    await seed_events(committed_db.maker, 1, [EventType.REFUND_REQUESTED])
    relay = make_relay(committed_db, kafka_env, UnreachableProducer(), max_attempts=3)
    caplog.set_level(logging.DEBUG)
    for i in range(3):
        await relay.run_once()
        await set_due_now(committed_db)
        (row,) = await outbox_rows(committed_db.maker)
        assert row["attempts"] == i + 1
        assert (row["parked_at"] is not None) == (i == 2)

    errors = [
        r
        for r in caplog.records
        if r.levelno == logging.ERROR and "outbox_event_parked" in r.getMessage()
    ]
    assert len(errors) == 1
    assert (await relay.run_once()).selected == 0  # parked: no more retries
    assert (await outbox_rows(committed_db.maker))[0]["attempts"] == 3


async def test_a_parked_event_blocks_later_events_of_its_aggregate_until_unparked(
    committed_db, kafka_env
) -> None:
    (agg, other) = await seed_events(
        committed_db.maker, 2, [EventType.REFUND_REQUESTED, EventType.REFUND_APPROVED]
    )
    async with committed_db.maker() as s:  # park the first event of `agg`
        await s.execute(
            text(
                "UPDATE outbox_events SET parked_at = now(), attempts = 10 "
                "WHERE aggregate_id = :a AND event_type = 'refund.requested'"
            ),
            {"a": str(agg)},
        )
        await s.commit()

    producer = kafka_env.producer()
    try:
        relay = make_relay(committed_db, kafka_env, producer)
        stats = await relay.drain()
        assert stats.published == 2  # only the OTHER aggregate's two events
        records = await read_topic(kafka_env, kafka_env.refund_topic, expected=2)
        assert {r.key for r in records} == {
            str(other).encode()
        }  # nothing from the blocked aggregate

        async with committed_db.maker() as s:
            blocked = await admin.blocked_by_parked(s)
        assert [(b["aggregate_id"], b["blocked_events"]) for b in blocked] == [(str(agg), 1)]

        async with committed_db.maker() as s:  # resolve it
            assert await admin.unpark(s, aggregate_id=str(agg)) == 1
            await s.commit()
        assert (await relay.drain()).published == 2
    finally:
        await producer.stop()

    final = await read_topic(kafka_env, kafka_env.refund_topic, expected=4)
    mine = [e.event_type for e in envelopes(final) if e.aggregate_id == str(agg)]
    assert mine == ["refund.requested", "refund.approved"]  # released in order
    assert all(
        r["parked_at"] is None and r["published_at"] for r in await outbox_rows(committed_db.maker)
    )


async def test_an_event_in_backoff_also_blocks_its_aggregate(committed_db, kafka_env) -> None:
    (agg, other) = await seed_events(
        committed_db.maker, 2, [EventType.REFUND_REQUESTED, EventType.REFUND_APPROVED]
    )
    async with committed_db.maker() as s:
        await s.execute(
            text(
                "UPDATE outbox_events SET attempts = 1, "
                "next_attempt_at = now() + interval '1 hour' "
                "WHERE aggregate_id = :a AND event_type = 'refund.requested'"
            ),
            {"a": str(agg)},
        )
        await s.commit()
    rec = RecordingProducer()
    stats = await make_relay(committed_db, kafka_env, rec).drain()
    assert stats.published == 2
    assert {key for _, key, _ in rec.published} == {str(other)}


async def test_one_failing_event_does_not_block_other_aggregates(committed_db, kafka_env) -> None:
    (bad, good) = await seed_events(committed_db.maker, 2, [EventType.REFUND_REQUESTED])
    producer = FlakyProducer(kafka_env.producer(), {str(bad)})
    try:
        stats = await make_relay(committed_db, kafka_env, producer, backoff_base_s=60.0).run_once()
    finally:
        await producer.stop()
    assert (stats.published, stats.failed) == (1, 1)
    rows = {r["aggregate_id"]: r for r in await outbox_rows(committed_db.maker)}
    assert rows[str(good)]["published_at"] is not None
    assert rows[str(bad)]["published_at"] is None and rows[str(bad)]["attempts"] == 1


# --- concurrency ---


async def test_two_relays_running_at_once_never_double_publish(committed_db, kafka_env) -> None:
    await seed_events(
        committed_db.maker, 40, [EventType.REFUND_REQUESTED, EventType.REFUND_APPROVED]
    )
    p1, p2 = (
        kafka_env.producer(),
        kafka_env.producer(),
    )  # real producers; SlowProducer overlaps them
    r1 = make_relay(committed_db, kafka_env, SlowProducer(p1), batch_size=10)
    r2 = make_relay(committed_db, kafka_env, SlowProducer(p2), batch_size=10)
    try:
        a, b = await asyncio.wait_for(asyncio.gather(r1.drain(), r2.drain()), 60)
    finally:
        await p1.stop()
        await p2.stop()
    assert a.published + b.published == 80
    assert a.published > 0 and b.published > 0, "both relays should have shared the work"

    records = await read_topic(kafka_env, kafka_env.refund_topic, expected=80)
    ids = [e.event_id for e in envelopes(records)]
    assert len(ids) == 80 and len(set(ids)) == 80, "an event was published twice"
    assert all(r["published_at"] for r in await outbox_rows(committed_db.maker))


async def test_a_second_relay_is_not_blocked_by_the_rows_the_first_has_locked(
    committed_db, kafka_env
) -> None:
    """SKIP LOCKED: while relay A is frozen mid-batch holding its rows, relay B takes the OTHER
    rows and finishes. Without SKIP LOCKED, B would queue behind A's locks."""
    await seed_events(committed_db.maker, 20, [EventType.REFUND_REQUESTED])
    real_a, real_b = kafka_env.producer(), kafka_env.producer()
    gated = GatedProducer(real_a, hold=1)
    relay_a = make_relay(committed_db, kafka_env, gated, batch_size=10)
    relay_b = make_relay(committed_db, kafka_env, real_b, batch_size=10)
    task_a = asyncio.create_task(relay_a.run_once())
    try:
        await asyncio.wait_for(gated.entered.wait(), 15)
        stats_b = await asyncio.wait_for(relay_b.run_once(), 8)  # must not wait for A
        assert stats_b.published == 10
        assert not task_a.done()
    finally:
        gated.release()
    stats_a = await asyncio.wait_for(task_a, 30)
    assert stats_a.published == 10
    await real_a.stop()
    await real_b.stop()
    records = await read_topic(kafka_env, kafka_env.refund_topic, expected=20)
    assert len({e.event_id for e in envelopes(records)}) == 20


async def test_per_aggregate_order_is_kept_even_when_relays_overlap(
    committed_db, kafka_env
) -> None:
    """Relay A is frozen holding the FIRST event of ten aggregates. Relay B must not publish those
    aggregates' later events past it. Afterwards every aggregate's events are in order."""
    aggs = await seed_events(committed_db.maker, 20)  # requested, approved, completed each
    real_a, real_b = kafka_env.producer(), kafka_env.producer()
    gated = GatedProducer(real_a, hold=1)
    relay_a = make_relay(committed_db, kafka_env, gated, batch_size=10)
    relay_b = make_relay(committed_db, kafka_env, real_b, batch_size=10)
    task_a = asyncio.create_task(relay_a.run_once())
    try:
        await asyncio.wait_for(gated.entered.wait(), 15)
        await asyncio.wait_for(relay_b.drain(), 30)
        mid = envelopes(await read_topic(kafka_env, kafka_env.refund_topic, settle=3.0))
        held = {
            r["aggregate_id"] for r in await outbox_rows(committed_db.maker, "published_at IS NULL")
        }
        assert held, "A should still be holding unpublished events"
        for agg in held:
            sent = [e.event_type for e in mid if e.aggregate_id == agg]
            # A may already have sent the FIRST event (acknowledged, not yet committed) but nothing
            # LATER may have overtaken it.
            assert sent in ([], ["refund.requested"]), f"{agg}: {sent} overtook the first event"
        assert any(e.aggregate_id not in held for e in mid), "B should have published the others"
    finally:
        gated.release()
        await asyncio.gather(task_a, return_exceptions=True)
    await asyncio.wait_for(relay_b.drain(), 30)
    await real_a.stop()
    await real_b.stop()

    records = await read_topic(kafka_env, kafka_env.refund_topic, expected=60)
    by_agg: dict[str, list[str]] = defaultdict(list)
    for e in envelopes(records):
        by_agg[e.aggregate_id].append(e.event_type)
    assert set(by_agg) == {str(a) for a in aggs}
    expected = [t.value for t in REFUND_SEQUENCE]
    for agg, types in by_agg.items():
        # in order; a retried first event (A sent it, then committed) may appear once more
        deduped = [t for i, t in enumerate(types) if i == 0 or t != types[i - 1]]
        assert deduped == expected, f"{agg}: {types}"


async def test_many_aggregates_two_relays_are_in_order_and_complete(
    committed_db, kafka_env
) -> None:
    aggs = await seed_events(
        committed_db.maker,
        12,
        [*REFUND_SEQUENCE, EventType.REFUND_REJECTED, EventType.REFUND_FAILED],
    )
    p1, p2 = kafka_env.producer(), kafka_env.producer()
    r1 = make_relay(committed_db, kafka_env, p1, batch_size=7)
    r2 = make_relay(committed_db, kafka_env, p2, batch_size=7)
    try:
        await asyncio.wait_for(asyncio.gather(r1.drain(), r2.drain()), 60)
    finally:
        await p1.stop()
        await p2.stop()
    records = await read_topic(kafka_env, kafka_env.refund_topic, expected=60)
    by_agg: dict[str, list[str]] = defaultdict(list)
    for e in envelopes(records):
        by_agg[e.aggregate_id].append(e.event_type)
    expected = [
        t.value for t in [*REFUND_SEQUENCE, EventType.REFUND_REJECTED, EventType.REFUND_FAILED]
    ]
    assert len(by_agg) == len(aggs) and all(v == expected for v in by_agg.values())


async def test_a_relay_crash_between_the_ack_and_the_commit_republishes_but_never_loses(
    committed_db, kafka_env
) -> None:
    """If the relay dies after the broker acknowledged but before the row was marked, the event is
    published again on the next run (at-least-once); consumers de-duplicate on event_id."""
    await seed_events(committed_db.maker, 1, [EventType.REFUND_REQUESTED])
    real = kafka_env.producer()

    class DieAfterAck:
        async def start(self):
            await real.start()

        async def stop(self):
            await real.stop()

        async def publish(self, *a, **kw):
            await real.publish(*a, **kw)
            raise asyncio.CancelledError  # the process "dies" right after the ack

    with pytest.raises(asyncio.CancelledError):
        await make_relay(committed_db, kafka_env, DieAfterAck()).run_once()
    assert (await outbox_rows(committed_db.maker))[0][
        "published_at"
    ] is None  # rolled back: not lost

    try:
        await make_relay(committed_db, kafka_env, real).drain()
    finally:
        await real.stop()
    records = await read_topic(kafka_env, kafka_env.refund_topic, expected=2)
    ids = [e.event_id for e in envelopes(records)]
    assert len(ids) == 2 and len(set(ids)) == 1  # the same event, delivered twice

"""The notification consumer against the REAL Redpanda: idempotent, offset-after-effect,
poison-proof."""

import asyncio
import uuid
from typing import Any

import pytest
from aiokafka.admin import AIOKafkaAdminClient
from sqlalchemy import func, select

from app.db.models import ProcessedEvent
from app.events.consumers.base import ConsumerConfig, EventConsumer
from app.events.envelope import EventEnvelope
from app.tests.conftest import CommittedDB
from app.tests.event_helpers import KafkaEnv, read_topic, valid_envelope_bytes

pytestmark = pytest.mark.usefixtures("committed_db")

NAME = "notifications"


class SimulatedCrash(BaseException):
    """The process dies (not an Exception: nothing in the consumer may swallow it)."""


class Recorder:
    """The 'effect'. Optionally fails for chosen events a number of times."""

    def __init__(self) -> None:
        self.effects: list[tuple[str, str]] = []  # (event_type, event_id)
        self.fail_times: dict[str, int] = {}  # event_type -> remaining failures
        self.always_fail: set[str] = set()

    async def __call__(self, session: Any, env: EventEnvelope) -> None:
        if env.event_type in self.always_fail:
            raise RuntimeError("handler always fails for " + env.event_type)
        if self.fail_times.get(env.event_type, 0) > 0:
            self.fail_times[env.event_type] -= 1
            raise RuntimeError("transient handler failure")
        self.effects.append((env.event_type, str(env.event_id)))

    @property
    def types(self) -> list[str]:
        return [t for t, _ in self.effects]


def make_consumer(
    db: CommittedDB,
    env: KafkaEnv,
    handler: Recorder,
    *,
    cls: type[EventConsumer] = EventConsumer,
    max_attempts: int = 3,
) -> EventConsumer:
    cfg = ConsumerConfig(
        name=NAME,
        bootstrap=env.bootstrap,
        topics=[env.refund_topic, env.ticket_topic],
        group_id=env.group,
        dlq_topic=env.dlq_topic,
        poll_timeout_ms=500,
        max_handler_attempts=max_attempts,
        handler_backoff_s=0.05,
    )
    return cls(db.maker, handler, cfg, env.producer())


async def produce(env: KafkaEnv, topic: str, *values: bytes, key: bytes = b"agg-1") -> None:
    producer = env.producer()
    await producer.start()
    try:
        for v in values:
            await producer.publish(topic, key, v)
    finally:
        await producer.stop()


async def processed(db: CommittedDB) -> int:
    async with db.maker() as s:
        return await s.scalar(select(func.count()).select_from(ProcessedEvent)) or 0


async def committed_total(env: KafkaEnv) -> int:
    """Sum of the group's committed offsets across all partitions of the consumed topics."""
    admin = AIOKafkaAdminClient(bootstrap_servers=env.bootstrap)
    await admin.start()
    try:
        offsets = await admin.list_consumer_group_offsets(env.group)
    finally:
        await admin.close()
    return sum(o.offset for tp, o in offsets.items() if tp.topic != env.dlq_topic)


def event(event_type: str, agg: uuid.UUID, **kw: Any) -> bytes:
    return valid_envelope_bytes(event_type, aggregate_id=agg, **kw)


# --- the happy path ---


async def test_events_are_processed_in_order_exactly_once_and_offsets_are_committed(
    committed_db, kafka_env
) -> None:
    agg = uuid.uuid4()
    sequence = ["refund.requested", "refund.approved", "refund.completed"]
    await produce(
        kafka_env, kafka_env.refund_topic, *[event(t, agg) for t in sequence], key=str(agg).encode()
    )
    rec = Recorder()
    await asyncio.wait_for(make_consumer(committed_db, kafka_env, rec).run(once=True), 60)

    assert rec.types == sequence
    assert len({i for _, i in rec.effects}) == 3
    assert await processed(committed_db) == 3
    assert await committed_total(kafka_env) == 3


async def test_a_duplicate_delivery_does_not_notify_twice(committed_db, kafka_env) -> None:
    agg, dup_id = uuid.uuid4(), uuid.uuid4()
    same = event("refund.completed", agg, event_id=dup_id)
    other = event("refund.requested", agg)
    await produce(kafka_env, kafka_env.refund_topic, same, other, same, same, key=str(agg).encode())
    rec = Recorder()
    await asyncio.wait_for(make_consumer(committed_db, kafka_env, rec).run(once=True), 60)

    assert sorted(rec.types) == ["refund.completed", "refund.requested"]  # the 3 copies -> 1 effect
    assert await processed(committed_db) == 2
    assert (
        await committed_total(kafka_env) == 4
    )  # but every copy was consumed and its offset committed


async def test_redelivery_after_a_restart_is_deduplicated(committed_db, kafka_env) -> None:
    agg = uuid.uuid4()
    msg = event("refund.requested", agg)
    await produce(kafka_env, kafka_env.refund_topic, msg, key=str(agg).encode())
    rec = Recorder()
    await asyncio.wait_for(make_consumer(committed_db, kafka_env, rec).run(once=True), 60)
    assert len(rec.effects) == 1

    # the same event is delivered again later (e.g. the relay re-published it)
    await produce(kafka_env, kafka_env.refund_topic, msg, key=str(agg).encode())
    await asyncio.wait_for(make_consumer(committed_db, kafka_env, rec).run(once=True), 60)
    assert len(rec.effects) == 1 and await processed(committed_db) == 1


# --- crash safety ---


class CrashOnFirstOffsetCommit(EventConsumer):
    """Dies after the effect has committed but before the Kafka offset commit."""

    async def _commit_offset(self, msg: Any) -> None:
        raise SimulatedCrash


async def test_a_crash_after_the_effect_but_before_the_offset_commit_has_exactly_one_effect(
    committed_db, kafka_env
) -> None:
    agg = uuid.uuid4()
    await produce(
        kafka_env, kafka_env.refund_topic, event("refund.completed", agg), key=str(agg).encode()
    )
    rec = Recorder()

    crashing = make_consumer(committed_db, kafka_env, rec, cls=CrashOnFirstOffsetCommit)
    with pytest.raises(SimulatedCrash):
        await asyncio.wait_for(crashing.run(once=True), 60)
    assert len(rec.effects) == 1 and await processed(committed_db) == 1  # the effect DID happen
    assert await committed_total(kafka_env) == 0  # ... but the offset was never committed

    survivor = make_consumer(committed_db, kafka_env, rec)  # restart: Kafka redelivers the message
    await asyncio.wait_for(survivor.run(once=True), 60)
    assert len(rec.effects) == 1, "the redelivered message must not repeat the effect"
    assert await processed(committed_db) == 1
    assert await committed_total(kafka_env) == 1  # now it is committed


class AssertEffectCommittedFirst(EventConsumer):
    """Records, at the moment each offset is committed, whether the effect was already durable."""

    seen: list[bool]

    async def _commit_offset(self, msg: Any) -> None:
        async with self.maker() as s:
            n = await s.scalar(select(func.count()).select_from(ProcessedEvent)) or 0
        self.seen.append(n > 0)
        await super()._commit_offset(msg)


async def test_the_offset_is_committed_only_after_the_effect_transaction_committed(
    committed_db, kafka_env
) -> None:
    await produce(kafka_env, kafka_env.refund_topic, event("refund.requested", uuid.uuid4()))
    consumer = make_consumer(committed_db, kafka_env, Recorder(), cls=AssertEffectCommittedFirst)
    consumer.seen = []
    await asyncio.wait_for(consumer.run(once=True), 60)
    assert consumer.seen == [True]


# --- handler failures ---


async def test_a_transient_handler_failure_is_retried_and_applied_once(
    committed_db, kafka_env
) -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    await produce(
        kafka_env,
        kafka_env.refund_topic,
        event("refund.requested", a),
        event("refund.completed", b),
        key=b"k",
    )
    rec = Recorder()
    rec.fail_times["refund.requested"] = 2  # fails twice, then succeeds (max_attempts=3)
    await asyncio.wait_for(make_consumer(committed_db, kafka_env, rec).run(once=True), 60)
    assert rec.types == ["refund.requested", "refund.completed"]  # in order, each once
    assert await processed(committed_db) == 2


async def test_a_handler_that_always_fails_is_dead_lettered_and_does_not_block_the_partition(
    committed_db, kafka_env
) -> None:
    good = uuid.uuid4()
    await produce(
        kafka_env,
        kafka_env.refund_topic,
        event("refund.failed", uuid.uuid4()),
        event("refund.completed", good),
        key=b"k",
    )
    rec = Recorder()
    rec.always_fail.add("refund.failed")
    await asyncio.wait_for(make_consumer(committed_db, kafka_env, rec).run(once=True), 60)

    assert rec.types == ["refund.completed"]  # the next message was not blocked
    assert await processed(committed_db) == 1  # nothing was claimed for the failed one
    dead = await read_topic(kafka_env, kafka_env.dlq_topic, expected=1)
    assert len(dead) == 1
    headers = {k: v.decode() for k, v in dead[0].headers}
    assert (
        headers["x-dlq-reason"] == "handler_failed"
        and headers["x-original-topic"] == kafka_env.refund_topic
    )
    assert await committed_total(kafka_env) == 2


# --- poison messages ---


POISON = [
    ("invalid_json", b"this is not json {"),
    ("invalid_json", b"\xff\xfe\x00\x01 not utf-8"),
    ("not_an_object", b"[1, 2, 3]"),
    ("invalid_envelope", b'{"event_id": "not-a-uuid"}'),
    ("unknown_event_type", valid_envelope_bytes("refund.exploded")),
    ("unsupported_version", valid_envelope_bytes("refund.requested", version=2)),
    ("empty_message", b""),
]


async def test_poison_messages_are_dead_lettered_without_crashing_or_blocking(
    committed_db, kafka_env
) -> None:
    good_agg = uuid.uuid4()
    unexpected = valid_envelope_bytes("refund.requested").replace(
        b'"data":{', b'"data":{"email":"a@b.c",'
    )
    values = [v for _, v in POISON] + [unexpected, event("refund.completed", good_agg)]
    await produce(kafka_env, kafka_env.refund_topic, *values, key=b"same-partition")
    rec = Recorder()
    await asyncio.wait_for(make_consumer(committed_db, kafka_env, rec).run(once=True), 90)

    assert rec.types == ["refund.completed"]  # the good message after nine bad ones was processed
    assert await committed_total(kafka_env) == len(
        values
    )  # every offset committed: partition not stuck

    dead = await read_topic(kafka_env, kafka_env.dlq_topic, expected=len(values) - 1)
    reasons = [dict((k, v.decode()) for k, v in m.headers)["x-dlq-reason"] for m in dead]
    assert reasons == [r for r, _ in POISON] + ["unexpected_fields"]  # in original order
    first = dict((k, v.decode()) for k, v in dead[0].headers)
    assert first["x-original-topic"] == kafka_env.refund_topic and first["x-original-offset"] == "0"
    assert first["x-consumer"] == NAME
    assert dead[0].value == POISON[0][1]  # the original bytes are preserved for forensics


# --- lifecycle ---


async def test_the_consumer_stops_gracefully_when_asked(committed_db, kafka_env) -> None:
    rec = Recorder()
    consumer = make_consumer(committed_db, kafka_env, rec)
    task = asyncio.create_task(consumer.run())  # runs until stopped
    await asyncio.sleep(1.0)
    await produce(kafka_env, kafka_env.refund_topic, event("refund.requested", uuid.uuid4()))
    for _ in range(100):
        if rec.effects:
            break
        await asyncio.sleep(0.2)
    assert len(rec.effects) == 1
    consumer.stop.set()
    await asyncio.wait_for(task, 15)  # returns cleanly, no exception
    assert await committed_total(kafka_env) == 1

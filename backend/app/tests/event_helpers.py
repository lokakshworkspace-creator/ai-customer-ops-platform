"""Helpers for tests of the event pipeline against the REAL Redpanda."""

import asyncio
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from aiokafka import AIOKafkaConsumer
from aiokafka.admin import AIOKafkaAdminClient
from aiokafka.structs import ConsumerRecord
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.events.envelope import EventEnvelope
from app.events.kafka import KafkaEventProducer, ensure_topics
from app.events.outbox import add_event
from app.events.types import (
    AggregateType,
    EventType,
    dead_letter_topic,
    event_topics,
    topic_for,
)

REFUND_SEQUENCE = [
    EventType.REFUND_REQUESTED,
    EventType.REFUND_APPROVED,
    EventType.REFUND_COMPLETED,
]


@dataclass
class KafkaEnv:
    bootstrap: str
    prefix: str

    @property
    def refund_topic(self) -> str:
        return topic_for(AggregateType.REFUND.value, self.prefix)

    @property
    def ticket_topic(self) -> str:
        return topic_for(AggregateType.TICKET.value, self.prefix)

    @property
    def dlq_topic(self) -> str:
        return dead_letter_topic(self.prefix)

    @property
    def group(self) -> str:
        return f"{self.prefix}-notifications"

    def producer(self, **kw: Any) -> KafkaEventProducer:
        return KafkaEventProducer(self.bootstrap, startup_timeout_s=10, **kw)


async def create_env() -> KafkaEnv:
    env = KafkaEnv(get_settings().kafka_bootstrap_servers, f"t{uuid.uuid4().hex[:10]}")
    topics = {t: 3 for t in event_topics(env.prefix)} | {env.dlq_topic: 1}
    await ensure_topics(env.bootstrap, topics)
    return env


async def delete_env(env: KafkaEnv) -> None:
    admin = AIOKafkaAdminClient(bootstrap_servers=env.bootstrap)
    await admin.start()
    try:
        await admin.delete_topics([*event_topics(env.prefix), env.dlq_topic])
    except Exception:  # best effort: unique prefixes mean leftovers are harmless
        pass
    finally:
        await admin.close()


async def read_topic(
    env: KafkaEnv,
    topic: str,
    *,
    expected: int | None = None,
    max_wait: float = 20.0,
    settle: float = 2.0,
) -> list[ConsumerRecord]:
    """Everything on a topic from the beginning (no consumer group, so no rebalance delay).
    Stops when `expected` messages arrived, or after `settle` quiet seconds. Records come back in
    per-partition order, which is the order Kafka guarantees for one key."""
    consumer = AIOKafkaConsumer(
        topic,
        bootstrap_servers=env.bootstrap,
        group_id=None,
        enable_auto_commit=False,
        auto_offset_reset="earliest",
    )
    await consumer.start()
    records: list[ConsumerRecord] = []
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max_wait
        quiet_since = loop.time()
        while loop.time() < deadline:
            batches = await consumer.getmany(timeout_ms=500)
            got = [m for msgs in batches.values() for m in msgs]
            records.extend(got)
            if got:
                quiet_since = loop.time()
            if expected is not None and len(records) >= expected:
                break
            if expected is None and loop.time() - quiet_since > settle:
                break
    finally:
        await consumer.stop()
    return records


def envelopes(records: Iterable[ConsumerRecord]) -> list[EventEnvelope]:
    return [EventEnvelope.model_validate_json(r.value) for r in records]


def refund_data(refund_id: uuid.UUID, status: str = "processing") -> dict[str, Any]:
    return {
        "refund_id": refund_id,
        "order_id": uuid.uuid4(),
        "customer_id": uuid.uuid4(),
        "requested_by": uuid.uuid4(),
        "amount": "10.00",
        "currency": "INR",
        "reason_code": "damaged",
        "status": status,
    }


async def seed_events(
    maker: async_sessionmaker[AsyncSession],
    aggregates: int,
    sequence: list[EventType] = REFUND_SEQUENCE,
) -> list[uuid.UUID]:
    """Committed outbox rows, interleaved across aggregates: all first events, then all second
    events, ... (the hardest ordering for a relay). Returns the aggregate ids."""
    ids = [uuid.uuid4() for _ in range(aggregates)]
    async with maker() as s:
        for event_type in sequence:
            for agg in ids:
                add_event(
                    s,
                    aggregate_type=AggregateType.REFUND,
                    aggregate_id=agg,
                    event_type=event_type,
                    data=_data_for(event_type, agg),
                    request_id="req-seeded-0001",
                )
        await s.commit()
    return ids


def _data_for(event_type: EventType, agg: uuid.UUID) -> dict[str, Any]:
    data = refund_data(agg)
    extra = {
        EventType.REFUND_APPROVED: {"auto": True, "approved_by": None},
        EventType.REFUND_COMPLETED: {"provider_ref": "sim_rf_x"},
        EventType.REFUND_FAILED: {"failure_code": "PAYOUT_DECLINED"},
        EventType.REFUND_REJECTED: {"rejected_by": uuid.uuid4()},
    }.get(event_type, {})
    return {**data, **extra}


def valid_envelope_bytes(
    event_type: str = "refund.requested",
    aggregate_id: uuid.UUID | None = None,
    event_id: uuid.UUID | None = None,
    **overrides: Any,
) -> bytes:
    agg = aggregate_id or uuid.uuid4()
    env = EventEnvelope(
        event_id=event_id or uuid.uuid4(),
        event_type=event_type,
        occurred_at="2026-10-08T00:00:00Z",  # type: ignore[arg-type]
        request_id="req-test-0001",
        aggregate_type="refund",
        aggregate_id=str(agg),
        data={k: str(v) for k, v in refund_data(agg).items()},
        **overrides,
    )
    return env.to_bytes()


async def outbox_rows(maker: async_sessionmaker[AsyncSession], where: str = "true") -> list[dict]:
    async with maker() as s:
        rows = await s.execute(text(f"SELECT * FROM outbox_events WHERE {where} ORDER BY seq"))
        return [dict(r) for r in rows.mappings()]


# --- fake producers ---------------------------------------------------------------------------


@dataclass
class RecordingProducer:
    """In-memory producer. `fail_keys` raise; `delay` simulates a slow broker."""

    fail_keys: set[str] = field(default_factory=set)
    delay: float = 0.0
    published: list[tuple[str, str, bytes]] = field(default_factory=list)  # topic, key, value

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def publish(self, topic: str, key: bytes, value: bytes, headers=None) -> None:
        if self.delay:
            await asyncio.sleep(self.delay)
        if key.decode() in self.fail_keys:
            raise ConnectionError(f"simulated broker failure for {key.decode()}")
        self.published.append((topic, key.decode(), value))


class UnreachableProducer:
    async def start(self) -> None:
        raise ConnectionError("broker unreachable (simulated)")

    async def stop(self) -> None: ...

    async def publish(self, *a: Any, **kw: Any) -> None:  # pragma: no cover
        raise AssertionError("publish must not be reached when start() failed")


class HangingProducer:
    """start() never returns: a broker that accepts the connection and never answers."""

    async def start(self) -> None:
        await asyncio.Event().wait()

    async def stop(self) -> None: ...

    async def publish(self, *a: Any, **kw: Any) -> None:  # pragma: no cover
        raise AssertionError("not reached")


class GatedProducer:
    """Wraps a producer; the first `hold` publishes wait until `release()` is called. Lets a test
    freeze a relay in the middle of a batch while it holds its row locks."""

    def __init__(self, inner: Any, hold: int = 1) -> None:
        self.inner = inner
        self.entered = asyncio.Event()
        self._gate = asyncio.Event()
        self._remaining = hold

    def release(self) -> None:
        self._gate.set()

    async def start(self) -> None:
        await self.inner.start()

    async def stop(self) -> None:
        await self.inner.stop()

    async def publish(self, topic: str, key: bytes, value: bytes, headers=None) -> None:
        if self._remaining > 0:
            self._remaining -= 1
            self.entered.set()
            await self._gate.wait()
        await self.inner.publish(topic, key, value, headers)


class SlowProducer:
    """Delays every publish, so that two relays genuinely overlap in time."""

    def __init__(self, inner: Any, delay: float = 0.02) -> None:
        self.inner, self.delay = inner, delay

    async def start(self) -> None:
        await self.inner.start()

    async def stop(self) -> None:
        await self.inner.stop()

    async def publish(self, topic: str, key: bytes, value: bytes, headers=None) -> None:
        await asyncio.sleep(self.delay)
        await self.inner.publish(topic, key, value, headers)


class FlakyProducer:
    """Real producer, but fails for chosen aggregate keys."""

    def __init__(self, inner: Any, fail_keys: set[str]) -> None:
        self.inner, self.fail_keys = inner, fail_keys

    async def start(self) -> None:
        await self.inner.start()

    async def stop(self) -> None:
        await self.inner.stop()

    async def publish(self, topic: str, key: bytes, value: bytes, headers=None) -> None:
        if key.decode() in self.fail_keys:
            raise ConnectionError("simulated failure for " + key.decode())
        await self.inner.publish(topic, key, value, headers)

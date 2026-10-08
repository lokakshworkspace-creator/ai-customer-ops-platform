"""Kafka (Redpanda) access for the event pipeline.

THIS MODULE AND THE RELAY / CONSUMERS ARE THE ONLY PLACES ALLOWED TO TALK TO KAFKA. Request
handlers never produce directly (golden rule 7): they write outbox rows, and only the relay turns
those into messages. tests/test_no_direct_publish.py enforces it.
"""

import asyncio
from typing import Any

import structlog
from aiokafka import AIOKafkaProducer
from aiokafka.admin import AIOKafkaAdminClient, NewTopic
from aiokafka.errors import TopicAlreadyExistsError

log = structlog.get_logger(__name__)


def producer_kwargs(bootstrap: str, client_id: str, request_timeout_ms: int) -> dict[str, Any]:
    """Producer settings. `acks="all"` + an idempotent producer: a message is acknowledged only
    once all in-sync replicas have it, and retries inside the producer cannot duplicate or reorder
    it. (The outbox is still at-least-once end to end; consumers de-duplicate.)"""
    return {
        "bootstrap_servers": bootstrap,
        "client_id": client_id,
        "acks": "all",
        "enable_idempotence": True,
        "request_timeout_ms": request_timeout_ms,
        "linger_ms": 0,
    }


class KafkaEventProducer:
    """Thin wrapper so the relay and consumers can be tested with fakes."""

    def __init__(
        self,
        bootstrap: str,
        *,
        client_id: str = "ops-platform",
        request_timeout_ms: int = 10_000,
        startup_timeout_s: float = 10.0,
    ) -> None:
        self._kwargs = producer_kwargs(bootstrap, client_id, request_timeout_ms)
        self._startup_timeout_s = startup_timeout_s
        self._producer: AIOKafkaProducer | None = None

    async def start(self) -> None:
        if self._producer is not None:
            return
        producer = AIOKafkaProducer(**self._kwargs)
        try:
            await asyncio.wait_for(producer.start(), timeout=self._startup_timeout_s)
        except BaseException:
            await producer.stop()  # don't leak the half-started client
            raise
        self._producer = producer

    async def stop(self) -> None:
        if self._producer is not None:
            producer, self._producer = self._producer, None
            await producer.stop()

    async def publish(
        self, topic: str, key: bytes, value: bytes, headers: list[tuple[str, bytes]] | None = None
    ) -> None:
        """Send one message and wait for the broker's acknowledgement. Raises on any failure."""
        if self._producer is None:
            raise RuntimeError("producer not started")
        await self._producer.send_and_wait(topic, value=value, key=key, headers=headers or [])


async def ensure_topics(bootstrap: str, topics: dict[str, int]) -> None:
    """Create topics (name -> partition count) if they do not exist. Idempotent."""
    admin = AIOKafkaAdminClient(bootstrap_servers=bootstrap)
    try:
        await admin.start()
        for name, partitions in topics.items():
            try:
                await admin.create_topics(
                    [NewTopic(name=name, num_partitions=partitions, replication_factor=1)]
                )
            except TopicAlreadyExistsError:
                pass
    finally:
        await admin.close()

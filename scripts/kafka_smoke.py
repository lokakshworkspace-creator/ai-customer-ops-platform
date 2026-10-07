"""Produce one message to Redpanda and consume it back, from the host.

Usage (from the repo root, with the backend venv):
    backend/.venv/Scripts/python scripts/kafka_smoke.py

Proves that the *advertised* listener is reachable from the host: after the bootstrap
connection, clients are told to reconnect to the advertised address, so a wrong advertised
address fails here even when the bootstrap address works. Uses a throwaway topic.
"""

import asyncio
import sys
import time
import uuid

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from aiokafka.admin import AIOKafkaAdminClient, NewTopic

from app.core.config import get_settings


async def main() -> int:
    bootstrap = get_settings().kafka_bootstrap_servers
    topic = f"smoke.{uuid.uuid4().hex[:12]}"
    payload = f"hello-{uuid.uuid4().hex}".encode()
    print(f"bootstrap={bootstrap} topic={topic}")

    admin = AIOKafkaAdminClient(bootstrap_servers=bootstrap)
    await admin.start()
    producer = AIOKafkaProducer(bootstrap_servers=bootstrap, acks="all", enable_idempotence=True)
    consumer: AIOKafkaConsumer | None = None
    try:
        await admin.create_topics([NewTopic(name=topic, num_partitions=1, replication_factor=1)])

        await producer.start()
        brokers = sorted(f"{b.host}:{b.port}" for b in producer.client.cluster.brokers())
        print(f"advertised brokers: {brokers}")
        t0 = time.perf_counter()
        meta = await producer.send_and_wait(topic, value=payload, key=b"k1")
        elapsed = time.perf_counter() - t0
        print(f"produced   partition={meta.partition} offset={meta.offset} ({elapsed:.2f}s)")

        consumer = AIOKafkaConsumer(
            topic,
            bootstrap_servers=bootstrap,
            group_id=f"smoke-{uuid.uuid4().hex[:8]}",
            auto_offset_reset="earliest",
            enable_auto_commit=False,
        )
        await consumer.start()
        msg = await asyncio.wait_for(consumer.getone(), timeout=20)
        ok = msg.value == payload and msg.key == b"k1"
        print(f"consumed   offset={msg.offset} key={msg.key!r} match={ok}")
        return 0 if ok else 1
    except Exception as exc:  # noqa: BLE001  (a smoke test: report anything)
        print(f"FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if consumer is not None:
            await consumer.stop()
        await producer.stop()
        try:
            await admin.delete_topics([topic])
        except Exception:  # noqa: BLE001
            pass
        await admin.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

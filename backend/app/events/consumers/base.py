"""A reusable idempotent consumer.

For each message, in partition order:

  1. Parse + validate it. A message that can never be processed (bad JSON, unknown type, wrong
     version, unexpected fields, ...) is a POISON message: it is published to the dead-letter topic
     with the reason, then skipped. It never crashes the consumer or blocks its partition.
  2. In ONE database transaction: claim `(consumer, event_id)` in `processed_events` and run the
     handler's effect. A duplicate delivery finds the claim already there and does nothing.
  3. Only AFTER that transaction has committed, commit the Kafka offset.

So delivery is at-least-once (a crash between 2 and 3 redelivers the message) and the effect is
applied effectively once (the redelivery hits the claim and is skipped). The one thing this cannot
make exactly-once is an effect outside the database; the notification stub only logs.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import structlog
from aiokafka import AIOKafkaConsumer, TopicPartition
from aiokafka.structs import ConsumerRecord
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import ProcessedEvent
from app.events.envelope import EventEnvelope, PoisonMessage, parse_envelope
from app.events.kafka import KafkaEventProducer
from app.events.runtime import Phase, sleep_or_stop

log = structlog.get_logger(__name__)

Handler = Callable[[AsyncSession, EventEnvelope], Awaitable[None]]


@dataclass(slots=True)
class ConsumerConfig:
    name: str  # identifies this consumer in processed_events
    bootstrap: str
    topics: list[str]
    group_id: str
    dlq_topic: str
    poll_timeout_ms: int = 1000
    max_handler_attempts: int = 5
    handler_backoff_s: float = 0.5
    client_id: str = "ops-consumer"


class EventConsumer:
    def __init__(
        self,
        maker: async_sessionmaker[AsyncSession],
        handler: Handler,
        config: ConsumerConfig,
        dlq_producer: KafkaEventProducer,
    ) -> None:
        self.maker = maker
        self.handler = handler
        self.cfg = config
        self._dlq = dlq_producer
        self._consumer: AIOKafkaConsumer | None = None
        self.stop = asyncio.Event()
        self.phase = Phase("idle")  # what the consumer is doing now (named in --once timeouts)

    # -- one message ---------------------------------------------------------------------------

    async def process_message(self, msg: ConsumerRecord) -> str:
        """Returns 'processed' | 'duplicate' | 'dead_lettered'. Raises only for infrastructure
        problems (database or broker down) that the caller should retry."""
        where = f"{msg.topic}[{msg.partition}] offset {msg.offset}"
        self.phase.set(f"parsing the message at {where}")
        try:
            envelope = parse_envelope(msg.value)
        except PoisonMessage as poison:
            self.phase.set(f"dead-lettering the message at {where} (producing to the DLQ topic)")
            await self._dead_letter(msg, poison.reason, poison.detail)
            await self._commit_offset(msg)
            return "dead_lettered"

        self.phase.set(f"applying the effect of the message at {where} (database transaction)")
        outcome = await self._apply_effect(envelope, msg)
        self.phase.set(f"committing the Kafka offset for {where}")
        await self._commit_offset(msg)  # only after the effect's transaction committed
        return outcome

    async def _apply_effect(self, envelope: EventEnvelope, msg: ConsumerRecord) -> str:
        last_error: Exception | None = None
        for attempt in range(1, self.cfg.max_handler_attempts + 1):
            try:
                async with self.maker() as session:
                    claimed = await session.scalar(
                        pg_insert(ProcessedEvent)
                        .values(consumer=self.cfg.name, event_id=envelope.event_id)
                        .on_conflict_do_nothing()
                        .returning(ProcessedEvent.event_id)
                    )
                    if claimed is None:  # already processed: a duplicate delivery
                        return "duplicate"
                    await self.handler(session, envelope)
                    await session.commit()  # the claim and the effect commit together
                return "processed"
            except Exception as exc:  # the transaction rolled back: nothing was claimed
                last_error = exc
                log.warning(
                    "consumer_handler_failed",
                    consumer=self.cfg.name,
                    event_id=str(envelope.event_id),
                    attempt=attempt,
                    error=f"{type(exc).__name__}: {exc}"[:300],
                )
                if attempt < self.cfg.max_handler_attempts and await sleep_or_stop(
                    self.stop, self.cfg.handler_backoff_s * attempt
                ):
                    raise
        await self._dead_letter(
            msg, "handler_failed", f"{type(last_error).__name__}: {last_error}"[:300]
        )
        return "dead_lettered"

    async def _commit_offset(self, msg: ConsumerRecord) -> None:
        assert self._consumer is not None
        await self._consumer.commit({TopicPartition(msg.topic, msg.partition): msg.offset + 1})

    async def _dead_letter(self, msg: ConsumerRecord, reason: str, detail: str) -> None:
        log.error(
            "message_dead_lettered",
            consumer=self.cfg.name,
            reason=reason,
            detail=detail[:200],
            topic=msg.topic,
            partition=msg.partition,
            offset=msg.offset,
        )
        await self._dlq.publish(
            self.cfg.dlq_topic,
            key=msg.key or b"",
            value=msg.value or b"",
            headers=[
                ("x-dlq-reason", reason.encode()),
                ("x-dlq-detail", detail[:300].encode("utf-8", "replace")),
                ("x-original-topic", msg.topic.encode()),
                ("x-original-partition", str(msg.partition).encode()),
                ("x-original-offset", str(msg.offset).encode()),
                ("x-consumer", self.cfg.name.encode()),
            ],
        )

    # -- the loop ----------------------------------------------------------------------------------

    async def run(self, *, once: bool = False, idle_polls: int = 2) -> None:
        """Consume until `self.stop` is set; with `once`, until `idle_polls` consecutive polls
        return nothing (i.e. the backlog is drained)."""
        consumer = AIOKafkaConsumer(
            *self.cfg.topics,
            bootstrap_servers=self.cfg.bootstrap,
            group_id=self.cfg.group_id,
            client_id=self.cfg.client_id,
            enable_auto_commit=False,  # offsets are committed by us, after the effect
            auto_offset_reset="earliest",
        )
        self._consumer = consumer
        self.phase.set(
            f"starting the Kafka consumer and joining group '{self.cfg.group_id}' (group join)"
        )
        await consumer.start()
        self.phase.set("starting the dead-letter producer")
        await self._dlq.start()
        try:
            empty_polls = 0
            while not self.stop.is_set():
                self.phase.set(f"polling {', '.join(self.cfg.topics)} for messages")
                batches = await consumer.getmany(
                    timeout_ms=self.cfg.poll_timeout_ms, max_records=100
                )
                if not batches:
                    empty_polls += 1
                    if once and empty_polls >= idle_polls:
                        return
                    continue
                empty_polls = 0
                for tp, messages in batches.items():
                    for msg in messages:
                        if self.stop.is_set():
                            return  # unprocessed messages are simply redelivered next time
                        try:
                            await self.process_message(msg)
                        except Exception as exc:
                            # infrastructure trouble (DB/broker): retry THIS message, in order
                            log.error(
                                "consumer_infrastructure_error",
                                consumer=self.cfg.name,
                                error=f"{type(exc).__name__}: {exc}"[:300],
                            )
                            consumer.seek(tp, msg.offset)
                            await sleep_or_stop(self.stop, self.cfg.handler_backoff_s)
                            break
        finally:
            await self._dlq.stop()
            await consumer.stop()
            self._consumer = None

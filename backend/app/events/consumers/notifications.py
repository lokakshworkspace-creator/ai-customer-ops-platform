"""The notification consumer (a stub: it logs what it WOULD send).

    python -m app.events.consumers.notifications           # forever, until SIGINT / SIGTERM
    python -m app.events.consumers.notifications --once    # drain the backlog, then exit

It proves the loop end to end (refund / ticket -> outbox -> relay -> topic -> consumer). A real
notifier would replace `notify`; because it runs inside the dedupe transaction it must be safe to
retry, or write only to the database.
"""

import argparse
import asyncio
import sys

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import get_engine, get_sessionmaker
from app.events.consumers.base import ConsumerConfig, EventConsumer
from app.events.envelope import EventEnvelope
from app.events.kafka import KafkaEventProducer, ensure_topics
from app.events.runtime import DeadlineExceeded, install_signal_handlers, run_with_deadline
from app.events.types import dead_letter_topic, event_topics

log = structlog.get_logger(__name__)

CONSUMER_NAME = "notifications"

MESSAGES = {
    "refund.requested": "refund {refund_id} was requested for order {order_id}",
    "refund.approved": "refund {refund_id} was approved",
    "refund.completed": "refund {refund_id} was paid out",
    "refund.failed": "refund {refund_id} could not be paid out",
    "refund.rejected": "refund {refund_id} was rejected",
    "ticket.created": "support ticket {ticket_id} was opened",
}


async def notify(session: AsyncSession, envelope: EventEnvelope) -> None:
    """The effect. Only ids and state are available (see EVENT_DATA_FIELDS)."""
    template = MESSAGES.get(envelope.event_type, "event {event_type}")
    text = template.format_map({**envelope.data, "event_type": envelope.event_type})
    log.info(
        "notification",
        event_id=str(envelope.event_id),
        event_type=envelope.event_type,
        aggregate_id=envelope.aggregate_id,
        request_id=envelope.request_id,
        message=text,
    )


def build_consumer(
    settings, *, prefix: str | None = None, group_id: str | None = None
) -> EventConsumer:
    prefix = prefix or settings.kafka_topic_prefix
    config = ConsumerConfig(
        name=CONSUMER_NAME,
        bootstrap=settings.kafka_bootstrap_servers,
        topics=event_topics(prefix),
        group_id=group_id or f"{prefix}-{settings.consumer_group}",
        dlq_topic=dead_letter_topic(prefix),
        poll_timeout_ms=settings.consumer_poll_timeout_ms,
        max_handler_attempts=settings.consumer_max_handler_attempts,
        handler_backoff_s=settings.consumer_handler_backoff_s,
        client_id="ops-notifications",
    )
    dlq = KafkaEventProducer(
        settings.kafka_bootstrap_servers,
        client_id="ops-notifications-dlq",
        request_timeout_ms=settings.kafka_request_timeout_ms,
        startup_timeout_s=settings.kafka_startup_timeout_s,
    )
    return EventConsumer(get_sessionmaker(), notify, config, dlq)


async def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    ap = argparse.ArgumentParser(description="Notification consumer (logs what it would send).")
    ap.add_argument("--once", action="store_true", help="drain the backlog, then exit")
    ap.add_argument("--idle-polls", type=int, default=2, help="with --once: empty polls = drained")
    ap.add_argument(
        "--deadline",
        type=float,
        default=settings.once_deadline_s,
        help="with --once: give up after this many seconds and exit 3, naming what it was "
        f"waiting on (default {settings.once_deadline_s:g}, ONCE_DEADLINE_S; 0 = no limit)",
    )
    args = ap.parse_args(argv)
    configure_logging(settings.log_level, json_logs=settings.is_prod)

    consumer = build_consumer(settings)
    restore = install_signal_handlers(consumer.stop)

    async def work() -> None:
        topics = {t: 3 for t in event_topics(settings.kafka_topic_prefix)}
        topics[dead_letter_topic(settings.kafka_topic_prefix)] = 1
        consumer.phase.set("checking/creating the topics on Redpanda (topic metadata)")
        await ensure_topics(settings.kafka_bootstrap_servers, topics)
        log.info("consumer_started", once=args.once, group=consumer.cfg.group_id)
        await consumer.run(once=args.once, idle_polls=args.idle_polls)

    try:
        if args.once:
            try:
                await run_with_deadline(work(), args.deadline, consumer.phase, "consumer --once")
            except DeadlineExceeded as exc:
                log.error(
                    "consumer_deadline_exceeded", waiting_on=exc.waiting_on, seconds=exc.seconds
                )
                print(f"consumer: {exc}", file=sys.stderr)
                return 3
        else:
            await work()
        log.info("consumer_stopped")
        return 0
    finally:
        restore()
        await get_engine().dispose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

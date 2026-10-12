"""The workflow-resume consumer: finishes a chat turn that is waiting for a human, by EVENT.

    python -m app.events.consumers.resume           # forever, until SIGINT / SIGTERM
    python -m app.events.consumers.resume --once    # drain the backlog, then exit

When an administrator approves or rejects a refund, nobody decides it in time, or its payout
finishes, the outbox relay publishes `refund.approved` / `refund.rejected` / `refund.expired` /
`refund.completed` / `refund.failed`. This
consumer finds the chat turn waiting for that refund and finishes it with the same templated
message the read-time fallback would write (app/agent/resume.py). It NEVER calls the model and
NEVER runs a tool: it only reads the refund, the approval and the workflow, and writes the
workflow's state and one assistant message.

It is a separate process built on the same base class as the notification consumer, so it inherits
the guarantees: the claim in `processed_events` and the effect commit in ONE transaction (a
duplicate delivery does nothing), the Kafka offset is committed after, and a message that can never
be processed is dead-lettered instead of blocking the partition.

What it reads from an event: `event_type`, `aggregate_id` and, from `data`, `refund_id` and
`customer_id` (ids only, golden rule 7). Everything else (amounts, the decision, the payout status)
comes from the DATABASE, the source of truth, not from the event. Whether the turn is final is
decided by `resume.waiting_for`: an approved refund whose payout is still `processing` keeps
waiting, and the later `refund.completed` / `refund.failed` (also emitted by the reconcile path)
finishes it.

The read-time fallback (`GET /workflows/{id}`) stays, so a stopped consumer cannot strand anyone.
"""

import argparse
import asyncio
import sys
import uuid

import structlog
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import resume
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.models import WorkflowRun
from app.db.models.enums import WorkflowStatus
from app.db.session import get_engine, get_sessionmaker
from app.events.consumers.base import ConsumerConfig, EventConsumer
from app.events.envelope import EventEnvelope
from app.events.kafka import KafkaEventProducer, ensure_topics
from app.events.runtime import DeadlineExceeded, install_signal_handlers, run_with_deadline
from app.events.types import AggregateType, EventType, dead_letter_topic, topic_for

log = structlog.get_logger(__name__)

CONSUMER_NAME = "workflow-resume"

# The events that can finish a waiting turn. Anything else is claimed and ignored.
RESUME_EVENTS = frozenset(
    {
        EventType.REFUND_APPROVED.value,
        EventType.REFUND_REJECTED.value,
        EventType.REFUND_EXPIRED.value,
        EventType.REFUND_COMPLETED.value,
        EventType.REFUND_FAILED.value,
    }
)

# `context ->> 'refund_id'` is written literally (not as a bound key) so that it matches the partial
# index of migration 0005 (ix_workflow_runs_waiting_refund).
_WAITING_FOR_REFUND = text("(workflow_runs.context ->> 'refund_id') = :refund_id")


async def resume_waiting_turn(session: AsyncSession, envelope: EventEnvelope) -> None:
    """The effect. Runs inside the dedupe transaction: it must not commit or roll back."""
    if envelope.event_type not in RESUME_EVENTS:
        return
    refund_id = str(uuid.UUID(str(envelope.data["refund_id"])))  # a bad id fails loudly
    if refund_id != str(uuid.UUID(envelope.aggregate_id)):
        log.warning(
            "resume_event_ignored", reason="refund_id_mismatch", event_id=str(envelope.event_id)
        )
        return
    query = select(WorkflowRun.id).where(
        WorkflowRun.status == WorkflowStatus.waiting_approval,
        _WAITING_FOR_REFUND.bindparams(refund_id=refund_id),
    )
    customer_id = envelope.data.get("customer_id")
    if customer_id:  # the turn must belong to the customer the event is about
        query = query.where(WorkflowRun.user_id == uuid.UUID(str(customer_id)))
    for run_id in list(await session.scalars(query)):
        if await resume.finish_in_transaction(session, run_id):
            log.info(
                "workflow_resumed",
                workflow_id=str(run_id),
                refund_id=refund_id,
                event_type=envelope.event_type,
                event_id=str(envelope.event_id),
                request_id=envelope.request_id,
            )


def build_consumer(
    settings, *, prefix: str | None = None, group_id: str | None = None
) -> EventConsumer:
    prefix = prefix or settings.kafka_topic_prefix
    config = ConsumerConfig(
        name=CONSUMER_NAME,
        bootstrap=settings.kafka_bootstrap_servers,
        topics=[topic_for(AggregateType.REFUND.value, prefix)],
        group_id=group_id or f"{prefix}-{settings.consumer_group}-resume",
        dlq_topic=dead_letter_topic(prefix),
        poll_timeout_ms=settings.consumer_poll_timeout_ms,
        max_handler_attempts=settings.consumer_max_handler_attempts,
        handler_backoff_s=settings.consumer_handler_backoff_s,
        client_id="ops-workflow-resume",
    )
    dlq = KafkaEventProducer(
        settings.kafka_bootstrap_servers,
        client_id="ops-workflow-resume-dlq",
        request_timeout_ms=settings.kafka_request_timeout_ms,
        startup_timeout_s=settings.kafka_startup_timeout_s,
    )
    return EventConsumer(get_sessionmaker(), resume_waiting_turn, config, dlq)


async def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    ap = argparse.ArgumentParser(description="Workflow-resume consumer (finishes waiting turns).")
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
        topics = {topic_for(AggregateType.REFUND.value, settings.kafka_topic_prefix): 3}
        topics[dead_letter_topic(settings.kafka_topic_prefix)] = 1
        consumer.phase.set("checking/creating the topics on Redpanda (topic metadata)")
        await ensure_topics(settings.kafka_bootstrap_servers, topics)
        log.info("resume_consumer_started", once=args.once, group=consumer.cfg.group_id)
        await consumer.run(once=args.once, idle_polls=args.idle_polls)

    try:
        if args.once:
            try:
                await run_with_deadline(work(), args.deadline, consumer.phase, "resume --once")
            except DeadlineExceeded as exc:
                log.error(
                    "consumer_deadline_exceeded", waiting_on=exc.waiting_on, seconds=exc.seconds
                )
                print(f"resume consumer: {exc}", file=sys.stderr)
                return 3
        else:
            await work()
        log.info("resume_consumer_stopped")
        return 0
    finally:
        restore()
        await get_engine().dispose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

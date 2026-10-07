"""The outbox relay: publishes `outbox_events` rows to Redpanda. Run it as its own process:

    python -m app.events.relay            # forever, until SIGINT / SIGTERM
    python -m app.events.relay --once     # publish everything that is due, then exit

Guarantees (see docs/events.md):
* AT-LEAST-ONCE: a row is marked published only AFTER the broker has acknowledged the message, and
  in the same transaction that holds its row lock. A crash in between re-publishes the event;
  consumers de-duplicate on `event_id`.
* SAFE TO RUN TWICE: rows are claimed with FOR UPDATE ... SKIP LOCKED, so concurrent relays take
  disjoint rows.
* PER-AGGREGATE ORDER: an event is not published while an EARLIER unpublished event of the same
  aggregate exists (even one another relay is working on, in backoff, or parked), and the Kafka key
  is the aggregate id, so one aggregate's events stay in order in one partition.
* RETRIES: a failed publish increments `attempts`, records `last_error` and delays the next try
  exponentially (`next_attempt_at`). After `max_attempts` the row is PARKED: retries stop and an
  error is logged. TRADE-OFF: a parked event is still unpublished, so it BLOCKS every later event
  of that aggregate until an operator resolves it (scripts/outbox_admin.py). Ordering and
  completeness are preferred over availability for a single aggregate.
"""

import argparse
import asyncio
from dataclasses import dataclass
from typing import Any, Protocol

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import get_engine, get_sessionmaker
from app.events.envelope import envelope_from_row
from app.events.kafka import KafkaEventProducer, ensure_topics
from app.events.runtime import install_signal_handlers, sleep_or_stop
from app.events.types import ENVELOPE_VERSION, event_topics, topic_for

log = structlog.get_logger(__name__)

# Due, unpublished, not parked, and no EARLIER unpublished event for the same aggregate.
# FOR UPDATE ... SKIP LOCKED: concurrent relays claim disjoint rows.
SELECT_DUE = text(
    """
    SELECT e.id, e.seq, e.aggregate_type, e.aggregate_id, e.event_type, e.payload,
           e.created_at, e.attempts
    FROM outbox_events e
    WHERE e.published_at IS NULL
      AND e.parked_at IS NULL
      AND e.next_attempt_at <= now()
      AND NOT EXISTS (
            SELECT 1 FROM outbox_events earlier
            WHERE earlier.aggregate_id = e.aggregate_id
              AND earlier.published_at IS NULL
              AND earlier.seq < e.seq)
    ORDER BY e.seq
    LIMIT :batch
    FOR UPDATE OF e SKIP LOCKED
    """
)
MARK_PUBLISHED = text("UPDATE outbox_events SET published_at = now() WHERE id = :id")
MARK_FAILED = text(
    """
    UPDATE outbox_events
    SET attempts = attempts + 1,
        last_error = :error,
        next_attempt_at = now() + make_interval(secs => :delay),
        parked_at = CASE WHEN attempts + 1 >= :max_attempts THEN now() ELSE parked_at END
    WHERE id = :id
    RETURNING attempts, parked_at
    """
)


class EventProducer(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def publish(
        self, topic: str, key: bytes, value: bytes, headers: list[tuple[str, bytes]] | None = None
    ) -> None: ...


@dataclass(slots=True)
class RelayStats:
    selected: int = 0
    published: int = 0
    failed: int = 0
    parked: int = 0

    def add(self, other: "RelayStats") -> None:
        self.selected += other.selected
        self.published += other.published
        self.failed += other.failed
        self.parked += other.parked


def backoff_seconds(attempts_so_far: int, base: float, cap: float) -> float:
    """Delay before the next try, given how many attempts have already FAILED (>= 0):
    base, 2*base, 4*base, ... capped."""
    return min(cap, base * (2**attempts_so_far))


class Relay:
    def __init__(
        self,
        maker: async_sessionmaker[AsyncSession],
        producer: EventProducer,
        *,
        topic_prefix: str = "ops",
        batch_size: int = 50,
        max_attempts: int = 10,
        backoff_base_s: float = 1.0,
        backoff_max_s: float = 300.0,
    ) -> None:
        self.maker = maker
        self.producer = producer
        self.topic_prefix = topic_prefix
        self.batch_size = batch_size
        self.max_attempts = max_attempts
        self.backoff_base_s = backoff_base_s
        self.backoff_max_s = backoff_max_s

    async def run_once(self) -> RelayStats:
        """Claim one batch, publish it, record the outcome, commit. One transaction."""
        stats = RelayStats()
        async with self.maker() as session:
            rows = list(
                (await session.execute(SELECT_DUE, {"batch": self.batch_size})).mappings().all()
            )
            stats.selected = len(rows)
            if not rows:
                return stats  # idle: nothing claimed, the broker is never contacted

            try:
                await self.producer.start()
            except Exception as exc:  # broker unreachable: every claimed row has failed
                outcomes: dict[Any, Exception | None] = {row["id"]: exc for row in rows}
            else:
                results = await asyncio.gather(*[self._publish_one(row) for row in rows])
                outcomes = {row["id"]: err for row, err in zip(rows, results, strict=True)}

            for row in rows:
                error = outcomes[row["id"]]
                if error is None:
                    await session.execute(MARK_PUBLISHED, {"id": row["id"]})
                    stats.published += 1
                else:
                    await self._record_failure(session, row, error, stats)
            await session.commit()
        return stats

    async def _publish_one(self, row: Any) -> Exception | None:
        """Publish one row and wait for the broker's acknowledgement. Returns the error, if any."""
        try:
            envelope = envelope_from_row(row)
            await self.producer.publish(
                topic_for(row["aggregate_type"], self.topic_prefix),
                key=row["aggregate_id"].encode("utf-8"),
                value=envelope.to_bytes(),
                headers=[
                    ("event_id", str(envelope.event_id).encode()),
                    ("event_type", envelope.event_type.encode()),
                    ("version", str(ENVELOPE_VERSION).encode()),
                ],
            )
        except Exception as exc:  # CancelledError is not an Exception: it propagates
            return exc
        return None

    async def _record_failure(
        self, session: AsyncSession, row: Any, error: Exception, stats: RelayStats
    ) -> None:
        delay = backoff_seconds(row["attempts"], self.backoff_base_s, self.backoff_max_s)
        result = await session.execute(
            MARK_FAILED,
            {
                "id": row["id"],
                "error": f"{type(error).__name__}: {error}"[:1000],
                "delay": delay,
                "max_attempts": self.max_attempts,
            },
        )
        attempts, parked_at = result.one()
        stats.failed += 1
        if parked_at is not None and row["attempts"] + 1 >= self.max_attempts:
            stats.parked += 1
            log.error(
                "outbox_event_parked",
                event_id=str(row["id"]),
                aggregate_id=row["aggregate_id"],
                event_type=row["event_type"],
                attempts=attempts,
                error=f"{type(error).__name__}: {error}"[:300],
            )
        else:
            log.warning(
                "outbox_publish_failed",
                event_id=str(row["id"]),
                aggregate_id=row["aggregate_id"],
                attempts=attempts,
                retry_in_s=delay,
                error=f"{type(error).__name__}: {error}"[:300],
            )

    async def drain(
        self, stop: asyncio.Event | None = None, max_rounds: int = 10_000
    ) -> RelayStats:
        """Run until nothing more is due (successive rounds are needed because a batch holds at
        most one event per aggregate, to keep per-aggregate order)."""
        total = RelayStats()
        for _ in range(max_rounds):
            if stop is not None and stop.is_set():
                break
            round_stats = await self.run_once()
            total.add(round_stats)
            if round_stats.selected == 0:
                break
        return total

    async def run_forever(self, stop: asyncio.Event, poll_interval_s: float) -> None:
        while not stop.is_set():
            try:
                await self.drain(stop)
            except Exception:  # e.g. the database blinked: log, wait, try again
                log.exception("relay_cycle_failed")
            if await sleep_or_stop(stop, poll_interval_s):
                break


async def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    ap = argparse.ArgumentParser(description="Publish outbox events to Redpanda.")
    ap.add_argument("--once", action="store_true", help="publish what is due, then exit")
    ap.add_argument("--batch-size", type=int, default=settings.outbox_batch_size)
    ap.add_argument("--poll-interval", type=float, default=settings.outbox_poll_interval_s)
    args = ap.parse_args(argv)
    configure_logging(settings.log_level, json_logs=settings.is_prod)

    producer = KafkaEventProducer(
        settings.kafka_bootstrap_servers,
        client_id="ops-outbox-relay",
        request_timeout_ms=settings.kafka_request_timeout_ms,
        startup_timeout_s=settings.kafka_startup_timeout_s,
    )
    relay = Relay(
        get_sessionmaker(),
        producer,
        topic_prefix=settings.kafka_topic_prefix,
        batch_size=args.batch_size,
        max_attempts=settings.outbox_max_attempts,
        backoff_base_s=settings.outbox_backoff_base_s,
        backoff_max_s=settings.outbox_backoff_max_s,
    )
    stop = asyncio.Event()
    restore = install_signal_handlers(stop)
    try:
        try:
            await ensure_topics(
                settings.kafka_bootstrap_servers,
                {t: 3 for t in event_topics(settings.kafka_topic_prefix)},
            )
        except Exception as exc:  # the broker may be down; publishing will back off and retry
            log.warning("ensure_topics_failed", error=f"{type(exc).__name__}: {exc}"[:200])
        log.info("relay_started", once=args.once, prefix=settings.kafka_topic_prefix)
        if args.once:
            stats = await relay.drain(stop)
            log.info("relay_finished", **{k: getattr(stats, k) for k in stats.__slots__})
        else:
            await relay.run_forever(stop, args.poll_interval)
        log.info("relay_stopped")
        return 0
    finally:
        restore()
        await producer.stop()
        await get_engine().dispose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

"""`relay --once` and `consumer --once` must never hang: past their deadline they exit non-zero
(3) with a message naming what they were waiting on."""

import asyncio
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from app.events import relay as relay_module
from app.events.consumers.base import ConsumerConfig, EventConsumer
from app.events.relay import Relay
from app.events.runtime import DeadlineExceeded, Phase, run_with_deadline
from app.events.types import EventType
from app.tests.conftest import CommittedDB, _test_db_url
from app.tests.event_helpers import (
    HangingProducer,
    KafkaEnv,
    RecordingProducer,
    outbox_rows,
    seed_events,
    valid_envelope_bytes,
)

BACKEND = Path(__file__).resolve().parents[2]


# --- the helper ---


async def test_work_that_finishes_in_time_returns_its_result() -> None:
    async def work() -> int:
        await asyncio.sleep(0.01)
        return 42

    assert await run_with_deadline(work(), 5, Phase(), "x") == 42


async def test_work_that_hangs_raises_deadline_exceeded_naming_the_phase() -> None:
    phase = Phase("initial")

    async def work() -> None:
        phase.set("waiting for the broker to acknowledge 3 events")
        await asyncio.Event().wait()

    with pytest.raises(DeadlineExceeded) as exc:
        await asyncio.wait_for(run_with_deadline(work(), 0.3, phase, "relay --once"), 10)
    message = str(exc.value)
    assert "relay --once exceeded its 0.3s deadline" in message
    assert "waiting on: waiting for the broker to acknowledge 3 events" in message
    assert "--deadline" in message and "ONCE_DEADLINE_S" in message
    assert (exc.value.seconds, exc.value.waiting_on) == (
        0.3,
        "waiting for the broker to acknowledge 3 events",
    )


async def test_zero_means_no_deadline() -> None:
    async def work() -> str:
        await asyncio.sleep(0.2)
        return "done"

    assert await run_with_deadline(work(), 0, Phase(), "x") == "done"


async def test_a_timeout_raised_by_the_work_itself_is_not_mistaken_for_the_deadline() -> None:
    async def work() -> None:
        raise TimeoutError("an inner timeout")

    with pytest.raises(TimeoutError, match="inner timeout"):
        await run_with_deadline(work(), 5, Phase(), "x")


# --- relay ---


@pytest.mark.usefixtures("committed_db")
async def test_a_relay_stuck_connecting_to_the_broker_reports_that(
    committed_db: CommittedDB,
) -> None:
    await seed_events(committed_db.maker, 1, [EventType.REFUND_REQUESTED])
    relay = Relay(committed_db.maker, HangingProducer())
    with pytest.raises(DeadlineExceeded) as exc:
        await asyncio.wait_for(
            run_with_deadline(relay.drain(), 1.0, relay.phase, "relay --once"), 15
        )
    assert "connecting the Kafka producer to Redpanda" in str(exc.value)
    # nothing was lost: the claimed row was rolled back and is still waiting in the outbox
    (row,) = await outbox_rows(committed_db.maker)
    assert row["published_at"] is None and row["attempts"] == 0


class _Engine:
    async def dispose(self) -> None: ...


async def _noop(*a, **kw) -> None:
    return None


@pytest.mark.usefixtures("committed_db")
async def test_relay_main_exits_3_with_a_clear_message_on_timeout(
    committed_db: CommittedDB, monkeypatch, capsys
) -> None:
    await seed_events(committed_db.maker, 1, [EventType.REFUND_REQUESTED])
    monkeypatch.setattr(relay_module, "get_sessionmaker", lambda: committed_db.maker)
    monkeypatch.setattr(relay_module, "get_engine", lambda: _Engine())
    monkeypatch.setattr(relay_module, "KafkaEventProducer", lambda *a, **kw: HangingProducer())
    monkeypatch.setattr(relay_module, "ensure_topics", _noop)

    assert await asyncio.wait_for(relay_module.main(["--once", "--deadline", "1"]), 20) == 3
    err = capsys.readouterr().err
    assert "relay: relay --once exceeded its 1s deadline" in err
    assert "connecting the Kafka producer to Redpanda" in err


@pytest.mark.usefixtures("committed_db")
async def test_relay_main_under_the_deadline_exits_0(
    committed_db: CommittedDB, monkeypatch
) -> None:
    await seed_events(committed_db.maker, 2, [EventType.REFUND_REQUESTED])
    monkeypatch.setattr(relay_module, "get_sessionmaker", lambda: committed_db.maker)
    monkeypatch.setattr(relay_module, "get_engine", lambda: _Engine())
    monkeypatch.setattr(relay_module, "KafkaEventProducer", lambda *a, **kw: RecordingProducer())
    monkeypatch.setattr(relay_module, "ensure_topics", _noop)
    assert await relay_module.main(["--once", "--deadline", "30"]) == 0
    assert all(r["published_at"] for r in await outbox_rows(committed_db.maker))


# --- consumer (real Redpanda) ---


async def test_a_consumer_stuck_in_its_handler_reports_that_and_loses_nothing(
    committed_db: CommittedDB, kafka_env: KafkaEnv
) -> None:
    producer = kafka_env.producer()
    await producer.start()
    try:
        await producer.publish(
            kafka_env.refund_topic, b"agg", valid_envelope_bytes("refund.requested")
        )
    finally:
        await producer.stop()

    async def blocks_forever(session, envelope) -> None:
        await asyncio.Event().wait()

    cfg = ConsumerConfig(
        name="notifications",
        bootstrap=kafka_env.bootstrap,
        topics=[kafka_env.refund_topic, kafka_env.ticket_topic],
        group_id=kafka_env.group,
        dlq_topic=kafka_env.dlq_topic,
        poll_timeout_ms=500,
    )
    consumer = EventConsumer(committed_db.maker, blocks_forever, cfg, kafka_env.producer())
    with pytest.raises(DeadlineExceeded) as exc:
        await asyncio.wait_for(
            run_with_deadline(consumer.run(once=True), 15, consumer.phase, "consumer --once"), 40
        )
    assert "applying the effect of the message at" in str(exc.value)
    assert kafka_env.refund_topic in str(exc.value)

    # the message was neither acknowledged nor claimed: a healthy consumer still gets it
    seen: list[str] = []

    async def ok(session, envelope) -> None:
        seen.append(envelope.event_type)

    healthy = EventConsumer(committed_db.maker, ok, cfg, kafka_env.producer())
    await asyncio.wait_for(healthy.run(once=True), 60)
    assert seen == ["refund.requested"]


# --- real processes against a broker that never answers ---


async def _mute_broker() -> tuple[asyncio.Server, int]:
    async def hold(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.read()  # accept, read, never answer
        finally:
            writer.close()

    server = await asyncio.start_server(hold, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


def _run(module: str, port: int, deadline: int) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(
        DATABASE_URL=_test_db_url().render_as_string(hide_password=False),
        KAFKA_BOOTSTRAP_SERVERS=f"127.0.0.1:{port}",
        KAFKA_TOPIC_PREFIX=f"t{uuid.uuid4().hex[:8]}",
        PYTHONIOENCODING="utf-8",
    )
    return subprocess.run(
        [sys.executable, "-m", module, "--once", "--deadline", str(deadline)],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=25,
    )


@pytest.mark.parametrize(
    ("module", "label"),
    [("app.events.relay", "relay"), ("app.events.consumers.notifications", "consumer")],
)
async def test_the_real_process_exits_3_instead_of_hanging_on_a_mute_broker(module, label) -> None:
    server, port = await _mute_broker()
    try:
        t0 = asyncio.get_running_loop().time()
        done = await asyncio.to_thread(_run, module, port, 3)
        elapsed = asyncio.get_running_loop().time() - t0
    finally:
        server.close()
    assert done.returncode == 3, done.stdout[-600:] + done.stderr[-600:]
    assert f"{label} --once exceeded its 3s deadline" in done.stderr
    assert "waiting on: checking/creating the topics on Redpanda (topic metadata)" in done.stderr
    assert elapsed < 40, f"took {elapsed:.0f}s: the deadline did not bound the run"

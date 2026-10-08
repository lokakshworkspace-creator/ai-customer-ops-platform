"""The relay and the consumer as real, separate processes (python -m ...), and their shutdown."""

import asyncio
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.core.config import get_settings
from app.db.models import ProcessedEvent
from app.events.relay import Relay
from app.events.runtime import install_signal_handlers, sleep_or_stop
from app.events.types import EventType
from app.tests.conftest import CommittedDB, _test_db_url
from app.tests.event_helpers import (
    KafkaEnv,
    RecordingProducer,
    envelopes,
    outbox_rows,
    read_topic,
    seed_events,
)

BACKEND = Path(__file__).resolve().parents[2]


def child_env(env: KafkaEnv) -> dict[str, str]:
    e = dict(os.environ)
    e.update(
        DATABASE_URL=_test_db_url().render_as_string(hide_password=False),
        KAFKA_TOPIC_PREFIX=env.prefix,
        KAFKA_BOOTSTRAP_SERVERS=env.bootstrap,
        PYTHONIOENCODING="utf-8",
        APP_ENV="dev",
    )
    return e


def run_module(
    module: str, env: KafkaEnv, *args: str, timeout: int = 90
) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            [sys.executable, "-m", module, *args],
            cwd=BACKEND,
            env=child_env(env),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:  # show where it was stuck
        out = exc.stdout or b""
        out = out.decode("utf-8", "replace") if isinstance(out, bytes) else out
        raise AssertionError(
            f"{module} hung for {timeout}s; output so far:\n{out[-3000:]}"
        ) from None


@pytest.mark.usefixtures("committed_db")
async def test_the_cli_entry_points_work_as_real_processes(
    committed_db: CommittedDB, kafka_env
) -> None:
    await seed_events(committed_db.maker, 2)  # 2 refunds x 3 events

    relay = await asyncio.to_thread(run_module, "app.events.relay", kafka_env, "--once")
    assert relay.returncode == 0, relay.stderr[-800:] + relay.stdout[-800:]
    assert "relay_finished" in relay.stdout and "published=6" in relay.stdout.replace(" ", "")
    assert all(r["published_at"] for r in await outbox_rows(committed_db.maker))
    assert len(envelopes(await read_topic(kafka_env, kafka_env.refund_topic, expected=6))) == 6

    consumer = await asyncio.to_thread(
        run_module, "app.events.consumers.notifications", kafka_env, "--once"
    )
    assert consumer.returncode == 0, consumer.stderr[-800:] + consumer.stdout[-800:]
    assert consumer.stdout.count("notification") >= 6
    async with committed_db.maker() as s:
        assert await s.scalar(select(func.count()).select_from(ProcessedEvent)) == 6

    # a second pass of both processes changes nothing (idempotent, nothing left to do)
    again = await asyncio.to_thread(run_module, "app.events.relay", kafka_env, "--once")
    assert again.returncode == 0 and "published=0" in again.stdout.replace(" ", "")
    again = await asyncio.to_thread(
        run_module, "app.events.consumers.notifications", kafka_env, "--once"
    )
    assert again.returncode == 0
    async with committed_db.maker() as s:
        assert await s.scalar(select(func.count()).select_from(ProcessedEvent)) == 6


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal delivery to a child process")
@pytest.mark.usefixtures("committed_db")
async def test_a_running_relay_process_shuts_down_cleanly_on_sigterm(
    committed_db, kafka_env
) -> None:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "app.events.relay",
        "--poll-interval",
        "0.2",
        cwd=BACKEND,
        env=child_env(kafka_env),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        await asyncio.sleep(4)
        assert proc.returncode is None, "the relay should still be running"
        proc.send_signal(signal.SIGTERM)
        # generous: a cold start costs ~11 s of imports (see the README), then shutdown
        raw, _ = await asyncio.wait_for(proc.communicate(), 60)
    finally:
        if proc.returncode is None:
            proc.kill()
    assert proc.returncode == 0 and "relay_stopped" in raw.decode("utf-8", "replace")


# --- shutdown, in process (works on every OS) ---


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
async def test_signals_set_the_stop_event_instead_of_killing_the_process(sig) -> None:
    stop = asyncio.Event()
    restore = install_signal_handlers(stop)
    try:
        assert not stop.is_set()
        signal.raise_signal(sig)
        await asyncio.wait_for(stop.wait(), 5)
    finally:
        restore()
    assert stop.is_set()


async def test_handlers_are_restored() -> None:
    before = signal.getsignal(signal.SIGINT)
    restore = install_signal_handlers(asyncio.Event())
    assert signal.getsignal(signal.SIGINT) is not before
    restore()
    assert signal.getsignal(signal.SIGINT) is before


async def test_sleep_or_stop_wakes_early() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.call_later(0.1, stop.set)
    t0 = loop.time()
    assert await sleep_or_stop(stop, 10) is True
    assert loop.time() - t0 < 2
    assert await sleep_or_stop(asyncio.Event(), 0.05) is False


@pytest.mark.usefixtures("committed_db")
async def test_run_forever_finishes_its_cycle_and_returns_when_asked_to_stop(
    committed_db: CommittedDB, kafka_env
) -> None:
    await seed_events(committed_db.maker, 3, [EventType.REFUND_REQUESTED])  # 3 events
    producer = RecordingProducer()
    relay = Relay(committed_db.maker, producer, topic_prefix=kafka_env.prefix)
    stop = asyncio.Event()
    task = asyncio.create_task(relay.run_forever(stop, poll_interval_s=0.1))
    for _ in range(100):
        if len(producer.published) == 3:
            break
        await asyncio.sleep(0.1)
    assert len(producer.published) == 3
    stop.set()
    await asyncio.wait_for(task, 5)  # returns promptly, no exception, nothing half-done
    assert all(r["published_at"] for r in await outbox_rows(committed_db.maker))


def test_the_default_settings_are_what_the_docs_say() -> None:
    s = get_settings()
    assert (s.outbox_max_attempts, s.outbox_backoff_base_s, s.outbox_backoff_max_s) == (
        10,
        1.0,
        300.0,
    )
    assert s.outbox_batch_size == 50 and s.kafka_topic_prefix == "ops"
    assert s.consumer_max_handler_attempts == 5

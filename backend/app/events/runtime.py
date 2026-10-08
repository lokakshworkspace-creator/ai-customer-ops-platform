"""Shared plumbing for the long-running event processes (relay, consumers)."""

import asyncio
import signal
import threading
from collections.abc import Awaitable, Callable

import structlog

log = structlog.get_logger(__name__)


def install_signal_handlers(stop: asyncio.Event) -> Callable[[], None]:
    """Set `stop` on SIGINT / SIGTERM so the process finishes its current unit of work and exits
    cleanly (nothing is cancelled mid-publish). Returns a function that restores the previous
    handlers. Uses signal.signal (not loop.add_signal_handler) so it also works on Windows."""
    if threading.current_thread() is not threading.main_thread():
        return lambda: None  # signals can only be handled from the main thread
    loop = asyncio.get_running_loop()

    def handler(signum: int, frame: object) -> None:
        log.info("shutdown_requested", signal=signal.Signals(signum).name)
        loop.call_soon_threadsafe(stop.set)

    previous = {sig: signal.signal(sig, handler) for sig in (signal.SIGINT, signal.SIGTERM)}

    def restore() -> None:
        for sig, old in previous.items():
            signal.signal(sig, old)

    return restore


async def sleep_or_stop(stop: asyncio.Event, seconds: float) -> bool:
    """Sleep up to `seconds`, waking early if `stop` is set. Returns True if stopping."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        return False
    return True


class Phase:
    """What a long-running process is doing right now, in words. A hang is only diagnosable if the
    message says what it was waiting on, so each stage records itself here."""

    def __init__(self, initial: str = "starting up") -> None:
        self.current = initial

    def set(self, text: str) -> None:
        self.current = text


class DeadlineExceeded(Exception):
    """A `--once` run did not finish in time. The message says what it was waiting on."""

    def __init__(self, what: str, seconds: float, waiting_on: str) -> None:
        self.what, self.seconds, self.waiting_on = what, seconds, waiting_on
        super().__init__(
            f"{what} exceeded its {seconds:g}s deadline. It was waiting on: {waiting_on}. "
            "Check that Postgres and Redpanda are up and reachable; nothing is lost (see "
            "docs/events.md). Raise the limit with --deadline SECONDS or ONCE_DEADLINE_S "
            "(0 disables it)."
        )


async def run_with_deadline[T](work: Awaitable[T], seconds: float, phase: Phase, what: str) -> T:
    """Run `work`, but give up after `seconds` (0 = no limit) with a DeadlineExceeded that names
    the phase it was stuck in. Only OUR timeout is converted: other TimeoutErrors pass through."""
    if seconds <= 0:
        return await work
    deadline = asyncio.timeout(seconds)
    try:
        async with deadline:
            return await work
    except TimeoutError:
        if not deadline.expired():
            raise
        raise DeadlineExceeded(what, seconds, phase.current) from None

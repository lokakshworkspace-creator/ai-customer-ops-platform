"""Shared plumbing for the long-running event processes (relay, consumers)."""

import asyncio
import signal
import threading
from collections.abc import Callable

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

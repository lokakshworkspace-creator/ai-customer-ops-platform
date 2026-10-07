"""Payment provider interface and a simulated implementation.

The provider call is the one step with an external side effect (money moves), so it is the one
place where retries must be safe: every call carries an idempotency key and the provider must
return the SAME result for the same key. The key is `refund-<refund uuid>`, never the caller's
own Idempotency-Key (which is only unique per user).
"""

import asyncio
import random
import uuid
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from typing import Literal, Protocol

from app.core.config import get_settings


class ProviderUnavailable(Exception):
    """The provider did not answer (timeout, network). The OUTCOME IS UNKNOWN: the payout may
    or may not have happened. Callers must leave the refund `processing` and retry with the
    same key."""


@dataclass(frozen=True, slots=True)
class ProviderResult:
    status: Literal["succeeded", "failed"]
    provider_ref: str | None = None
    error: str | None = None


def provider_key_for(refund_id: uuid.UUID) -> str:
    return f"refund-{refund_id}"


class PaymentProvider(Protocol):
    async def refund(
        self,
        *,
        payment_ref: str | None,
        amount: Decimal,
        currency: str,
        idempotency_key: str,
    ) -> ProviderResult: ...


@dataclass(slots=True)
class LedgerEntry:
    idempotency_key: str
    amount: Decimal
    currency: str
    result: ProviderResult


class SimulatedPaymentProvider:
    """In-memory provider with an idempotency ledger, configurable latency and failures.

    The ledger lives in this process only (a real provider keeps it server-side), so exactly-once
    across process restarts is demonstrated by sharing one instance, as the tests do.
    """

    def __init__(
        self,
        *,
        latency_ms: int = 0,
        failure_rate: float = 0.0,
        rng: random.Random | None = None,
    ) -> None:
        self.latency_ms = latency_ms
        self.failure_rate = failure_rate
        self._rng = rng or random.Random()
        self.ledger: dict[str, LedgerEntry] = {}
        self.calls = 0
        self._fail_next = 0
        self._unavailable_next = 0
        self._lose_response_next = 0
        self._locks: dict[str, asyncio.Lock] = {}

    # -- test / dev controls --------------------------------------------------------------
    def fail_next(self, n: int = 1) -> None:
        """The next n NEW payouts are declined (and recorded as declined)."""
        self._fail_next = n

    def unavailable_next(self, n: int = 1) -> None:
        """The next n calls time out BEFORE doing anything (nothing is recorded)."""
        self._unavailable_next = n

    def lose_response_next(self, n: int = 1) -> None:
        """The next n payouts happen (and are recorded) but the caller never hears back."""
        self._lose_response_next = n

    @property
    def succeeded_entries(self) -> list[LedgerEntry]:
        return [e for e in self.ledger.values() if e.result.status == "succeeded"]

    # -- the interface ---------------------------------------------------------------------
    async def refund(
        self,
        *,
        payment_ref: str | None,
        amount: Decimal,
        currency: str,
        idempotency_key: str,
    ) -> ProviderResult:
        self.calls += 1
        if self._unavailable_next > 0:
            self._unavailable_next -= 1
            raise ProviderUnavailable("simulated timeout before processing")

        # One payout per key even when the same key arrives concurrently.
        lock = self._locks.setdefault(idempotency_key, asyncio.Lock())
        async with lock:
            entry = self.ledger.get(idempotency_key)
            if entry is None:
                if self.latency_ms:
                    await asyncio.sleep(self.latency_ms / 1000)
                declined = self._fail_next > 0 or self._rng.random() < self.failure_rate
                if self._fail_next > 0:
                    self._fail_next -= 1
                result = (
                    ProviderResult("failed", error="payment declined by the simulated provider")
                    if declined
                    else ProviderResult("succeeded", provider_ref=f"sim_rf_{uuid.uuid4().hex[:12]}")
                )
                entry = LedgerEntry(idempotency_key, amount, currency, result)
                self.ledger[idempotency_key] = entry
            result = entry.result

        if self._lose_response_next > 0:
            self._lose_response_next -= 1
            raise ProviderUnavailable("simulated lost response (the payout DID happen)")
        return result


@lru_cache
def get_payment_provider() -> PaymentProvider:
    s = get_settings()
    return SimulatedPaymentProvider(
        latency_ms=s.payment_sim_latency_ms, failure_rate=s.payment_sim_failure_rate
    )

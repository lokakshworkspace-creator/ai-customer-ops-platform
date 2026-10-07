"""The simulated payment provider: idempotent by key, with controllable failures."""

import asyncio
import uuid
from decimal import Decimal

import pytest

from app.modules.refunds.payments import (
    ProviderUnavailable,
    SimulatedPaymentProvider,
    provider_key_for,
)


def call(p: SimulatedPaymentProvider, key: str, amount: str = "10.00"):
    return p.refund(
        payment_ref="sim_1", amount=Decimal(amount), currency="INR", idempotency_key=key
    )


async def test_same_key_gives_the_same_result_and_one_ledger_entry() -> None:
    p = SimulatedPaymentProvider()
    first, second = await call(p, "refund-a"), await call(p, "refund-a")
    assert first == second and first.status == "succeeded" and first.provider_ref
    assert len(p.ledger) == 1 and p.calls == 2


async def test_different_keys_are_different_payouts() -> None:
    p = SimulatedPaymentProvider()
    a, b = await call(p, "refund-a"), await call(p, "refund-b")
    assert a.provider_ref != b.provider_ref and len(p.ledger) == 2


async def test_concurrent_calls_with_one_key_pay_out_once() -> None:
    p = SimulatedPaymentProvider(latency_ms=30)
    results = await asyncio.gather(*[call(p, "refund-x") for _ in range(10)])
    assert len({r.provider_ref for r in results}) == 1
    assert len(p.ledger) == 1 and len(p.succeeded_entries) == 1


async def test_a_declined_payout_is_recorded_and_stays_declined_for_that_key() -> None:
    p = SimulatedPaymentProvider()
    p.fail_next()
    declined = await call(p, "refund-a")
    assert declined.status == "failed" and declined.error
    assert (await call(p, "refund-a")) == declined  # same key: same answer, no retry of the charge
    assert (await call(p, "refund-b")).status == "succeeded"  # next payout unaffected
    assert len(p.succeeded_entries) == 1


async def test_unavailable_before_processing_records_nothing() -> None:
    p = SimulatedPaymentProvider()
    p.unavailable_next()
    with pytest.raises(ProviderUnavailable):
        await call(p, "refund-a")
    assert p.ledger == {}
    assert (await call(p, "refund-a")).status == "succeeded"  # a retry works


async def test_lost_response_means_the_payout_happened_but_the_caller_never_knew() -> None:
    p = SimulatedPaymentProvider()
    p.lose_response_next()
    with pytest.raises(ProviderUnavailable):
        await call(p, "refund-a")
    assert len(p.succeeded_entries) == 1  # the money moved
    retry = await call(p, "refund-a")  # the safe retry returns the recorded result...
    assert retry.status == "succeeded" and len(p.succeeded_entries) == 1  # ...without paying twice


async def test_failure_rate_one_declines_everything_and_zero_declines_nothing() -> None:
    always = SimulatedPaymentProvider(failure_rate=1.0)
    never = SimulatedPaymentProvider(failure_rate=0.0)
    assert (await call(always, "k1")).status == "failed"
    assert (await call(never, "k1")).status == "succeeded"


async def test_latency_is_applied() -> None:
    p = SimulatedPaymentProvider(latency_ms=120)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await call(p, "refund-a")
    assert loop.time() - t0 >= 0.1
    t1 = loop.time()
    await call(p, "refund-a")  # a replay is answered from the ledger, not re-processed
    assert loop.time() - t1 < 0.1


def test_provider_key_is_derived_from_the_refund_id_not_from_any_user_input() -> None:
    rid = uuid.uuid4()
    assert provider_key_for(rid) == f"refund-{rid}"

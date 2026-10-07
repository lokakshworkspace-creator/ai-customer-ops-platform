"""scripts/reconcile_refunds.py refuses to run against the in-memory simulated provider."""

import importlib.util
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from app.modules.refunds.payments import SimulatedPaymentProvider
from app.modules.refunds.reconcile import SIMULATED_REFUSAL, reconcile_refusal

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "reconcile_refunds.py"


class RealishProvider:
    """Stands in for a real provider: anything that is not the simulated one."""

    async def refund(self, **kw):  # pragma: no cover
        raise NotImplementedError


class _Engine:
    async def dispose(self) -> None: ...


@pytest.fixture
def script():
    spec = importlib.util.spec_from_file_location("reconcile_script", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_guard_function() -> None:
    sim = SimulatedPaymentProvider()
    assert reconcile_refusal(sim, allow_simulated=False) == SIMULATED_REFUSAL
    assert reconcile_refusal(sim, allow_simulated=True) is None
    assert reconcile_refusal(RealishProvider(), allow_simulated=False) is None  # type: ignore[arg-type]


def test_the_message_explains_why() -> None:
    assert "memory" in SIMULATED_REFUSAL and "AGAIN" in SIMULATED_REFUSAL
    assert "--allow-simulated" in SIMULATED_REFUSAL


async def test_the_script_refuses_the_simulated_provider_before_touching_the_database(
    script, monkeypatch, capsys
) -> None:
    def no_db(*a, **kw):  # the refusal must come first
        raise AssertionError("the database was touched before the refusal")

    monkeypatch.setattr(sys, "argv", ["reconcile_refunds.py"])
    monkeypatch.setattr(script, "get_sessionmaker", no_db)
    monkeypatch.setattr(script, "get_payment_provider", lambda: SimulatedPaymentProvider())
    assert await script.main() == 2
    err = capsys.readouterr().err
    assert "SIMULATED" in err and "--allow-simulated" in err


async def test_allow_simulated_lets_it_run(script, monkeypatch, capsys) -> None:
    calls: list[timedelta] = []

    async def fake_reconcile(maker, provider, *, older_than, limit):
        calls.append(older_than)
        return []

    argv = ["reconcile_refunds.py", "--allow-simulated", "--older-than", "60"]
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(script, "get_sessionmaker", lambda: object())
    monkeypatch.setattr(script, "get_engine", lambda: _Engine())
    monkeypatch.setattr(script, "get_payment_provider", lambda: SimulatedPaymentProvider())
    monkeypatch.setattr(script, "reconcile_stuck_refunds", fake_reconcile)
    assert await script.main() == 0
    assert calls == [timedelta(seconds=60)]
    assert "reconciled 0 refund(s)" in capsys.readouterr().out


async def test_a_non_simulated_provider_needs_no_flag(script, monkeypatch) -> None:
    async def fake_reconcile(maker, provider, *, older_than, limit):
        return []

    monkeypatch.setattr(sys, "argv", ["reconcile_refunds.py"])
    monkeypatch.setattr(script, "get_sessionmaker", lambda: object())
    monkeypatch.setattr(script, "get_engine", lambda: _Engine())
    monkeypatch.setattr(script, "get_payment_provider", lambda: RealishProvider())
    monkeypatch.setattr(script, "reconcile_stuck_refunds", fake_reconcile)
    assert await script.main() == 0


async def test_dry_run_needs_no_flag_and_never_reconciles(script, monkeypatch, capsys) -> None:
    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    async def fake_find(session, *, older_than, limit):
        return []

    def boom(*a, **kw):  # pragma: no cover
        raise AssertionError("dry-run must not reconcile")

    monkeypatch.setattr(sys, "argv", ["reconcile_refunds.py", "--dry-run"])
    monkeypatch.setattr(script, "get_sessionmaker", lambda: lambda: _Session())
    monkeypatch.setattr(script, "get_engine", lambda: _Engine())
    monkeypatch.setattr(script, "get_payment_provider", lambda: SimulatedPaymentProvider())
    monkeypatch.setattr(script, "find_stuck", fake_find)
    monkeypatch.setattr(script, "reconcile_stuck_refunds", boom)
    assert await script.main() == 0
    assert "0 refund(s) stuck" in capsys.readouterr().out

"""scripts/outbox_admin.py and app.events.admin: list and un-park."""

import importlib.util
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from app.events import admin
from app.events.types import EventType
from app.tests.event_helpers import outbox_rows, seed_events

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "outbox_admin.py"
pytestmark = pytest.mark.usefixtures("committed_db")


async def park(db, where: str, **params) -> None:
    async with db.maker() as s:
        await s.execute(
            text(
                f"UPDATE outbox_events SET parked_at = now(), attempts = 10, last_error = 'boom' "
                f"WHERE {where}"
            ),
            params,
        )
        await s.commit()


async def test_list_filters_by_state(committed_db) -> None:
    a, b = await seed_events(committed_db.maker, 2, [EventType.REFUND_REQUESTED])
    await park(committed_db, "aggregate_id = :a", a=str(a))
    async with committed_db.maker() as s:
        assert {r["aggregate_id"] for r in await admin.list_events(s, "parked")} == {str(a)}
        assert {r["aggregate_id"] for r in await admin.list_events(s, "pending")} == {str(b)}
        assert len(await admin.list_events(s, "all")) == 2
        assert await admin.list_events(s, "published") == []


async def test_unpark_by_id_aggregate_and_all(committed_db) -> None:
    a, b, c = await seed_events(committed_db.maker, 3, [EventType.REFUND_REQUESTED])
    await park(committed_db, "true")
    rows = {r["aggregate_id"]: r["id"] for r in await outbox_rows(committed_db.maker)}

    async with committed_db.maker() as s:
        assert await admin.unpark(s, event_ids=[rows[str(a)]]) == 1
        assert await admin.unpark(s, aggregate_id=str(b)) == 1
        await s.commit()
    states = {r["aggregate_id"]: r for r in await outbox_rows(committed_db.maker)}
    assert states[str(a)]["parked_at"] is None and states[str(a)]["attempts"] == 0
    assert states[str(b)]["parked_at"] is None
    assert states[str(c)]["parked_at"] is not None  # untouched

    async with committed_db.maker() as s:
        assert await admin.unpark(s, all_parked=True) == 1
        assert await admin.unpark(s, all_parked=True) == 0  # idempotent
        await s.commit()
    assert all(r["parked_at"] is None for r in await outbox_rows(committed_db.maker))


async def test_unpark_makes_the_event_due_immediately_and_never_touches_published_rows(
    committed_db,
) -> None:
    (a,) = await seed_events(committed_db.maker, 1, [EventType.REFUND_REQUESTED])
    async with committed_db.maker() as s:
        await s.execute(
            text(
                "UPDATE outbox_events SET parked_at = now(), "
                "next_attempt_at = now() + interval '1 day'"
            )
        )
        await s.commit()
        assert await admin.unpark(s, aggregate_id=str(a)) == 1
        await s.commit()
        due = await s.scalar(text("SELECT next_attempt_at <= now() FROM outbox_events"))
    assert due is True

    async with committed_db.maker() as s:  # a published row is never "un-parked"
        await s.execute(text("UPDATE outbox_events SET published_at = now(), parked_at = now()"))
        await s.commit()
        assert await admin.unpark(s, all_parked=True) == 0


async def test_unpark_requires_exactly_one_selector(committed_db) -> None:
    async with committed_db.maker() as s:
        with pytest.raises(ValueError):
            await admin.unpark(s)
        with pytest.raises(ValueError):
            await admin.unpark(s, aggregate_id="x", all_parked=True)


async def test_blocked_by_parked_counts_the_stuck_events(committed_db) -> None:
    (a,) = await seed_events(committed_db.maker, 1)  # 3 events for one aggregate
    await park(committed_db, "aggregate_id = :a AND event_type = 'refund.requested'", a=str(a))
    async with committed_db.maker() as s:
        (row,) = await admin.blocked_by_parked(s)
    assert row["aggregate_id"] == str(a) and row["blocked_events"] == 2


async def test_the_script_lists_and_unparks(committed_db, monkeypatch, capsys) -> None:
    import sys

    (a,) = await seed_events(committed_db.maker, 1, [EventType.REFUND_REQUESTED])
    await park(committed_db, "true")
    spec = importlib.util.spec_from_file_location("outbox_admin_script", SCRIPT)
    script = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(script)  # type: ignore[union-attr]

    class _E:
        async def dispose(self) -> None: ...

    monkeypatch.setattr(script, "get_sessionmaker", lambda: committed_db.maker)
    monkeypatch.setattr(script, "get_engine", lambda: _E())

    monkeypatch.setattr(sys, "argv", ["x"])
    assert await script.main(["list", "--state", "parked"]) == 0
    assert str(a) in capsys.readouterr().out

    assert await script.main(["unpark", "--aggregate", str(a), "--dry-run"]) == 0
    assert "would un-park" in capsys.readouterr().out
    assert (await outbox_rows(committed_db.maker))[0][
        "parked_at"
    ] is not None  # dry-run changed nothing

    assert await script.main(["unpark"]) == 2  # a selector is mandatory
    assert await script.main(["unpark", "--id", str(uuid.uuid4()), "--all"]) == 2
    assert await script.main(["unpark", "--aggregate", str(a)]) == 0
    assert "un-parked 1 event(s)" in capsys.readouterr().out
    assert (await outbox_rows(committed_db.maker))[0]["parked_at"] is None

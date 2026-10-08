"""Phase 2 live demo: refund -> policy -> outbox -> relay -> Redpanda -> consumer -> notification.

Run it against the DEV stack (dev database `ops`, dev Redpanda, default `ops.*` topics):

    docker compose up -d postgres redis redpanda
    cd backend && python -m alembic upgrade head          # once; no seed data is needed
    backend/.venv/Scripts/python scripts/demo_phase2.py   # from the repo root

It starts the API, the outbox relay and the notification consumer as three separate processes, walks
through four scenarios, prints the refund / outbox / notification state after each, and stops every
process it started, by PID.

REPEATABLE BY CONSTRUCTION. Every run INSERTS its own fresh rows and never reads, changes or deletes
anything else:
  * a demo customer and a demo admin (`demo-customer-<run>@example.com`, `demo-admin-<run>@example.com`),
  * two delivered orders (2000.00 INR each) with a succeeded payment each,
  * refunds / events / approvals / tool-call and audit rows produced by the scenarios.
It does not use the seeded users or orders, and the Idempotency-Keys are unique per run, so running it
again gives identical outcomes (the last line, `OUTCOMES {...}`, is the same for every run).
The rows stay in the dev database afterwards (nothing is dropped or reset); they are clearly named.
"""

import argparse
import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import text

from app.core.config import get_settings
from app.core.security import hash_password
from app.db.models import Order, OrderItem, Payment, User
from app.db.models.enums import OrderStatus, PaymentStatus, Role
from app.db.session import get_engine, get_sessionmaker

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "backend"
DEMO_PASSWORD = "demo-password-123"
ORDER_TOTAL = Decimal("2000.00")
ANSI = re.compile(r"\x1b\[[0-9;]*m")
NOTIFICATION = re.compile(
    r"notification\b.*?aggregate_id=(?P<agg>\S+).*?event_type=(?P<type>\S+)"
    r".*?message=(?P<msg>.*?)(?:\s+request_id=\S+)?\s*$"
)


@dataclass
class Proc:
    name: str
    popen: subprocess.Popen
    log: Path
    ready_marker: str | None = None

    @property
    def pid(self) -> int:
        return self.popen.pid


@dataclass
class Checks:
    items: list[tuple[str, bool]] = field(default_factory=list)

    def ok(self, label: str, passed: bool) -> bool:
        self.items.append((label, passed))
        print(f"      [{'OK' if passed else 'FAIL'}] {label}")
        return passed


def banner(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def say(msg: str = "") -> None:
    print(f"  {msg}")


# --- processes ------------------------------------------------------------------------------------


def port_is_free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def read_log(path: Path) -> str:
    return ANSI.sub("", path.read_text(encoding="utf-8", errors="replace"))


def tail(path: Path, n: int = 15) -> str:
    return "\n".join(read_log(path).splitlines()[-n:])


def start(name: str, args: list[str], logdir: Path, ready: str | None) -> Proc:
    log = logdir / f"{name}.log"
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    handle = open(log, "w", encoding="utf-8")  # noqa: SIM115  (stays open for the child's lifetime)
    popen = subprocess.Popen(
        [sys.executable, *args], cwd=BACKEND, env=env, stdout=handle, stderr=subprocess.STDOUT
    )
    return Proc(name, popen, log, ready)


async def wait_ready(proc: Proc, max_wait: float = 60.0) -> None:
    deadline = time.time() + max_wait
    while time.time() < deadline:
        if proc.popen.poll() is not None:
            code = proc.popen.returncode
            raise RuntimeError(f"{proc.name} exited early (code {code}):\n{tail(proc.log)}")
        if proc.ready_marker is None or proc.ready_marker in read_log(proc.log):
            return
        await asyncio.sleep(0.3)
    raise RuntimeError(f"{proc.name} not ready after {max_wait}s:\n{tail(proc.log)}")


def stop_all(procs: list[Proc]) -> None:
    """Stop every process this script started, by PID. Windows has no graceful SIGTERM for a child
    process, so there they are terminated; the pipeline is at-least-once, so that is safe."""
    for p in reversed(procs):
        if p.popen.poll() is None:
            p.popen.terminate()
    for p in reversed(procs):
        try:
            p.popen.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.popen.kill()
            p.popen.wait(timeout=10)
        say(f"stopped {p.name:<9} pid {p.pid}  (exit code {p.popen.returncode})")


# --- database (read-only helpers, plus the one INSERT-only setup) ------------------------------------


async def scalar(sql: str, **params: Any) -> Any:
    async with get_sessionmaker()() as s:
        return await s.scalar(text(sql), params)


async def rows(sql: str, **params: Any) -> list[dict]:
    async with get_sessionmaker()() as s:
        return [dict(r) for r in (await s.execute(text(sql), params)).mappings()]


async def counts() -> dict[str, int]:
    return {
        t: await scalar(f"SELECT count(*) FROM {t}")
        for t in ("refunds", "outbox_events", "tool_calls", "approvals")
    }


async def outbox_for(aggregate_id: str) -> list[dict]:
    return await rows(
        "SELECT seq, event_type, attempts, published_at IS NOT NULL AS published "
        "FROM outbox_events WHERE aggregate_id = :a ORDER BY seq",
        a=aggregate_id,
    )


@dataclass
class DemoData:
    run: str
    customer_email: str
    admin_email: str
    customer_id: uuid.UUID
    admin_id: uuid.UUID
    order_a: uuid.UUID
    order_b: uuid.UUID
    payment_a: uuid.UUID
    payment_b: uuid.UUID


async def create_demo_data(run: str) -> DemoData:
    """INSERT ONLY: this run's own customer, admin, two delivered orders and succeeded payments."""
    placed = datetime.now(UTC) - timedelta(days=3)
    async with get_sessionmaker()() as s:
        pw = hash_password(DEMO_PASSWORD)
        customer = User(
            email=f"demo-customer-{run}@example.com",
            full_name="Demo Customer",
            role=Role.customer,
            password_hash=pw,
        )
        admin = User(
            email=f"demo-admin-{run}@example.com",
            full_name="Demo Admin",
            role=Role.admin,
            password_hash=pw,
        )
        s.add_all([customer, admin])
        await s.flush()
        orders, payments = [], []
        for tag in ("a", "b"):
            order = Order(
                user_id=customer.id,
                status=OrderStatus.delivered,
                total_amount=ORDER_TOTAL,
                currency="INR",
                placed_at=placed,
                items=[
                    OrderItem(
                        sku=f"DEMO-{tag.upper()}",
                        name=f"Demo item {tag.upper()}",
                        quantity=1,
                        unit_price=ORDER_TOTAL,
                    )
                ],
            )
            s.add(order)
            await s.flush()
            payment = Payment(
                order_id=order.id,
                amount=ORDER_TOTAL,
                currency="INR",
                status=PaymentStatus.succeeded,
                provider="simulated",
                provider_ref=f"demo_{run}_{tag}",
            )
            s.add(payment)
            await s.flush()
            orders.append(order.id)
            payments.append(payment.id)
        data = DemoData(
            run,
            customer.email,
            admin.email,
            customer.id,
            admin.id,
            orders[0],
            orders[1],
            payments[0],
            payments[1],
        )
        await s.commit()
    return data


# --- notifications ---------------------------------------------------------------------------------


def notification_lines(log: Path, aggregate_id: str) -> list[tuple[str, str, str]]:
    """(event_type, message, raw log line) for this aggregate, from the consumer's log."""
    found = []
    for line in read_log(log).splitlines():
        m = NOTIFICATION.search(line)
        if m and m.group("agg") == aggregate_id:
            found.append((m.group("type"), m.group("msg").strip().strip("'\""), line.strip()))
    return found


async def wait_for_notifications(log: Path, agg: str, n: int, max_wait: float = 45.0):
    deadline = time.time() + max_wait
    while time.time() < deadline:
        got = notification_lines(log, agg)
        if len(got) >= n:
            return got
        await asyncio.sleep(0.4)
    return notification_lines(log, agg)


async def show_state(
    label: str,
    refund_id: str,
    log: Path,
    expect: int,
    headers: dict,
    api: httpx.AsyncClient,
    show_raw: bool = False,
):
    got = await wait_for_notifications(log, refund_id, expect)
    refund = (await api.get(f"/api/v1/refunds/{refund_id}", headers=headers)).json()["refund"]
    say(label)
    say(f"  refund       : {refund['id']}")
    say(
        f"                 status={refund['status']}  amount={refund['amount']} "
        f"{refund['currency']}  reason={refund['reason_code']}"
    )
    say("  outbox       :")
    for r in await outbox_for(refund_id):
        say(
            f"                 seq={r['seq']:<6} {r['event_type']:<17} "
            f"published={r['published']}  attempts={r['attempts']}"
        )
    say("  notifications (from the consumer's log):")
    for event_type, message, raw in got:
        say(f"                 {raw.split()[0][11:19]}  {event_type:<17} {message}")
    if not got:
        say("                 (none yet)")
    if got and show_raw:
        say("  one complete log line, as the consumer wrote it:")
        say(f"                 {got[0][2]}")
    return got


# --- preflight -------------------------------------------------------------------------------------


async def preflight(port: int) -> None:
    settings = get_settings()
    if settings.is_prod:
        sys.exit("refusing to run the demo with APP_ENV=prod")
    if not port_is_free(port):
        sys.exit(f"port {port} is already in use; stop that process or pass --port")
    try:
        migrated = await scalar(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = 'outbox_events' AND column_name = 'seq'"
        )
    except Exception as exc:
        sys.exit(
            f"cannot reach the dev database ({type(exc).__name__}). "
            "Is `docker compose up -d` running?"
        )
    if not migrated:
        sys.exit(
            "the dev database is not migrated: run `python -m alembic upgrade head` in backend/"
        )
    try:
        from aiokafka.admin import AIOKafkaAdminClient

        admin = AIOKafkaAdminClient(bootstrap_servers=settings.kafka_bootstrap_servers)
        await asyncio.wait_for(admin.start(), 15)
        await admin.close()
    except Exception as exc:
        sys.exit(
            f"cannot reach Redpanda at {settings.kafka_bootstrap_servers} "
            f"({type(exc).__name__}). Is `docker compose up -d` running?"
        )


# --- the walk-through ------------------------------------------------------------------------------


async def main(port: int) -> int:
    settings = get_settings()
    await preflight(port)
    run = uuid.uuid4().hex[:8]
    logdir = Path(tempfile.mkdtemp(prefix="ops-demo-"))
    base = f"http://127.0.0.1:{port}"
    procs: list[Proc] = []
    chk = Checks()
    outcomes: dict[str, Any] = {}
    try:
        banner("SETUP")
        say(f"dev database : {settings.database_url.split('@')[-1]}")
        say(
            f"redpanda     : {settings.kafka_bootstrap_servers}   "
            f"topics {settings.kafka_topic_prefix}.refunds / .tickets / .dead-letter"
        )
        say(f"logs         : {logdir}")
        before = await counts()
        say(f"table counts : {before}")
        d = await create_demo_data(run)
        say("rows CREATED by this run (insert only; nothing else is read or changed):")
        say(f"  customer   {d.customer_id}  {d.customer_email}")
        say(f"  admin      {d.admin_id}  {d.admin_email}")
        say(
            f"  order A    {d.order_a}  delivered  {ORDER_TOTAL} INR   payment {d.payment_a} (succeeded)"
        )
        say(
            f"  order B    {d.order_b}  delivered  {ORDER_TOTAL} INR   payment {d.payment_b} (succeeded)"
        )

        api_proc = start(
            "api",
            ["-m", "uvicorn", "app.main:app", "--port", str(port), "--log-level", "warning"],
            logdir,
            None,
        )
        relay = start(
            "relay", ["-m", "app.events.relay", "--poll-interval", "0.5"], logdir, "relay_started"
        )
        consumer = start(
            "consumer",
            ["-m", "app.events.consumers.notifications"],
            logdir,
            "Setting newly assigned partitions",
        )
        procs += [api_proc, relay, consumer]
        for p in procs:
            say(f"started {p.name:<9} pid {p.pid}")

        async with httpx.AsyncClient(base_url=base, timeout=30) as api:
            for _ in range(160):
                try:
                    if (await api.get("/health")).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.25)
            else:
                raise RuntimeError("the API did not become healthy:\n" + tail(api_proc.log))
            await wait_ready(relay)
            await wait_ready(consumer)
            say("API healthy, relay running, consumer joined its group")

            async def login(email: str) -> dict:
                r = await api.post(
                    "/api/v1/auth/login", json={"email": email, "password": DEMO_PASSWORD}
                )
                r.raise_for_status()
                return {"Authorization": f"Bearer {r.json()['access_token']}"}

            cust, admin = await login(d.customer_email), await login(d.admin_email)

            async def refund(order: uuid.UUID, amount: str, key: str) -> httpx.Response:
                return await api.post(
                    "/api/v1/refunds",
                    headers={**cust, "Idempotency-Key": key},
                    json={"order_id": str(order), "amount": amount, "reason_code": "damaged"},
                )

            # (a) -------------------------------------------------------------------------------
            banner("(a) 250 INR refund -> within policy -> auto-approved, paid out -> notified")
            key_a = f"demo-a-{run}"
            ra = await refund(d.order_a, "250.00", key_a)
            say(
                f"POST /api/v1/refunds  amount=250.00  Idempotency-Key={key_a}  ->  HTTP {ra.status_code}"
            )
            id_a = ra.json()["refund"]["id"]
            chk.ok(
                "HTTP 201, refund.status = succeeded",
                ra.status_code == 201 and ra.json()["refund"]["status"] == "succeeded",
            )
            chk.ok(
                "policy: ALLOW / REFUND_ALLOWED",
                ra.json()["policy"]["reason_code"] == "REFUND_ALLOWED",
            )
            got = await show_state(
                "state after (a):", id_a, consumer.log, 3, cust, api, show_raw=True
            )
            seq_a = [t for t, _, _ in got]
            chk.ok(
                "notified, in order: requested, approved, completed",
                seq_a == ["refund.requested", "refund.approved", "refund.completed"],
            )
            chk.ok(
                "every outbox row is published", all(r["published"] for r in await outbox_for(id_a))
            )
            outcomes["a"] = {
                "http": ra.status_code,
                "status": ra.json()["refund"]["status"],
                "policy": ra.json()["policy"]["reason_code"],
                "notifications": seq_a,
            }

            # (b) -------------------------------------------------------------------------------
            banner("(b) 700 INR refund -> needs approval -> admin approves -> paid out -> notified")
            key_b = f"demo-b-{run}"
            rb = await refund(d.order_b, "700.00", key_b)
            say(
                f"POST /api/v1/refunds  amount=700.00  Idempotency-Key={key_b}  ->  HTTP {rb.status_code}"
            )
            id_b, approval_b = rb.json()["refund"]["id"], rb.json()["approval_id"]
            chk.ok(
                "HTTP 202, refund.status = pending_approval",
                rb.status_code == 202 and rb.json()["refund"]["status"] == "pending_approval",
            )
            chk.ok(
                "policy: REQUIRE_APPROVAL / REFUND_ABOVE_AUTO_LIMIT",
                rb.json()["policy"]["reason_code"] == "REFUND_ABOVE_AUTO_LIMIT",
            )
            await show_state("state while it waits for a human:", id_b, consumer.log, 1, cust, api)
            pending = (await api.get("/api/v1/approvals?limit=100", headers=admin)).json()
            chk.ok(
                "it is in the admin's pending list",
                approval_b in {i["id"] for i in pending["items"]},
            )
            ap = await api.post(
                f"/api/v1/approvals/{approval_b}/approve",
                headers=admin,
                json={"note": "demo: looks fine"},
            )
            say(f"POST /api/v1/approvals/{approval_b}/approve  ->  HTTP {ap.status_code}")
            chk.ok(
                "approve -> HTTP 200, refund.status = succeeded",
                ap.status_code == 200 and ap.json()["refund"]["status"] == "succeeded",
            )
            got = await show_state("state after approval:", id_b, consumer.log, 3, cust, api)
            seq_b = [t for t, _, _ in got]
            chk.ok(
                "notified, in order: requested, approved, completed",
                seq_b == ["refund.requested", "refund.approved", "refund.completed"],
            )
            outcomes["b"] = {
                "http_request": rb.status_code,
                "status_request": "pending_approval",
                "http_approve": ap.status_code,
                "status_final": ap.json()["refund"]["status"],
                "policy": rb.json()["policy"]["reason_code"],
                "notifications": seq_b,
            }

            # (c) -------------------------------------------------------------------------------
            banner("(c) the SAME Idempotency-Key again -> replay: no second refund, no new event")
            snap, notes_before = await counts(), len(notification_lines(consumer.log, id_a))
            rc = await refund(d.order_a, "250.00", key_a)
            say(
                f"POST /api/v1/refunds  (identical, Idempotency-Key={key_a})  ->  HTTP {rc.status_code}"
            )
            say(
                f"  header Idempotent-Replayed: {rc.headers.get('idempotent-replayed')}"
                f"    body replayed={rc.json()['replayed']}    same refund id: "
                f"{rc.json()['refund']['id'] == id_a}"
            )
            chk.ok(
                "HTTP 200, replayed, same refund",
                rc.status_code == 200
                and rc.json()["replayed"]
                and rc.json()["refund"]["id"] == id_a,
            )
            await asyncio.sleep(4)  # give a (wrong) duplicate event time to show up
            after = await counts()
            say(
                "table counts before -> after the replay: "
                + ", ".join(f"{k} {snap[k]}->{after[k]}" for k in snap)
            )
            chk.ok(
                "no new refund, outbox event or approval",
                all(after[k] == snap[k] for k in ("refunds", "outbox_events", "approvals")),
            )
            chk.ok(
                "the attempt is still logged (tool_calls +1)",
                after["tool_calls"] == snap["tool_calls"] + 1,
            )
            chk.ok(
                "no extra notification", len(notification_lines(consumer.log, id_a)) == notes_before
            )
            say("outbox for that refund (unchanged):")
            for r in await outbox_for(id_a):
                say(
                    f"                 seq={r['seq']:<6} {r['event_type']:<17} published={r['published']}"
                )
            clash = await refund(d.order_a, "251.00", key_a)
            say(
                f"same key, different amount (251.00)  ->  HTTP {clash.status_code}  code={clash.json().get('code')}"
            )
            chk.ok(
                "same key + different payload -> 409 IDEMPOTENCY_KEY_REUSED",
                clash.status_code == 409,
            )
            outcomes["c"] = {
                "http": rc.status_code,
                "replayed": rc.json()["replayed"],
                "new_rows": [after[k] - snap[k] for k in ("refunds", "outbox_events", "approvals")],
                "different_payload_http": clash.status_code,
            }

            # (d) -------------------------------------------------------------------------------
            banner("(d) 50,000 INR refund -> forbidden by policy -> 403, no refund, no event")
            snap = await counts()
            rd = await refund(d.order_a, "50000.00", f"demo-d-{run}")
            say(
                f"POST /api/v1/refunds  amount=50000.00  ->  HTTP {rd.status_code}  code={rd.json().get('code')}"
            )
            say(f"  reason: {rd.json().get('detail')}")
            chk.ok(
                "HTTP 403, AMOUNT_EXCEEDS_ORDER",
                rd.status_code == 403 and rd.json().get("code") == "AMOUNT_EXCEEDS_ORDER",
            )
            await asyncio.sleep(3)
            after = await counts()
            say(
                "table counts before -> after: "
                + ", ".join(f"{k} {snap[k]}->{after[k]}" for k in snap)
            )
            chk.ok(
                "no refund, outbox event or approval created",
                all(after[k] == snap[k] for k in ("refunds", "outbox_events", "approvals")),
            )
            call = await rows(
                "SELECT tool_name, status, policy_decision, policy_reason_code "
                "FROM tool_calls WHERE id = :i",
                i=rd.json()["tool_call_id"],
            )
            say(f"  yet the denial is on record -> tool_calls: {call[0]}")
            chk.ok(
                "the denial was logged (status denied, decision DENY)",
                bool(call)
                and call[0]["status"] == "denied"
                and call[0]["policy_decision"] == "DENY",
            )
            outcomes["d"] = {
                "http": rd.status_code,
                "code": rd.json().get("code"),
                "new_rows": [after[k] - snap[k] for k in ("refunds", "outbox_events", "approvals")],
            }

        banner("SUMMARY")
        final = await counts()
        created = {k: final[k] - before[k] for k in final}
        say(f"table counts : {before}  ->  {final}")
        say(
            f"this run added: {created['refunds']} refunds, {created['outbox_events']} outbox events, "
            f"{created['approvals']} approval, {created['tool_calls']} tool-call log rows"
        )
        failed = [label for label, passed in chk.items if not passed]
        say(f"checks       : {len(chk.items) - len(failed)}/{len(chk.items)} passed")
        for label in failed:
            say(f"  FAILED: {label}")
        outcomes["created"] = created
        outcomes["checks_passed"] = not failed
        print("\nOUTCOMES " + json.dumps(outcomes, sort_keys=True))
        return 0 if not failed else 1
    except Exception as exc:
        print(f"\nDEMO ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        for p in procs:
            print(f"--- {p.name} log tail ---\n{tail(p.log)}", file=sys.stderr)
        return 2
    finally:
        banner("CLEANUP")
        stop_all(procs)
        await get_engine().dispose()
        say(f"(process logs kept in {logdir})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--port", type=int, default=8000, help="port for the API (default 8000)")
    raise SystemExit(asyncio.run(main(parser.parse_args().port)))

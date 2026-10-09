"""Phase 5 live demo: an admin decides a refund, and the customer's waiting chat turn finishes BY EVENT.

Run it against the DEV stack (dev database `ops`, migrated to 0005; dev Redpanda):

    docker compose up -d postgres redis redpanda
    backend/.venv/Scripts/python scripts/demo_phase5.py            # from the repo root

It starts the API, the outbox RELAY and the workflow-RESUME consumer as three separate processes
(stopped by PID at the end), with the FAKE LLM and FAKE embedder forced whatever .env says, and
drives the same endpoints the web pages use. Scenarios:

  (a) a customer asks for a 700 refund through the chat: the turn waits ("approval")
  (b) the admin's view: GET /auth/me and GET /approvals show the request with its review reasons;
      a customer gets 403 on the approvals list and on the staff policy lookup
  (c) the admin approves; BEFORE ANY GET /workflows the script reads the DATABASE: the consumer
      has already finished the turn (one "Update:" message, a `workflow_resumed` line in its log).
      The read-time sync was never called, so this is the event path.
  (d) the same event delivered again changes nothing (duplicate delivery)
  (e) the consumer is stopped by PID, a second refund is approved, and the turn stays waiting until
      the customer's next GET /workflows, when the read-time FALLBACK finishes it
  (f) an admin approving their own request is refused (SELF_APPROVAL)
  (g) the staff policy lookup finds INTERNAL documents for an admin

PRIVATE TOPICS. The relay and the consumer use a private topic prefix (`d5<run>.refunds`), so
nothing is mixed with other processes. NOTE: the relay publishes EVERY unpublished row of the dev
outbox table, including rows left by earlier dev activity, to that private prefix and marks them
published, so a relay run later with the default prefix will not publish those rows. The script
prints how many unpublished rows existed before it started, and refuses to start if a relay
process is already running (it never stops processes it did not start).

Every run INSERTS its own demo customer, admin and orders (`demo-customer-<run>@example.com`) and
its own knowledge-base namespace (`demo_<run>`), reads nothing else and deletes nothing: repeat it
as often as you like (the final OUTCOMES line is identical each time). Remove the rows later with
scripts/demo_cleanup.py --apply and scripts/demo_phase4.py --cleanup --apply (dry run by default).
It refuses to run unless APP_ENV=dev.
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from app.core.config import dev_only_refusal, get_settings

sys.path.insert(0, str(Path(__file__).resolve().parent))
import demo_phase2 as d2  # noqa: E402  (shared helpers: processes, checks, demo data)
import demo_phase4 as d4  # noqa: E402  (the fake-embedder knowledge-base ingest)

READY_CONSUMER = "Setting newly assigned partitions"


def running_relays() -> list[int]:
    """PIDs of relay processes already running (best effort). The demo never touches them."""
    try:
        if os.name == "nt":
            cmd = (
                "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match "
                "'app\\.events\\.relay' } | Select-Object -ExpandProperty ProcessId"
            )
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command", cmd],
                capture_output=True,
                text=True,
                timeout=30,
            ).stdout
        else:
            out = subprocess.run(
                ["pgrep", "-f", "app.events.relay"], capture_output=True, text=True, timeout=30
            ).stdout
    except Exception:
        return []
    return [int(x) for x in out.split() if x.isdigit() and int(x) != os.getpid()]


def start_proc(
    name: str, args: list[str], logdir: Path, env_extra: dict[str, str], ready: str | None
):
    log = logdir / f"{name}.log"
    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
        "LLM_PROVIDER": "fake",  # never the real model or embedder in a demo
        "GEMINI_API_KEY": "",
        "LLM_MODEL": "",
        "EMBEDDING_PROVIDER": "fake",
        "EMBEDDING_MODEL": "",
        **env_extra,
    }
    handle = open(log, "w", encoding="utf-8")  # noqa: SIM115  (open for the child's lifetime)
    popen = subprocess.Popen(
        [sys.executable, *args], cwd=d2.BACKEND, env=env, stdout=handle, stderr=subprocess.STDOUT
    )
    return d2.Proc(name, popen, log, ready)


async def wait_until(cond, timeout: float = 30.0, every: float = 0.3) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if await cond():
            return True
        await asyncio.sleep(every)
    return False


async def preflight(port: int) -> int:
    settings = get_settings()
    if settings.is_prod:
        sys.exit("refusing to run the demo with APP_ENV=prod")
    if not d2.port_is_free(port):
        sys.exit(f"port {port} is already in use; stop that process or pass --port")
    pids = running_relays()
    if pids:
        sys.exit(
            f"an outbox relay is already running (pid {', '.join(map(str, pids))}). The demo's "
            "relay would compete with it for the dev outbox rows; stop that one yourself first."
        )
    try:
        migrated = await d2.scalar(
            "SELECT count(*) FROM pg_indexes WHERE indexname = 'ix_workflow_runs_waiting_refund'"
        )
    except Exception as exc:
        sys.exit(
            f"cannot reach the dev database ({type(exc).__name__}). "
            "Is `docker compose up -d` running?"
        )
    if not migrated:
        sys.exit(
            "the dev database is not at 0005: run `python -m alembic upgrade head` in backend/"
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
    return int(await d2.scalar("SELECT count(*) FROM outbox_events WHERE published_at IS NULL"))


async def main(port: int) -> int:
    refusal = dev_only_refusal("demo_phase5.py")  # before ANY settings or database access
    if refusal:
        print(refusal, file=sys.stderr)
        return 2
    settings = get_settings()
    unpublished = await preflight(port)
    run = uuid.uuid4().hex[:8]
    prefix, ns = f"d5{run}", f"demo_{run}"
    chk = d2.Checks()
    outcomes: dict[str, Any] = {}
    procs: list[d2.Proc] = []
    logdir = Path(tempfile.mkdtemp(prefix="ops-demo5-"))
    try:
        d2.banner("SETUP")
        d2.say(f"dev database : {settings.database_url.split('@')[-1]}")
        d2.say(f"private topic prefix: {prefix}   knowledge-base namespace: {ns}")
        d2.say(
            f"unpublished outbox rows already in the dev database: {unpublished} "
            "(the demo relay will publish them to its private prefix)"
        )
        d = await d2.create_demo_data(run)
        d2.say(f"customer {d.customer_email}   admin {d.admin_email}   password {d2.DEMO_PASSWORD}")
        written = await d4.ingest(ns)
        d2.say(f"knowledge base: {written} chunk(s), FAKE embedder")

        env_topics = {"KAFKA_TOPIC_PREFIX": prefix}
        api_proc = start_proc(
            "api",
            ["-m", "uvicorn", "app.main:app", "--port", str(port), "--log-level", "warning"],
            logdir,
            {"KB_NAMESPACE": ns},
            None,
        )
        relay = start_proc(
            "relay",
            ["-m", "app.events.relay", "--poll-interval", "0.5"],
            logdir,
            env_topics,
            "relay_started",
        )
        resume = start_proc(
            "resume",
            ["-m", "app.events.consumers.resume"],
            logdir,
            env_topics,
            READY_CONSUMER,
        )
        procs += [api_proc, relay, resume]
        for p in procs:
            d2.say(f"started {p.name:<7} pid {p.pid}")

        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=60) as api:
            for _ in range(160):
                try:
                    if (await api.get("/health")).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.25)
            else:
                raise RuntimeError("the API did not become healthy:\n" + d2.tail(api_proc.log))
            await d2.wait_ready(relay)
            await d2.wait_ready(resume)
            d2.say("API healthy, relay running, resume consumer joined its group")

            async def login(email: str) -> dict:
                r = await api.post(
                    "/api/v1/auth/login", json={"email": email, "password": d2.DEMO_PASSWORD}
                )
                r.raise_for_status()
                return {"Authorization": f"Bearer {r.json()['access_token']}"}

            cust, admin = await login(d.customer_email), await login(d.admin_email)

            async def chat(text_: str) -> dict:
                body = {"message": text_, "message_id": str(uuid.uuid4())}
                r = await api.post("/api/v1/chat", json=body, headers=cust)
                r.raise_for_status()
                j = r.json()
                d2.say(f"assistant [{j['llm']}] ({j['status']}): {j['reply'][:110]}")
                chk.ok('the model was the built-in FAKE ("llm": "fake")', j["llm"] == "fake")
                return j

            async def run_row(workflow_id: str) -> dict:
                rows = await d2.rows(
                    "SELECT status, state FROM workflow_runs WHERE id = :i",
                    i=uuid.UUID(workflow_id),
                )
                return rows[0]

            async def updates(workflow_id: str) -> list[str]:
                rows = await d2.rows(
                    "SELECT content FROM messages WHERE workflow_run_id = :i AND role = 'assistant' "
                    "ORDER BY seq",
                    i=uuid.UUID(workflow_id),
                )
                texts = [b.get("text", "") for r in rows for b in r["content"]]  # tool_use has none
                return [t for t in texts if t.startswith("Update:")]

            async def approval_for(order: uuid.UUID) -> dict:
                rows = await d2.rows(
                    "SELECT a.id, a.status FROM approvals a JOIN refunds r ON r.id = a.refund_id "
                    "WHERE r.order_id = :o ORDER BY a.created_at DESC",
                    o=order,
                )
                return rows[0]

            # (a) -------------------------------------------------------------------------------
            d2.banner("(a) a customer asks for a 700 refund: the turn waits for a human")
            text_a = f"my order {d.order_a} arrived damaged, refund 700"
            d2.say(f"customer: {text_a}")
            ja = await chat(text_a)
            appr_a = await approval_for(d.order_a)
            wf_a = (await api.get(f"/api/v1/workflows/{ja['workflow_id']}", headers=cust)).json()
            chk.ok(
                "the turn is waiting_approval, one pending approval",
                ja["status"] == "waiting_approval" and appr_a["status"] == "pending",
            )
            chk.ok(
                'GET /workflows says waiting_for = "approval"', wf_a["waiting_for"] == "approval"
            )
            outcomes["a"] = {"status": ja["status"], "waiting_for": wf_a["waiting_for"]}

            # (b) -------------------------------------------------------------------------------
            d2.banner("(b) what the admin page shows, and who may see it")
            me = (await api.get("/api/v1/auth/me", headers=admin)).json()
            listing = (await api.get("/api/v1/approvals", headers=admin)).json()
            mine = next(i for i in listing["items"] if i["id"] == str(appr_a["id"]))
            d2.say(f"GET /auth/me as admin: {me['role']}   pending approvals: {listing['total']}")
            d2.say(
                f"approval {mine['id'][:8]}: {mine['refund']['amount']} {mine['refund']['currency']} "
                f"{mine['refund']['reason_code']}, review reasons {mine['review_reasons']}"
            )
            r_cust = await api.get("/api/v1/approvals", headers=cust)
            r_kb = await api.get(
                "/api/v1/admin/knowledge/search", params={"q": "escalation codes"}, headers=cust
            )
            chk.ok(
                "the admin sees the request with its review reasons",
                mine["refund"]["amount"] == "700.00"
                and "REFUND_ABOVE_AUTO_LIMIT" in mine["review_reasons"],
            )
            chk.ok(
                "a customer gets 403 on the approvals list and on the staff lookup",
                (r_cust.status_code, r_kb.status_code) == (403, 403),
            )
            outcomes["b"] = {
                "admin_role": me["role"],
                "customer_list": r_cust.status_code,
                "customer_lookup": r_kb.status_code,
            }

            # (c) -------------------------------------------------------------------------------
            d2.banner("(c) the admin approves; the consumer finishes the turn BEFORE any GET")
            ap = await api.post(f"/api/v1/approvals/{appr_a['id']}/approve", json={}, headers=admin)
            d2.say(f"POST /api/v1/approvals/{{id}}/approve as the admin -> HTTP {ap.status_code}")

            async def done_a() -> bool:
                return (await run_row(ja["workflow_id"]))["status"] == "completed"

            finished = await wait_until(done_a, 30)
            msgs_a = await updates(ja["workflow_id"])
            d2.say(
                f"read from the DATABASE (no GET /workflows was made): {await run_row(ja['workflow_id'])}"
            )
            resume_log = d2.read_log(resume.log)
            chk.ok("approval accepted (200)", ap.status_code == 200)
            chk.ok(
                "the turn is completed in the database, with no GET /workflows involved", finished
            )
            chk.ok(
                "exactly one 'Update:' message, saying the refund was paid",
                len(msgs_a) == 1 and "approved and has been paid" in msgs_a[0],
            )
            chk.ok(
                "the resume consumer logged workflow_resumed for it",
                "workflow_resumed" in resume_log and ja["workflow_id"] in resume_log,
            )
            claims = await d2.scalar(
                "SELECT count(*) FROM processed_events WHERE consumer = 'workflow-resume'"
            )
            outcomes["c"] = {
                "approve_http": ap.status_code,
                "finished_by_event": finished,
                "updates": len(msgs_a),
            }

            # (d) -------------------------------------------------------------------------------
            d2.banner("(d) the same event delivered again changes nothing")
            from app.events.envelope import envelope_from_row
            from app.events.kafka import KafkaEventProducer
            from app.events.types import topic_for

            refund_a = str(mine["refund_id"])
            row = (
                await d2.rows(
                    "SELECT id, event_type, aggregate_type, aggregate_id, payload, created_at "
                    "FROM outbox_events WHERE aggregate_id = :a AND event_type = 'refund.completed'",
                    a=refund_a,
                )
            )[0]
            envelope = envelope_from_row(row)  # the very same event (same event_id)
            producer = KafkaEventProducer(settings.kafka_bootstrap_servers, startup_timeout_s=15)
            await producer.start()
            try:
                await producer.publish(
                    topic_for("refund", prefix), refund_a.encode(), envelope.to_bytes()
                )
            finally:
                await producer.stop()
            await asyncio.sleep(4)
            after = await updates(ja["workflow_id"])
            claims_after = await d2.scalar(
                "SELECT count(*) FROM processed_events WHERE consumer = 'workflow-resume'"
            )
            chk.ok(
                "still exactly one 'Update:' message after the duplicate delivery", len(after) == 1
            )
            chk.ok("the duplicate was recognised (no new claim)", claims_after == claims)
            outcomes["d"] = {
                "updates_after_duplicate": len(after),
                "new_claims": claims_after - claims,
            }

            # (e) -------------------------------------------------------------------------------
            d2.banner("(e) the consumer is stopped: the read-time FALLBACK still finishes the turn")
            resume.popen.terminate()
            resume.popen.wait(timeout=15)
            d2.say(f"stopped the resume consumer, pid {resume.pid}")
            text_e = f"my order {d.order_b} arrived damaged, refund 700"
            d2.say(f"customer: {text_e}")
            je = await chat(text_e)
            appr_e = await approval_for(d.order_b)
            await api.post(f"/api/v1/approvals/{appr_e['id']}/approve", json={}, headers=admin)
            await asyncio.sleep(4)  # the relay publishes; nobody consumes
            still = (await run_row(je["workflow_id"]))["status"]
            wf_e = (await api.get(f"/api/v1/workflows/{je['workflow_id']}", headers=cust)).json()
            chk.ok(
                "with the consumer stopped the turn is still waiting before any GET",
                still == "waiting_approval",
            )
            chk.ok(
                "GET /workflows/{id} (the fallback) finishes it",
                wf_e["status"] == "completed"
                and "approved and has been paid" in (wf_e["latest_message"] or ""),
            )
            outcomes["e"] = {"waiting_before_get": still, "after_get": wf_e["status"]}

            # (f) -------------------------------------------------------------------------------
            d2.banner("(f) an admin cannot approve their own request")
            h = {**admin, "Idempotency-Key": f"demo5-{run}-self"}
            body = {"order_id": str(d.order_b), "amount": "700.00", "reason_code": "damaged"}
            rq = await api.post("/api/v1/refunds", json=body, headers=h)
            own = rq.json().get("approval_id")
            self_ap = await api.post(f"/api/v1/approvals/{own}/approve", json={}, headers=admin)
            d2.say(
                f"admin's own refund -> approval {str(own)[:8]}; the same admin approves -> HTTP {self_ap.status_code} {self_ap.json().get('code')}"
            )
            await api.post(
                f"/api/v1/approvals/{own}/reject", json={"note": "withdrawn"}, headers=admin
            )
            chk.ok(
                "refused with 403 SELF_APPROVAL",
                (self_ap.status_code, self_ap.json().get("code")) == (403, "SELF_APPROVAL"),
            )
            outcomes["f"] = {"http": self_ap.status_code, "code": self_ap.json().get("code")}

            # (g) -------------------------------------------------------------------------------
            d2.banner("(g) the staff policy lookup finds INTERNAL documents (admin only)")
            kb = (
                await api.get(
                    "/api/v1/admin/knowledge/search",
                    params={"q": "AMBER-HOLD RED-STOP escalation codes"},
                    headers=admin,
                )
            ).json()
            top = kb["results"][0] if kb["results"] else {}
            d2.say(f"top result: {top.get('title')} [{top.get('audience')}]")
            chk.ok("an admin finds the internal document", top.get("audience") == "internal")
            outcomes["g"] = {"found": kb["found"], "top_audience": top.get("audience")}

        d2.banner("SUMMARY")
        passed = sum(1 for _, ok in chk.items if ok)
        d2.say(f"checks: {passed}/{len(chk.items)} passed")
        d2.say(f"(demo rows: namespace {ns}, topics {prefix}.*; logs {logdir})")
        outcomes["checks_passed"] = passed == len(chk.items)
        print("\nOUTCOMES " + json.dumps(outcomes, sort_keys=True))
        return 0 if outcomes["checks_passed"] else 1
    finally:
        d2.banner("CLEANUP")
        if procs:
            d2.stop_all(procs)
        from app.db.session import get_engine

        await get_engine().dispose()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.port)))

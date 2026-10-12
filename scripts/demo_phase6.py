"""Phase 6 live demo: login rate limiting, logout, approval expiry, the pipeline endpoint, request ids.

Run it against the DEV stack (dev database `ops`, migrated to 0007; dev Redis and Redpanda):

    docker compose up -d postgres redis redpanda
    backend/.venv/Scripts/python scripts/demo_phase6.py            # from the repo root

It starts the API, the outbox RELAY and the workflow-RESUME consumer as separate processes (stopped
by PID at the end), plus a second API whose Redis is deliberately unreachable, with the FAKE LLM and
FAKE embedder forced whatever .env says. The sweeper is run as a real process with `--once`.

  (a) login rate limiting: 3 wrong passwords block that email + IP (429 with Retry-After, even for
      the right password); the same email from another IP and another email from the same IP are
      not blocked; a successful login resets the counter
  (b) Redis down: a second API pointing at a dead Redis keeps logging users in (the limiter FAILS
      OPEN) and /health names redis as the failing dependency
  (c) logout: the token is refused everywhere afterwards, the user's other token is not, and the
      revocation is a row in Postgres
  (d) approval expiry: an old approval is refused by approve AND reject before the sweeper has run
      (the overdue guard); the sweeper then expires it (approval and refund `expired`, a
      refund.expired event); BEFORE any GET the resume consumer has told the customer ("expired");
      an expired approval cannot be approved; it shows on the Expired tab; its reserved money is
      released (the customer can ask again)
  (e) the pipeline endpoint: with the consumer stopped, a turn that is still waiting long after a
      final decision shows up in GET /api/v1/admin/pipeline; the customer's next GET (the read-time
      fallback) finishes it and the count goes back down
  (f) request ids: echoed in the response header and present in the API's log

WHAT THE DEMO DOES NOT DO TO YOUR DATA. The expiry limit is a setting, and the dev database holds
older pending approvals from earlier work. So that the sweeper cannot touch them, every demo
process runs with APPROVAL_TTL_HOURS=99999, and the demo ages ITS OWN approvals past that (it
writes `created_at` of its own rows in the database instead of waiting 48 hours). Anything older
than 99999 hours (about 11 years) would be swept, and nothing in a dev database is that old.

PRIVATE TOPICS. The relay and the consumer use a private topic prefix (`d6<run>.refunds`). As in
the Phase 5 demo, the demo's relay publishes EVERY unpublished row of the dev outbox table,
including rows left by earlier dev activity, to that private prefix and marks them published; it
refuses to start if a relay is already running.

Every run INSERTS its own demo users and orders and a few Redis keys (deleted at the end), reads
nothing else of yours and deletes no database rows; repeat it as often as you like (the final
OUTCOMES line is identical each time). Remove the rows with scripts/demo_cleanup.py --apply (dry
run by default). It refuses to run unless APP_ENV=dev.
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import text

from app.core.config import dev_only_refusal, get_settings
from app.db.session import get_sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent))
import demo_phase2 as d2  # noqa: E402  (shared helpers: processes, checks, demo data)
import demo_phase5 as d5  # noqa: E402  (start_proc, wait_until, the relay check)

TTL_HOURS = "99999"  # see the docstring: so that the sweeper cannot reach anyone else's approvals
AGED_HOURS = 100000  # the demo ages its own approvals past that
MAX_FAILURES = 3
WINDOW_S = 120
IP1, IP2 = "203.0.113.10", "198.51.100.20"  # two clients, seen through X-Forwarded-For
LOCAL_ENV = {
    "APPROVAL_TTL_HOURS": TTL_HOURS,
    "LOGIN_MAX_FAILURES": str(MAX_FAILURES),
    "LOGIN_WINDOW_SECONDS": str(WINDOW_S),
    "TRUSTED_PROXY_HOPS": "1",  # the demo plays the proxy: it sets X-Forwarded-For itself
    "LOGIN_LIMITER_TIMEOUT_S": "0.3",
}


async def execute(sql: str, **params: Any) -> None:
    """The demo's only writes to existing rows: it ages and back-dates ITS OWN approvals and refunds
    (so that it does not have to wait 48 hours). Everything it touches is selected by its own ids."""
    async with get_sessionmaker()() as s:
        await s.execute(text(sql), params)
        await s.commit()


async def preflight(port: int) -> int:
    settings = get_settings()
    if settings.is_prod:
        sys.exit("refusing to run the demo with APP_ENV=prod")
    for p in (port, port + 1):
        if not d2.port_is_free(p):
            sys.exit(f"port {p} is already in use; stop that process or pass --port")
    pids = d5.running_relays()
    if pids:
        sys.exit(
            f"an outbox relay is already running (pid {', '.join(map(str, pids))}). The demo's "
            "relay would compete with it for the dev outbox rows; stop that one yourself first."
        )
    try:
        migrated = await d2.scalar(
            "SELECT count(*) FROM pg_constraint WHERE conname = 'ck_refunds_refund_status' "
            "AND pg_get_constraintdef(oid) LIKE '%expired%'"
        ) and await d2.scalar("SELECT to_regclass('revoked_tokens') IS NOT NULL")
    except Exception as exc:
        sys.exit(
            f"cannot reach the dev database ({type(exc).__name__}). "
            "Is `docker compose up -d` running?"
        )
    if not migrated:
        sys.exit(
            "the dev database is not at 0007: run `python -m alembic upgrade head` in backend/"
        )
    try:
        import redis.asyncio as aioredis

        r = aioredis.Redis.from_url(settings.redis_url)
        await asyncio.wait_for(r.ping(), 5)
        await r.aclose()
    except Exception as exc:
        sys.exit(f"cannot reach Redis ({type(exc).__name__}). Is `docker compose up -d` running?")
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


async def run_sweeper_once(env_extra: dict[str, str]) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "LLM_PROVIDER": "fake",
        "GEMINI_API_KEY": "",
        "EMBEDDING_PROVIDER": "fake",
        **env_extra,
    }
    return await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "app.jobs.sweeper", "--once", "--deadline", "60"],
        cwd=d2.BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


async def delete_login_keys(keys: list[tuple[str, str]]) -> None:
    """The demo's own login-failure counters (they expire by themselves after the window)."""
    import redis.asyncio as aioredis

    from app.core.login_limiter import LoginLimiter

    r = aioredis.Redis.from_url(get_settings().redis_url, decode_responses=True)
    try:
        limiter = LoginLimiter(r, max_failures=MAX_FAILURES, window_s=WINDOW_S)
        for email, ip in keys:
            await r.delete(limiter.key(email, ip))
    finally:
        await r.aclose()


async def main(port: int) -> int:
    refusal = dev_only_refusal("demo_phase6.py")  # before ANY settings or database access
    if refusal:
        print(refusal, file=sys.stderr)
        return 2
    settings = get_settings()
    unpublished = await preflight(port)
    run = uuid.uuid4().hex[:8]
    prefix = f"d6{run}"
    chk = d2.Checks()
    outcomes: dict[str, Any] = {}
    procs: list[d2.Proc] = []
    login_keys: list[tuple[str, str]] = []
    logdir = Path(tempfile.mkdtemp(prefix="ops-demo6-"))
    try:
        d2.banner("SETUP")
        d2.say(f"dev database : {settings.database_url.split('@')[-1]}")
        d2.say(
            f"private topic prefix: {prefix}   expiry limit for every demo process: {TTL_HOURS} h"
        )
        d2.say(
            f"unpublished outbox rows already in the dev database: {unpublished} "
            "(the demo relay will publish them to its private prefix)"
        )
        d1 = await d2.create_demo_data(run + "1")  # the login-limit victim and a second account
        d2d = await d2.create_demo_data(run + "2")  # refunds, logout, expiry
        d2.say(
            f"accounts: {d1.customer_email}, {d1.admin_email}, {d2d.customer_email}, {d2d.admin_email}"
        )

        env_api = {**LOCAL_ENV}
        env_topics = {**LOCAL_ENV, "KAFKA_TOPIC_PREFIX": prefix}
        api_proc = d5.start_proc(
            "api",
            ["-m", "uvicorn", "app.main:app", "--port", str(port), "--log-level", "warning"],
            logdir,
            env_api,
            None,
        )
        api_dead = d5.start_proc(
            "api-no-redis",
            ["-m", "uvicorn", "app.main:app", "--port", str(port + 1), "--log-level", "warning"],
            logdir,
            {**env_api, "REDIS_URL": "redis://127.0.0.1:1/0"},  # nothing listens there
            None,
        )
        relay = d5.start_proc(
            "relay",
            ["-m", "app.events.relay", "--poll-interval", "0.5"],
            logdir,
            env_topics,
            "relay_started",
        )
        resume = d5.start_proc(
            "resume", ["-m", "app.events.consumers.resume"], logdir, env_topics, d5.READY_CONSUMER
        )
        procs += [api_proc, api_dead, relay, resume]
        for p in procs:
            d2.say(f"started {p.name:<13} pid {p.pid}")

        async def healthy(client: httpx.AsyncClient, proc: d2.Proc, expect: int) -> None:
            for _ in range(160):
                try:
                    if (await client.get("/health")).status_code == expect:
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.25)
            raise RuntimeError(f"{proc.name} did not become ready:\n" + d2.tail(proc.log))

        async with (
            httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=60) as api,
            httpx.AsyncClient(base_url=f"http://127.0.0.1:{port + 1}", timeout=60) as dead,
        ):
            await healthy(api, api_proc, 200)
            await healthy(dead, api_dead, 503)  # its Redis is dead, so /health says degraded
            await d2.wait_ready(relay)
            await d2.wait_ready(resume)
            d2.say("API healthy, relay running, resume consumer joined; second API up (Redis dead)")

            async def login(client, email: str, password: str, ip: str | None = None):
                headers = {"X-Forwarded-For": ip} if ip else {}
                return await client.post(
                    "/api/v1/auth/login",
                    json={"email": email, "password": password},
                    headers=headers,
                )

            async def token(client, email: str, ip: str | None = None) -> dict:
                r = await login(client, email, d2.DEMO_PASSWORD, ip)
                r.raise_for_status()
                return {"Authorization": f"Bearer {r.json()['access_token']}"}

            # (a) -------------------------------------------------------------------------------
            d2.banner(f"(a) login rate limiting: {MAX_FAILURES} failures per email + IP, then 429")
            victim = d1.customer_email
            login_keys += [(victim, IP1), (victim, IP2), (d1.admin_email, IP1)]
            wrong = [
                (await login(api, victim, "wrong-password", IP1)).status_code for _ in range(3)
            ]
            d2.say(f"{MAX_FAILURES} wrong passwords for {victim} from {IP1}: {wrong}")
            blocked = await login(api, victim, "wrong-password", IP1)
            retry = int(blocked.headers.get("Retry-After", "0"))
            d2.say(
                f"next attempt: HTTP {blocked.status_code}, Retry-After {retry}s, {blocked.json()}"
            )
            right_blocked = await login(api, victim, d2.DEMO_PASSWORD, IP1)
            other_ip = await login(api, victim, d2.DEMO_PASSWORD, IP2)
            other_email = await login(api, d1.admin_email, d2.DEMO_PASSWORD, IP1)
            chk.ok("three wrong passwords are three plain 401s", wrong == [401, 401, 401])
            chk.ok(
                "the next attempt is 429 with a Retry-After inside the window",
                blocked.status_code == 429 and 1 <= retry <= WINDOW_S,
            )
            chk.ok(
                "even the RIGHT password is refused while blocked (no password work is done)",
                right_blocked.status_code == 429 and "access_token" not in right_blocked.text,
            )
            chk.ok(
                "the same email from ANOTHER IP is not blocked (the limit is per email + IP)",
                other_ip.status_code == 200,
            )
            chk.ok(
                "ANOTHER email from the blocked IP is not blocked", other_email.status_code == 200
            )
            seq = []
            for pw in ("bad-1", "bad-2", d2.DEMO_PASSWORD, "bad-3", "bad-4", d2.DEMO_PASSWORD):
                seq.append((await login(api, d1.admin_email, pw, IP2)).status_code)
            login_keys.append((d1.admin_email, IP2))
            d2.say(f"{d1.admin_email} from {IP2}: bad, bad, GOOD, bad, bad, GOOD -> {seq}")
            chk.ok(
                "a successful login resets the counter (never a 429 despite 4 failures in all)",
                seq == [401, 401, 200, 401, 401, 200],
            )
            outcomes["a"] = {
                "wrong": wrong,
                "blocked": blocked.status_code,
                "retry_after_in_window": 1 <= retry <= WINDOW_S,
                "right_password_while_blocked": right_blocked.status_code,
                "other_ip": other_ip.status_code,
                "other_email": other_email.status_code,
                "reset_sequence": seq,
            }

            # (b) -------------------------------------------------------------------------------
            d2.banner("(b) Redis is down: the limiter FAILS OPEN and /health names redis")
            attempts = [
                (await login(dead, d2d.customer_email, "wrong-password", IP1)).status_code
                for _ in range(MAX_FAILURES + 2)
            ]
            good = await login(dead, d2d.customer_email, d2.DEMO_PASSWORD, IP1)
            hz = await dead.get("/health")
            d2.say(f"{MAX_FAILURES + 2} wrong passwords against the Redis-less API: {attempts}")
            d2.say(
                f"then the right one: HTTP {good.status_code};  /health: {hz.status_code} {hz.json()}"
            )
            chk.ok("no 429 ever: the limiter does nothing without Redis", 429 not in attempts)
            chk.ok(
                "wrong passwords are still 401 and the right one still logs in",
                set(attempts) == {401} and good.status_code == 200,
            )
            chk.ok(
                "/health is 503 and names redis (and only redis) as down",
                hz.status_code == 503
                and hz.json() == {"status": "degraded", "postgres": "ok", "redis": "error"},
            )
            outcomes["b"] = {
                "attempts": sorted(set(attempts)),
                "login_after": good.status_code,
                "health": hz.json(),
            }

            # (c) -------------------------------------------------------------------------------
            d2.banner("(c) logout revokes the token (a row in Postgres) and only that token")
            t1, t2 = (
                await token(api, d2d.customer_email, IP2),
                await token(api, d2d.customer_email, IP2),
            )
            me1 = (await api.get("/api/v1/auth/me", headers=t1)).status_code
            lo = await api.post("/api/v1/auth/logout", headers=t1)
            after_me = (await api.get("/api/v1/auth/me", headers=t1)).status_code
            after_orders = (await api.get("/api/v1/orders", headers=t1)).status_code
            again = (await api.post("/api/v1/auth/logout", headers=t1)).status_code
            other = (await api.get("/api/v1/auth/me", headers=t2)).status_code
            rows = await d2.scalar(
                "SELECT count(*) FROM revoked_tokens r JOIN users u ON u.id = r.user_id "
                "WHERE u.email = :e",
                e=d2d.customer_email,
            )
            d2.say(
                f"token 1 before: {me1}; logout: {lo.status_code}; after: /auth/me {after_me}, /orders {after_orders}"
            )
            d2.say(
                f"logging out again: {again};  the user's other token: {other};  revoked_tokens rows: {rows}"
            )
            chk.ok("logout is 204 and the token worked before", (me1, lo.status_code) == (200, 204))
            chk.ok(
                "the revoked token is 401 everywhere afterwards",
                (after_me, after_orders) == (401, 401),
            )
            chk.ok("logging out twice is a 401, not a second row", again == 401 and rows == 1)
            chk.ok("the user's OTHER token is untouched", other == 200)
            outcomes["c"] = {
                "logout": lo.status_code,
                "after": [after_me, after_orders],
                "again": again,
                "other_token": other,
                "rows": rows,
            }

            cust, admin = t2, await token(api, d2d.admin_email, IP2)

            async def chat(text_: str) -> dict:
                body = {"message": text_, "message_id": str(uuid.uuid4())}
                r = await api.post("/api/v1/chat", json=body, headers=cust)
                r.raise_for_status()
                j = r.json()
                d2.say(f"assistant [{j['llm']}] ({j['status']}): {j['reply'][:100]}")
                chk.ok('the model was the built-in FAKE ("llm": "fake")', j["llm"] == "fake")
                return j

            async def approval_row(workflow_id: str) -> dict:
                run_row = (
                    await d2.rows(
                        "SELECT context FROM workflow_runs WHERE id = :i", i=uuid.UUID(workflow_id)
                    )
                )[0]
                return (
                    await d2.rows(
                        "SELECT a.id, a.status, a.decided_by, r.id AS refund_id, r.status AS refund_status "
                        "FROM approvals a JOIN refunds r ON r.id = a.refund_id WHERE a.id = :a",
                        a=uuid.UUID(run_row["context"]["approval_id"]),
                    )
                )[0]

            async def run_status(workflow_id: str) -> str:
                return (
                    await d2.rows(
                        "SELECT status FROM workflow_runs WHERE id = :i", i=uuid.UUID(workflow_id)
                    )
                )[0]["status"]

            async def updates(workflow_id: str) -> list[str]:
                rows = await d2.rows(
                    "SELECT content FROM messages WHERE workflow_run_id = :i AND role = 'assistant' "
                    "ORDER BY seq",
                    i=uuid.UUID(workflow_id),
                )
                texts = [b.get("text", "") for r in rows for b in r["content"]]
                return [t for t in texts if t.startswith("Update:")]

            async def age(approval_id: Any) -> None:
                await execute(
                    "UPDATE approvals SET created_at = now() - make_interval(hours => :h) WHERE id = :a",
                    h=AGED_HOURS,
                    a=approval_id,
                )

            # (d) -------------------------------------------------------------------------------
            d2.banner("(d) approval expiry: the overdue guard, the sweeper, the customer is told")
            text_d = f"my order {d2d.order_a} arrived damaged, refund 700"
            d2.say(f"customer: {text_d}")
            jd = await chat(text_d)
            ap_d = await approval_row(jd["workflow_id"])
            chk.ok(
                "the turn waits for a human, one pending approval",
                jd["status"] == "waiting_approval" and ap_d["status"] == "pending",
            )
            await age(ap_d["id"])
            d2.say(f"(the demo ages its own approval {AGED_HOURS} hours in the database)")
            listing = (await api.get("/api/v1/approvals?limit=100", headers=admin)).json()
            mine = next(i for i in listing["items"] if i["id"] == str(ap_d["id"]))
            d2.say(f"the admin page's deadline for it: {mine['expires_at']}")

            appr = await api.post(f"/api/v1/approvals/{ap_d['id']}/approve", json={}, headers=admin)
            rej = await api.post(f"/api/v1/approvals/{ap_d['id']}/reject", json={}, headers=admin)
            still = (await approval_row(jd["workflow_id"]))["status"]
            d2.say(
                f"BEFORE the sweeper: approve -> {appr.status_code} {appr.json().get('code')}, "
                f"reject -> {rej.status_code} {rej.json().get('code')}; approval is still {still!r}"
            )
            chk.ok(
                "approve AND reject are refused (409 APPROVAL_EXPIRED) before the sweeper has run",
                (appr.status_code, appr.json().get("code")) == (409, "APPROVAL_EXPIRED")
                and (rej.status_code, rej.json().get("code")) == (409, "APPROVAL_EXPIRED"),
            )
            chk.ok("the refusal changed nothing (still pending)", still == "pending")

            sw = await run_sweeper_once(env_api)
            d2.say(f"python -m app.jobs.sweeper --once -> exit code {sw.returncode}")
            if sw.returncode != 0:
                d2.say((sw.stdout + sw.stderr)[-1500:])
            ap_after = await approval_row(jd["workflow_id"])
            evs = await d2.scalar(
                "SELECT count(*) FROM outbox_events WHERE aggregate_id = :a "
                "AND event_type = 'refund.expired'",
                a=str(ap_after["refund_id"]),
            )
            chk.ok("the sweeper exited 0", sw.returncode == 0)
            chk.ok(
                "the approval and its refund are both `expired`, and nobody decided",
                ap_after["status"] == "expired"
                and ap_after["refund_status"] == "expired"
                and ap_after["decided_by"] is None,
            )
            chk.ok("exactly one refund.expired event is in the outbox", evs == 1)

            async def done() -> bool:
                return await run_status(jd["workflow_id"]) == "completed"

            finished = await d5.wait_until(done, 40)
            msgs = await updates(jd["workflow_id"])
            d2.say(
                f"read from the DATABASE (no GET /workflows was made): turn {await run_status(jd['workflow_id'])!r}"
            )
            d2.say(f"customer's message: {msgs[0] if msgs else None}")
            resume_log = d2.read_log(resume.log)
            chk.ok("the turn was finished by the consumer, with no GET involved", finished)
            chk.ok(
                "exactly one 'Update:' message, saying the request expired (and nothing about paid)",
                len(msgs) == 1
                and msgs[0].endswith("expired without a decision.")
                and "paid" not in msgs[0],
            )
            chk.ok(
                "the resume consumer logged workflow_resumed for it",
                "workflow_resumed" in resume_log and jd["workflow_id"] in resume_log,
            )

            late = await api.post(f"/api/v1/approvals/{ap_d['id']}/approve", json={}, headers=admin)
            expired_tab = (
                await api.get("/api/v1/approvals?status=expired&limit=100", headers=admin)
            ).json()
            card = next((i for i in expired_tab["items"] if i["id"] == str(ap_d["id"])), None)
            pending_ids = {
                i["id"]
                for i in (await api.get("/api/v1/approvals?limit=100", headers=admin)).json()[
                    "items"
                ]
            }
            chk.ok(
                "an expired approval cannot be approved afterwards either (409 APPROVAL_EXPIRED)",
                (late.status_code, late.json().get("code")) == (409, "APPROVAL_EXPIRED"),
            )
            chk.ok(
                "it is on the Expired tab (nobody decided, refund expired) and off the Pending tab",
                card is not None
                and card["decided_by"] is None
                and card["refund"]["status"] == "expired"
                and str(ap_d["id"]) not in pending_ids,
            )
            again_text = f"my order {d2d.order_a} arrived damaged, refund 700"
            d2.say(f"customer asks again: {again_text}")
            j_again = await chat(again_text)
            chk.ok(
                "the expired refund holds no money: the same request waits for a human again",
                j_again["status"] == "waiting_approval",
            )
            outcomes["d"] = {
                "first_waiting": jd["status"],
                "approve_before_sweep": [appr.status_code, appr.json().get("code")],
                "reject_before_sweep": [rej.status_code, rej.json().get("code")],
                "still_pending": still,
                "sweeper_exit": sw.returncode,
                "approval": ap_after["status"],
                "refund": ap_after["refund_status"],
                "events": evs,
                "finished_by_event": finished,
                "updates": len(msgs),
                "approve_after": [late.status_code, late.json().get("code")],
                "on_expired_tab": card is not None,
                "asks_again": j_again["status"],
            }

            # (e) -------------------------------------------------------------------------------
            d2.banner("(e) the pipeline endpoint sees a dead consumer; the fallback still serves")

            async def pipeline() -> dict:
                r = await api.get("/api/v1/admin/pipeline", headers=admin)
                r.raise_for_status()
                return r.json()

            cust_pipe = await api.get("/api/v1/admin/pipeline", headers=cust)
            chk.ok("a customer gets 403 on the pipeline endpoint", cust_pipe.status_code == 403)
            before = (await pipeline())["waiting_turns"]
            resume.popen.terminate()
            resume.popen.wait(timeout=15)
            d2.say(f"stopped the resume consumer, pid {resume.pid}")
            ap_e = await approval_row(j_again["workflow_id"])
            await age(ap_e["id"])
            sw2 = await run_sweeper_once(env_api)
            await asyncio.sleep(4)  # the relay publishes; nobody consumes
            waiting_still = await run_status(j_again["workflow_id"])
            await execute(
                "UPDATE approvals SET decided_at = now() - interval '5 minutes' WHERE id = :a",
                a=ap_e["id"],
            )
            await execute(
                "UPDATE refunds SET processed_at = now() - interval '5 minutes' WHERE id = :r",
                r=ap_e["refund_id"],
            )
            down = await pipeline()
            stuck_delta = (
                down["waiting_turns"]["decided_not_finished"] - before["decided_not_finished"]
            )
            d2.say(
                f"consumer down: the turn is {waiting_still!r}; decided_not_finished "
                f"{before['decided_not_finished']} -> {down['waiting_turns']['decided_not_finished']}; "
                f"status {down['status']!r}, attention {down['attention']}"
            )
            chk.ok("the sweeper expired the second approval too (exit 0)", sw2.returncode == 0)
            chk.ok(
                "with the consumer stopped the turn is still waiting, before any GET",
                waiting_still == "waiting_approval",
            )
            chk.ok(
                "the pipeline endpoint counts it as decided but not finished",
                stuck_delta == 1
                and "turns_not_finished" in down["attention"]
                and down["status"] == "attention",
            )
            wf = (await api.get(f"/api/v1/workflows/{j_again['workflow_id']}", headers=cust)).json()
            fixed = await pipeline()
            fixed_delta = (
                fixed["waiting_turns"]["decided_not_finished"] - before["decided_not_finished"]
            )
            chk.ok(
                "the customer's GET (the read-time fallback) finishes the turn with the expired message",
                wf["status"] == "completed"
                and (wf["latest_message"] or "").endswith("expired without a decision."),
            )
            chk.ok("and the pipeline count goes back down", fixed_delta == 0)
            snapshot_text = json.dumps(down)
            chk.ok(
                "the snapshot holds counts and ages only: no ids, no emails",
                "@" not in snapshot_text
                and str(ap_e["id"]) not in snapshot_text
                and str(d2d.customer_id) not in snapshot_text,
            )
            outcomes["e"] = {
                "customer_pipeline": cust_pipe.status_code,
                "waiting_before_get": waiting_still,
                "stuck_delta_while_down": stuck_delta,
                "attention_has_turns_not_finished": "turns_not_finished" in down["attention"],
                "after_get": wf["status"],
                "stuck_delta_after_get": fixed_delta,
            }

            # (f) -------------------------------------------------------------------------------
            d2.banner("(f) request ids: in the response header and in the API's log")
            rid = f"demo6-trace-{run}"
            hz2 = await api.get("/health", headers={"X-Request-ID": rid})
            unauth = await api.get("/api/v1/orders")
            await asyncio.sleep(0.5)
            api_log = d2.read_log(api_proc.log)
            d2.say(
                f"GET /health with X-Request-ID {rid}: {hz2.status_code} {hz2.json()}, header {hz2.headers.get('x-request-id')}"
            )
            chk.ok(
                "/health is 200 and says postgres and redis are ok",
                hz2.status_code == 200
                and hz2.json() == {"status": "ok", "postgres": "ok", "redis": "ok"},
            )
            chk.ok(
                "the request id is echoed in the response header",
                hz2.headers.get("x-request-id") == rid,
            )
            chk.ok("and it is in the API's log lines", rid in api_log)
            chk.ok(
                "a 401 carries a generated request id too",
                unauth.status_code == 401 and len(unauth.headers.get("x-request-id", "")) == 32,
            )
            outcomes["f"] = {
                "health": hz2.json(),
                "id_echoed": hz2.headers.get("x-request-id") == rid,
                "id_in_log": rid in api_log,
                "unauth": unauth.status_code,
            }

        d2.banner("SUMMARY")
        passed = sum(1 for _, ok in chk.items if ok)
        d2.say(f"checks: {passed}/{len(chk.items)} passed")
        d2.say(f"(demo rows: accounts demo-*-{run}1/2, topics {prefix}.*; logs {logdir})")
        outcomes["checks_passed"] = passed == len(chk.items)
        print("\nOUTCOMES " + json.dumps(outcomes, sort_keys=True))
        return 0 if outcomes["checks_passed"] else 1
    finally:
        d2.banner("CLEANUP")
        if procs:
            d2.stop_all(procs)
        try:
            await delete_login_keys(login_keys)
            d2.say(f"deleted the demo's {len(login_keys)} Redis login counters")
        except Exception as exc:  # noqa: BLE001  (they expire on their own after the window)
            d2.say(
                f"could not delete the Redis counters ({type(exc).__name__}); they expire by themselves"
            )
        from app.db.session import get_engine

        await get_engine().dispose()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "--port", type=int, default=8000, help="the API's port; the second API uses port + 1"
    )
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.port)))

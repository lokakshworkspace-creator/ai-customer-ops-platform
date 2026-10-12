"""Observability: the request ID is on EVERY response (errors and crashes included), is bound to
every log line of the request and does not leak into the next one, and reaches the rows the request
writes. /health says which dependency is down and never hangs or leaks details."""

import asyncio
import logging
import re
import uuid

import pytest
import structlog
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.deps import get_redis, get_session
from app.api.middleware import REQUEST_ID_HEADER, RequestIDMiddleware
from app.api.routers import health as health_module
from app.db.models import AuditLog, OutboxEvent, ToolCall
from app.main import app
from app.tests.test_approvals_api import REFUNDS
from app.tests.tokens import auth_for
from app.tests.worlds import World, make_world

HEX32 = re.compile(r"[0-9a-f]{32}")


def _mini_app() -> FastAPI:
    """The real middleware around routes that misbehave in the ways real ones can."""
    mini = FastAPI()
    mini.add_middleware(RequestIDMiddleware)

    @mini.get("/ok")
    async def ok() -> dict:
        structlog.get_logger("test").info("inside_request")
        return {"request_id": structlog.contextvars.get_contextvars().get("request_id")}

    @mini.get("/boom")
    async def boom() -> dict:
        raise RuntimeError("secret internal detail")

    return mini


async def _client(application: FastAPI) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=application, raise_app_exceptions=False), base_url="http://t"
    )


# --- the request ID on every kind of response -------------------------------------------


async def test_an_unhandled_error_is_a_clean_500_that_still_carries_the_request_id() -> None:
    async with await _client(_mini_app()) as c:
        r = await c.get("/boom", headers={REQUEST_ID_HEADER: "my-request-id-1234"})
    assert r.status_code == 500
    assert r.headers[REQUEST_ID_HEADER] == "my-request-id-1234"
    assert r.json() == {"detail": "Internal server error", "request_id": "my-request-id-1234"}
    assert "secret" not in r.text and "RuntimeError" not in r.text


async def test_a_500_without_an_inbound_id_gets_a_generated_one() -> None:
    async with await _client(_mini_app()) as c:
        r = await c.get("/boom")
    rid = r.headers[REQUEST_ID_HEADER]
    assert HEX32.fullmatch(rid) and r.json()["request_id"] == rid


def _events(caplog: pytest.LogCaptureFixture) -> list[dict]:
    """The structlog event dicts that reached the stdlib log (the app's real logging path).
    caplog, not structlog.testing.capture_logs: structlog caches loggers on first use, so
    capture_logs only sees loggers nobody used earlier in the run."""
    return [r.msg for r in caplog.records if isinstance(r.msg, dict)]


async def test_the_crash_is_still_logged_with_its_id_and_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO):
        async with await _client(_mini_app()) as c:
            await c.get("/boom", headers={REQUEST_ID_HEADER: "crash-request-id1"})
    errors = [e for e in _events(caplog) if e.get("event") == "unhandled_exception"]
    assert len(errors) == 1
    assert errors[0]["request_id"] == "crash-request-id1"  # bound while the request ran
    assert errors[0].get("exc_info")  # the traceback is kept for the operator
    access = [e for e in _events(caplog) if e.get("event") == "request"]
    assert access and access[0]["status"] == 500


@pytest.mark.parametrize(
    ("method", "path", "headers", "body", "status"),
    [
        ("GET", "/api/v1/orders", {}, None, 401),  # no login
        ("GET", "/api/v1/nope", {}, None, 404),
        ("DELETE", "/health", {}, None, 405),
        ("POST", "/api/v1/auth/login", {}, {"email": ""}, 422),  # validation
        ("GET", "/health", {}, None, None),  # 200 or 503, whichever the stack is in
    ],
)
async def test_every_status_carries_the_request_id(
    client: AsyncClient, method, path, headers, body, status
) -> None:
    r = await client.request(method, path, headers=headers, json=body)
    if status is not None:
        assert r.status_code == status
    assert HEX32.fullmatch(r.headers[REQUEST_ID_HEADER])


async def test_a_429_carries_the_request_id_and_the_clients_own_id_when_valid(
    db_client: AsyncClient, login_limiter
) -> None:
    login_limiter.max_failures = 1
    body = {"email": "nobody@example.com", "password": "x"}
    await db_client.post("/api/v1/auth/login", json=body)
    r = await db_client.post(
        "/api/v1/auth/login", json=body, headers={REQUEST_ID_HEADER: "client-chosen-id-77"}
    )
    assert r.status_code == 429
    assert r.headers[REQUEST_ID_HEADER] == "client-chosen-id-77"


# --- logs -------------------------------------------------------------------------------


async def test_the_id_is_bound_to_log_lines_during_the_request_and_not_after() -> None:
    async with await _client(_mini_app()) as c:
        first = await c.get("/ok", headers={REQUEST_ID_HEADER: "first-request-id1"})
        assert first.json() == {"request_id": "first-request-id1"}
        second = await c.get("/ok")
    assert second.json()["request_id"] == second.headers[REQUEST_ID_HEADER]
    assert second.json()["request_id"] != "first-request-id1"  # nothing leaked between requests
    assert structlog.contextvars.get_contextvars() == {}  # and nothing is left behind


async def test_each_request_writes_exactly_one_access_line_with_method_path_status_duration(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO):
        async with await _client(_mini_app()) as c:
            await c.get("/ok", headers={REQUEST_ID_HEADER: "access-line-id-01"})
    (line,) = [e for e in _events(caplog) if e.get("event") == "request"]
    assert (line["method"], line["path"], line["status"]) == ("GET", "/ok", 200)
    assert line["request_id"] == "access-line-id-01"
    assert isinstance(line["duration_ms"], float) and line["duration_ms"] >= 0


# --- the id reaches what the request writes ---------------------------------------------


@pytest.fixture
async def world(db_session) -> World:
    return await make_world(db_session)


async def test_the_request_id_reaches_the_tool_call_audit_and_outbox_rows(
    refund_client, db_session, world
) -> None:
    rid = "trace-me-0001-abcd"
    headers = {
        **auth_for(world.alice.id),
        "Idempotency-Key": f"key-{uuid.uuid4().hex}",
        REQUEST_ID_HEADER: rid,
    }
    r = await refund_client.post(
        REFUNDS,
        json={"order_id": str(world.order), "amount": "800.00", "reason_code": "damaged"},
        headers=headers,
    )
    assert r.status_code == 202 and r.headers[REQUEST_ID_HEADER] == rid

    calls = (await db_session.scalars(select(ToolCall.request_id))).all()
    audits = (await db_session.scalars(select(AuditLog.request_id))).all()
    events = (await db_session.scalars(select(OutboxEvent.payload))).all()
    assert rid in calls
    assert rid in audits
    assert any(p["meta"]["request_id"] == rid for p in events)


# --- /health ----------------------------------------------------------------------------


class _Redis:
    def __init__(self, behaviour: str) -> None:
        self.behaviour = behaviour

    async def ping(self) -> bool:
        if self.behaviour == "down":
            raise ConnectionError("redis://user:secret@host: connection refused")
        if self.behaviour == "hang":
            await asyncio.sleep(30)
        return True


class _Session:
    def __init__(self, behaviour: str) -> None:
        self.behaviour = behaviour

    async def execute(self, *_a):
        if self.behaviour == "down":
            raise ConnectionError("postgresql://ops:secret@db: connection refused")
        if self.behaviour == "hang":
            await asyncio.sleep(30)


async def _health(pg: str, rd: str):
    async def session():
        yield _Session(pg)

    app.dependency_overrides[get_session] = session
    app.dependency_overrides[get_redis] = lambda: _Redis(rd)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            return await c.get("/health")
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(get_redis, None)


@pytest.mark.parametrize(
    ("pg", "rd", "code", "expected"),
    [
        ("ok", "ok", 200, {"status": "ok", "postgres": "ok", "redis": "ok"}),
        ("down", "ok", 503, {"status": "degraded", "postgres": "error", "redis": "ok"}),
        ("ok", "down", 503, {"status": "degraded", "postgres": "ok", "redis": "error"}),
        ("down", "down", 503, {"status": "degraded", "postgres": "error", "redis": "error"}),
    ],
)
async def test_health_names_the_dependency_that_is_down(pg, rd, code, expected) -> None:
    r = await _health(pg, rd)
    assert r.status_code == code
    assert r.json() == expected
    assert HEX32.fullmatch(r.headers[REQUEST_ID_HEADER])


async def test_health_never_leaks_connection_details() -> None:
    r = await _health("down", "down")
    assert "secret" not in r.text and "refused" not in r.text and "@" not in r.text


async def test_a_hanging_dependency_cannot_hang_health(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health_module, "CHECK_TIMEOUT_S", 0.2)
    started = asyncio.get_running_loop().time()
    r = await _health("hang", "hang")
    assert asyncio.get_running_loop().time() - started < 3
    assert r.status_code == 503
    assert r.json()["postgres"] == "error" and r.json()["redis"] == "error"


async def test_health_needs_no_login_and_is_not_rate_limited(client: AsyncClient) -> None:
    for _ in range(12):
        r = await client.get("/health")
        assert r.status_code in (200, 503)  # never 401 or 429

"""Login rate limiting: per email + IP, Redis-backed, fail open.

These tests need the dev Redis (docker compose) except the fail-open ones, which point the limiter
at a port nobody listens on."""

import asyncio

import pytest
from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from app.api.deps import get_login_limiter
from app.core.config import get_settings
from app.core.login_limiter import LoginLimiter, client_ip
from app.main import app
from app.modules.users import service as users_service
from app.tests.factories import create_user

URL = "/api/v1/auth/login"
PASSWORD = "correct horse battery"
XFF = "X-Forwarded-For"


async def _login(client: AsyncClient, email: str, password: str, **headers: str):
    return await client.post(URL, json={"email": email, "password": password}, headers=headers)


@pytest.fixture
def limiter(login_limiter: LoginLimiter) -> LoginLimiter:
    """Three failures are enough to block in these tests (keeps the argon2 work small)."""
    login_limiter.max_failures = 3
    return login_limiter


async def _fail(client: AsyncClient, email: str, n: int, **headers: str) -> None:
    for _ in range(n):
        r = await _login(client, email, "wrong", **headers)
        assert r.status_code == 401


async def test_the_attempt_after_max_failures_is_429_with_retry_after(
    db_client: AsyncClient, db_session: AsyncSession, limiter: LoginLimiter
) -> None:
    await create_user(db_session, email="alice@example.com", password=PASSWORD)
    await _fail(db_client, "alice@example.com", 3)
    r = await _login(db_client, "alice@example.com", "wrong")
    assert r.status_code == 429
    wait = int(r.headers["Retry-After"])
    assert 1 <= wait <= limiter.window_s
    assert str(wait) in r.json()["detail"]
    assert "access_token" not in r.text


async def test_a_blocked_caller_is_refused_even_with_the_right_password(
    db_client: AsyncClient, db_session: AsyncSession, limiter: LoginLimiter
) -> None:
    await create_user(db_session, email="alice@example.com", password=PASSWORD)
    await _fail(db_client, "alice@example.com", 3)
    r = await _login(db_client, "alice@example.com", PASSWORD)
    assert r.status_code == 429
    assert "access_token" not in r.text


async def test_a_blocked_attempt_does_no_password_work_and_does_not_extend_the_window(
    db_client: AsyncClient,
    db_session: AsyncSession,
    limiter: LoginLimiter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await create_user(db_session, email="alice@example.com", password=PASSWORD)
    await _fail(db_client, "alice@example.com", 3)
    first = int((await _login(db_client, "alice@example.com", "x")).headers["Retry-After"])

    calls: list[str] = []
    monkeypatch.setattr(users_service, "verify_password", lambda *a: calls.append("hit") or False)
    for _ in range(3):
        assert (await _login(db_client, "alice@example.com", "x")).status_code == 429
    assert calls == []  # not even a dummy argon2 verification
    again = int((await _login(db_client, "alice@example.com", "x")).headers["Retry-After"])
    assert again <= first  # blocked attempts are not counted: the window only runs down


async def test_a_successful_login_resets_the_counter(
    db_client: AsyncClient, db_session: AsyncSession, limiter: LoginLimiter
) -> None:
    await create_user(db_session, email="alice@example.com", password=PASSWORD)
    await _fail(db_client, "alice@example.com", 2)
    assert (await _login(db_client, "alice@example.com", PASSWORD)).status_code == 200
    await _fail(db_client, "alice@example.com", 2)  # would be the 4th failure without the reset
    assert (await _login(db_client, "alice@example.com", PASSWORD)).status_code == 200


async def test_the_limit_is_per_email(
    db_client: AsyncClient, db_session: AsyncSession, limiter: LoginLimiter
) -> None:
    await create_user(db_session, email="alice@example.com", password=PASSWORD)
    await create_user(db_session, email="bob@example.com", password=PASSWORD)
    await _fail(db_client, "alice@example.com", 3)
    assert (await _login(db_client, "alice@example.com", "x")).status_code == 429
    assert (await _login(db_client, "bob@example.com", PASSWORD)).status_code == 200


async def test_the_limit_is_per_ip(
    db_client: AsyncClient,
    db_session: AsyncSession,
    limiter: LoginLimiter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another IP trying the same email is not blocked: a victim cannot be locked out from afar."""
    monkeypatch.setattr(get_settings(), "trusted_proxy_hops", 1)
    await create_user(db_session, email="alice@example.com", password=PASSWORD)
    await _fail(db_client, "alice@example.com", 3, **{XFF: "203.0.113.7"})
    blocked = await _login(db_client, "alice@example.com", "x", **{XFF: "203.0.113.7"})
    assert blocked.status_code == 429
    other = await _login(db_client, "alice@example.com", PASSWORD, **{XFF: "198.51.100.9"})
    assert other.status_code == 200


async def test_an_unknown_email_is_counted_and_blocked_exactly_like_a_known_one(
    db_client: AsyncClient, db_session: AsyncSession, limiter: LoginLimiter
) -> None:
    await create_user(db_session, email="alice@example.com", password=PASSWORD)
    await _fail(db_client, "alice@example.com", 3)
    await _fail(db_client, "nobody@example.com", 3)
    known = await _login(db_client, "alice@example.com", "x")
    unknown = await _login(db_client, "nobody@example.com", "x")
    assert known.status_code == unknown.status_code == 429
    assert known.json()["detail"].split(" in ")[0] == unknown.json()["detail"].split(" in ")[0]


async def test_the_window_ends_and_the_caller_can_try_again(
    db_client: AsyncClient, db_session: AsyncSession, limiter: LoginLimiter
) -> None:
    await create_user(db_session, email="alice@example.com", password=PASSWORD)
    await _fail(db_client, "alice@example.com", 3)
    assert (await _login(db_client, "alice@example.com", PASSWORD)).status_code == 429
    await limiter._redis.pexpire(limiter.key("alice@example.com", "127.0.0.1"), 1)
    await asyncio.sleep(0.05)  # the window has run out
    assert (await _login(db_client, "alice@example.com", PASSWORD)).status_code == 200


async def test_the_counter_always_has_a_ttl_and_never_holds_the_email(
    db_client: AsyncClient, db_session: AsyncSession, limiter: LoginLimiter
) -> None:
    await create_user(db_session, email="alice@example.com", password=PASSWORD)
    await _fail(db_client, "alice@example.com", 1)
    key = limiter.key("alice@example.com", "127.0.0.1")
    assert limiter.key("  ALICE@example.com", "127.0.0.1") == key  # normalised
    assert "alice" not in key
    ttl = await limiter._redis.pttl(key)
    assert 0 < ttl <= limiter.window_s * 1000


async def test_redis_down_fails_open_and_login_still_works(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    dead = Redis.from_url("redis://127.0.0.1:1/0", decode_responses=True)
    app.dependency_overrides[get_login_limiter] = lambda: LoginLimiter(
        dead, max_failures=1, window_s=900, timeout_s=0.3
    )
    try:
        await create_user(db_session, email="alice@example.com", password=PASSWORD)
        for _ in range(3):  # would be blocked after 1 failure if the limiter worked
            assert (await _login(db_client, "alice@example.com", "wrong")).status_code == 401
        assert (await _login(db_client, "alice@example.com", PASSWORD)).status_code == 200
    finally:
        await dead.aclose()


async def test_a_redis_that_hangs_does_not_stall_login(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    class Hanging:
        async def get(self, *_a):
            await asyncio.sleep(30)

        eval = pttl = delete = get

    app.dependency_overrides[get_login_limiter] = lambda: LoginLimiter(
        Hanging(),  # type: ignore[arg-type]
        max_failures=1,
        window_s=900,
        timeout_s=0.2,
    )
    await create_user(db_session, email="alice@example.com", password=PASSWORD)
    assert (await _login(db_client, "alice@example.com", PASSWORD)).status_code == 200


def _request(peer: str | None, xff: str | None) -> Request:
    headers = [(b"x-forwarded-for", xff.encode())] if xff is not None else []
    scope = {"type": "http", "headers": headers, "client": (peer, 1234) if peer else None}
    return Request(scope)


@pytest.mark.parametrize(
    ("peer", "xff", "hops", "expected"),
    [
        ("10.0.0.1", "1.2.3.4", 0, "10.0.0.1"),  # hops 0: the header is never read
        ("10.0.0.1", "1.2.3.4", 1, "1.2.3.4"),
        ("10.0.0.1", "9.9.9.9, 1.2.3.4", 1, "1.2.3.4"),  # spoofed left side ignored
        ("10.0.0.1", "9.9.9.9, 1.2.3.4, 10.0.0.2", 2, "1.2.3.4"),
        ("10.0.0.1", None, 1, "10.0.0.1"),  # no header: the peer
        ("10.0.0.1", "1.2.3.4", 2, "10.0.0.1"),  # fewer entries than hops: the peer
        ("10.0.0.1", "not-an-ip", 1, "10.0.0.1"),  # malformed: the peer
        (None, None, 0, "unknown"),
    ],
)
def test_client_ip(peer: str | None, xff: str | None, hops: int, expected: str) -> None:
    assert client_ip(_request(peer, xff), hops) == expected

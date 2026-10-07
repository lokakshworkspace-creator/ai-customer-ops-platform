import pytest
from httpx import AsyncClient

from app.api.deps import get_redis, get_session
from app.main import app


class FakeSession:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    async def execute(self, *_args, **_kwargs) -> None:
        if self.fail:
            raise ConnectionRefusedError("postgres down")


class FakeRedis:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    async def ping(self) -> bool:
        if self.fail:
            raise ConnectionError("redis down")
        return True


def _override(pg_fail: bool, redis_fail: bool) -> None:
    async def session():
        yield FakeSession(pg_fail)

    app.dependency_overrides[get_session] = session
    app.dependency_overrides[get_redis] = lambda: FakeRedis(redis_fail)


async def test_health_ok(client: AsyncClient) -> None:
    _override(False, False)
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "postgres": "ok", "redis": "ok"}


@pytest.mark.parametrize(
    ("pg_fail", "redis_fail", "expected"),
    [
        (True, False, {"status": "degraded", "postgres": "error", "redis": "ok"}),
        (False, True, {"status": "degraded", "postgres": "ok", "redis": "error"}),
        (True, True, {"status": "degraded", "postgres": "error", "redis": "error"}),
    ],
)
async def test_health_degraded_returns_503(
    client: AsyncClient, pg_fail: bool, redis_fail: bool, expected: dict
) -> None:
    _override(pg_fail, redis_fail)
    r = await client.get("/health")
    assert r.status_code == 503
    assert r.json() == expected


async def test_request_id_generated_and_echoed(client: AsyncClient) -> None:
    _override(False, False)
    r = await client.get("/health")
    assert len(r.headers["x-request-id"]) == 32


async def test_inbound_request_id_is_honoured(client: AsyncClient) -> None:
    _override(False, False)
    r = await client.get("/health", headers={"X-Request-ID": "abc-123"})
    assert r.headers["x-request-id"] == "abc-123"

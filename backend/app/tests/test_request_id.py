import re

import pytest
from httpx import AsyncClient
from starlette.types import Message

from app.api.deps import get_redis, get_session
from app.api.middleware import REQUEST_ID_HEADER, RequestIDMiddleware
from app.main import app

GENERATED = re.compile(r"[0-9a-f]{32}")


class _OkSession:
    async def execute(self, *_a, **_k) -> None:
        return None


class _OkRedis:
    async def ping(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def _healthy_deps():
    async def session():
        yield _OkSession()

    app.dependency_overrides[get_session] = session
    app.dependency_overrides[get_redis] = lambda: _OkRedis()


async def test_generated_when_absent(client: AsyncClient) -> None:
    r = await client.get("/health")
    assert GENERATED.fullmatch(r.headers[REQUEST_ID_HEADER])


@pytest.mark.parametrize("rid", ["abc-12345", "A_b-C_d-E", "a" * 8, "a" * 64, "550e8400-e29b-41d4"])
async def test_valid_id_is_honoured(client: AsyncClient, rid: str) -> None:
    r = await client.get("/health", headers={REQUEST_ID_HEADER: rid})
    assert r.headers[REQUEST_ID_HEADER] == rid


@pytest.mark.parametrize(
    "rid",
    [
        "a" * 65,  # too long
        "a" * 500,  # way too long
        "abc-123",  # too short (7)
        "a",  # too short
        "has space in it",
        "semi;colon=bad",
        "inject<script>",
        "../../etc/passwd",
        "ünïcödé-id-1234",
    ],
)
async def test_invalid_id_is_replaced(client: AsyncClient, rid: str) -> None:
    r = await client.get("/health", headers={REQUEST_ID_HEADER: rid.encode("latin-1")})
    out = r.headers[REQUEST_ID_HEADER]
    assert out != rid
    assert GENERATED.fullmatch(out)


@pytest.mark.parametrize(
    "rid", [b"abcdefgh\n", b"abcdefgh\r\nX-Evil: 1", b"abc\ndefghijkl", b"abcdefgh\r"]
)
async def test_id_with_newlines_is_replaced(rid: bytes) -> None:
    """Driven at the ASGI level: HTTP clients refuse to send raw newlines in headers."""
    sent: list[Message] = []

    async def inner(scope, receive, send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/x",
        "headers": [(REQUEST_ID_HEADER.lower().encode(), rid)],
    }
    await RequestIDMiddleware(inner)(scope, receive, send)

    headers = dict(sent[0]["headers"])
    out = headers[REQUEST_ID_HEADER.lower().encode()].decode()
    assert GENERATED.fullmatch(out)
    assert scope["state"]["request_id"] == out

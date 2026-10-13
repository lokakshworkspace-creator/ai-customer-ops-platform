"""API-level response hygiene: nosniff everywhere, no-store under /api/, no CORS at all, and no
interactive docs in production. (The Next.js page's own headers are in frontend/next.config.ts and
are checked in a real browser.)"""

import pytest
from fastapi import Response
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.main import app, create_app, docs_urls

PROD = Settings(app_env="prod", jwt_secret="p" * 40)  # the production secret guard needs a real one


def _client(application) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=application, raise_app_exceptions=False), base_url="http://t"
    )


def _app_with_extra_routes():
    application = create_app()

    @application.get("/api/v1/_boom")
    async def boom() -> dict:
        raise RuntimeError("secret internal detail")

    @application.get("/api/v1/_own_cache_control")
    async def own(response: Response) -> dict:
        response.headers["Cache-Control"] = "private, max-age=5"
        return {}

    return application


# --- nosniff and no-store ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/health", None),
        ("GET", "/api/v1/orders", None),  # 401
        ("GET", "/api/v1/nope", None),  # 404
        ("POST", "/api/v1/auth/login", {"email": ""}),  # 422
        ("GET", "/docs", None),
        ("GET", "/openapi.json", None),
    ],
)
async def test_nosniff_is_on_every_response(client: AsyncClient, method, path, body) -> None:
    r = await client.request(method, path, json=body)
    assert r.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/api/v1/orders", None),  # 401
        ("GET", "/api/v1/auth/me", None),  # 401
        ("GET", "/api/v1/nope", None),  # 404 under /api/
        ("POST", "/api/v1/auth/login", {"email": "", "password": ""}),  # 422
        ("POST", "/api/v1/auth/login", {"email": "x@example.com", "password": "wrong"}),  # 401
    ],
)
async def test_everything_under_api_is_no_store(db_client: AsyncClient, method, path, body) -> None:
    r = await db_client.request(method, path, json=body)
    assert r.headers["cache-control"] == "no-store"


async def test_a_login_response_that_carries_a_token_is_no_store(
    db_client: AsyncClient, db_session
) -> None:
    from app.tests.factories import create_user

    await create_user(db_session, email="alice@example.com", password="correct horse battery")
    r = await db_client.post(
        "/api/v1/auth/login",
        json={"email": "alice@example.com", "password": "correct horse battery"},
    )
    assert r.status_code == 200 and "access_token" in r.json()
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-content-type-options"] == "nosniff"


async def test_health_and_the_docs_are_not_forced_to_no_store(client: AsyncClient) -> None:
    """Only /api/ is: a health probe and the docs page are not secrets."""
    assert "cache-control" not in (await client.get("/health")).headers


async def test_an_endpoint_that_sets_its_own_cache_control_keeps_it() -> None:
    async with _client(_app_with_extra_routes()) as c:
        r = await c.get("/api/v1/_own_cache_control")
    assert r.headers["cache-control"] == "private, max-age=5"
    assert r.headers["x-content-type-options"] == "nosniff"


async def test_a_crash_500_carries_the_headers_and_the_request_id_too() -> None:
    async with _client(_app_with_extra_routes()) as c:
        r = await c.get("/api/v1/_boom")
    assert r.status_code == 500
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-request-id"] and "secret" not in r.text


# --- CORS: nothing is allowed -----------------------------------------------------------


async def test_there_is_no_cors_middleware_in_the_app() -> None:
    names = {m.cls.__name__ for m in app.user_middleware}
    assert not {n for n in names if "cors" in n.lower()}
    assert names == {"RequestIDMiddleware", "SecurityHeadersMiddleware"}


async def test_a_cross_origin_request_gets_no_cors_headers(client: AsyncClient) -> None:
    r = await client.get("/health", headers={"Origin": "https://evil.example"})
    assert not [h for h in r.headers if h.lower().startswith("access-control-")]


async def test_a_cors_preflight_is_refused_and_grants_nothing(client: AsyncClient) -> None:
    r = await client.options(
        "/api/v1/auth/login",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization, content-type",
        },
    )
    assert r.status_code == 405
    assert not [h for h in r.headers if h.lower().startswith("access-control-")]


# --- the docs are for development -------------------------------------------------------


async def test_the_docs_exist_in_development(client: AsyncClient) -> None:
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert (await client.get(path)).status_code == 200, path


async def test_production_has_no_docs_redoc_or_schema() -> None:
    assert PROD.is_prod
    async with _client(create_app(PROD)) as c:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert (await c.get(path)).status_code == 404, path
        # and the API itself is still there
        assert (await c.get("/api/v1/orders")).status_code == 401


def test_docs_urls_follow_the_environment() -> None:
    assert docs_urls(PROD) == {"docs_url": None, "redoc_url": None, "openapi_url": None}
    dev = Settings(app_env="dev")
    assert docs_urls(dev) == {
        "docs_url": "/docs",
        "redoc_url": "/redoc",
        "openapi_url": "/openapi.json",
    }

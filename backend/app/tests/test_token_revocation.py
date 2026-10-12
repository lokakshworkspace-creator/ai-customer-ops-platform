"""Token revocation: a revoked token is refused everywhere, other tokens are not affected, the
`jti` is required, and expired rows are purged. The `/auth/logout` endpoint that creates the rows
is tested in test_logout.py."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.routing import APIRoute
from httpx import AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_token, get_current_user
from app.core.security import InvalidTokenError, create_access_token, decode_token
from app.db.models import RevokedToken
from app.db.models.enums import Role
from app.main import app
from app.modules.users import tokens as users_tokens
from app.tests.factories import create_user
from app.tests.tokens import bearer, forge_token

# Every route that is not public.
PUBLIC = {("GET", "/health"), ("POST", "/api/v1/auth/login")}


def _api_routes() -> list:
    """Every route, including the ones this FastAPI version keeps inside included routers."""
    out = []
    for r in app.routes:
        inner = getattr(r, "original_router", None)
        out.extend(inner.routes if inner is not None else [r])
    return out


def _walk(dependant) -> set:
    calls = {dependant.call}
    for d in dependant.dependencies:
        calls |= _walk(d)
    return calls


def test_every_non_public_route_goes_through_the_token_check() -> None:
    """Revocation lives in get_current_token, which get_current_user depends on. If some route
    authenticated another way, a revoked token could still work there."""
    checked = 0
    for route in _api_routes():
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods:
            if (method, route.path) in PUBLIC:
                continue
            calls = _walk(route.dependant)
            assert get_current_user in calls and get_current_token in calls, (method, route.path)
            checked += 1
    assert checked >= 11  # a floor, so an empty loop cannot pass by accident


async def _revoke(db_session: AsyncSession, token: str) -> None:
    claims = decode_token(token)
    await users_tokens.revoke(
        db_session, jti=claims.jti, user_id=claims.user_id, expires_at=claims.expires_at
    )


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/auth/me"),
        ("GET", "/api/v1/orders"),
        ("GET", f"/api/v1/orders/{uuid.uuid4()}"),
        ("GET", f"/api/v1/refunds/{uuid.uuid4()}"),
        ("POST", "/api/v1/refunds"),
        ("GET", "/api/v1/approvals"),
        ("POST", f"/api/v1/approvals/{uuid.uuid4()}/approve"),
        ("POST", f"/api/v1/approvals/{uuid.uuid4()}/reject"),
        ("POST", "/api/v1/chat"),
        ("GET", f"/api/v1/workflows/{uuid.uuid4()}"),
        ("GET", "/api/v1/admin/knowledge/search?q=shipping"),
    ],
)
async def test_a_revoked_token_gets_401_everywhere_and_a_live_one_does_not(
    db_client: AsyncClient, db_session: AsyncSession, method: str, path: str
) -> None:
    admin = await create_user(db_session, email="admin@example.com", role=Role.admin)
    await db_session.commit()  # some endpoints end their read transaction: the user must be saved
    token, _ = create_access_token(admin.id)

    live = await db_client.request(method, path, headers=bearer(token))
    assert live.status_code != 401  # authenticated (whatever the endpoint then says)

    await _revoke(db_session, token)
    r = await db_client.request(method, path, headers=bearer(token))
    assert r.status_code == 401
    assert r.json() == {"detail": "Not authenticated"}
    assert r.headers["www-authenticate"] == "Bearer"


async def test_revoking_one_token_leaves_the_users_other_tokens_alone(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await create_user(db_session, email="alice@example.com")
    first, _ = create_access_token(user.id)
    second, _ = create_access_token(user.id)
    assert decode_token(first).jti != decode_token(second).jti
    await _revoke(db_session, first)
    assert (await db_client.get("/api/v1/auth/me", headers=bearer(first))).status_code == 401
    assert (await db_client.get("/api/v1/auth/me", headers=bearer(second))).status_code == 200


async def test_revoking_is_idempotent(db_session: AsyncSession) -> None:
    user = await create_user(db_session, email="alice@example.com")
    token, _ = create_access_token(user.id)
    await _revoke(db_session, token)
    await _revoke(db_session, token)
    assert await db_session.scalar(select(func.count()).select_from(RevokedToken)) == 1


async def test_a_token_without_a_jti_is_refused(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    """It could never be revoked, so it is not accepted at all."""
    user = await create_user(db_session, email="alice@example.com")
    r = await db_client.get("/api/v1/auth/me", headers=bearer(forge_token(user.id, jti=None)))
    assert r.status_code == 401
    with pytest.raises(InvalidTokenError):
        decode_token(forge_token(user.id, jti=None))
    with pytest.raises(InvalidTokenError):
        decode_token(forge_token(user.id, jti="x" * 65))
    assert decode_token(forge_token(user.id)).user_id == user.id


async def test_a_forged_token_with_a_revoked_jti_is_still_just_a_bad_signature(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await create_user(db_session, email="alice@example.com")
    r = await db_client.get(
        "/api/v1/auth/me", headers=bearer(forge_token(user.id, secret="not-the-secret"))
    )
    assert r.status_code == 401


async def test_purge_removes_only_rows_whose_token_has_expired_anyway(
    db_session: AsyncSession,
) -> None:
    user = await create_user(db_session, email="alice@example.com")
    now = datetime.now(UTC)
    for jti, delta in (
        ("old1", -timedelta(minutes=1)),
        ("old2", -timedelta(hours=2)),
        ("live", timedelta(minutes=20)),
    ):
        await users_tokens.revoke(db_session, jti=jti, user_id=user.id, expires_at=now + delta)
    assert await users_tokens.purge_expired(db_session, now=now) == 2
    left = (await db_session.scalars(select(RevokedToken.jti))).all()
    assert left == ["live"]


async def test_deleting_a_user_deletes_their_revocations(db_session: AsyncSession) -> None:
    user = await create_user(db_session, email="alice@example.com")
    await users_tokens.revoke(
        db_session, jti="abc", user_id=user.id, expires_at=datetime.now(UTC) + timedelta(minutes=5)
    )
    await db_session.execute(text("DELETE FROM users WHERE id = :i"), {"i": user.id})
    assert await db_session.scalar(select(func.count()).select_from(RevokedToken)) == 0

"""POST /api/v1/auth/logout revokes the token it was called with, and only that one."""

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, decode_token
from app.db.models import AuditLog, RevokedToken
from app.tests.factories import create_user
from app.tests.tokens import bearer

URL = "/api/v1/auth/logout"
PASSWORD = "correct horse battery"


async def _token_for(client: AsyncClient, email: str) -> str:
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200
    return r.json()["access_token"]


async def test_logout_makes_the_token_useless_everywhere(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await create_user(db_session, email="alice@example.com", password=PASSWORD)
    token = await _token_for(db_client, "alice@example.com")
    assert (await db_client.get("/api/v1/auth/me", headers=bearer(token))).status_code == 200

    r = await db_client.post(URL, headers=bearer(token))
    assert r.status_code == 204
    assert r.content == b""

    for path in ("/api/v1/auth/me", "/api/v1/orders"):
        after = await db_client.get(path, headers=bearer(token))
        assert after.status_code == 401
        assert after.json() == {"detail": "Not authenticated"}


async def test_logout_revokes_only_the_token_it_was_called_with(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await create_user(db_session, email="alice@example.com", password=PASSWORD)
    laptop = await _token_for(db_client, "alice@example.com")
    phone = await _token_for(db_client, "alice@example.com")
    await db_client.post(URL, headers=bearer(laptop))
    assert (await db_client.get("/api/v1/auth/me", headers=bearer(laptop))).status_code == 401
    assert (await db_client.get("/api/v1/auth/me", headers=bearer(phone))).status_code == 200


async def test_logout_does_not_touch_another_users_tokens(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await create_user(db_session, email="alice@example.com", password=PASSWORD)
    bob = await create_user(db_session, email="bob@example.com", password=PASSWORD)
    alice_token = await _token_for(db_client, "alice@example.com")
    bob_token = create_access_token(bob.id)[0]
    await db_client.post(URL, headers=bearer(alice_token))
    assert (await db_client.get("/api/v1/auth/me", headers=bearer(bob_token))).status_code == 200


async def test_logging_out_twice_is_a_401_the_second_time_and_one_row(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await create_user(db_session, email="alice@example.com", password=PASSWORD)
    token = await _token_for(db_client, "alice@example.com")
    assert (await db_client.post(URL, headers=bearer(token))).status_code == 204
    assert (await db_client.post(URL, headers=bearer(token))).status_code == 401
    assert await db_session.scalar(select(func.count()).select_from(RevokedToken)) == 1


async def test_logout_needs_a_valid_token(db_client: AsyncClient) -> None:
    assert (await db_client.post(URL)).status_code == 401
    assert (await db_client.post(URL, headers=bearer("garbage"))).status_code == 401


async def test_the_row_holds_the_tokens_own_id_and_expiry_and_is_audited(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await create_user(db_session, email="alice@example.com", password=PASSWORD)
    token = await _token_for(db_client, "alice@example.com")
    claims = decode_token(token)
    await db_client.post(URL, headers=bearer(token))

    row = (await db_session.scalars(select(RevokedToken))).one()
    assert row.jti == claims.jti
    assert row.user_id == user.id
    assert (
        row.expires_at == claims.expires_at
    )  # the token's own expiry, so the sweeper can purge it

    audit = (
        await db_session.scalars(select(AuditLog).where(AuditLog.action == "auth.logout"))
    ).one()
    assert audit.actor_user_id == user.id
    assert audit.resource_id == claims.jti
    assert token not in str(audit.details)  # the token itself is never stored


async def test_a_revoked_token_cannot_log_in_again_but_a_new_login_works(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await create_user(db_session, email="alice@example.com", password=PASSWORD)
    old = await _token_for(db_client, "alice@example.com")
    await db_client.post(URL, headers=bearer(old))
    new = await _token_for(db_client, "alice@example.com")
    assert decode_token(new).jti != decode_token(old).jti
    assert (await db_client.get("/api/v1/auth/me", headers=bearer(new))).status_code == 200

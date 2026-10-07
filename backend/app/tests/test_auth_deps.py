"""get_current_user and require_role, exercised through a tiny throwaway app."""

import uuid
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_session, require_role
from app.core.security import create_access_token
from app.db.models import User
from app.db.models.enums import Role
from app.tests.factories import create_user
from app.tests.tokens import bearer, forge_token


@pytest.fixture
async def probe(db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    app = FastAPI()

    @app.get("/whoami")
    async def whoami(user: CurrentUser) -> dict:
        return {"id": str(user.id), "role": user.role}

    @app.get("/admin")
    async def admin(user: User = Depends(require_role(Role.admin))) -> dict:
        return {"id": str(user.id)}

    @app.get("/either")
    async def either(user: User = Depends(require_role(Role.admin, Role.customer))) -> dict:
        return {"id": str(user.id)}

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_session] = _session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _token_for(user: User) -> dict[str, str]:
    return bearer(create_access_token(user.id)[0])


async def test_valid_token_authenticates(probe: AsyncClient, db_session: AsyncSession) -> None:
    user = await create_user(db_session)
    r = await probe.get("/whoami", headers=_token_for(user))
    assert r.status_code == 200
    assert r.json() == {"id": str(user.id), "role": "customer"}


async def test_no_token_is_401(probe: AsyncClient) -> None:
    r = await probe.get("/whoami")
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer "},
        {"Authorization": "Bearer garbage"},
        {"Authorization": "Basic dXNlcjpwYXNz"},
        {"Authorization": "token abc"},
    ],
)
async def test_malformed_authorization_header_is_401(probe: AsyncClient, headers: dict) -> None:
    assert (await probe.get("/whoami", headers=headers)).status_code == 401


async def test_expired_token_is_401(probe: AsyncClient, db_session: AsyncSession) -> None:
    user = await create_user(db_session)
    token = forge_token(user.id, expires_in=timedelta(seconds=-1))
    assert (await probe.get("/whoami", headers=bearer(token))).status_code == 401


async def test_tampered_signature_is_401(probe: AsyncClient, db_session: AsyncSession) -> None:
    victim = await create_user(db_session)
    attacker = await create_user(db_session)
    header, _payload, signature = create_access_token(attacker.id)[0].split(".")
    # Swap in the victim's id while keeping the attacker's signature.
    victim_payload = forge_token(victim.id).split(".")[1]
    forged = f"{header}.{victim_payload}.{signature}"
    assert (await probe.get("/whoami", headers=bearer(forged))).status_code == 401


async def test_token_signed_with_wrong_secret_is_401(
    probe: AsyncClient, db_session: AsyncSession
) -> None:
    user = await create_user(db_session)
    token = forge_token(user.id, secret="attacker-controlled-secret-0123456789")
    assert (await probe.get("/whoami", headers=bearer(token))).status_code == 401


async def test_alg_none_token_is_401(probe: AsyncClient, db_session: AsyncSession) -> None:
    user = await create_user(db_session, role=Role.admin)
    token = forge_token(user.id, algorithm="none")
    assert token.endswith(".")  # unsigned
    r = await probe.get("/whoami", headers=bearer(token))
    assert r.status_code == 401


async def test_token_missing_exp_or_sub_is_401(
    probe: AsyncClient, db_session: AsyncSession
) -> None:
    user = await create_user(db_session)
    no_exp = forge_token(user.id, expires_in=None)
    no_sub = forge_token(None)
    assert (await probe.get("/whoami", headers=bearer(no_exp))).status_code == 401
    assert (await probe.get("/whoami", headers=bearer(no_sub))).status_code == 401


async def test_token_for_unknown_user_is_401(probe: AsyncClient) -> None:
    token = create_access_token(uuid.uuid4())[0]
    assert (await probe.get("/whoami", headers=bearer(token))).status_code == 401


async def test_token_for_a_deleted_user_is_401(
    probe: AsyncClient, db_session: AsyncSession
) -> None:
    user = await create_user(db_session)
    headers = _token_for(user)
    assert (await probe.get("/whoami", headers=headers)).status_code == 200

    await db_session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user.id})
    db_session.expire_all()
    assert (await probe.get("/whoami", headers=headers)).status_code == 401


async def test_valid_token_for_an_inactive_user_is_401(
    probe: AsyncClient, db_session: AsyncSession
) -> None:
    user = await create_user(db_session)
    headers = _token_for(user)
    assert (await probe.get("/whoami", headers=headers)).status_code == 200

    user.is_active = False
    await db_session.flush()
    assert (await probe.get("/whoami", headers=headers)).status_code == 401


async def test_role_claim_in_token_is_ignored(probe: AsyncClient, db_session: AsyncSession) -> None:
    """A properly signed token that *claims* admin must not elevate a customer."""
    customer = await create_user(db_session, role=Role.customer)
    token = forge_token(customer.id, extra={"role": "admin", "roles": ["admin"], "is_admin": True})
    headers = bearer(token)

    me = await probe.get("/whoami", headers=headers)
    assert me.status_code == 200
    assert me.json()["role"] == "customer"
    assert (await probe.get("/admin", headers=headers)).status_code == 403


async def test_role_changes_take_effect_immediately(
    probe: AsyncClient, db_session: AsyncSession
) -> None:
    user = await create_user(db_session, role=Role.customer)
    headers = _token_for(user)  # same token throughout
    assert (await probe.get("/admin", headers=headers)).status_code == 403

    user.role = Role.admin
    await db_session.flush()
    assert (await probe.get("/admin", headers=headers)).status_code == 200

    user.role = Role.customer
    await db_session.flush()
    assert (await probe.get("/admin", headers=headers)).status_code == 403


async def test_require_role_403_for_wrong_role_and_200_for_allowed(
    probe: AsyncClient, db_session: AsyncSession
) -> None:
    customer = await create_user(db_session, role=Role.customer)
    admin = await create_user(db_session, role=Role.admin)

    denied = await probe.get("/admin", headers=_token_for(customer))
    assert denied.status_code == 403
    assert denied.json() == {"detail": "Insufficient permissions"}

    assert (await probe.get("/admin", headers=_token_for(admin))).status_code == 200
    # multiple allowed roles
    assert (await probe.get("/either", headers=_token_for(customer))).status_code == 200
    assert (await probe.get("/either", headers=_token_for(admin))).status_code == 200


async def test_require_role_without_token_is_401_not_403(probe: AsyncClient) -> None:
    assert (await probe.get("/admin")).status_code == 401

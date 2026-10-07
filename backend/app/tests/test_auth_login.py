import pytest
from argon2 import PasswordHasher
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_access_token, verify_password
from app.modules.users import service as users_service
from app.tests.factories import create_user

URL = "/api/v1/auth/login"
PASSWORD = "correct horse battery"


async def _login(client: AsyncClient, email: str, password: str):
    return await client.post(URL, json={"email": email, "password": password})


async def test_good_login_returns_a_working_token(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await create_user(db_session, email="alice@example.com", password=PASSWORD)
    r = await _login(db_client, "alice@example.com", PASSWORD)
    assert r.status_code == 200
    body = r.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 30 * 60
    assert decode_access_token(body["access_token"]) == user.id


async def test_email_is_normalised(db_client: AsyncClient, db_session: AsyncSession) -> None:
    await create_user(db_session, email="alice@example.com", password=PASSWORD)
    r = await _login(db_client, "  ALICE@Example.COM \t", PASSWORD)
    assert r.status_code == 200


async def test_wrong_password_is_401(db_client: AsyncClient, db_session: AsyncSession) -> None:
    await create_user(db_session, email="alice@example.com", password=PASSWORD)
    r = await _login(db_client, "alice@example.com", "wrong")
    assert r.status_code == 401
    assert r.json() == {"detail": "Invalid email or password"}
    assert "access_token" not in r.text


async def test_unknown_email_is_indistinguishable_from_wrong_password(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await create_user(db_session, email="alice@example.com", password=PASSWORD)
    wrong_pw = await _login(db_client, "alice@example.com", "wrong")
    unknown = await _login(db_client, "nobody@example.com", PASSWORD)

    assert unknown.status_code == wrong_pw.status_code == 401
    assert unknown.json() == wrong_pw.json()
    for h in ("content-type", "content-length", "www-authenticate"):
        assert unknown.headers.get(h) == wrong_pw.headers.get(h)


async def test_inactive_user_cannot_log_in_and_looks_like_wrong_password(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await create_user(db_session, email="gone@example.com", password=PASSWORD, is_active=False)
    await create_user(db_session, email="alice@example.com", password=PASSWORD)

    inactive = await _login(db_client, "gone@example.com", PASSWORD)  # correct password!
    wrong_pw = await _login(db_client, "alice@example.com", "wrong")
    assert inactive.status_code == 401
    assert inactive.json() == wrong_pw.json()


async def test_unknown_email_still_runs_a_password_verification(
    db_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Timing equalisation: no user must not mean no argon2 work."""
    calls: list[str] = []
    real = users_service.verify_password

    def spy(password: str, password_hash: str) -> bool:
        calls.append(password_hash)
        return real(password, password_hash)

    monkeypatch.setattr(users_service, "verify_password", spy)
    r = await _login(db_client, "nobody@example.com", PASSWORD)

    assert r.status_code == 401
    assert len(calls) == 1
    assert calls[0].startswith("$argon2id$")


async def test_login_rehashes_outdated_password_hash(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    weak_hash = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash(PASSWORD)
    user = await create_user(db_session, email="old@example.com", password_hash=weak_hash)

    r = await _login(db_client, "old@example.com", PASSWORD)
    assert r.status_code == 200

    await db_session.refresh(user)
    assert user.password_hash != weak_hash
    assert verify_password(PASSWORD, user.password_hash)
    # ...and the upgraded hash keeps working.
    assert (await _login(db_client, "old@example.com", PASSWORD)).status_code == 200


async def test_failed_login_does_not_rehash(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    weak_hash = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash(PASSWORD)
    user = await create_user(db_session, email="old@example.com", password_hash=weak_hash)
    assert (await _login(db_client, "old@example.com", "wrong")).status_code == 401
    await db_session.refresh(user)
    assert user.password_hash == weak_hash


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"email": "a@example.com"},
        {"password": "x"},
        {"email": "", "password": "x"},
        {"email": "a@example.com", "password": ""},
        {"email": "a@example.com", "password": "p" * 1025},
        {"email": "a" * 255, "password": "x"},
    ],
)
async def test_invalid_login_payloads_are_422(db_client: AsyncClient, payload: dict) -> None:
    r = await db_client.post(URL, json=payload)
    assert r.status_code == 422

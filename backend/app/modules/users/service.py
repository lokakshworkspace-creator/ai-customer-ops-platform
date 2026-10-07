from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import (
    dummy_password_hash,
    hash_password,
    password_needs_rehash,
    verify_password,
)
from app.db.models import User
from app.modules.users import repo


async def authenticate(session: AsyncSession, email: str, password: str) -> User | None:
    """Return the user if the credentials are valid and the account is active, else None.

    Callers must not distinguish the failure reasons (unknown email / wrong password /
    inactive account): the caller gets None for all of them. A password verification is
    always performed, against a dummy hash when the user doesn't exist, so the response
    time doesn't reveal which emails are registered.
    """
    user = await repo.get_by_email(session, email)
    password_ok = verify_password(password, user.password_hash if user else dummy_password_hash())

    if user is None or not password_ok or not user.is_active:
        return None

    if password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
        await session.commit()
    return user

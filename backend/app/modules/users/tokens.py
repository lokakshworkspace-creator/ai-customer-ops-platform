"""Token revocation (logout), kept in Postgres. See `app/db/models/revoked_token.py` for why."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import RevokedToken


async def is_revoked(session: AsyncSession, jti: str) -> bool:
    return (
        await session.scalar(select(RevokedToken.jti).where(RevokedToken.jti == jti))
    ) is not None


async def revoke(
    session: AsyncSession, *, jti: str, user_id: uuid.UUID, expires_at: datetime
) -> None:
    """Idempotent: revoking a token twice is not an error. The caller commits."""
    await session.execute(
        insert(RevokedToken)
        .values(jti=jti, user_id=user_id, expires_at=expires_at)
        .on_conflict_do_nothing(index_elements=[RevokedToken.jti])
    )


async def purge_expired(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Delete rows whose token has expired on its own (it is invalid anyway). Returns the count.
    The caller commits."""
    result = await session.execute(
        delete(RevokedToken).where(RevokedToken.expires_at < (now or datetime.now(UTC)))
    )
    return result.rowcount or 0

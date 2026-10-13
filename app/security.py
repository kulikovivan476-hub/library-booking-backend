import hashlib
import secrets
from datetime import timedelta
from pwdlib import PasswordHash
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool
from app.config import settings
from app.models import AuthSession, User, utc_now

password_hasher = PasswordHash.recommended()
_dummy_hash = password_hasher.hash(secrets.token_urlsafe(32))


async def hash_password(password: str) -> str:
    return await run_in_threadpool(password_hasher.hash, password)


async def verify_password(password: str, user: User | None) -> bool:
    valid = await run_in_threadpool(password_hasher.verify, password, user.password_hash if user else _dummy_hash)
    return user is not None and valid


def digest_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def issue_session(db: AsyncSession, user: User) -> dict:
    now = utc_now()
    await db.execute(delete(AuthSession).where(AuthSession.expires_at <= now))
    token = secrets.token_urlsafe(32)
    expires = now + timedelta(hours=settings.session_hours)
    db.add(AuthSession(token_hash=digest_token(token), user_id=user.id, expires_at=expires))
    return {"access_token": token, "expires_at": expires, "user": user}

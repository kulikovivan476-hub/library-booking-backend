from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.models import AuthSession, User, aware, utc_now
from app.security import digest_token

bearer = HTTPBearer(auto_error=False)


async def current_user(credentials: HTTPAuthorizationCredentials | None = Depends(bearer), db: AsyncSession = Depends(get_db)) -> User:
    if credentials is None or len(credentials.credentials) > 200:
        raise HTTPException(401, "Необходимо войти в аккаунт", headers={"WWW-Authenticate": "Bearer"})
    session = await db.get(AuthSession, digest_token(credentials.credentials))
    if session is None or aware(session.expires_at) <= utc_now():
        raise HTTPException(401, "Сессия истекла или недействительна", headers={"WWW-Authenticate": "Bearer"})
    user = await db.get(User, session.user_id)
    if user is None:
        raise HTTPException(401, "Аккаунт не найден")
    return user


async def staff_user(user: User = Depends(current_user)) -> User:
    if user.role not in {"staff", "admin"}:
        raise HTTPException(403, "Требуется доступ сотрудника")
    return user


async def admin_user(user: User = Depends(staff_user)) -> User:
    if user.role != "admin":
        raise HTTPException(403, "Требуется доступ администратора")
    return user


def check_branch(user: User, branch_id: int):
    if user.role == "staff" and user.branch_id != branch_id:
        raise HTTPException(404, "Мероприятие не найдено")

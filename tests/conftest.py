import os
import secrets
from datetime import timedelta
from uuid import uuid4
import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from app import consent
from app.database import Base, get_db, make_engine
from app.main import app
from app.models import AuthSession, Branch, Event, User, utc_now
from app.security import digest_token, hash_password


@pytest.fixture
async def system(tmp_path):
    url = os.environ.get("LIBRARY_TEST_DATABASE_URL")
    cleanup_engine = None
    schema = "test_" + uuid4().hex
    if url:
        if make_url(url).database != "library_booking_test" or not url.startswith("postgresql+asyncpg://"):
            raise RuntimeError("Тесты PostgreSQL разрешены только в отдельной базе library_booking_test")
        cleanup_engine = create_async_engine(url)
        async with cleanup_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    else:
        engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async def override_db():
        async with factory() as session:
            try:
                yield session
            finally:
                await session.rollback()

    app.dependency_overrides[get_db] = override_db
    tokens = {}
    async with factory() as db:
        branch = Branch(name="Первый филиал", address="Первый адрес")
        other = Branch(name="Второй филиал", address="Второй адрес")
        db.add_all([branch, other])
        await db.flush()
        password = await hash_password("OnlyForTests123!")
        for role in ("reader", "reader2", "staff", "admin"):
            user = User(email=f"{role}@example.org", full_name=f"Тест {role}", password_hash=password,
                        role="reader" if role == "reader2" else role,
                        branch_id=branch.id if role == "staff" else None,
                        consent_version=consent.VERSION, consent_text=consent.TEXT)
            db.add(user)
            await db.flush()
            token = secrets.token_urlsafe(32)
            db.add(AuthSession(token_hash=digest_token(token), user_id=user.id, expires_at=utc_now() + timedelta(hours=1)))
            tokens[role] = {"Authorization": "Bearer " + token}
        start = utc_now() + timedelta(days=2)
        fields = dict(title="Читательский клуб", description="Разговор о книгах", category="Обсуждение",
                      starts_at=start, ends_at=start + timedelta(hours=1), registration_opens_at=utc_now() - timedelta(hours=1),
                      registration_closes_at=start, capacity=2, age_rating=16)
        event = Event(branch_id=branch.id, status="published", **fields)
        foreign = Event(branch_id=other.id, status="published", **fields)
        draft = Event(branch_id=branch.id, status="draft", **fields)
        db.add_all([event, foreign, draft])
        await db.commit()
        ids = {"event": event.id, "foreign": foreign.id, "draft": draft.id, "branch": branch.id, "other_branch": other.id}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        try:
            yield client, factory, tokens, ids
        finally:
            app.dependency_overrides.clear()
            await engine.dispose()
            if cleanup_engine:
                async with cleanup_engine.begin() as connection:
                    await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
                await cleanup_engine.dispose()

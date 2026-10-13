from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from app.config import settings
from app.database import engine, get_db
from app.routes import router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Таблицы создаются миграциями, а не автоматически при каждом запуске.
    yield
    await engine.dispose()


app = FastAPI(title="Library Event Booking API", version="1.0.0",
              description="Запись читателей на бесплатные мероприятия библиотеки. Один читатель — одно место.",
              lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=False,
                   allow_methods=["GET", "POST", "PUT", "PATCH"], allow_headers=["Authorization", "Content-Type"])
app.include_router(router)


@app.get("/", tags=["Служебное"])
async def root():
    return {"service": "library-booking-backend", "version": "1.0.0", "docs": "/docs", "timezone": settings.app_timezone}


@app.get("/health", tags=["Служебное"])
async def health(db: AsyncSession = Depends(get_db)):
    try:
        await db.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        raise HTTPException(503, "База данных недоступна") from exc
    return {"status": "ok"}

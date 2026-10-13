import csv
import io
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from app import consent
from app.config import settings
from app.database import get_db
from app.dependencies import admin_user, bearer, check_branch, current_user, staff_user
from app.models import AuthSession, Booking, Branch, Event, User, aware, utc_now
from app.schemas import (AttendanceInput, BookingInput, BookingPage, BookingView, BranchInput, BranchView,
                         EventInput, EventPage, EventView, Login, ParticipantPage, Register, StatusInput, TokenView, UserView)
from app.security import digest_token, hash_password, issue_session, verify_password
from app.services import booking_view, cancel_own_booking, event_view, locked_event, reserve, reserved_count

router = APIRouter(prefix="/api/v1")


@router.get("/consent", tags=["Служебное"], summary="Текст и версия согласия для регистрации")
async def get_consent():
    return {"version": consent.VERSION, "text": consent.TEXT, "demo": True}


@router.post("/auth/register", response_model=TokenView, status_code=201, tags=["Аккаунт"])
async def register(data: Register, db: AsyncSession = Depends(get_db)):
    if not data.privacy_consent:
        raise HTTPException(400, "Необходимо согласие на обработку данных")
    if data.consent_version != consent.VERSION:
        raise HTTPException(409, "Текст согласия обновился. Получите актуальную версию")
    user = User(email=str(data.email).lower(), full_name=data.full_name, phone=data.phone,
                password_hash=await hash_password(data.password.get_secret_value()), role="reader",
                consent_version=consent.VERSION, consent_text=consent.TEXT)
    db.add(user)
    try:
        await db.flush()
        token = await issue_session(db, user)
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(409, "Аккаунт с таким email уже существует") from exc
    return token


@router.post("/auth/login", response_model=TokenView, tags=["Аккаунт"])
async def login(data: Login, db: AsyncSession = Depends(get_db)):
    user = await db.scalar(select(User).where(User.email == str(data.email).lower()))
    if not await verify_password(data.password.get_secret_value(), user):
        raise HTTPException(401, "Неверный email или пароль", headers={"WWW-Authenticate": "Bearer"})
    token = await issue_session(db, user)
    await db.commit()
    return token


@router.post("/auth/logout", status_code=204, tags=["Аккаунт"])
async def logout(user: User = Depends(current_user), credentials: HTTPAuthorizationCredentials = Depends(bearer), db: AsyncSession = Depends(get_db)):
    await db.execute(delete(AuthSession).where(AuthSession.token_hash == digest_token(credentials.credentials)))
    await db.commit()


@router.get("/auth/me", response_model=UserView, tags=["Аккаунт"])
async def me(user: User = Depends(current_user)):
    return user


@router.get("/branches", response_model=list[BranchView], tags=["Афиша"])
async def branches(db: AsyncSession = Depends(get_db)):
    return (await db.scalars(select(Branch).order_by(Branch.name))).all()


@router.get("/categories", response_model=list[str], tags=["Афиша"])
async def categories(db: AsyncSession = Depends(get_db)):
    return (await db.scalars(select(Event.category).where(Event.status != "draft").distinct().order_by(Event.category))).all()


def public_filter(branch_id, category, q, date_from, date_to, available_only, include_past):
    query = select(Event).where(Event.status != "draft")
    now = utc_now()
    if not include_past:
        query = query.where(Event.ends_at > now)
    if branch_id:
        query = query.where(Event.branch_id == branch_id)
    if category:
        query = query.where(Event.category == category)
    if q:
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        query = query.where(or_(Event.title.ilike(f"%{escaped}%", escape="\\"), Event.description.ilike(f"%{escaped}%", escape="\\")))
    zone = ZoneInfo(settings.app_timezone)
    if date_from:
        query = query.where(Event.starts_at >= datetime.combine(date_from, time.min, zone))
    if date_to:
        query = query.where(Event.starts_at < datetime.combine(date_to + timedelta(days=1), time.min, zone))
    if available_only:
        count = select(func.count(Booking.id)).where(Booking.event_id == Event.id, Booking.status == "confirmed").correlate(Event).scalar_subquery()
        query = query.where(Event.status == "published", Event.registration_opens_at <= now,
                            Event.registration_closes_at > now, Event.starts_at > now, count < Event.capacity)
    return query


@router.get("/events", response_model=EventPage, tags=["Афиша"], summary="Афиша: поиск, фильтры и остаток мест")
async def events(branch_id: int | None = Query(None, gt=0), category: str | None = Query(None, max_length=100),
                 q: str | None = Query(None, min_length=1, max_length=200), date_from: date | None = None,
                 date_to: date | None = None, available_only: bool = False, include_past: bool = False,
                 limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0), db: AsyncSession = Depends(get_db)):
    if date_from and date_to and date_to < date_from:
        raise HTTPException(422, "Конец периода раньше начала")
    query = public_filter(branch_id, category, q, date_from, date_to, available_only, include_past)
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    rows = (await db.scalars(query.order_by(Event.starts_at, Event.id).offset(offset).limit(limit))).all()
    return {"items": [await event_view(db, row) for row in rows], "total": total, "limit": limit, "offset": offset}


@router.get("/events/{event_id}", response_model=EventView, tags=["Афиша"])
async def event_detail(event_id: int, db: AsyncSession = Depends(get_db)):
    event = await db.get(Event, event_id)
    if event is None or event.status == "draft":
        raise HTTPException(404, "Мероприятие не найдено")
    return await event_view(db, event)


@router.post("/bookings", response_model=BookingView, status_code=201, tags=["Брони"], summary="Забронировать одно место для текущего пользователя")
async def create_booking(data: BookingInput, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    try:
        booking = await reserve(db, data.event_id, user)
        result = await booking_view(db, booking)
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(409, "У вас уже есть бронь на это мероприятие") from exc
    return result


@router.get("/bookings", response_model=BookingPage, tags=["Брони"], summary="Личный кабинет: предстоящие записи и история")
async def my_bookings(scope: str = Query("all", pattern="^(all|upcoming|history)$"), limit: int = Query(20, ge=1, le=100),
                      offset: int = Query(0, ge=0), user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    query = select(Booking).join(Event).where(Booking.user_id == user.id)
    if scope == "upcoming":
        query = query.where(Booking.status == "confirmed", Event.ends_at > utc_now(), Event.status != "completed")
    if scope == "history":
        query = query.where(or_(Booking.status != "confirmed", Event.ends_at <= utc_now(), Event.status == "completed"))
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    rows = (await db.scalars(query.order_by(Booking.created_at.desc(), Booking.id.desc()).offset(offset).limit(limit))).all()
    return {"items": [await booking_view(db, row) for row in rows], "total": total, "limit": limit, "offset": offset}


@router.get("/bookings/{booking_id}", response_model=BookingView, tags=["Брони"])
async def my_booking(booking_id: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    booking = await db.scalar(select(Booking).where(Booking.id == booking_id, Booking.user_id == user.id))
    if booking is None:
        raise HTTPException(404, "Бронь не найдена")
    return await booking_view(db, booking)


@router.post("/bookings/{booking_id}/cancel", response_model=BookingView, tags=["Брони"])
async def cancel_booking(booking_id: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    booking = await cancel_own_booking(db, booking_id, user)
    result = await booking_view(db, booking)
    await db.commit()
    return result


@router.post("/admin/branches", response_model=BranchView, status_code=201, tags=["Сотрудники"])
async def create_branch(data: BranchInput, user: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    branch = Branch(**data.model_dump())
    db.add(branch)
    await db.commit()
    return branch


@router.get("/admin/events", response_model=EventPage, tags=["Сотрудники"], summary="События своего филиала, включая черновики")
async def admin_events(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0), user: User = Depends(staff_user), db: AsyncSession = Depends(get_db)):
    query = select(Event)
    if user.role == "staff":
        query = query.where(Event.branch_id == user.branch_id)
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    rows = (await db.scalars(query.order_by(Event.starts_at.desc(), Event.id.desc()).offset(offset).limit(limit))).all()
    return {"items": [await event_view(db, row) for row in rows], "total": total, "limit": limit, "offset": offset}


def event_fields(data: EventInput):
    fields = data.model_dump()
    fields["poster_url"] = str(data.poster_url) if data.poster_url else None
    return fields


@router.post("/admin/events", response_model=EventView, status_code=201, tags=["Сотрудники"])
async def create_event(data: EventInput, user: User = Depends(staff_user), db: AsyncSession = Depends(get_db)):
    check_branch(user, data.branch_id)
    if await db.get(Branch, data.branch_id) is None:
        raise HTTPException(404, "Филиал не найден")
    if data.starts_at <= utc_now():
        raise HTTPException(422, "Мероприятие должно начинаться в будущем")
    event = Event(**event_fields(data), status="draft")
    db.add(event)
    await db.flush()
    result = await event_view(db, event)
    await db.commit()
    return result


@router.get("/admin/events/{event_id}", response_model=EventView, tags=["Сотрудники"])
async def admin_event_detail(event_id: int, user: User = Depends(staff_user), db: AsyncSession = Depends(get_db)):
    event = await db.get(Event, event_id)
    if event is None:
        raise HTTPException(404, "Мероприятие не найдено")
    check_branch(user, event.branch_id)
    return await event_view(db, event)


@router.put("/admin/events/{event_id}", response_model=EventView, tags=["Сотрудники"], summary="Полностью заменить поля события, сохранив его статус")
async def edit_event(event_id: int, data: EventInput, user: User = Depends(staff_user), db: AsyncSession = Depends(get_db)):
    event = await locked_event(db, event_id, user)
    check_branch(user, data.branch_id)
    if event.status in {"completed", "cancelled"}:
        raise HTTPException(409, "Завершённое или отменённое событие нельзя изменять")
    if await db.get(Branch, data.branch_id) is None:
        raise HTTPException(404, "Филиал не найден")
    if data.starts_at <= utc_now():
        raise HTTPException(422, "Мероприятие должно начинаться в будущем")
    count = await reserved_count(db, event_id)
    if data.capacity < count:
        raise HTTPException(409, "Вместимость меньше числа действующих броней")
    if count and data.branch_id != event.branch_id:
        raise HTTPException(409, "При действующих бронях нельзя менять филиал")
    for key, value in event_fields(data).items():
        setattr(event, key, value)
    await db.flush()
    result = await event_view(db, event, count)
    await db.commit()
    return result


TRANSITIONS = {
    "draft": {"published", "cancelled"}, "published": {"closed", "completed", "cancelled"},
    "closed": {"published", "completed", "cancelled"}, "completed": set(), "cancelled": set(),
}


@router.patch("/admin/events/{event_id}/status", response_model=EventView, tags=["Сотрудники"])
async def event_status(event_id: int, data: StatusInput, user: User = Depends(staff_user), db: AsyncSession = Depends(get_db)):
    event = await locked_event(db, event_id, user)
    if data.status == event.status:
        return await event_view(db, event)
    if data.status not in TRANSITIONS[event.status]:
        raise HTTPException(409, "Недопустимый переход статуса мероприятия")
    now = utc_now()
    if data.status == "published" and (aware(event.starts_at) <= now or aware(event.registration_closes_at) <= now):
        raise HTTPException(409, "Период записи уже закончился")
    if data.status == "completed" and aware(event.ends_at) > now:
        raise HTTPException(409, "Мероприятие ещё не закончилось")
    if data.status == "cancelled":
        if not data.reason:
            raise HTTPException(422, "Укажите причину отмены")
        event.cancellation_reason = data.reason
        await db.execute(update(Booking).where(Booking.event_id == event_id, Booking.status == "confirmed").values(
            status="event_cancelled", cancelled_at=now, cancellation_reason=data.reason))
    event.status = data.status
    await db.flush()
    result = await event_view(db, event)
    await db.commit()
    return result


async def staff_event(db, event_id, user):
    event = await db.get(Event, event_id)
    if event is None:
        raise HTTPException(404, "Мероприятие не найдено")
    check_branch(user, event.branch_id)
    return event


@router.get("/admin/events/{event_id}/participants", response_model=ParticipantPage, tags=["Сотрудники"])
async def participants(event_id: int, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0),
                       user: User = Depends(staff_user), db: AsyncSession = Depends(get_db)):
    await staff_event(db, event_id, user)
    query = select(Booking, User).join(User, User.id == Booking.user_id).where(Booking.event_id == event_id)
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    rows = (await db.execute(query.order_by(Booking.id).offset(offset).limit(limit))).all()
    return {"items": [{"booking_id": b.id, "number": f"БВ-{b.id:04d}", "user": UserView.model_validate(u),
                       "status": b.status, "attended": b.attended} for b, u in rows],
            "total": total, "limit": limit, "offset": offset}


def safe_csv(value) -> str:
    text = "" if value is None else str(value)
    # Excel может воспринимать внешние значения как формулы, даже после пробелов.
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) or text.startswith(("\t", "\r")) else text


@router.get("/admin/events/{event_id}/participants/export", tags=["Сотрудники"], response_class=Response,
            responses={200: {"content": {"text/csv": {}}}})
async def export_participants(event_id: int, user: User = Depends(staff_user), db: AsyncSession = Depends(get_db)):
    await staff_event(db, event_id, user)
    rows = (await db.execute(select(Booking, User).join(User, User.id == Booking.user_id)
                            .where(Booking.event_id == event_id).order_by(Booking.id))).all()
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(["Номер брони", "Имя", "Email", "Телефон", "Статус", "Посещение"])
    for booking, reader in rows:
        writer.writerow([safe_csv(v) for v in [f"БВ-{booking.id:04d}", reader.full_name, reader.email,
                         reader.phone, booking.status, "" if booking.attended is None else ("да" if booking.attended else "нет")]])
    return Response(buffer.getvalue().encode("utf-8-sig"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="event-{event_id}-participants.csv"'})


@router.patch("/admin/events/{event_id}/participants/{booking_id}/attendance", tags=["Сотрудники"])
async def attendance(event_id: int, booking_id: int, data: AttendanceInput, user: User = Depends(staff_user), db: AsyncSession = Depends(get_db)):
    event = await locked_event(db, event_id, user)
    booking = await db.scalar(select(Booking).where(Booking.id == booking_id, Booking.event_id == event_id))
    if booking is None:
        raise HTTPException(404, "Бронь не найдена")
    if booking.status != "confirmed" or event.status == "cancelled":
        raise HTTPException(409, "Нельзя отметить посещение отменённой записи")
    if utc_now() < aware(event.starts_at):
        raise HTTPException(409, "Посещение отмечается после начала мероприятия")
    booking.attended = data.attended
    await db.commit()
    return {"booking_id": booking.id, "attended": booking.attended}


@router.get("/admin/stats", tags=["Сотрудники"])
async def stats(user: User = Depends(staff_user), db: AsyncSession = Depends(get_db)):
    condition = [] if user.role == "admin" else [Event.branch_id == user.branch_id]
    event_count = await db.scalar(select(func.count(Event.id)).where(*condition))
    confirmed = await db.scalar(select(func.count(Booking.id)).join(Event).where(*condition, Booking.status == "confirmed"))
    visited = await db.scalar(select(func.count(Booking.id)).join(Event).where(*condition, Booking.status == "confirmed", Booking.attended.is_(True)))
    return {"events": event_count, "confirmed_bookings": confirmed, "attended": visited}

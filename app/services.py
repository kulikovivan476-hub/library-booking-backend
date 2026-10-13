from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from app.dependencies import check_branch
from app.models import Booking, Branch, Event, User, aware, utc_now
from app.schemas import EventView, BookingView


async def locked_event(db: AsyncSession, event_id: int, user: User | None = None) -> Event:
    # Все операции, меняющие остаток мест, сначала блокируют одну и ту же строку события.
    event = await db.scalar(select(Event).where(Event.id == event_id).with_for_update().execution_options(populate_existing=True))
    if event is None:
        raise HTTPException(404, "Мероприятие не найдено")
    if user:
        check_branch(user, event.branch_id)
    return event


async def reserved_count(db: AsyncSession, event_id: int) -> int:
    return await db.scalar(select(func.count(Booking.id)).where(Booking.event_id == event_id, Booking.status == "confirmed")) or 0


def booking_state(event: Event, count: int) -> str:
    now = utc_now()
    if event.status in {"draft", "cancelled", "completed"}:
        return event.status
    if event.status == "closed" or now >= aware(event.registration_closes_at) or now >= aware(event.starts_at):
        return "closed"
    if now < aware(event.registration_opens_at):
        return "not_open"
    return "full" if count >= event.capacity else "open"


async def event_view(db: AsyncSession, event: Event, count: int | None = None) -> EventView:
    count = await reserved_count(db, event.id) if count is None else count
    return EventView(id=event.id, branch=await db.get(Branch, event.branch_id), title=event.title,
                     description=event.description, program=event.program, category=event.category,
                     age_rating=event.age_rating, poster_url=event.poster_url,
                     starts_at=aware(event.starts_at), ends_at=aware(event.ends_at),
                     registration_opens_at=aware(event.registration_opens_at),
                     registration_closes_at=aware(event.registration_closes_at), capacity=event.capacity,
                     reserved_seats=count, available_seats=max(0, event.capacity - count), status=event.status,
                     booking_state=booking_state(event, count), cancellation_reason=event.cancellation_reason)


async def booking_view(db: AsyncSession, booking: Booking) -> BookingView:
    event = await db.get(Event, booking.event_id)
    return BookingView(id=booking.id, number=f"БВ-{booking.id:04d}", status=booking.status,
                       event=await event_view(db, event), attended=booking.attended,
                       created_at=aware(booking.created_at),
                       cancelled_at=aware(booking.cancelled_at) if booking.cancelled_at else None,
                       cancellation_reason=booking.cancellation_reason,
                       can_cancel=booking.status == "confirmed" and utc_now() < aware(event.starts_at)
                                  and event.status not in {"completed", "cancelled"})


async def reserve(db: AsyncSession, event_id: int, user: User) -> Booking:
    event = await locked_event(db, event_id)
    if event.status == "draft":
        raise HTTPException(404, "Мероприятие не найдено")
    duplicate = await db.scalar(select(Booking.id).where(Booking.event_id == event_id, Booking.user_id == user.id, Booking.status == "confirmed"))
    if duplicate:
        raise HTTPException(409, "У вас уже есть бронь на это мероприятие")
    count = await reserved_count(db, event_id)
    state = booking_state(event, count)
    if state != "open":
        raise HTTPException(409, "Мест больше нет" if state == "full" else "Запись на мероприятие недоступна")
    booking = Booking(event_id=event_id, user_id=user.id, status="confirmed")
    db.add(booking)
    await db.flush()
    return booking


async def cancel_own_booking(db: AsyncSession, booking_id: int, user: User) -> Booking:
    booking = await db.scalar(select(Booking).where(Booking.id == booking_id, Booking.user_id == user.id))
    if booking is None:
        raise HTTPException(404, "Бронь не найдена")
    event = await locked_event(db, booking.event_id)
    await db.refresh(booking)
    if booking.status != "confirmed":
        return booking  # Повторная отмена не освобождает место второй раз.
    if utc_now() >= aware(event.starts_at) or event.status == "completed":
        raise HTTPException(409, "После начала мероприятия отмена недоступна")
    booking.status = "cancelled"
    booking.cancelled_at = utc_now()
    booking.cancellation_reason = "Отменено читателем"
    await db.flush()
    return booking

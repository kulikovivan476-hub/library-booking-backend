import asyncio
import os
from datetime import timedelta
import pytest
from sqlalchemy import select
from app import consent
from app.models import AuthSession, Booking, Event, User, aware, utc_now
from app.schemas import EventInput


def registration(**extra):
    return dict(full_name="Новый читатель", email="new@example.org", password="OnlyForTests123!",
                privacy_consent=True, consent_version=consent.VERSION, **extra)


async def payload(factory, event_id):
    async with factory() as db:
        event = await db.get(Event, event_id)
        return {k: (aware(getattr(event, k)).isoformat() if k.endswith("_at") else getattr(event, k))
                for k in EventInput.model_fields}


async def book(client, headers, event_id):
    response = await client.post("/api/v1/bookings", headers=headers, json={"event_id": event_id})
    assert response.status_code == 201, response.text
    return response.json()


async def test_registration_consent_and_no_role_escalation(system):
    client, factory, _, _ = system
    data = registration()
    for change, expected in [({"privacy_consent": False}, 400), ({"consent_version": "old"}, 409),
                             ({"privacy_consent": "true"}, 422), ({"role": "admin"}, 422),
                             ({"full_name": "   "}, 422), ({"phone": "+7123"}, 422)]:
        assert (await client.post("/api/v1/auth/register", json={**data, **change})).status_code == expected
    response = await client.post("/api/v1/auth/register", json=data)
    assert response.status_code == 201, response.text
    assert response.json()["user"]["role"] == "reader"
    assert "password" not in response.text
    assert (await client.post("/api/v1/auth/register", json={**data, "email": "NEW@example.org"})).status_code == 409
    async with factory() as db:
        user = await db.scalar(select(User).where(User.email == "new@example.org"))
        assert user.password_hash.startswith("$argon2id$")
        assert user.consent_text == consent.TEXT


async def test_login_logout_and_expired_session(system):
    client, factory, headers, _ = system
    assert (await client.post("/api/v1/auth/login", json={"email": "reader@example.org", "password": "wrong"})).status_code == 401
    response = await client.post("/api/v1/auth/login", json={"email": "READER@example.org", "password": "OnlyForTests123!"})
    assert response.status_code == 200
    token = {"Authorization": "Bearer " + response.json()["access_token"]}
    assert (await client.get("/api/v1/auth/me", headers=token)).status_code == 200
    assert (await client.post("/api/v1/auth/logout", headers=token)).status_code == 204
    assert (await client.get("/api/v1/auth/me", headers=token)).status_code == 401
    async with factory() as db:
        sessions = (await db.scalars(select(AuthSession))).all()
        for session in sessions:
            session.expires_at = utc_now() - timedelta(seconds=1)
        await db.commit()
    assert (await client.get("/api/v1/auth/me", headers=headers["reader"])).status_code == 401


async def test_public_filters_and_private_drafts(system):
    client, _, headers, ids = system
    response = await client.get("/api/v1/events")
    assert response.status_code == 200
    assert response.json()["total"] == 2
    assert "example.org" not in response.text and "password" not in response.text
    assert (await client.get(f"/api/v1/events/{ids['draft']}")).status_code == 404
    data = (await client.get("/api/v1/events", params={"branch_id": ids["branch"], "q": "клуб", "available_only": True})).json()
    assert data["total"] == 1 and data["items"][0]["available_seats"] == 2
    assert (await client.get("/api/v1/events", params={"date_from": "2030-01-02", "date_to": "2030-01-01"})).status_code == 422
    assert (await client.get("/api/v1/events", params={"q": "%"})).json()["total"] == 0
    assert (await client.post("/api/v1/bookings", headers=headers["reader"], json={"event_id": ids["draft"]})).status_code == 404


async def test_booking_duplicate_capacity_cancel_and_rebook(system):
    client, _, headers, ids = system
    first = await book(client, headers["reader"], ids["event"])
    assert first["seats"] == 1 and first["event"]["available_seats"] == 1
    assert (await client.post("/api/v1/bookings", headers=headers["reader"], json={"event_id": ids["event"]})).status_code == 409
    await book(client, headers["reader2"], ids["event"])
    assert (await client.post("/api/v1/bookings", headers=headers["admin"], json={"event_id": ids["event"]})).status_code == 409
    for _ in range(2):
        result = await client.post(f"/api/v1/bookings/{first['id']}/cancel", headers=headers["reader"])
        assert result.status_code == 200 and result.json()["status"] == "cancelled"
        assert result.json()["event"]["available_seats"] == 1
    second = await book(client, headers["reader"], ids["event"])
    assert second["id"] != first["id"]
    assert (await client.get("/api/v1/bookings?scope=history", headers=headers["reader"])).json()["total"] == 1
    assert (await client.get("/api/v1/bookings?scope=upcoming", headers=headers["reader"])).json()["total"] == 1


async def test_ownership_and_role_boundaries(system):
    client, _, headers, ids = system
    booking = await book(client, headers["reader"], ids["event"])
    assert (await client.get("/api/v1/bookings")).status_code == 401
    assert (await client.get(f"/api/v1/bookings/{booking['id']}", headers=headers["reader2"])).status_code == 404
    assert (await client.post(f"/api/v1/bookings/{booking['id']}/cancel", headers=headers["reader2"])).status_code == 404
    assert (await client.get("/api/v1/bookings", headers=headers["reader2"])).json()["total"] == 0
    assert (await client.get("/api/v1/admin/events", headers=headers["reader"])).status_code == 403
    for suffix in ("", "/participants", "/participants/export"):
        assert (await client.get(f"/api/v1/admin/events/{ids['foreign']}{suffix}", headers=headers["staff"])).status_code == 404
    events = (await client.get("/api/v1/admin/events", headers=headers["staff"])).json()
    assert events["total"] == 2 and all(e["branch"]["id"] == ids["branch"] for e in events["items"])


@pytest.mark.parametrize("state", ["not_open", "expired", "closed", "started", "cancelled"])
async def test_closed_registration_states(system, state):
    client, factory, headers, ids = system
    async with factory() as db:
        event = await db.get(Event, ids["event"])
        now = utc_now()
        if state == "not_open":
            event.registration_opens_at = now + timedelta(hours=1)
        elif state == "expired":
            event.registration_closes_at = now - timedelta(seconds=1)
        elif state == "started":
            event.starts_at = now - timedelta(minutes=5)
            event.registration_closes_at = event.starts_at
        else:
            event.status = state
        await db.commit()
    assert (await client.post("/api/v1/bookings", headers=headers["reader"], json={"event_id": ids["event"]})).status_code == 409


async def test_event_edits_and_publish_validation(system):
    client, factory, headers, ids = system
    fields = await payload(factory, ids["event"])
    invalid = {**fields, "starts_at": fields["ends_at"]}
    assert (await client.post("/api/v1/admin/events", headers=headers["admin"], json=invalid)).status_code == 422
    assert (await client.post("/api/v1/admin/events", headers=headers["staff"], json={**fields, "branch_id": ids["other_branch"]})).status_code == 404
    created = await client.post("/api/v1/admin/events", headers=headers["staff"], json=fields)
    assert created.status_code == 201 and created.json()["status"] == "draft"
    published = await client.patch(f"/api/v1/admin/events/{created.json()['id']}/status", headers=headers["staff"], json={"status": "published"})
    assert published.status_code == 200
    await book(client, headers["reader"], ids["event"])
    await book(client, headers["reader2"], ids["event"])
    assert (await client.put(f"/api/v1/admin/events/{ids['event']}", headers=headers["staff"], json={**fields, "capacity": 1})).status_code == 409
    assert (await client.put(f"/api/v1/admin/events/{ids['event']}", headers=headers["admin"], json={**fields, "branch_id": ids["other_branch"]})).status_code == 409


async def test_organizer_cancellation_reason_and_terminal_status(system):
    client, _, headers, ids = system
    booking = await book(client, headers["reader"], ids["event"])
    path = f"/api/v1/admin/events/{ids['event']}/status"
    assert (await client.patch(path, headers=headers["staff"], json={"status": "cancelled"})).status_code == 422
    response = await client.patch(path, headers=headers["staff"], json={"status": "cancelled", "reason": "Встреча перенесена"})
    assert response.status_code == 200 and response.json()["available_seats"] == 2
    row = (await client.get(f"/api/v1/bookings/{booking['id']}", headers=headers["reader"])).json()
    assert row["status"] == "event_cancelled" and row["cancellation_reason"] == "Встреча перенесена" and not row["can_cancel"]
    assert (await client.patch(path, headers=headers["staff"], json={"status": "published"})).status_code == 409


async def test_attendance_after_start_without_changing_capacity(system):
    client, factory, headers, ids = system
    booking = await book(client, headers["reader"], ids["event"])
    path = f"/api/v1/admin/events/{ids['event']}/participants/{booking['id']}/attendance"
    assert (await client.patch(path, headers=headers["staff"], json={"attended": True})).status_code == 409
    async with factory() as db:
        event = await db.get(Event, ids["event"])
        event.starts_at = utc_now() - timedelta(minutes=5)
        event.registration_closes_at = event.starts_at
        await db.commit()
    assert (await client.patch(path, headers=headers["staff"], json={"attended": True})).status_code == 200
    assert (await client.post(f"/api/v1/bookings/{booking['id']}/cancel", headers=headers["reader"])).status_code == 409
    row = (await client.get(f"/api/v1/events/{ids['event']}")).json()
    assert row["reserved_seats"] == 1 and row["available_seats"] == 1


async def test_csv_formula_protection_and_staff_stats(system):
    client, factory, headers, ids = system
    await book(client, headers["reader"], ids["event"])
    async with factory() as db:
        user = await db.scalar(select(User).where(User.email == "reader@example.org"))
        user.full_name = "=HYPERLINK(\"https://example.org\")"
        await db.commit()
    response = await client.get(f"/api/v1/admin/events/{ids['event']}/participants/export", headers=headers["staff"])
    assert response.status_code == 200 and response.content.startswith(b"\xef\xbb\xbf")
    assert "'=HYPERLINK" in response.text
    stats = (await client.get("/api/v1/admin/stats", headers=headers["staff"])).json()
    assert stats == {"events": 2, "confirmed_bookings": 1, "attended": 0}


async def test_health_openapi_and_cors(system):
    client, _, _, _ = system
    assert (await client.get("/health")).json() == {"status": "ok"}
    assert (await client.get("/openapi.json")).status_code == 200
    response = await client.options("/api/v1/events", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"})
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


@pytest.mark.postgres
@pytest.mark.skipif(not os.environ.get("LIBRARY_TEST_DATABASE_URL"), reason="Требуется изолированный PostgreSQL")
async def test_simultaneous_last_seat_and_duplicate(system):
    client, factory, headers, ids = system
    async with factory() as db:
        event = await db.get(Event, ids["event"])
        event.capacity = 1
        await db.commit()
    results = await asyncio.gather(*[client.post("/api/v1/bookings", headers=headers[name], json={"event_id": ids["event"]})
                                   for name in ("reader", "reader2", "admin", "staff")])
    assert sorted(r.status_code for r in results) == [201, 409, 409, 409]
    async with factory() as db:
        assert len((await db.scalars(select(Booking).where(Booking.event_id == ids["event"], Booking.status == "confirmed"))).all()) == 1
    duplicates = await asyncio.gather(*[client.post("/api/v1/bookings", headers=headers["reader"], json={"event_id": ids["foreign"]}) for _ in range(3)])
    assert sorted(r.status_code for r in duplicates) == [201, 409, 409]

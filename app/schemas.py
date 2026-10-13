import re
from datetime import datetime, timezone
from typing import Literal
from pydantic import AwareDatetime, BaseModel, ConfigDict, EmailStr, Field, HttpUrl, SecretStr, StrictBool, field_validator, model_validator

EventStatus = Literal["draft", "published", "closed", "completed", "cancelled"]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Register(Input):
    full_name: str = Field(min_length=2, max_length=150)
    email: EmailStr = Field(max_length=254)
    phone: str | None = Field(None, max_length=20)
    password: SecretStr = Field(min_length=10, max_length=128)
    privacy_consent: StrictBool
    consent_version: str = Field(max_length=50)

    @field_validator("phone")
    @classmethod
    def valid_phone(cls, value):
        if value is None:
            return None
        value = re.sub(r"[\s()\-]", "", value)
        if not re.fullmatch(r"\+[1-9]\d{9,14}", value):
            raise ValueError("Укажите телефон в международном формате, например +79990000000")
        return value


class Login(Input):
    email: EmailStr
    password: SecretStr = Field(min_length=1, max_length=128)


class UserView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    email: str
    full_name: str
    phone: str | None
    role: str
    branch_id: int | None


class TokenView(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime
    user: UserView


class BranchInput(Input):
    name: str = Field(min_length=2, max_length=200)
    address: str = Field(min_length=2, max_length=500)


class BranchView(BranchInput):
    model_config = ConfigDict(from_attributes=True)
    id: int


class EventInput(Input):
    branch_id: int = Field(gt=0)
    title: str = Field(min_length=2, max_length=200)
    description: str = Field(min_length=2, max_length=20000)
    program: str = Field("", max_length=20000)
    category: str = Field(min_length=2, max_length=100)
    age_rating: Literal[0, 6, 12, 16, 18] = 0
    poster_url: HttpUrl | None = Field(None, max_length=2000)
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    registration_opens_at: AwareDatetime
    registration_closes_at: AwareDatetime
    capacity: int = Field(ge=1, le=10000)

    @field_validator("starts_at", "ends_at", "registration_opens_at", "registration_closes_at")
    @classmethod
    def utc_datetime(cls, value):
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def valid_periods(self):
        if self.ends_at <= self.starts_at:
            raise ValueError("Окончание должно быть позже начала")
        if not self.registration_opens_at < self.registration_closes_at <= self.starts_at:
            raise ValueError("Запись должна открыться раньше закрытия и закрыться не позже начала события")
        return self


class EventView(BaseModel):
    id: int
    branch: BranchView
    title: str
    description: str
    program: str
    category: str
    age_rating: int
    poster_url: str | None
    starts_at: datetime
    ends_at: datetime
    registration_opens_at: datetime
    registration_closes_at: datetime
    capacity: int
    reserved_seats: int
    available_seats: int
    status: EventStatus
    booking_state: Literal["not_open", "open", "full", "closed", "completed", "cancelled", "draft"]
    cancellation_reason: str | None


class StatusInput(Input):
    status: EventStatus
    reason: str | None = Field(None, min_length=2, max_length=2000)


class BookingInput(Input):
    event_id: int = Field(gt=0)


class BookingView(BaseModel):
    id: int
    number: str
    status: str
    seats: Literal[1] = 1
    event: EventView
    attended: bool | None
    created_at: datetime
    cancelled_at: datetime | None
    cancellation_reason: str | None
    can_cancel: bool


class AttendanceInput(Input):
    attended: StrictBool


class EventPage(BaseModel):
    items: list[EventView]
    total: int
    limit: int
    offset: int


class BookingPage(BaseModel):
    items: list[BookingView]
    total: int
    limit: int
    offset: int


class ParticipantView(BaseModel):
    booking_id: int
    number: str
    user: UserView
    status: str
    attended: bool | None


class ParticipantPage(BaseModel):
    items: list[ParticipantView]
    total: int
    limit: int
    offset: int

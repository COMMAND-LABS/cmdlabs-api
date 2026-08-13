"""
Pydantic models for the contacts router.
"""
from pydantic import BaseModel, ConfigDict

from src.routers.pagination import Page
from typing import Optional, List
from datetime import date, datetime


# ── Contact models ────────────────────────────────────────────────────────────

class CreateContactRequest(BaseModel):
    first_name: str
    middle_name: Optional[str] = None
    last_name: Optional[str] = None
    email: str  # the default (primary) email — "Default email" in the UI
    alt_email_1: Optional[str] = None
    alt_email_2: Optional[str] = None
    phone: Optional[str] = None
    source: Optional[str] = None
    # Social media profile URLs
    linkedin_url: Optional[str] = None
    instagram_url: Optional[str] = None
    youtube_url: Optional[str] = None
    x_url: Optional[str] = None


class UpdateContactRequest(CreateContactRequest):
    """A PATCH-shaped Create: same twelve fields, none of them required.

    Derived rather than restated so the two cannot drift. Adding a column meant
    adding it here as well, by hand, in the right place — and a field present on
    create but missing on update is silently un-editable rather than an error.

    ONLY the required/optional axis may be overridden below. If a field ever
    needs a genuinely different type or constraint on update, that is a sign
    the two requests are not the same shape and this should go back to being
    written out in full.
    """
    first_name: Optional[str] = None
    email: Optional[str] = None


class ContactEventResponse(BaseModel):
    id: int
    contact_id: int
    account_id: int
    event_type: str
    title: str
    description: Optional[str] = None
    occurred_at: datetime
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ContactResponse(BaseModel):
    id: int
    account_id: int
    first_name: str
    middle_name: Optional[str] = None
    last_name: Optional[str] = None
    name: str  # hybrid property: "{first_name} {middle_name} {last_name}".strip()
    email: str
    alt_email_1: Optional[str] = None
    alt_email_2: Optional[str] = None
    phone: Optional[str] = None
    source: Optional[str] = None
    linkedin_url: Optional[str] = None
    instagram_url: Optional[str] = None
    youtube_url: Optional[str] = None
    x_url: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    events: List[ContactEventResponse] = []

    model_config = ConfigDict(from_attributes=True)


class ContactSummaryResponse(BaseModel):
    """Lightweight contact response without events (for list views)."""
    id: int
    account_id: int
    first_name: str
    middle_name: Optional[str] = None
    last_name: Optional[str] = None
    name: str  # hybrid property: "{first_name} {middle_name} {last_name}".strip()
    email: str
    alt_email_1: Optional[str] = None
    alt_email_2: Optional[str] = None
    phone: Optional[str] = None
    source: Optional[str] = None
    linkedin_url: Optional[str] = None
    instagram_url: Optional[str] = None
    youtube_url: Optional[str] = None
    x_url: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ContactListResponse(Page):
    """Paginated envelope for the contacts list."""
    contacts: List[ContactSummaryResponse]


# ── Event models ──────────────────────────────────────────────────────────────

class CreateContactEventRequest(BaseModel):
    event_type: str
    title: str
    description: Optional[str] = None
    occurred_at: Optional[datetime] = None


class UpdateContactEventRequest(BaseModel):
    event_type: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    occurred_at: Optional[datetime] = None


# ── Career Timeline models ────────────────────────────────────────────────────

class CreateCareerTimelineRequest(BaseModel):
    title: str
    description: Optional[str] = None
    start_date: date
    end_date: Optional[date] = None


class UpdateCareerTimelineRequest(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None


class CareerTimelineResponse(BaseModel):
    id: int
    contact_id: int
    account_id: int
    title: str
    description: Optional[str] = None
    start_date: date
    end_date: Optional[date] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

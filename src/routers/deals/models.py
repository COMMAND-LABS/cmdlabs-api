"""
Pydantic models for the deals router.
"""
from pydantic import BaseModel, ConfigDict

from src.routers.pagination import Page
from typing import Optional, List
from datetime import date, datetime


class CreateDealRequest(BaseModel):
    title: str
    description: Optional[str] = None
    amount: Optional[float] = None
    currency: Optional[str] = None  # defaults to "USD" server-side
    stage: Optional[str] = None     # defaults to "lead" server-side
    expected_close_date: Optional[date] = None
    closed_at: Optional[datetime] = None
    # Optional link to a contact. None => account-level deal not yet tied
    # to a person.
    contact_id: Optional[int] = None


class UpdateDealRequest(CreateDealRequest):
    """Same fields as create, none required. See UpdateContactRequest."""
    title: Optional[str] = None


class DealResponse(BaseModel):
    id: int
    account_id: int
    contact_id: Optional[int] = None
    contact_name: Optional[str] = None  # from Deal.contact (eager-loaded)
    title: str
    description: Optional[str] = None
    amount: Optional[float] = None
    currency: str
    stage: str
    expected_close_date: Optional[date] = None
    closed_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class DealListResponse(Page):
    """Paginated envelope for the deals list."""
    deals: List[DealResponse]

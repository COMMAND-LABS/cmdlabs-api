"""
Shared Pydantic models for the billing router.
"""
from pydantic import BaseModel, ConfigDict
from typing import Optional


class CheckoutSessionResponse(BaseModel):
    """A hosted Stripe Checkout session for the client to redirect to."""
    checkout_url: str
    session_id: str


class PortalSessionResponse(BaseModel):
    """A hosted Stripe billing-portal session for the client to redirect to."""
    portal_url: str


class SubscriptionResponse(BaseModel):
    """The authenticated account's subscription state."""
    status: Optional[str] = None
    active: bool
    current_period_end: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)

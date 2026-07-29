"""
Shared Pydantic models for the accounts router.
"""
from pydantic import BaseModel, ConfigDict
from typing import Literal, Optional


AccountRole = Literal['admin', 'premium', 'free']


class AccountResponse(BaseModel):
    """Response model for account data (excludes sensitive fields)."""
    id: int
    email: str
    newsletter_subscribed: bool
    stripe_customer_id: Optional[str] = None
    role: AccountRole
    # Written only by the Stripe webhook. `subscription_active` is the field to
    # gate paid features on — `subscription_status` is for display.
    subscription_status: Optional[str] = None
    subscription_active: bool = False

    model_config = ConfigDict(from_attributes=True)


class UpdateAccountRequest(BaseModel):
    """Request model for updating account fields.

    `role` is deliberately absent: an account holder must not be able to
    escalate their own privileges through this endpoint.
    """
    email: Optional[str] = None
    newsletter_subscribed: Optional[bool] = None

    model_config = ConfigDict(populate_by_name=True)

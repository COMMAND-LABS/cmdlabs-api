"""
Shared Pydantic models for the accounts router.
"""
from pydantic import BaseModel, ConfigDict
from typing import Optional


class AccountResponse(BaseModel):
    """Response model for account data (excludes sensitive fields)."""
    id: int
    email: str
    # Self-reported display name; None until the user provides one.
    name: Optional[str] = None
    newsletter_subscribed: bool
    stripe_customer_id: Optional[str] = None
    # Platform super admins. Replaced a `role` field that also carried
    # premium/free — those were a cache of the subscription, and the two below
    # are the fact.
    is_super_admin: bool = False
    # Written only by the Stripe webhook. `subscription_active` is the field to
    # gate paid features on — `subscription_status` is for display.
    subscription_status: Optional[str] = None
    subscription_active: bool = False

    model_config = ConfigDict(from_attributes=True)


class UpdateAccountRequest(BaseModel):
    """Request model for updating account fields.

    `is_super_admin` is deliberately absent: an account holder must not be able
    to escalate their own privileges through this endpoint. It is granted and
    revoked out of band by scripts/super_admin.py and by no API path at all.
    """
    email: Optional[str] = None
    # Whitespace-only clears the name back to NULL — "optional" includes the
    # way back out, and omitting the field (None) must stay distinct from
    # clearing it.
    name: Optional[str] = None
    newsletter_subscribed: Optional[bool] = None

    model_config = ConfigDict(populate_by_name=True)

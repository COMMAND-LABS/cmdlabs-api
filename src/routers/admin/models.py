"""Response models for the platform-admin surface."""
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel


class OrganizationSummary(BaseModel):
    """One org, as platform staff sees it in the org list.

    Deliberately contains NO tenant data — counts and configuration only.
    Staff administer orgs from here; reading an org's contacts still requires
    joining it.
    """
    id: int
    # None for a personal workspace, which has no public page.
    name: str
    # True when the org has exactly one member — a workspace, not a team.
    is_personal: bool
    # 'active' | 'grace' | 'lapsed' — the owner's billing, derived per request.
    billing_state: str
    # Null means "follows the owner's subscription". Set means staff pinned a
    # plan and no webhook may change it — the comp.
    pinned_plan: str | None
    owner_account_id: Optional[int] = None
    owner_email: Optional[str] = None
    member_count: int
    tier_count: int
    # What the plan in force opens. Derived, never stored.
    modules: List[str]
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class OrganizationListResponse(BaseModel):
    organizations: List[OrganizationSummary]
    total: int

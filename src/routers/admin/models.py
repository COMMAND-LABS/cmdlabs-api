"""Response models for the platform-admin surface."""
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel


class OrganizationSummary(BaseModel):
    """One org, as a platform super admin sees it in the org list.

    Deliberately contains NO tenant data — counts and configuration only.
    Super admins administer orgs from here; reading an org's contacts still
    requires joining it.
    """
    id: int
    # None for a personal workspace, which has no public page.
    name: str
    # True when the org has exactly one member — a workspace, not a team.
    is_personal: bool
    # 'active' | 'grace' | 'lapsed' — the owner's billing, derived per request.
    billing_state: str
    # Null means "follows the owner's subscription". Set means super admins
    # pinned a plan and no webhook may change it — the comp.
    pinned_plan: str | None
    owner_account_id: Optional[int] = None
    owner_email: Optional[str] = None
    member_count: int
    # What the plan in force opens. Derived, never stored.
    modules: List[str]
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class OrganizationListResponse(BaseModel):
    organizations: List[OrganizationSummary]
    total: int


class LeadMagnetStatRow(BaseModel):
    """Per-magnet signup counts. Aggregates only — no emails — for the same
    reason OrganizationSummary carries no tenant data."""
    slug: str
    # None when the slug has rows but is no longer in the registry.
    title: Optional[str] = None
    signups: int
    unique_emails: int
    first_signup_at: Optional[datetime] = None
    last_signup_at: Optional[datetime] = None


class LeadMagnetStatsResponse(BaseModel):
    since: Optional[datetime] = None
    until: Optional[datetime] = None
    rows: List[LeadMagnetStatRow]

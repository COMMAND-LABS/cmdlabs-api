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
    slug: Optional[str] = None
    name: str
    # True for a personal workspace: one member, who owns it, no public page.
    is_personal: bool
    status: str                # 'active' | 'read_only'
    owner_account_id: Optional[int] = None
    owner_email: Optional[str] = None
    member_count: int
    tier_count: int
    granted_modules: List[str]
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class OrganizationListResponse(BaseModel):
    organizations: List[OrganizationSummary]
    total: int

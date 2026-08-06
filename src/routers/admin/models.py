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
    # True when the org has no public page (no slug). Kept because the row
    # renders the slug, NOT as a stand-in for "solo" — member_count says that.
    is_personal: bool
    status: str                # 'active' | 'read_only'
    # Who owns granted_modules. 'grant' means staff set the ceiling by hand and
    # no webhook may undo it — i.e. this org is comped. Surfaced here because it
    # is the one fact about an org that appears nowhere else, and "who is on a
    # free ride?" is a question the list should answer at a glance.
    ceiling_managed_by: str    # 'subscription' | 'grant'
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

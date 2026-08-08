"""
What the caller may open, in their active organization.

The UI hydrates its side menu from this instead of the hardcoded lists in
cmdlabs-ui/src/config/roles.ts, so a ceiling or tier change takes effect on the
next page load with no deploy.
"""
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.deps import db_dependency, org_dependency
from src.rate_limit import limiter
from src.services import modules, organizations
from src.utils.errors import handle_db_error

router = APIRouter()


class EntitlementsResponse(BaseModel):
    org_id: int
    tier_key: str
    is_owner: bool
    is_super_admin: bool
    # True when this org has exactly one member — a workspace, not a team.
    is_personal: bool
    # True during the grace window after the owner's subscription lapsed:
    # everything still opens, nothing may be changed. The banner that explains
    # it is global, so every member of a lapsed org sees WHY a save fails
    # rather than only the owner.
    read_only: bool
    # When read-only becomes a downgrade to the free plan. ISO, or null.
    grace_ends_at: Optional[datetime] = None
    # The plan THIS ORG has: 'free' | 'premium'. Distinct from `modules` on
    # purpose — modules say what opens, the plan says what was bought, and the
    # course catalog needs the second to show somebody what they do not have
    # yet.
    #
    # The ORG's, not the viewer's: an account invited into a paid org is
    # covered by it, the same way the ceiling above already covers them. What
    # the viewer bought THEMSELVES is a different question, answered by
    # /api/billing/subscription — and only the Membership screen should be
    # asking it, because only that screen offers to change it.
    plan: str
    modules: List[str]
    # The org's whole ceiling, so an owner's admin UI can show what exists but
    # is not enabled. None for non-owners, who have no use for it.
    ceiling: Optional[List[str]] = None


@router.get("/me/entitlements", response_model=EntitlementsResponse)
@limiter.limit("120/minute")
async def my_entitlements(db: db_dependency, org: org_dependency, request: Request):
    try:
        return EntitlementsResponse(
            org_id=org.org_id,
            tier_key=org.tier_key,
            is_owner=org.is_owner,
            is_super_admin=org.is_super_admin,
            is_personal=organizations.is_solo(db, org.org_id),
            read_only=org.read_only,
            grace_ends_at=org.grace_ends_at,
            plan=org.plan,
            modules=modules.effective_modules(db, org),
            ceiling=(modules.ceiling_for(db, org.org_id)
                     if (org.is_owner or org.is_super_admin) else None),
        )
    except HTTPException:
        raise
    except Exception as e:
        raise handle_db_error(e, "[ENTITLEMENTS]")

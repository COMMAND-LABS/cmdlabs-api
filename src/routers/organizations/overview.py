"""
The owner's view of their own organization, on one page.

WHY THIS EXISTS
---------------
Platform super admins have had /api/admin/organizations/{id} — every org, in
full — since orgs shipped. An org's OWNER had no equivalent for their own org:
the answer to "what is the state of my organization?" was spread across the
members list, the tiers matrix, the membership page and the audit log, and
nowhere did those four add up to one view. This is that view.

A READ MODEL, AND NOTHING ELSE
------------------------------
Every field here is already readable through an endpoint the owner can call.
Composing them adds a page, not a permission — so there is no new way to change
anything, and a bug in this file can at worst show an owner their own org's
configuration in the wrong shape. Editing still happens where it happened
before, which is why the UI deep-links out rather than growing forms.

Owner only, 404 to everyone else, matching tiers.py: a member who cannot
administer the org should not have its admin surface confirm it exists.
Platform super admins are not owners here and are not admitted either — they
administer an org by setting its ceiling, not by reading the owner's console.
"""
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import func

from src.config.modules_registry import BY_KEY, MODULE_KEYS
from src.db.models import (
    Account,
    Organization,
    OrganizationMember,
    OrganizationTier,
)
from src.db.space_models import SPACE_ACTIVE, Space, SpaceMember
from src.deps import db_dependency, org_dependency
from src.rate_limit import limiter
from src.services import modules
from src.utils.errors import handle_db_error

router = APIRouter()

# How many recent joins the page shows. Enough to answer "did that invite land?"
# without turning the overview into a second, worse members table.
RECENT_MEMBER_LIMIT = 5


class ModuleSummary(BaseModel):
    key: str
    label: str


class RecentMember(BaseModel):
    account_id: int
    email: str
    tier_key: str
    is_owner: bool
    created_at: Optional[datetime] = None


class TierSummary(BaseModel):
    tier_key: str
    label: str
    module_count: int
    member_count: int


class OwnedSpace(BaseModel):
    id: int
    name: str
    member_count: int
    discoverable: bool


class OrganizationOverviewResponse(BaseModel):
    # --- Identity -------------------------------------------------------
    org_id: int
    name: str
    is_personal: bool
    created_at: Optional[datetime] = None

    # --- Plan -----------------------------------------------------------
    # True during the grace window after a lapse. Derived, never stored.
    read_only: bool
    grace_ends_at: Optional[datetime] = None
    # Null means "follows your subscription"; set means super admins pinned
    # this plan and billing cannot change it.
    pinned_plan: Optional[str] = None
    # The plan in force right now: 'free' | 'premium'.
    plan: str
    ceiling: List[ModuleSummary]
    # Denominator for "12 of 19 modules". Sent rather than hardcoded in the UI
    # so adding a module to the registry does not need a front-end deploy.
    module_total: int

    # --- People ---------------------------------------------------------
    member_count: int
    owner_count: int
    recent_members: List[RecentMember]

    # --- Access ---------------------------------------------------------
    tiers: List[TierSummary]

    # --- Spaces billed to this org --------------------------------------
    #
    # THE ONE PLACE owner_org_id is read, and it is read for exactly what the
    # column is for: accountability. "Which spaces is this organization
    # answerable for and paying for?" is the owner's question and it has had no
    # home until now.
    #
    # It is NOT an access list. Nobody reaches a space's content because of
    # anything on this page — that is SpaceMember's job, and the org owner
    # appears here whether or not they are a member of the spaces listed.
    owned_spaces: List[OwnedSpace]


def _require_owner(org):
    """Only an owner sees their org's console.

    404 rather than 403, the same choice tiers.py, members.py and
    require_super_admin all make: the surface does not confirm its own
    existence to somebody who cannot use it.
    """
    if not org.is_owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Not found")


@router.get("/me/overview", response_model=OrganizationOverviewResponse)
@limiter.limit("60/minute")
async def my_organization(db: db_dependency, org: org_dependency, request: Request):
    """Identity, plan, people and access for the caller's active org."""
    try:
        _require_owner(org)

        organization = (db.query(Organization)
                          .filter(Organization.id == org.org_id).one())

        entitlement = modules.org_entitlement(db, org.org_id)
        ceiling = entitlement.ceiling

        # Counted in the database rather than by loading rows: an org with a
        # thousand members should still render this page in one small query.
        member_count = (db.query(func.count(OrganizationMember.id))
                          .filter(OrganizationMember.org_id == org.org_id)
                          .scalar()) or 0
        # An org names one owner, so this is 1 when that account is actually a
        # member and 0 when it is not — which is the state worth surfacing,
        # because an owner outside their own org cannot administer it.
        owner_id = (db.query(Organization.owner_account_id)
                      .filter(Organization.id == org.org_id).scalar())
        owner_count = (db.query(func.count(OrganizationMember.id))
                         .filter(OrganizationMember.org_id == org.org_id,
                                 OrganizationMember.account_id == owner_id)
                         .scalar()) or 0
        recent = (
            db.query(OrganizationMember, Account)
            .join(Account, Account.id == OrganizationMember.account_id)
            .filter(OrganizationMember.org_id == org.org_id)
            .order_by(OrganizationMember.created_at.desc(),
                      OrganizationMember.id.desc())
            .limit(RECENT_MEMBER_LIMIT)
            .all()
        )

        per_tier = dict(
            db.query(OrganizationMember.tier_key,
                     func.count(OrganizationMember.id))
              .filter(OrganizationMember.org_id == org.org_id)
              .group_by(OrganizationMember.tier_key)
              .all()
        )
        tier_rows = (db.query(OrganizationTier)
                       .filter(OrganizationTier.org_id == org.org_id)
                       .order_by(OrganizationTier.id.asc()).all())

        space_rows = (db.query(Space)
                        .filter(Space.owner_org_id == org.org_id,
                                Space.status == SPACE_ACTIVE)
                        .order_by(Space.name.asc()).all())
        space_members = dict(
            db.query(SpaceMember.space_id, func.count(SpaceMember.id))
              .filter(SpaceMember.space_id.in_([s.id for s in space_rows] or [0]))
              .group_by(SpaceMember.space_id).all()
        )

        return OrganizationOverviewResponse(
            org_id=organization.id,
            name=organization.name,
            is_personal=(member_count == 1),
            created_at=organization.created_at,
            read_only=entitlement.read_only,
            grace_ends_at=entitlement.grace_ends_at,
            pinned_plan=organization.pinned_plan,
            plan=entitlement.plan,
            # Labelled here because the keys are stable identifiers, not
            # display names — the UI must never render a raw module key.
            ceiling=[
                ModuleSummary(key=k, label=BY_KEY[k].label)
                for k in ceiling if k in BY_KEY
            ],
            module_total=len(MODULE_KEYS),
            member_count=member_count,
            owner_count=owner_count,
            recent_members=[
                RecentMember(
                    account_id=m.account_id, email=a.email,
                    tier_key=m.tier_key, is_owner=(m.account_id == owner_id),
                    created_at=m.created_at,
                )
                for m, a in recent
            ],
            tiers=[
                TierSummary(
                    tier_key=t.tier_key, label=t.label,
                    module_count=len(t.modules or []),
                    member_count=per_tier.get(t.tier_key, 0),
                )
                for t in tier_rows
            ],
            owned_spaces=[
                OwnedSpace(id=s.id, name=s.name,
                           member_count=space_members.get(s.id, 0),
                           discoverable=s.discoverable)
                for s in space_rows
            ],
        )
    except HTTPException:
        raise
    except Exception as e:
        raise handle_db_error(e, "[ORG OVERVIEW]")

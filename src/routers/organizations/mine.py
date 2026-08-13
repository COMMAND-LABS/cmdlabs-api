"""
Which organizations the caller belongs to, and what they are in each.

Two readers, one answer:
  the SWITCHER      picks which org the dashboard acts in
  account SETTINGS  shows the caller their standing across all of them

Returns only orgs the caller is actually a member of, resolved fresh on every
call. That is deliberate: the switcher is the one UI that names orgs by id, and
if it could list an org the caller has been removed from, they would pick it
and get a 403 they could not explain.

NOT ORG-SCOPED, AND THAT IS THE THING TO BE CAREFUL ABOUT
---------------------------------------------------------
Every other read in this codebase passes through org_scope.tenant_predicate and
gets `org_id == ctx.org_id` as a backstop. This one cannot: answering "which
orgs am I in" across orgs is the entire point, so the filter on
`OrganizationMember.account_id` IS the whole boundary here, with nothing behind
it.

So the rule for anything added below: it must be a fact about THE CALLER'S OWN
MEMBERSHIP, never a fact about the org. `role` qualifies — it is the caller's
own row, and strictly less than /me/entitlements already tells them about the
active org. The org's MEMBER LIST does not: that is served by
organizations/members.py, which requires naming the org you are asking about.

Carries no tenant data — no contact, deal, or document, and no row belonging to
another member.
"""
from typing import List, Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel

from sqlalchemy import func

from src.config import roles_registry as roles
from src.db.models import Organization, OrganizationMember
from src.deps import db_dependency, org_dependency
from src.rate_limit import limiter

router = APIRouter()


class MyOrganization(BaseModel):
    id: int
    name: str
    is_owner: bool
    is_personal: bool
    is_active: bool
    # The caller's OWN role in this org. Their membership row, nobody else's.
    #
    # NOW COMPARABLE ACROSS ORGS, which it deliberately was not before. This
    # was `tier_key`, a string scoped to one org — 'member' here and 'member'
    # there were unrelated bundles opening different modules, so the UI had to
    # avoid presenting them as a comparable column. Roles are platform-wide
    # constants, so 'manager' means the same thing everywhere and a column of
    # them is honest.
    role: str
    # Display name for the role. Resolved from a constant rather than a row.
    role_label: str


class MyOrganizationsResponse(BaseModel):
    active_org_id: int
    organizations: List[MyOrganization]


@router.get("/mine", response_model=MyOrganizationsResponse)
@limiter.limit("120/minute")
async def my_organizations(db: db_dependency, org: org_dependency, request: Request):
    rows = (
        db.query(Organization, OrganizationMember)
        .join(OrganizationMember,
              OrganizationMember.org_id == Organization.id)
        .filter(OrganizationMember.account_id == org.account_id)
        .order_by(Organization.name.asc())
        .all()
    )
    # One count query for the whole list rather than a property per row —
    # `is_personal` is now "has exactly one member", which is a count.
    member_counts = dict(
        db.query(OrganizationMember.org_id,
                 func.count(OrganizationMember.id))
          .filter(OrganizationMember.org_id.in_([o.id for o, _ in rows] or [0]))
          .group_by(OrganizationMember.org_id).all()
    )

    # No lookup for the label any more. It used to be a query against
    # organization_tiers, carefully filtered to the caller's own (org, tier)
    # pairs so the org's other tiers never entered the process. Roles are
    # constants, so there is nothing to fetch and nothing to over-fetch.

    return MyOrganizationsResponse(
        active_org_id=org.org_id,
        organizations=[
            MyOrganization(
                id=o.id,
                name=o.name,
                # From the org's own column. The Organization is already
                # joined for the name, so this needs no extra query.
                is_owner=(o.owner_account_id == org.account_id),
                is_personal=(member_counts.get(o.id, 0) == 1),
                is_active=(o.id == org.org_id),
                # From the membership row this query was already joining
                # and discarding.
                role=m.role,
                role_label=roles.label(m.role),
            )
            for o, m in rows
        ],
    )

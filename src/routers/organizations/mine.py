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
MEMBERSHIP, never a fact about the org. `tier_key` and its label qualify — they
are the caller's own row, and strictly less than /me/entitlements already tells
them about the active org. The org's OTHER tiers, its member list, and
OrganizationTier.modules do not: that is the owner's matrix, served by
organizations/overview.py behind _require_owner and 404 to everyone else.

Carries no tenant data — no contact, deal, or document, and no row belonging to
another member.
"""
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from sqlalchemy import func

from src.db.models import Organization, OrganizationMember, OrganizationTier
from src.deps import db_dependency, org_dependency
from src.rate_limit import limiter
from src.utils.errors import handle_db_error

router = APIRouter()


class MyOrganization(BaseModel):
    id: int
    name: str
    is_owner: bool
    is_personal: bool
    is_active: bool
    # The caller's OWN tier in this org. Their membership row, nobody else's.
    #
    # ORG-LOCAL, AND THE UI MUST NOT INVITE COMPARISON. A tier_key is a string
    # scoped to one org (uq_org_tier_key) — 'member' in one org and 'member' in
    # another are unrelated bundles that open different modules. Rendering a
    # column of raw keys side by side would quietly suggest otherwise.
    # Ownership is the field that IS comparable across orgs, because
    # owner_account_id means the same thing everywhere.
    tier_key: str
    # The owner's display name for that tier. None when the membership names a
    # tier with no row in organization_tiers — possible, since tier_key is a
    # plain string rather than an FK, and the reason this is Optional rather
    # than falling back to the raw key: a key is an identifier, and showing one
    # where a label belongs is how internal vocabulary leaks into the product.
    tier_label: Optional[str] = None


class MyOrganizationsResponse(BaseModel):
    active_org_id: int
    organizations: List[MyOrganization]


@router.get("/mine", response_model=MyOrganizationsResponse)
@limiter.limit("120/minute")
async def my_organizations(db: db_dependency, org: org_dependency, request: Request):
    try:
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

        # Labels for the tiers the caller actually holds — keyed by
        # (org_id, tier_key), which is what uq_org_tier_key makes unique.
        #
        # Filtered to the caller's OWN (org, tier) pairs rather than fetching
        # each org's tier table and picking from it. Same rendered output, but
        # the org's other tiers never enter the process, so this cannot grow
        # into a leak of the owner's matrix by someone later reusing the dict.
        held = {(m.org_id, m.tier_key) for _, m in rows}
        tier_labels: dict = {}
        if held:
            tier_labels = {
                (t.org_id, t.tier_key): t.label
                for t in db.query(OrganizationTier).filter(
                    OrganizationTier.org_id.in_({oid for oid, _ in held}),
                    OrganizationTier.tier_key.in_({key for _, key in held}),
                ).all()
                if (t.org_id, t.tier_key) in held
            }

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
                    tier_key=m.tier_key,
                    tier_label=tier_labels.get((o.id, m.tier_key)),
                )
                for o, m in rows
            ],
        )
    except HTTPException:
        raise
    except Exception as e:
        raise handle_db_error(e, "[MY ORGS]")

"""
Platform-admin browsers: an org's members, and its access groups.

ADMINISTER IS NOT READ, AND THIS FILE IS WHERE THAT LINE SITS
------------------------------------------------------------
Staff can see WHO is in an org and WHICH groups exist with whom in them,
without joining. That is access metadata — the answer to "who can reach our
data?" — and staff need it to support a customer, audit a complaint, or work
out why somebody cannot open a screen.

Staff cannot see the org's DATA from here: not a contact, not a deal, not the
contents of a knowledge base. Reading those still means joining the org, which
writes a staff.join audit event and puts staff in the customer's own member
list. That is what makes the sentence checkable rather than promised: "our
staff cannot read your data without appearing in your member list."

So the rule for anything added to this file: names, counts, roles and
timestamps are fine; a row from a tenant table is not. The response models
below contain no tenant data, and
tests/test_admin_organizations.test_response_carries_no_tenant_data is what
keeps that true.
"""
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import func as sa_func
from pydantic import BaseModel

from src.db.models import (
    AccessGroup,
    AccessGroupMember,
    Account,
    Organization,
    OrganizationMember,
    OrganizationTier,
)
from src.deps import db_dependency, super_admin_dependency
from src.rate_limit import limiter
from src.utils.errors import handle_db_error

router = APIRouter()


class AdminMember(BaseModel):
    account_id: int
    email: str
    tier_key: str
    is_owner: bool
    granted_by: str
    # Which modules this member actually resolves to — ceiling ∩ tier, with the
    # owner bypass applied. The single most asked support question is "why
    # can't they see X", and answering it from a tier name alone requires
    # re-deriving the intersection by hand.
    effective_modules: List[str]
    created_at: Optional[datetime] = None


class AdminGroupMember(BaseModel):
    account_id: int
    email: str
    role: str
    # False when someone is in a group but no longer in the org that owns it.
    # A stale group row grants nothing (grants are org-confined) but it reads
    # like access, so it is worth surfacing rather than hiding.
    in_org: bool


class AdminGroup(BaseModel):
    id: int
    name: str
    owner_account_id: Optional[int] = None
    owner_email: Optional[str] = None
    member_count: int
    members: List[AdminGroupMember]


class AdminTier(BaseModel):
    tier_key: str
    label: str
    modules: List[str]
    member_count: int


class OrganizationDetailResponse(BaseModel):
    id: int
    slug: Optional[str] = None
    name: str
    is_personal: bool
    status: str
    granted_modules: List[str]
    ceiling_managed_by: str
    owner_account_id: Optional[int] = None
    created_at: Optional[datetime] = None
    members: List[AdminMember]
    tiers: List[AdminTier]
    groups: List[AdminGroup]


def _org_or_404(db, org_id: int) -> Organization:
    org = db.query(Organization).filter(Organization.id == org_id).first()
    if not org:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Organization not found")
    return org


@router.get("/organizations/{org_id}", response_model=OrganizationDetailResponse)
@limiter.limit("60/minute")
async def organization_detail(
    org_id: int, db: db_dependency, staff: super_admin_dependency, request: Request,
):
    """One org: its members, its tiers, and its access groups.

    Assembled in one response rather than three endpoints because the question
    staff actually have is "what does this org look like", and answering it
    across three round trips invites a UI that shows two of them and forgets
    the third.
    """
    try:
        org = _org_or_404(db, org_id)
        ceiling = list(org.granted_modules or [])

        tier_modules = {
            t.tier_key: list(t.modules or [])
            for t in db.query(OrganizationTier).filter(
                OrganizationTier.org_id == org_id).all()
        }

        member_rows = (
            db.query(OrganizationMember, Account)
            .join(Account, Account.id == OrganizationMember.account_id)
            .filter(OrganizationMember.org_id == org_id)
            .order_by(OrganizationMember.is_owner.desc(), Account.email.asc())
            .all()
        )

        def _effective(member) -> List[str]:
            # Mirrors services.modules.effective_modules. Recomputed here from
            # already-loaded rows rather than called per member, which would be
            # two queries each; the intersection itself is the same expression.
            if member.is_owner:
                return ceiling
            granted = set(tier_modules.get(member.tier_key, []))
            return [k for k in ceiling if k in granted]

        members = [
            AdminMember(
                account_id=m.account_id, email=a.email, tier_key=m.tier_key,
                is_owner=m.is_owner, granted_by=m.granted_by,
                effective_modules=_effective(m), created_at=m.created_at,
            )
            for m, a in member_rows
        ]
        member_account_ids = {m.account_id for m, _ in member_rows}

        tier_counts: dict = {}
        for m, _ in member_rows:
            tier_counts[m.tier_key] = tier_counts.get(m.tier_key, 0) + 1
        tiers = [
            AdminTier(tier_key=t.tier_key, label=t.label,
                      modules=list(t.modules or []),
                      member_count=tier_counts.get(t.tier_key, 0))
            for t in db.query(OrganizationTier).filter(
                OrganizationTier.org_id == org_id
            ).order_by(OrganizationTier.id.asc()).all()
        ]

        groups = []
        group_rows = (db.query(AccessGroup)
                        .filter(AccessGroup.org_id == org_id)
                        .order_by(AccessGroup.name.asc()).all())
        for g in group_rows:
            gm = (
                db.query(AccessGroupMember, Account)
                .join(Account, Account.id == AccessGroupMember.account_id)
                .filter(AccessGroupMember.access_group_id == g.id)
                .order_by(Account.email.asc())
                .all()
            )
            owner_email = (db.query(Account.email)
                             .filter(Account.id == g.owner_account_id).scalar())
            groups.append(AdminGroup(
                id=g.id, name=g.name, owner_account_id=g.owner_account_id,
                owner_email=owner_email, member_count=len(gm),
                members=[
                    AdminGroupMember(
                        account_id=agm.account_id, email=acct.email,
                        role=agm.role,
                        in_org=(agm.account_id in member_account_ids),
                    )
                    for agm, acct in gm
                ],
            ))

        return OrganizationDetailResponse(
            id=org.id, slug=org.slug, name=org.name,
            is_personal=org.is_personal, status=org.status,
            granted_modules=ceiling,
            ceiling_managed_by=org.ceiling_managed_by,
            owner_account_id=org.owner_account_id, created_at=org.created_at,
            members=members, tiers=tiers, groups=groups,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise handle_db_error(e, "[ADMIN ORG DETAIL]")


class GroupSearchItem(BaseModel):
    id: int
    name: str
    org_id: Optional[int] = None
    org_name: Optional[str] = None
    org_slug: Optional[str] = None
    member_count: int


@router.get("/groups", response_model=List[GroupSearchItem])
@limiter.limit("60/minute")
async def all_groups(
    db: db_dependency, staff: super_admin_dependency, request: Request,
    q: Optional[str] = None, limit: int = 200,
):
    """Every access group on the platform, across all orgs.

    The cross-org view exists for the question the per-org page cannot answer:
    "which org is this group in?" — asked when somebody reports access they
    should not have and only knows the group's name.

    A group with org_id NULL is listed too, and deliberately so. It predates
    org scoping, grants nothing (assert_same_org treats an unclassified org as
    unusable), and is exactly the kind of leftover that should be visible
    rather than filtered out of the one screen built to find it.
    """
    try:
        limit = max(1, min(limit, 500))
        query = (
            db.query(AccessGroup, Organization)
            .outerjoin(Organization, Organization.id == AccessGroup.org_id)
        )
        if q:
            query = query.filter(AccessGroup.name.ilike(f"%{q.strip()}%"))
        rows = query.order_by(AccessGroup.name.asc()).limit(limit).all()

        counts = dict(
            db.query(AccessGroupMember.access_group_id,
                     sa_func.count(AccessGroupMember.id))
            .group_by(AccessGroupMember.access_group_id).all()
        )
        return [
            GroupSearchItem(
                id=g.id, name=g.name,
                org_id=o.id if o else None,
                org_name=o.name if o else None,
                org_slug=o.slug if o else None,
                member_count=counts.get(g.id, 0),
            )
            for g, o in rows
        ]
    except HTTPException:
        raise
    except Exception as e:
        raise handle_db_error(e, "[ADMIN GROUPS]")

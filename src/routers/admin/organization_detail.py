"""
Platform-admin browsers: an org's members, and the spaces it is answerable for.

ADMINISTER IS NOT READ, AND THIS FILE IS WHERE THAT LINE SITS
------------------------------------------------------------
Super admins can see WHO is in an org and WHICH spaces it owns, without
joining. That is access metadata — the answer to "who can reach our data?" —
and super admins need it to support a customer, audit a complaint, or work out
why somebody cannot open a screen.

Super admins cannot see the org's DATA from here: not a contact, not a deal,
not the contents of a knowledge base. Reading those still means joining the
org, which writes a super_admin.join audit event and puts super admins in the
customer's own member list. That is what makes the sentence checkable rather
than promised: "our super admins cannot read your data without appearing in
your member list."

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
    Account,
    Organization,
    OrganizationMember,
    OrganizationTier,
)
from src.db.space_models import Space, SpaceMember
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


class AdminSpaceMember(BaseModel):
    account_id: int
    email: str
    tier_key: str
    is_owner: bool
    # False when this member is not in the org that owns the space. NOT an
    # anomaly — it is the normal case and the reason spaces exist. Surfaced so
    # super admins reading "who can reach this content" can see at a glance
    # that the answer deliberately runs past the org's own member list.
    in_org: bool


class AdminSpace(BaseModel):
    """A space this ORG is accountable for. Attribution, never access.

    Being the owner org grants nobody anything: a space's content is reached by
    SpaceMember rows and by nothing else. What this list answers is "what is
    this customer publishing, and who is it reaching" — which is exactly the
    question support gets when somebody outside the org reports seeing content.
    """
    id: int
    name: str
    discoverable: bool
    join_policy: str
    owner_account_id: Optional[int] = None
    owner_email: Optional[str] = None
    member_count: int
    members: List[AdminSpaceMember]


class AdminTier(BaseModel):
    tier_key: str
    label: str
    modules: List[str]
    member_count: int


class OrganizationDetailResponse(BaseModel):
    id: int
    name: str
    is_personal: bool
    # 'active' | 'grace' | 'lapsed'. Derived from the owner's subscription.
    billing_state: str
    # The plan in force and what it opens. Derived, never stored.
    plan: str
    modules: List[str]
    # Null means "follows the owner's subscription".
    pinned_plan: Optional[str] = None
    owner_account_id: Optional[int] = None
    created_at: Optional[datetime] = None
    members: List[AdminMember]
    tiers: List[AdminTier]
    spaces: List[AdminSpace]



def _billing_state(db, org) -> str:
    """'active' | 'grace' | 'lapsed', for the org's OWNER.

    The support question this answers is "why can't they save anything?", and
    the answer is almost always that they are mid-grace. Derived here rather
    than stored, like everywhere else it is asked.
    """
    from src.config import plans_registry as plans

    if org.pinned_plan is not None:
        # Pinned. Billing has nothing to say about it in either direction.
        return plans.BILLING_ACTIVE
    if org.owner_account_id is None:
        return plans.BILLING_ACTIVE
    owner = (db.query(Account.subscription_status,
                      Account.subscription_lapsed_at)
               .filter(Account.id == org.owner_account_id).first())
    if owner is None:
        return plans.BILLING_ACTIVE
    return plans.billing_state(owner[0], owner[1])


def _org_or_404(db, org_id: int) -> Organization:
    org = db.query(Organization).filter(Organization.id == org_id).first()
    if not org:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Organization not found")
    return org


@router.get("/organizations/{org_id}", response_model=OrganizationDetailResponse)
@limiter.limit("60/minute")
async def organization_detail(
    org_id: int, db: db_dependency, super_admin: super_admin_dependency, request: Request,
):
    """One org: its members, its tiers, and the spaces it is answerable for.

    Assembled in one response rather than three endpoints because the question
    super admins actually have is "what does this org look like", and answering
    it across three round trips invites a UI that shows two of them and forgets
    the third.
    """
    try:
        org = _org_or_404(db, org_id)
        from src.services import modules as modules_service

        entitlement = modules_service.org_entitlement(db, org_id)
        ceiling = entitlement.ceiling

        tier_modules = {
            t.tier_key: list(t.modules or [])
            for t in db.query(OrganizationTier).filter(
                OrganizationTier.org_id == org_id).all()
        }

        # The org's one owner. Everything below asks "is this member that
        # account?" rather than reading a per-row flag, so this page cannot
        # disagree with what the org itself says.
        owner_id = org.owner_account_id

        member_rows = (
            db.query(OrganizationMember, Account)
            .join(Account, Account.id == OrganizationMember.account_id)
            .filter(OrganizationMember.org_id == org_id)
            .order_by((OrganizationMember.account_id == owner_id).desc(),
                      Account.email.asc())
            .all()
        )

        def _effective(member) -> List[str]:
            # Mirrors services.modules.effective_modules. Recomputed here from
            # already-loaded rows rather than called per member, which would be
            # two queries each; the intersection itself is the same expression.
            if member.account_id == owner_id:
                return ceiling
            granted = set(tier_modules.get(member.tier_key, []))
            return [k for k in ceiling if k in granted]

        members = [
            AdminMember(
                account_id=m.account_id, email=a.email, tier_key=m.tier_key,
                is_owner=(m.account_id == owner_id), granted_by=m.granted_by,
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

        spaces = []
        space_rows = (db.query(Space)
                        .filter(Space.owner_org_id == org_id)
                        .order_by(Space.name.asc()).all())
        for sp in space_rows:
            sm = (
                db.query(SpaceMember, Account)
                .join(Account, Account.id == SpaceMember.account_id)
                .filter(SpaceMember.space_id == sp.id)
                .order_by(Account.email.asc())
                .all()
            )
            owner_email = (db.query(Account.email)
                             .filter(Account.id == sp.owner_account_id).scalar())
            spaces.append(AdminSpace(
                id=sp.id, name=sp.name, discoverable=sp.discoverable,
                join_policy=sp.join_policy,
                owner_account_id=sp.owner_account_id,
                owner_email=owner_email, member_count=len(sm),
                members=[
                    AdminSpaceMember(
                        account_id=member.account_id, email=acct.email,
                        tier_key=member.tier_key, is_owner=member.is_owner,
                        in_org=(member.account_id in member_account_ids),
                    )
                    for member, acct in sm
                ],
            ))

        return OrganizationDetailResponse(
            id=org.id, name=org.name,
            is_personal=(len(members) == 1),
            billing_state=_billing_state(db, org),
            plan=entitlement.plan,
            modules=ceiling,
            pinned_plan=org.pinned_plan,
            owner_account_id=org.owner_account_id, created_at=org.created_at,
            members=members, tiers=tiers, spaces=spaces,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise handle_db_error(e, "[ADMIN ORG DETAIL]")


class SpaceSearchItem(BaseModel):
    id: int
    name: str
    owner_org_id: Optional[int] = None
    owner_org_name: Optional[str] = None
    discoverable: bool
    member_count: int


@router.get("/spaces", response_model=List[SpaceSearchItem])
@limiter.limit("60/minute")
async def all_spaces(
    db: db_dependency, super_admin: super_admin_dependency, request: Request,
    q: Optional[str] = None, limit: int = 200,
):
    """Every space on the platform, across all orgs.

    The cross-org view exists for the question the per-org page cannot answer:
    "who is publishing this?" — asked when somebody reports reaching content
    and only knows what it was called.

    Names and counts only. A space's CONTENT is not listed here for the same
    reason a tenant's contacts are not: super admins administer access, and
    reading what is in a space means joining it.
    """
    try:
        limit = max(1, min(limit, 500))
        query = (
            db.query(Space, Organization)
            .outerjoin(Organization, Organization.id == Space.owner_org_id)
        )
        if q:
            query = query.filter(Space.name.ilike(f"%{q.strip()}%"))
        rows = query.order_by(Space.name.asc()).limit(limit).all()

        counts = dict(
            db.query(SpaceMember.space_id, sa_func.count(SpaceMember.id))
            .group_by(SpaceMember.space_id).all()
        )
        return [
            SpaceSearchItem(
                id=sp.id, name=sp.name,
                owner_org_id=o.id if o else None,
                owner_org_name=o.name if o else None,
                discoverable=sp.discoverable,
                member_count=counts.get(sp.id, 0),
            )
            for sp, o in rows
        ]
    except HTTPException:
        raise
    except Exception as e:
        raise handle_db_error(e, "[ADMIN SPACES]")

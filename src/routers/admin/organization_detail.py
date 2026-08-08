"""
Platform-admin browser: an org's members, tiers and plan.

ADMINISTER IS NOT READ, AND THIS FILE IS WHERE THAT LINE SITS
------------------------------------------------------------
Super admins can see WHO is in an org, without joining. (They could also see
WHICH SPACES it was answerable for, until spaces were removed.) That is access
metadata — the answer to "who can reach our data?" —
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
from pydantic import BaseModel

from src.db.models import (
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
    """One org: its members and its tiers.

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

        return OrganizationDetailResponse(
            id=org.id, name=org.name,
            is_personal=(len(members) == 1),
            billing_state=_billing_state(db, org),
            plan=entitlement.plan,
            modules=ceiling,
            pinned_plan=org.pinned_plan,
            owner_account_id=org.owner_account_id, created_at=org.created_at,
            members=members, tiers=tiers,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise handle_db_error(e, "[ADMIN ORG DETAIL]")

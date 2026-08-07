"""
Platform administration of an organization: its plan, and joining it.

Both are super-admin only, and both are audited — these are the two staff
powers a customer would most want a record of.
"""
from typing import List

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from src.config import plans_registry as plans
from src.db.models import Organization, OrganizationMember
from src.deps import db_dependency, super_admin_dependency
from src.rate_limit import limiter
from src.services import audit
from src.services.organizations import GRANTED_BY_GRANT, TIER_ORG_OWNER
from src.utils.errors import handle_db_error

router = APIRouter()


class SetPlanRequest(BaseModel):
    plan: str | None = Field(
        description="'free' | 'premium' to pin, or null to follow the owner's "
                    "subscription again.")


class PlanResponse(BaseModel):
    org_id: int
    # Null means "follows the owner's subscription".
    pinned_plan: str | None
    # What that resolves to right now, so the caller does not have to derive it.
    plan: str
    modules: List[str]


class JoinResponse(BaseModel):
    org_id: int
    org_name: str
    account_id: int
    tier_key: str


@router.put("/organizations/{org_id}/plan", response_model=PlanResponse)
@limiter.limit("30/minute")
async def set_plan(
    org_id: int,
    body: SetPlanRequest,
    db: db_dependency,
    staff: super_admin_dependency,
    request: Request,
):
    """Pin an org to a plan, or release it back to following its subscription.

    THE COMP, AND THE WHOLE OF IT. Pinning means billing can no longer change
    what this org may open: the next Stripe event for the owner's account will
    not quietly take a granted module back out. Same asymmetry as
    OrganizationMember.granted_by, one level up.

    This used to take a LIST OF MODULES and write it to the org. That made a
    comp a snapshot, so a module added to the premium plan afterwards never
    reached any comped client — all three on the platform silently ended up
    without `courses` and `spaces`. A plan tracks PLAN_MODULES as it grows,
    which is the only version of this that stays true on its own.

    If a client genuinely needs a set no plan sells, the answer is a new plan
    in config/plans_registry.py — one line, named, and it applies to everybody
    who is given it rather than to one row nobody can explain later.

    Narrowing does NOT rewrite the org's tiers. Entitlement intersects at read
    time, so the change takes effect on the next request and no tier row is
    left holding a grant that could re-widen access later.
    """
    try:
        if body.plan is not None and not plans.is_valid(body.plan):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"plan must be one of {plans.PLAN_KEYS}, or null")

        org = db.query(Organization).filter(Organization.id == org_id).first()
        if not org:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="Organization not found")

        before = org.pinned_plan
        org.pinned_plan = body.plan

        if before != org.pinned_plan:
            audit.record_org_change(
                db,
                event_type=audit.ORG_CEILING_CHANGE,
                org_id=org.id,
                detail=(f"pinned to the {org.pinned_plan} plan"
                        if org.pinned_plan
                        else "released — follows the owner's subscription"),
                actor_account_id=staff.id,
            )
        db.commit()

        from src.services import modules as modules_service

        entitlement = modules_service.org_entitlement(db, org.id)
        return PlanResponse(org_id=org.id, pinned_plan=org.pinned_plan,
                            plan=entitlement.plan, modules=entitlement.ceiling)
    except HTTPException:
        raise
    except Exception as e:
        raise handle_db_error(e, "[SET PLAN]")


@router.post("/organizations/{org_id}/join", status_code=status.HTTP_201_CREATED,
             response_model=JoinResponse)
@limiter.limit("10/minute")
async def join_organization(
    org_id: int,
    db: db_dependency,
    staff: super_admin_dependency,
    request: Request,
):
    """Join an organization in order to read its data.

    This is the ONLY way platform staff reach a tenant's rows. There is no
    filter bypass anywhere: org_id holds with zero exceptions, so staff who
    need to see a customer's contacts become a visible member of that customer's
    organization.

    That is a deliberate trade. Invisible access would be more convenient and
    would make the audit log a lie — a customer could not tell whether anyone
    had looked. Joining leaves two marks: a row in their member list, and a
    staff.join entry naming who and when.
    """
    try:
        org = db.query(Organization).filter(Organization.id == org_id).first()
        if not org:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="Organization not found")

        existing = (db.query(OrganizationMember)
                      .filter(OrganizationMember.org_id == org_id,
                              OrganizationMember.account_id == staff.id).first())
        if existing:
            return JoinResponse(org_id=org.id, org_name=org.name,
                                account_id=staff.id, tier_key=existing.tier_key)

        member = OrganizationMember(
            org_id=org.id,
            account_id=staff.id,
            tier_key=TIER_ORG_OWNER,
            granted_by=GRANTED_BY_GRANT,
            # NOT an owner of the customer's org — staff join to read, not to
            # take over. Ownership stays with the customer.
            is_owner=False,
        )
        db.add(member)
        audit.record_membership(
            db, event_type=audit.STAFF_JOIN, org_id=org.id,
            account_id=staff.id, tier_key=TIER_ORG_OWNER,
            actor_account_id=staff.id,
        )
        db.commit()
        db.refresh(member)

        return JoinResponse(org_id=org.id, org_name=org.name,
                            account_id=staff.id, tier_key=member.tier_key)
    except HTTPException:
        raise
    except Exception as e:
        raise handle_db_error(e, "[STAFF JOIN ORG]")

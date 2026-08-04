"""
Platform administration of an organization: its module ceiling, and joining it.

Both are super-admin only, and both are audited — these are the two staff
powers a customer would most want a record of.
"""
from typing import List

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from src.config.modules_registry import normalize
from src.db.models import Organization, OrganizationMember
from src.deps import db_dependency, super_admin_dependency
from src.rate_limit import limiter
from src.services import audit
from src.services.organizations import GRANTED_BY_GRANT, TIER_ORG_OWNER
from src.utils.errors import handle_db_error

router = APIRouter()


class SetCeilingRequest(BaseModel):
    modules: List[str] = Field(
        description="Full replacement set of module keys this org may use.")


class CeilingResponse(BaseModel):
    org_id: int
    granted_modules: List[str]


class JoinResponse(BaseModel):
    org_id: int
    org_slug: str
    account_id: int
    tier_key: str


@router.put("/organizations/{org_id}/ceiling", response_model=CeilingResponse)
@limiter.limit("30/minute")
async def set_ceiling(
    org_id: int,
    body: SetCeilingRequest,
    db: db_dependency,
    staff: super_admin_dependency,
    request: Request,
):
    """Set which modules an org may use at all.

    Every ceiling is bespoke — there is no plan table. "Cohort 3" gets exactly
    the two modules you check for it; a paying customer gets a different set;
    no two orgs need match.

    Lowering a ceiling does NOT rewrite the org's tiers. Entitlement intersects
    at read time, so the removal takes effect on the next request and no tier
    row is left holding a grant that could re-widen access later.
    """
    try:
        org = db.query(Organization).filter(Organization.id == org_id).first()
        if not org:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="Organization not found")

        before = list(org.granted_modules or [])
        org.granted_modules = normalize(body.modules)

        if org.granted_modules != before:
            audit.record_org_change(
                db,
                event_type=audit.ORG_CEILING_CHANGE,
                org_id=org.id,
                detail=",".join(org.granted_modules) or "(none)",
                actor_account_id=staff.id,
            )
        db.commit()
        db.refresh(org)
        return CeilingResponse(org_id=org.id,
                               granted_modules=list(org.granted_modules or []))
    except HTTPException:
        raise
    except Exception as e:
        raise handle_db_error(e, "[SET CEILING]")


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
            return JoinResponse(org_id=org.id, org_slug=org.slug,
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

        return JoinResponse(org_id=org.id, org_slug=org.slug,
                            account_id=staff.id, tier_key=member.tier_key)
    except HTTPException:
        raise
    except Exception as e:
        raise handle_db_error(e, "[STAFF JOIN ORG]")

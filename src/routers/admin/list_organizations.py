"""
List every organization on the platform. Super admin only.

This is the administrative view: which orgs exist, how big they are, what
module ceiling each has, and whether any is suspended. It returns no tenant
data — staff read an org's contacts by joining that org, which is visible to
its members.
"""
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import func as sa_func

from src.db.models import Account, Organization, OrganizationMember, OrganizationTier
from src.deps import db_dependency, super_admin_dependency
from src.rate_limit import limiter
from src.utils.errors import handle_db_error

from .models import OrganizationListResponse, OrganizationSummary

router = APIRouter()


@router.get("/organizations", response_model=OrganizationListResponse)
@limiter.limit("30/minute")
async def list_organizations(
    db: db_dependency,
    staff: super_admin_dependency,
    request: Request,
):
    try:
        orgs = db.query(Organization).order_by(Organization.id.asc()).all()
        if not orgs:
            return OrganizationListResponse(organizations=[], total=0)

        org_ids = [o.id for o in orgs]

        # Counts in two grouped queries rather than per-org lookups, so the
        # page cost stays flat as the number of orgs grows.
        member_counts = dict(
            db.query(OrganizationMember.org_id, sa_func.count(OrganizationMember.id))
            .filter(OrganizationMember.org_id.in_(org_ids))
            .group_by(OrganizationMember.org_id)
            .all()
        )
        tier_counts = dict(
            db.query(OrganizationTier.org_id, sa_func.count(OrganizationTier.id))
            .filter(OrganizationTier.org_id.in_(org_ids))
            .group_by(OrganizationTier.org_id)
            .all()
        )

        owner_ids = [o.owner_account_id for o in orgs if o.owner_account_id]
        owner_emails = dict(
            db.query(Account.id, Account.email)
            .filter(Account.id.in_(owner_ids))
            .all()
        ) if owner_ids else {}

        return OrganizationListResponse(
            organizations=[
                OrganizationSummary(
                    id=o.id,
                    slug=o.slug,
                    name=o.name,
                    is_personal=o.is_personal,
                    status=o.status,
                    ceiling_managed_by=o.ceiling_managed_by,
                    owner_account_id=o.owner_account_id,
                    owner_email=owner_emails.get(o.owner_account_id),
                    member_count=member_counts.get(o.id, 0),
                    tier_count=tier_counts.get(o.id, 0),
                    granted_modules=list(o.granted_modules or []),
                    created_at=o.created_at,
                )
                for o in orgs
            ],
            total=len(orgs),
        )
    except HTTPException:
        raise
    except Exception as e:
        raise handle_db_error(e, "[ADMIN LIST ORGANIZATIONS]")

"""
List every organization on the platform. Super admin only.

This is the administrative view: which orgs exist, how big they are, what
module ceiling each has, and where each one's billing stands. It returns no
tenant data — super admins read an org's contacts by joining that org, which is
visible to its members.
"""
from typing import Optional

from fastapi import APIRouter, Query, Request
from sqlalchemy import func as sa_func, or_

from src.config import plans_registry as plans
from src.db.models import Account, Organization, OrganizationMember
from src.deps import db_dependency, super_admin_dependency
from src.rate_limit import limiter

from .models import OrganizationListResponse, OrganizationSummary

router = APIRouter()


@router.get("/organizations", response_model=OrganizationListResponse)
@limiter.limit("30/minute")
async def list_organizations(
    db: db_dependency,
    super_admin: super_admin_dependency,
    request: Request,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    q: Optional[str] = Query(None, max_length=200,
                             description="Match on org name or owner email"),
):
    """One page of orgs, oldest first, optionally filtered by `q`."""
    query = db.query(Organization)
    if q and q.strip():
        like = f"%{q.strip()}%"
        query = (query.outerjoin(Account, Account.id == Organization.owner_account_id)
                      .filter(or_(Organization.name.ilike(like),
                                  Account.email.ilike(like))))
    total = query.count()
    orgs = (query.order_by(Organization.id.asc())
                 .offset(offset).limit(limit).all())
    if not orgs:
        return OrganizationListResponse.of([], total=total, limit=limit,
                                           offset=offset)

    org_ids = [o.id for o in orgs]

    # Counts in two grouped queries rather than per-org lookups, so the
    # page cost stays flat as the number of orgs grows.
    member_counts = dict(
        db.query(OrganizationMember.org_id, sa_func.count(OrganizationMember.id))
        .filter(OrganizationMember.org_id.in_(org_ids))
        .group_by(OrganizationMember.org_id)
        .all()
    )

    owner_ids = [o.owner_account_id for o in orgs if o.owner_account_id]
    # One query for every owner, not one per org. Billing state is derived
    # per org below from these two columns; asking the database again for
    # each row would turn a super admin page into a few hundred round
    # trips.
    owners = (
        db.query(Account.id, Account.email, Account.subscription_status,
                 Account.subscription_lapsed_at)
        .filter(Account.id.in_(owner_ids)).all()
    ) if owner_ids else []
    owner_emails = {oid: email for oid, email, _, _ in owners}
    owner_billing = {oid: (st, lapsed) for oid, _, st, lapsed in owners}

    return OrganizationListResponse.of(
        [
            OrganizationSummary(
                id=o.id,
                name=o.name,
                is_personal=(member_counts.get(o.id, 0) == 1),
                billing_state=plans.org_billing_state(
                    o.pinned_plan, owner_billing.get(o.owner_account_id)),
                pinned_plan=o.pinned_plan,
                owner_account_id=o.owner_account_id,
                owner_email=owner_emails.get(o.owner_account_id),
                member_count=member_counts.get(o.id, 0),
                modules=plans.modules_for_plan(plans.org_plan(
                    o.pinned_plan, owner_billing.get(o.owner_account_id))),
                created_at=o.created_at,
            )
            for o in orgs
        ],
        total=total, limit=limit, offset=offset,
    )

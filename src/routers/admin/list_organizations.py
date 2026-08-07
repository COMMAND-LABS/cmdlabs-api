"""
List every organization on the platform. Super admin only.

This is the administrative view: which orgs exist, how big they are, what
module ceiling each has, and where each one's billing stands. It returns no
tenant data — super admins read an org's contacts by joining that org, which is
visible to its members.
"""
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import func as sa_func

from src.config import plans_registry as plans
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
    super_admin: super_admin_dependency,
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

        def _state(org) -> str:
            """Pinned orgs are always 'active': super admins gave them a plan,
            so a
            payment says nothing about them in either direction."""
            if org.pinned_plan is not None:
                return plans.BILLING_ACTIVE
            billing = owner_billing.get(org.owner_account_id)
            if billing is None:
                return plans.BILLING_ACTIVE
            return plans.billing_state(billing[0], billing[1])

        def _plan_of(org) -> str:
            """The plan in force. Same rule as services.modules.org_entitlement,
            evaluated from the rows already loaded above rather than one query
            per org."""
            billing = owner_billing.get(org.owner_account_id)
            if billing is None:
                return plans.PLAN_FREE
            return plans.plan_for(billing[0], billing[1])

        return OrganizationListResponse(
            organizations=[
                OrganizationSummary(
                    id=o.id,
                    name=o.name,
                    is_personal=(member_counts.get(o.id, 0) == 1),
                    billing_state=_state(o),
                    pinned_plan=o.pinned_plan,
                    owner_account_id=o.owner_account_id,
                    owner_email=owner_emails.get(o.owner_account_id),
                    member_count=member_counts.get(o.id, 0),
                    tier_count=tier_counts.get(o.id, 0),
                    modules=plans.modules_for_plan(
                        o.pinned_plan or _plan_of(o)),
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

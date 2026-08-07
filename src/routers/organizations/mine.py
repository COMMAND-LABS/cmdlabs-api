"""
Which organizations the caller belongs to — the switcher's data source.

Returns only orgs the caller is actually a member of, resolved fresh on every
call. That is deliberate: the switcher is the one UI that names orgs by id, and
if it could list an org the caller has been removed from, they would pick it
and get a 403 they could not explain.

Carries no tenant data — an id, a name, and whether it is the active one.
"""
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from sqlalchemy import func

from src.db.models import Organization, OrganizationMember
from src.deps import db_dependency, org_dependency
from src.rate_limit import limiter
from src.utils.errors import handle_db_error

router = APIRouter()


class MyOrganization(BaseModel):
    id: int
    name: str
    # None for a personal workspace, which has no public page.
    is_owner: bool
    is_personal: bool
    is_active: bool


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
        return MyOrganizationsResponse(
            active_org_id=org.org_id,
            organizations=[
                MyOrganization(
                    id=o.id,
                    name=o.name,
                    is_owner=m.is_owner,
                    is_personal=(member_counts.get(o.id, 0) == 1),
                    is_active=(o.id == org.org_id),
                )
                for o, m in rows
            ],
        )
    except HTTPException:
        raise
    except Exception as e:
        raise handle_db_error(e, "[MY ORGS]")

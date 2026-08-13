"""
List career timeline entries for a contact.
"""
from typing import List
from fastapi import APIRouter, Request
from src.deps import org_dependency, db_dependency, auth_dependency, account_id_from_claims, ensure_account
from src.services.org_scope import get_scoped_or_404, tenant_predicate
from src.db.models import Contact, CareerTimeline

from ..models import CareerTimelineResponse
from src.rate_limit import limiter

router = APIRouter()

@router.get("/", response_model=List[CareerTimelineResponse])
@limiter.limit("60/minute")
async def list_career_timeline(
    contact_id: int,
    db: db_dependency,
    auth: auth_dependency,
    org: org_dependency,
    request: Request,
):
    account_id = account_id_from_claims(auth)
    account = ensure_account(db, account_id)

    get_scoped_or_404(db, Contact, contact_id, org)

    entries = (
        db.query(CareerTimeline)
        .filter(
            CareerTimeline.contact_id == contact_id,
            tenant_predicate(CareerTimeline, org),
        )
        .order_by(CareerTimeline.start_date.desc())
        .all()
    )

    return entries

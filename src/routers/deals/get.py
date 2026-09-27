"""
Get single deal endpoint.
"""
from fastapi import APIRouter, Request
from src.deps import org_dependency, db_dependency, auth_dependency
from src.services.org_scope import get_scoped_or_404
from src.db.models import Deal

from .models import DealResponse
from src.rate_limit import limiter

router = APIRouter()


@router.get("/{deal_id}", response_model=DealResponse)
@limiter.limit("60/minute")
async def get_deal(
    deal_id: int,
    db: db_dependency,
    auth: auth_dependency,
    org: org_dependency,
    request: Request,
):
    deal = get_scoped_or_404(db, Deal, deal_id, org)

    return deal

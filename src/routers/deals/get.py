"""
Get single deal endpoint.
"""
from fastapi import APIRouter, HTTPException, status, Request
from src.deps import org_dependency, db_dependency, auth_dependency, account_id_from_claims, ensure_account
from src.services.org_scope import tenant_predicate
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
    account_id = account_id_from_claims(auth)
    account = ensure_account(db, account_id)

    deal = db.query(Deal).filter(
        Deal.id == deal_id,
        tenant_predicate(Deal, org),
    ).first()

    if not deal:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deal not found")

    return deal

"""
Delete deal endpoint.
"""
from fastapi import APIRouter, status, Request
from src.deps import org_dependency, db_dependency, auth_dependency
from src.services.org_scope import get_scoped_or_404
from src.db.models import Deal
from src.rate_limit import limiter

router = APIRouter()


@router.delete("/{deal_id}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("30/minute")
async def delete_deal(
    deal_id: int,
    db: db_dependency,
    auth: auth_dependency,
    org: org_dependency,
    request: Request,
):
    deal = get_scoped_or_404(db, Deal, deal_id, org)

    db.delete(deal)
    db.commit()

    return None

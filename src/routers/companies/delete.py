"""
Delete company endpoint.
"""
from fastapi import APIRouter, status, Request
from src.deps import org_dependency, db_dependency, auth_dependency
from src.services.org_scope import get_scoped_or_404
from src.db.models import Company
from src.rate_limit import limiter

router = APIRouter()

@router.delete("/{company_id}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("30/minute")
async def delete_company(
    company_id: int,
    db: db_dependency,
    auth: auth_dependency,
    org: org_dependency,
    request: Request,
):
    company = get_scoped_or_404(db, Company, company_id, org)

    # The company_contacts join rows cascade; the contacts themselves remain.
    db.delete(company)
    db.commit()

    return None

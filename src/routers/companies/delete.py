"""
Delete company endpoint.
"""
from fastapi import APIRouter, HTTPException, status, Request
from src.deps import org_dependency, db_dependency, auth_dependency, account_id_from_claims, ensure_account
from src.services.org_scope import tenant_predicate
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
    account_id = account_id_from_claims(auth)
    account = ensure_account(db, account_id)

    company = db.query(Company).filter(
        Company.id == company_id,
        tenant_predicate(Company, org),
    ).first()

    if not company:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found")

    # The company_contacts join rows cascade; the contacts themselves remain.
    db.delete(company)
    db.commit()

    return None

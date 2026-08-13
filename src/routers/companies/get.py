"""
Get single company endpoint (includes full list of associated contacts).
"""
from fastapi import APIRouter, Request
from src.deps import org_dependency, db_dependency, auth_dependency, account_id_from_claims, ensure_account
from src.services.org_scope import get_scoped_or_404
from src.db.models import Company

from .models import CompanyResponse, CompanyContactResponse
from src.rate_limit import limiter

router = APIRouter()

@router.get("/{company_id}", response_model=CompanyResponse)
@limiter.limit("60/minute")
async def get_company(
    company_id: int,
    db: db_dependency,
    auth: auth_dependency,
    org: org_dependency,
    request: Request,
):
    account_id = account_id_from_claims(auth)
    account = ensure_account(db, account_id)

    company = get_scoped_or_404(db, Company, company_id, org)

    # The ORM relationship is `contact_memberships` (CompanyContact join
    # rows); map it onto the response's `contacts` field explicitly.
    return CompanyResponse(
        id=company.id,
        account_id=company.account_id,
        name=company.name,
        domain=company.domain,
        website=company.website,
        industry=company.industry,
        description=company.description,
        linkedin_url=company.linkedin_url,
        created_at=company.created_at,
        updated_at=company.updated_at,
        contacts=[
            CompanyContactResponse.model_validate(m)
            for m in company.contact_memberships
        ],
    )

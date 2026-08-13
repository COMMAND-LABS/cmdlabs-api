"""
Create company endpoint.
"""
from fastapi import APIRouter, HTTPException, status, Request
from src.deps import org_dependency, db_dependency, auth_dependency, account_id_from_claims, ensure_account
from src.db.models import Company

from .models import CreateCompanyRequest, CompanySummaryResponse
from src.rate_limit import limiter

router = APIRouter()

@router.post("/", status_code=status.HTTP_201_CREATED, response_model=CompanySummaryResponse)
@limiter.limit("30/minute")
async def create_company(
    request_body: CreateCompanyRequest,
    db: db_dependency,
    auth: auth_dependency,
    org: org_dependency,
    request: Request,
):
    account_id = account_id_from_claims(auth)
    account = ensure_account(db, account_id)

    if not request_body.name or not request_body.name.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Company name cannot be empty")

    company = Company(
        org_id=org.org_id,
        account_id=account_id,
        name=request_body.name.strip(),
        domain=request_body.domain,
        website=request_body.website,
        industry=request_body.industry,
        description=request_body.description,
        linkedin_url=request_body.linkedin_url,
    )

    db.add(company)
    db.commit()
    db.refresh(company)

    return CompanySummaryResponse(
        id=company.id,
        account_id=company.account_id,
        name=company.name,
        domain=company.domain,
        website=company.website,
        industry=company.industry,
        description=company.description,
        linkedin_url=company.linkedin_url,
        contact_count=0,
        created_at=company.created_at,
        updated_at=company.updated_at,
    )

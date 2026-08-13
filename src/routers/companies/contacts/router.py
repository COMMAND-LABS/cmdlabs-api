"""
Company contacts sub-router — associate / disassociate contacts with a company.

This is the many-to-many link between Company and Contact via the
company_contacts join table (CompanyContact). A contact can belong to many
companies and vice versa.
"""
from typing import List
from fastapi import APIRouter, HTTPException, status, Request
from sqlalchemy.exc import IntegrityError
from src.deps import org_dependency, db_dependency, auth_dependency, account_id_from_claims
from src.services.org_scope import tenant_predicate
from src.db.models import Company, CompanyContact, Contact

from src.routers.companies.models import (
    CompanyContactResponse,
    AddContactToCompanyRequest,
    BulkAddContactsToCompanyRequest,
)
from src.rate_limit import limiter

router = APIRouter()

@router.get("/", response_model=List[CompanyContactResponse])
@limiter.limit("60/minute")
async def list_company_contacts(
    company_id: int,
    db: db_dependency,
    auth: auth_dependency,
    org: org_dependency,
    request: Request,
):
    """List all contacts associated with a given company."""
    account_id = account_id_from_claims(auth)

    company = db.query(Company).filter(
        Company.id == company_id,
        tenant_predicate(Company, org),
    ).first()

    if not company:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found")

    memberships = (
        db.query(CompanyContact)
        .filter(CompanyContact.company_id == company_id, tenant_predicate(CompanyContact, org))
        .all()
    )
    return memberships

@router.post("/", status_code=status.HTTP_201_CREATED, response_model=CompanyContactResponse)
@limiter.limit("60/minute")
async def add_company_contact(
    company_id: int,
    request_body: AddContactToCompanyRequest,
    db: db_dependency,
    auth: auth_dependency,
    org: org_dependency,
    request: Request,
):
    """Associate a single contact with a company."""
    try:
        account_id = account_id_from_claims(auth)

        company = db.query(Company).filter(
            Company.id == company_id,
            tenant_predicate(Company, org),
        ).first()

        if not company:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found")

        contact = db.query(Contact).filter(
            Contact.id == request_body.contact_id,
            tenant_predicate(Contact, org),
        ).first()

        if not contact:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Contact not found")

        membership = CompanyContact(
            org_id=org.org_id,
            company_id=company_id,
            contact_id=request_body.contact_id,
            account_id=account_id,
            title=request_body.title,
        )
        db.add(membership)
        db.commit()
        db.refresh(membership)

        return membership

    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Contact is already associated with this company",
        )
@router.post("/bulk", status_code=status.HTTP_200_OK)
@limiter.limit("30/minute")
async def bulk_add_company_contacts(
    company_id: int,
    request_body: BulkAddContactsToCompanyRequest,
    db: db_dependency,
    auth: auth_dependency,
    org: org_dependency,
    request: Request,
):
    """Associate multiple contacts with a company in one call, skipping duplicates."""
    account_id = account_id_from_claims(auth)

    company = db.query(Company).filter(
        Company.id == company_id,
        tenant_predicate(Company, org),
    ).first()

    if not company:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found")

    existing_ids = {
        m.contact_id
        for m in db.query(CompanyContact)
        .filter(CompanyContact.company_id == company_id, tenant_predicate(CompanyContact, org))
        .all()
    }

    valid_contacts = db.query(Contact).filter(
        Contact.id.in_(request_body.contact_ids),
        tenant_predicate(Contact, org),
    ).all()
    valid_ids = {c.id for c in valid_contacts}

    added = 0
    for contact_id in request_body.contact_ids:
        if contact_id in valid_ids and contact_id not in existing_ids:
            db.add(CompanyContact(
                org_id=org.org_id,
                company_id=company_id,
                contact_id=contact_id,
                account_id=account_id,
            ))
            added += 1

    db.commit()
    return {"added": added, "skipped": len(request_body.contact_ids) - added}

@router.delete("/{contact_id}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("60/minute")
async def remove_company_contact(
    company_id: int,
    contact_id: int,
    db: db_dependency,
    auth: auth_dependency,
    org: org_dependency,
    request: Request,
):
    """Disassociate a contact from a company."""
    account_id = account_id_from_claims(auth)

    company = db.query(Company).filter(
        Company.id == company_id,
        tenant_predicate(Company, org),
    ).first()

    if not company:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found")

    membership = db.query(CompanyContact).filter(
        CompanyContact.company_id == company_id,
        tenant_predicate(CompanyContact, org),
        CompanyContact.contact_id == contact_id,
    ).first()

    if not membership:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Contact is not associated with this company")

    db.delete(membership)
    db.commit()

    return None

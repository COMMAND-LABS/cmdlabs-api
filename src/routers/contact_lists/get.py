"""
Get single contact list endpoint (includes full member list).
"""
from fastapi import APIRouter, HTTPException, status, Request
from src.deps import org_dependency, db_dependency, auth_dependency, account_id_from_claims, ensure_account
from src.services.org_scope import tenant_predicate
from src.db.models import ContactList

from .models import ContactListResponse
from src.rate_limit import limiter

router = APIRouter()

@router.get("/{list_id}", response_model=ContactListResponse)
@limiter.limit("60/minute")
async def get_contact_list(
    list_id: int,
    db: db_dependency,
    auth: auth_dependency,
    org: org_dependency,
    request: Request,
):
    account_id = account_id_from_claims(auth)
    account = ensure_account(db, account_id)

    contact_list = db.query(ContactList).filter(
        ContactList.id == list_id,
        tenant_predicate(ContactList, org),
    ).first()

    if not contact_list:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Contact list not found")

    return contact_list

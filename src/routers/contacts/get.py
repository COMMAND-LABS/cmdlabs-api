"""
Get single contact endpoint (includes full event timeline).
"""
from fastapi import APIRouter, Request
from src.deps import org_dependency, db_dependency, auth_dependency, account_id_from_claims, ensure_account
from src.services.org_scope import get_scoped_or_404
from src.db.models import Contact

from .models import ContactResponse
from src.rate_limit import limiter

router = APIRouter()

@router.get("/{contact_id}", response_model=ContactResponse)
@limiter.limit("60/minute")
async def get_contact(
    contact_id: int,
    db: db_dependency,
    auth: auth_dependency,
    org: org_dependency,
    request: Request,
):
    account_id = account_id_from_claims(auth)
    account = ensure_account(db, account_id)

    return get_scoped_or_404(db, Contact, contact_id, org)

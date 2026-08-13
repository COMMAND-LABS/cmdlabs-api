"""
Delete contact list endpoint.
"""
from fastapi import APIRouter, status, Request
from src.deps import org_dependency, db_dependency, auth_dependency, account_id_from_claims, ensure_account
from src.services.org_scope import get_scoped_or_404
from src.db.models import ContactList
from src.rate_limit import limiter

router = APIRouter()

@router.delete("/{list_id}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("30/minute")
async def delete_contact_list(
    list_id: int,
    db: db_dependency,
    auth: auth_dependency,
    org: org_dependency,
    request: Request,
):
    account_id = account_id_from_claims(auth)
    account = ensure_account(db, account_id)

    contact_list = get_scoped_or_404(db, ContactList, list_id, org)

    db.delete(contact_list)
    db.commit()

    return None

"""
List contact lists endpoint.
"""
from typing import List
from fastapi import APIRouter, Request
from src.deps import org_dependency, db_dependency, auth_dependency, account_id_from_claims, ensure_account
from src.services.org_scope import tenant_predicate
from src.db.models import ContactList, ContactListMember

from .models import ContactListSummaryResponse
from src.rate_limit import limiter

router = APIRouter()

@router.get("/", response_model=List[ContactListSummaryResponse])
@limiter.limit("60/minute")
async def list_contact_lists(
    db: db_dependency,
    auth: auth_dependency,
    org: org_dependency,
    request: Request,
):
    """List all contact lists for the authenticated account."""
    account_id = account_id_from_claims(auth)
    account = ensure_account(db, account_id)

    contact_lists = (
        db.query(ContactList)
        .filter(tenant_predicate(ContactList, org))
        .order_by(ContactList.updated_at.desc())
        .all()
    )

    results = []
    for cl in contact_lists:
        count = (
            db.query(ContactListMember)
            .filter(ContactListMember.contact_list_id == cl.id)
            .count()
        )
        results.append(
            ContactListSummaryResponse(
                id=cl.id,
                account_id=cl.account_id,
                name=cl.name,
                description=cl.description,
                member_count=count,
                created_at=cl.created_at,
                updated_at=cl.updated_at,
            )
        )

    return results

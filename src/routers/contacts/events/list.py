"""
List contact events endpoint.
"""
from typing import List
from fastapi import APIRouter, Request
from src.deps import org_dependency, db_dependency, auth_dependency, account_id_from_claims, ensure_account
from src.services.org_scope import get_scoped_or_404, tenant_predicate
from src.db.models import Contact, ContactEvent

from ..models import ContactEventResponse
from src.rate_limit import limiter

router = APIRouter()

@router.get("/", response_model=List[ContactEventResponse])
@limiter.limit("60/minute")
async def list_contact_events(
    contact_id: int,
    db: db_dependency,
    auth: auth_dependency,
    org: org_dependency,
    request: Request,
):
    """Return events for a contact ordered most-recent first."""
    account_id = account_id_from_claims(auth)
    account = ensure_account(db, account_id)

    get_scoped_or_404(db, Contact, contact_id, org)

    events = (
        db.query(ContactEvent)
        .filter(ContactEvent.contact_id == contact_id, tenant_predicate(ContactEvent, org))
        .order_by(ContactEvent.occurred_at.desc())
        .all()
    )

    return events

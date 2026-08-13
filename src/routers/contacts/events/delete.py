"""
Delete a contact event endpoint.
"""
import logging
from fastapi import APIRouter, status, Request
from src.deps import org_dependency, db_dependency, auth_dependency, account_id_from_claims, ensure_account
from src.services.org_scope import get_scoped_or_404
from src.db.models import Contact, ContactEvent
from src.services.crm_vector_service import delete_vector
from src.rate_limit import limiter

logger = logging.getLogger(__name__)
router = APIRouter()

@router.delete("/{event_id}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("30/minute")
async def delete_contact_event(
    contact_id: int,
    event_id: int,
    db: db_dependency,
    auth: auth_dependency,
    org: org_dependency,
    request: Request,
):
    account_id = account_id_from_claims(auth)
    account = ensure_account(db, account_id)

    get_scoped_or_404(db, Contact, contact_id, org)
    event = get_scoped_or_404(db, ContactEvent, event_id, org,
                              ContactEvent.contact_id == contact_id,
                              label="Event")

    db.delete(event)
    db.commit()

    try:
        delete_vector(f"contact_event_{event_id}")
    except Exception as vec_err:
        logger.warning("[DELETE CONTACT EVENT] vector delete failed: %s", vec_err)

    return None

"""
Revoke a knowledge-base access grant. Index owner only.

It used to also admit a manager of the granted GROUP. Groups are spaces now,
and a space share is revoked from the space by its owner — the authority moved
with the thing it was an authority over.
"""
from fastapi import APIRouter, HTTPException, status, Request
from src.deps import db_dependency, jwt_dependency, account_id_from_claims
from src.db.models import AccessGrant, VectorStore
from src.services import access
from src.services.access_admin import record_access_event
from src.utils.errors import handle_db_error
from src.rate_limit import limiter

router = APIRouter()


@router.delete("/grants/{grant_id}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("10/minute")
async def revoke_grant(
    grant_id: int,
    db: db_dependency,
    jwt: jwt_dependency,
    request: Request,
):
    """Revoke a KB grant. Index owner only."""
    try:
        account_id = account_id_from_claims(jwt)

        grant = db.query(AccessGrant).filter(
            AccessGrant.id == grant_id,
            AccessGrant.resource_type == access.VECTOR_STORE,
        ).first()
        if not grant:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Grant not found")

        store = db.query(VectorStore).filter(VectorStore.id == grant.resource_id).first()
        is_owner = store is not None and store.owner_account_id == account_id

        if not is_owner:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to revoke this grant")

        record_access_event(
            db,
            event_type="revoke",
            actor_account_id=account_id,
            resource_type=access.VECTOR_STORE,
            resource_id=grant.resource_id,
            principal_type=grant.principal_type,
            principal_id=grant.principal_id,
            role=grant.role,
        )
        db.delete(grant)
        db.commit()
        return None
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise handle_db_error(e, "[REVOKE VS GRANT]")

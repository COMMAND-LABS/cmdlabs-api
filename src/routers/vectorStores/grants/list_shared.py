"""
List knowledge bases shared with the caller (via access-group membership).
"""
from fastapi import APIRouter, Request
from typing import List
from src.deps import org_dependency, db_dependency, jwt_dependency, account_id_from_claims
from .models import SharedVectorStore
from src.services.vector_store_access import list_shared_vector_stores
from src.rate_limit import limiter

router = APIRouter()


@router.get("/shared", response_model=List[SharedVectorStore])
@limiter.limit("30/minute")
async def list_shared(
    db: db_dependency,
    jwt: jwt_dependency,
    org: org_dependency,
    request: Request,
):
    """Knowledge bases shared with the caller, with the caller's write capability."""
    account_id = account_id_from_claims(jwt)
    shared = list_shared_vector_stores(db, account_id, org_id=org.org_id)
    return [SharedVectorStore(**s) for s in shared]

"""
Get credential with full decrypted data endpoint.
"""
from fastapi import APIRouter, Request
from src.deps import db_dependency, jwt_dependency, account_id_from_claims, ensure_account
from ._shared import flexible_detail_response, invalid_data_as_400, owned_credential_or_404
from .models import FlexibleCredentialDetailResponse
from src.rate_limit import limiter

router = APIRouter()


@router.get("/{credential_id}/full", response_model=FlexibleCredentialDetailResponse)
@limiter.limit("30/minute")
async def get_credential_full(
    credential_id: int,
    db: db_dependency,
    jwt: jwt_dependency,
    request: Request
):
    """Get a specific credential with full decrypted data structure."""
    account_id = account_id_from_claims(jwt)
    ensure_account(db, account_id)

    credential = owned_credential_or_404(db, account_id, credential_id=credential_id)
    with invalid_data_as_400(db, 'retrieving full credential'):
        return flexible_detail_response(credential)

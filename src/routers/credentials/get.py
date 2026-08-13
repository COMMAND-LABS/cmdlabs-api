"""
Get credential endpoint (legacy).
"""
from fastapi import APIRouter, Request
from src.deps import db_dependency, jwt_dependency, account_id_from_claims, ensure_account
from ._shared import invalid_data_as_400, legacy_detail_response, owned_credential_or_404
from .models import CredentialDetailResponse
from src.rate_limit import limiter

router = APIRouter()


@router.get("/{credential_id}", response_model=CredentialDetailResponse)
@limiter.limit("30/minute")
async def get_credential(
    credential_id: int,
    db: db_dependency,
    jwt: jwt_dependency,
    request: Request
):
    """
    Get a specific credential by ID, including the decrypted API key.
    Only returns credentials belonging to the authenticated user.

    LEGACY ENDPOINT: Returns api_key field for backward compatibility.
    For full credential data (DB connections, etc.), use GET /{id}/full
    """
    account_id = account_id_from_claims(jwt)
    ensure_account(db, account_id)

    credential = owned_credential_or_404(db, account_id, credential_id=credential_id)
    with invalid_data_as_400(db, 'retrieving credential'):
        return legacy_detail_response(credential)

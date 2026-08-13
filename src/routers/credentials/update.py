"""
Update credential endpoint (legacy).
"""
from fastapi import APIRouter, Request
from src.deps import db_dependency, jwt_dependency, account_id_from_claims, ensure_account
from ._shared import invalid_data_as_400, metadata_response, owned_credential_or_404
from .encryption import encrypt_credential_data
from .models import UpdateCredentialRequest, CredentialResponse
from src.rate_limit import limiter

router = APIRouter()


@router.put("/{credential_id}", response_model=CredentialResponse)
@limiter.limit("10/minute")
async def update_credential(
    credential_id: int,
    request_body: UpdateCredentialRequest,
    db: db_dependency,
    jwt: jwt_dependency,
    request: Request
):
    """
    Update an existing credential's API key.

    LEGACY ENDPOINT: For simple API key updates.
    For flexible credentials, use PUT /{id}/full
    """
    account_id = account_id_from_claims(jwt)
    ensure_account(db, account_id)

    credential = owned_credential_or_404(db, account_id, credential_id=credential_id)
    with invalid_data_as_400(db, 'updating credential'):
        credential.encrypted_data = encrypt_credential_data({"api_key": request_body.api_key})
        credential.auth_type = "api_key"
        db.commit()
        db.refresh(credential)

        return metadata_response(credential)

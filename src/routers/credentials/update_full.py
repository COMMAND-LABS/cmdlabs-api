"""
Update credential with full flexible data endpoint.
"""
from fastapi import APIRouter, Request
from src.deps import db_dependency, jwt_dependency, account_id_from_claims, ensure_account
from ._shared import invalid_data_as_400, metadata_response, owned_credential_or_404
from .encryption import encrypt_credential_data
from .models import UpdateFlexibleCredentialRequest, CredentialResponse
from src.rate_limit import limiter

router = APIRouter()


@router.put("/{credential_id}/full", response_model=CredentialResponse)
@limiter.limit("10/minute")
async def update_credential_full(
    credential_id: int,
    request_body: UpdateFlexibleCredentialRequest,
    db: db_dependency,
    jwt: jwt_dependency,
    request: Request
):
    """Update a credential with full flexible data structure."""
    account_id = account_id_from_claims(jwt)
    ensure_account(db, account_id)

    credential = owned_credential_or_404(db, account_id, credential_id=credential_id)
    with invalid_data_as_400(db, 'updating flexible credential'):
        credential.encrypted_data = encrypt_credential_data(request_body.credential_data)
        if request_body.credential_name is not None:
            credential.credential_name = request_body.credential_name
        if request_body.metadata is not None:
            credential.credential_metadata = request_body.metadata

        db.commit()
        db.refresh(credential)

        return metadata_response(credential)

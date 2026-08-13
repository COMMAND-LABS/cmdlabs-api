"""
Create credential endpoint (legacy).
"""
from fastapi import APIRouter, status, Request
from src.deps import db_dependency, jwt_dependency, account_id_from_claims, ensure_account
from src.db.models import Credential
from ._shared import invalid_data_as_400, metadata_response
from .encryption import encrypt_credential_data
from .models import CreateCredentialRequest, CredentialResponse
from src.rate_limit import limiter

router = APIRouter()


@router.post("/", status_code=status.HTTP_201_CREATED, response_model=CredentialResponse)
@limiter.limit("10/minute")
async def create_credential(
    request_body: CreateCredentialRequest,
    db: db_dependency,
    jwt: jwt_dependency,
    request: Request
):
    """
    Create a new credential (API key) for a third-party service.
    The API key will be encrypted before storage.

    LEGACY ENDPOINT: For simple API key storage.
    For flexible credentials (DB connections, OAuth, etc.), use POST /flexible
    """
    account_id = account_id_from_claims(jwt)
    ensure_account(db, account_id)

    with invalid_data_as_400(db, 'creating credential'):
        credential = Credential(
            account_id=account_id,
            credential_type=request_body.credential_type,
            auth_type="api_key",
            encrypted_data=encrypt_credential_data({"api_key": request_body.api_key}),
        )
        db.add(credential)
        db.commit()
        db.refresh(credential)

        return metadata_response(credential)

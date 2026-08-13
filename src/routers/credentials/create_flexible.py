"""
Create flexible credential endpoint.
"""
from fastapi import APIRouter, status, Request
from src.deps import db_dependency, jwt_dependency, account_id_from_claims, ensure_account
from src.db.models import Credential
from ._shared import invalid_data_as_400, metadata_response
from .encryption import encrypt_credential_data
from .models import CreateFlexibleCredentialRequest, CredentialResponse
from src.rate_limit import limiter

router = APIRouter()


@router.post("/flexible", status_code=status.HTTP_201_CREATED, response_model=CredentialResponse)
@limiter.limit("10/minute")
async def create_flexible_credential(
    request_body: CreateFlexibleCredentialRequest,
    db: db_dependency,
    jwt: jwt_dependency,
    request: Request
):
    """
    Create a new flexible credential for any type of service.

    Supports:
    - API keys: {"api_key": "sk-..."}
    - Database connections: {"host": "...", "port": 5432, "username": "...", "password": "...", "database": "..."}
    - OAuth: {"client_id": "...", "client_secret": "...", "access_token": "...", "refresh_token": "..."}
    - SSH keys: {"private_key": "-----BEGIN...", "passphrase": "..."}
    - Certificates: {"certificate": "...", "private_key": "..."}
    - AWS access key pairs: {"aws_access_key_id": "AKIA...", "aws_secret_access_key": "...", "aws_region": "...", "from_email": "..."}

    Example request:
    {
        "credential_type": "OPENAI_API_KEY",
        "auth_type": "api_key",
        "credential_name": "Production",
        "credential_data": {"api_key": "sk-abc123..."},
        "metadata": {"notes": "Main production key"}
    }
    """
    account_id = account_id_from_claims(jwt)
    ensure_account(db, account_id)

    with invalid_data_as_400(db, 'creating flexible credential'):
        credential = Credential(
            account_id=account_id,
            credential_type=request_body.credential_type,
            auth_type=request_body.auth_type,
            credential_name=request_body.credential_name,
            encrypted_data=encrypt_credential_data(request_body.credential_data),
            credential_metadata=request_body.metadata,
        )
        db.add(credential)
        db.commit()
        db.refresh(credential)

        return metadata_response(credential)

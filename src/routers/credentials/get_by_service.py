"""
Get credential by service name endpoint (legacy).
"""
from fastapi import APIRouter, Request
from src.deps import db_dependency, jwt_dependency, account_id_from_claims, ensure_account
from src.db.service_name import ServiceName
from ._shared import invalid_data_as_400, legacy_detail_response, owned_credential_or_404
from .models import CredentialDetailResponse
from src.rate_limit import limiter

router = APIRouter()


@router.get("/service/{service_name}", response_model=CredentialDetailResponse)
@limiter.limit("30/minute")
async def get_credential_by_service(
    service_name: ServiceName,
    db: db_dependency,
    jwt: jwt_dependency,
    request: Request
):
    """
    Get a credential by service/provider type (first match), including the decrypted API key.

    LEGACY ENDPOINT: Returns api_key field for backward compatibility.
    For full credential data, use GET /service/{service_name}/full
    """
    account_id = account_id_from_claims(jwt)
    ensure_account(db, account_id)

    credential = owned_credential_or_404(db, account_id, service_name=service_name)
    with invalid_data_as_400(db, 'retrieving credential by service'):
        return legacy_detail_response(credential)

"""
Get credential by service name with full decrypted data endpoint.
"""
from fastapi import APIRouter, Request
from src.deps import db_dependency, jwt_dependency, account_id_from_claims, ensure_account
from src.db.service_name import ServiceName
from ._shared import flexible_detail_response, invalid_data_as_400, owned_credential_or_404
from .models import FlexibleCredentialDetailResponse
from src.rate_limit import limiter

router = APIRouter()


@router.get("/service/{service_name}/full", response_model=FlexibleCredentialDetailResponse)
@limiter.limit("30/minute")
async def get_credential_by_service_full(
    service_name: ServiceName,
    db: db_dependency,
    jwt: jwt_dependency,
    request: Request
):
    """
    Get a credential by service/provider type (first match) with full decrypted data.
    """
    account_id = account_id_from_claims(jwt)
    ensure_account(db, account_id)

    credential = owned_credential_or_404(db, account_id, service_name=service_name)
    with invalid_data_as_400(db, 'retrieving full credential by service'):
        return flexible_detail_response(credential)

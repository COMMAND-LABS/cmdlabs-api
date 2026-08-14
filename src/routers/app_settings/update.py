"""
Update app settings endpoint.

Partial upsert: creates the (account, org) row on first save, applies only the
fields the client sent, and treats an explicit null as "clear this field".
"""
from fastapi import APIRouter, HTTPException, Request, status

from src.deps import db_dependency, jwt_dependency, org_dependency, account_id_from_claims, ensure_account
from src.db.models import AppSettings
from src.services.agent_access import can_access_agent
from src.rate_limit import limiter
from .models import AppSettingsResponse, UpdateAppSettingsRequest

router = APIRouter()


@router.put("/", response_model=AppSettingsResponse)
@limiter.limit("30/minute")
async def update_app_settings(
    request_body: UpdateAppSettingsRequest,
    db: db_dependency,
    jwt: jwt_dependency,
    org: org_dependency,
    request: Request
):
    """
    Update the authenticated user's app settings for the current org.

    Updatable fields:
    - default_agent_id: the agent Agent Chat opens with (null clears it)
    - elevenlabs_voice_id: the TTS voice (null clears it)
    """
    account_id = account_id_from_claims(jwt)
    ensure_account(db, account_id)

    if not request_body.model_fields_set:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one field must be provided for update",
        )

    if request_body.default_agent_id is not None and not can_access_agent(
        db, account_id, request_body.default_agent_id, org_id=org.org_id
    ):
        # 404, not 403, to avoid leaking the existence of other orgs' agents.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found",
        )

    settings = db.query(AppSettings).filter(
        AppSettings.account_id == account_id,
        AppSettings.org_id == org.org_id,
    ).first()

    if settings is None:
        settings = AppSettings(account_id=account_id, org_id=org.org_id)
        db.add(settings)

    if 'default_agent_id' in request_body.model_fields_set:
        settings.default_agent_id = request_body.default_agent_id
    if 'elevenlabs_voice_id' in request_body.model_fields_set:
        settings.elevenlabs_voice_id = request_body.elevenlabs_voice_id

    db.commit()
    db.refresh(settings)

    return AppSettingsResponse(
        default_agent_id=settings.default_agent_id,
        elevenlabs_voice_id=settings.elevenlabs_voice_id,
    )

"""
Get app settings endpoint.

Returns the caller's preferences for the org they are acting in. A caller who
has never saved anything gets an all-null response rather than a 404 — "no
settings yet" and "everything at its default" are the same state to a client.
"""
from fastapi import APIRouter, Request

from src.deps import db_dependency, jwt_dependency, org_dependency, account_id_from_claims, ensure_account
from src.db.models import AppSettings
from src.services.agent_access import can_access_agent
from src.rate_limit import limiter
from .models import AppSettingsResponse

router = APIRouter()


@router.get("/", response_model=AppSettingsResponse)
@limiter.limit("30/minute")
async def get_app_settings(
    db: db_dependency,
    jwt: jwt_dependency,
    org: org_dependency,
    request: Request
):
    """
    Get the authenticated user's app settings for the current org.
    """
    account_id = account_id_from_claims(jwt)
    ensure_account(db, account_id)

    settings = db.query(AppSettings).filter(
        AppSettings.account_id == account_id,
        AppSettings.org_id == org.org_id,
    ).first()

    if settings is None:
        return AppSettingsResponse()

    # Deletion clears the FK (SET NULL), but an agent that merely became
    # unreachable (unshared, visibility narrowed) still has a row. Mask it
    # rather than serving a default the caller can no longer open.
    default_agent_id = settings.default_agent_id
    if default_agent_id is not None and not can_access_agent(
        db, account_id, default_agent_id, org_id=org.org_id
    ):
        default_agent_id = None

    return AppSettingsResponse(
        default_agent_id=default_agent_id,
        elevenlabs_voice_id=settings.elevenlabs_voice_id,
    )

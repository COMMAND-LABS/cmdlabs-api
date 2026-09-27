"""The campaign lookup every email-campaign endpoint repeated.

Campaigns are looked up by (id, account_id): the account_id filter IS the
ownership check. Used by the CRUD, send and ratings routes here and by
POST /api/emails/send.
"""
from fastapi import HTTPException
from sqlalchemy.orm import Session

from src.db.models import EmailCampaign


def owned_campaign_or_404(
    db: Session,
    campaign_id: int,
    account_id: int,
    *,
    detail: str = "Email campaign not found",
) -> EmailCampaign:
    """The caller's campaign by id, or 404 with ``detail``."""
    campaign = db.query(EmailCampaign).filter(
        EmailCampaign.id == campaign_id,
        EmailCampaign.account_id == account_id,
    ).first()
    if not campaign:
        raise HTTPException(status_code=404, detail=detail)
    return campaign

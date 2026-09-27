"""Model A send primitive.

POST /api/emails/send — self-contained, per-recipient send. Renders an immutable
template against request ``variables`` + contact personalization + system tokens,
delivers via SES, and writes the idempotent ``email_events`` ledger. The campaign
is a correlation tag only: it stores no template or content, so there is nothing
to diverge from. Re-running a send loop is safe — a confirmed (campaign, contact)
send no-ops as ``skipped_duplicate``.
"""
import logging

from fastapi import APIRouter, HTTPException, Request

from src.deps import org_dependency, db_dependency, auth_dependency, account_id_from_claims
from src.services.org_scope import get_scoped_or_404
from src.db.models import Contact
from src.routers.email_campaigns._shared import owned_campaign_or_404
from src.routers.email_templates._shared import owned_template_or_404
from src.rate_limit import limiter
from src.services.email_dispatch import (
    CredentialError,
    MissingVariablesError,
    SesSendError,
    dispatch_one,
    load_ses_credential,
)

from .models import SendEmailRequest, SendEmailResponse

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/send", response_model=SendEmailResponse)
@limiter.limit("120/minute")
async def send_email(
    body: SendEmailRequest,
    db: db_dependency,
    auth: auth_dependency,
    org: org_dependency,
    request: Request,
):
    account_id = account_id_from_claims(auth)

    # ── Campaign (correlation tag) must exist ────────────────────────────────
    campaign = owned_campaign_or_404(db, body.campaign_id, account_id)

    # ── Template (immutable shape) ───────────────────────────────────────────
    template = owned_template_or_404(db, body.template_id, account_id)

    # ── Resolve recipient ────────────────────────────────────────────────────
    contact = None
    if body.recipient.contact_id is not None:
        contact = get_scoped_or_404(db, Contact, body.recipient.contact_id, org)
        to_email = contact.email
    else:
        to_email = body.recipient.email

    if not to_email:
        raise HTTPException(status_code=422, detail="Recipient has no email address")

    # ── Credential ───────────────────────────────────────────────────────────
    try:
        credential_cfg = load_ses_credential(db, account_id, body.credential_id)
    except CredentialError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)

    # ── Dispatch (idempotent ledger write) ───────────────────────────────────
    try:
        result = dispatch_one(
            db,
            account_id=account_id,
            campaign_id=campaign.id,
            template=template,
            contact=contact,
            to_email=to_email,
            request_vars=body.variables,
            credential_cfg=credential_cfg,
            credential_id=body.credential_id,
            dry_run=body.dry_run,
        )
    except MissingVariablesError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Required template variables are unresolved.",
                "missing": exc.missing,
            },
        )
    except SesSendError as exc:
        raise HTTPException(status_code=502, detail=f"SES send failed: {exc}")

    return SendEmailResponse(**result)

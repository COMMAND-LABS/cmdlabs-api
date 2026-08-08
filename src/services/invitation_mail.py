"""
Where an invitation's accept link is built, and the one place that queues it.

SEPARATE FROM services/invitations ON PURPOSE. That module decides who may
join what, and is pure database work with no idea the web exists. This one
knows the front end's URL shape and how mail is sent. Keeping them apart is
what lets the invitation rules be tested without a mail client and without an
APP_BASE_URL.

SEPARATE FROM THE ROUTERS for a duller reason: invite and resend must produce
the same email, and the fastest way for them to stop doing that is to build the
URL twice.
"""
import logging
import os
from urllib.parse import quote

from fastapi import BackgroundTasks
from sqlalchemy.orm import Session

from src.config import roles_registry as roles
from src.db.models import Account, Organization, OrganizationInvitation
from src.routers.auth.background_tasks.send_org_invitation_email_ses import (
    send_org_invitation_email_ses,
)
from src.services import invitations

logger = logging.getLogger(__name__)

# Same default as routers/billing/checkout, and for the same reason: the API
# decides its own outbound URLs. A caller-supplied one would be an open
# redirect with an invitation wrapped around it.
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:3001")

INVITE_PATH = "/invite"


def accept_url(token: str) -> str:
    return f"{APP_BASE_URL}{INVITE_PATH}/{quote(token, safe='')}"


def send_invitation(db: Session, background_tasks: BackgroundTasks,
                    invitation: OrganizationInvitation, token: str) -> None:
    """Queue the invitation email for `invitation`, using the RAW token.

    The token cannot be recovered from the row — only its hash is stored — so
    it is passed in by whoever just minted it. That is the awkward-looking part
    of the signature and it is the honest one: if this function could look the
    token up, so could anyone with database access.

    Queued as a background task, so a slow or failing SES call cannot make an
    invite look like it failed when the row was written. The sender swallows
    and logs its own errors for the same reason.
    """
    org_name = (db.query(Organization.name)
                  .filter(Organization.id == invitation.org_id).scalar())
    inviter_email = None
    if invitation.invited_by_account_id is not None:
        inviter_email = (db.query(Account.email)
                           .filter(Account.id == invitation.invited_by_account_id)
                           .scalar())

    background_tasks.add_task(
        send_org_invitation_email_ses,
        invitation.email,
        org_name or "an organization",
        inviter_email,
        roles.label(invitation.role),
        accept_url(token),
        invitations.TTL_DAYS,
    )

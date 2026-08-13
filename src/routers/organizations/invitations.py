"""
The invitee's side of an invitation: reading one, and answering it.

Everything an OWNER does to an invitation — creating, resending, revoking —
lives in members.py with the rest of the membership administration, because it
is administration and it is owner-gated. What is here is the other end: routes
the person being invited calls about an invitation addressed to them.

THE SPLIT ON AUTHENTICATION, WHICH IS THE WHOLE DESIGN
------------------------------------------------------
    GET  /invitations/{token}          NO AUTH. The accept page must render for
                                       somebody who has never signed in — that
                                       is who most invitations are for. Returns
                                       the org name, who invited them, and the
                                       role. Nothing about the org's data, its
                                       members, its size or its plan.

    GET  /invitations/mine             AUTH. Every live invitation addressed to
                                       the caller's own email.

    POST /invitations/{id}/accept      AUTH, and the session's email must MATCH
    POST /invitations/{id}/decline     the invitation.

WHY ANSWERING IS KEYED ON THE ID AND NOT THE TOKEN
--------------------------------------------------
Because for an authenticated caller the token is doing no work. Accepting
requires a signed-in session whose email equals the invitation's, so the token
cannot be what authorizes the join — a forwarded link gets a stranger a page
and nothing else. Once that check exists, the token's only remaining job is to
say WHICH invitation, and an id says that just as well while being something
the server can hand back.

It also has to be this way: only the token's sha256 is stored (see
services/invitations), so /mine could not return raw tokens even if it wanted
to. An endpoint that answers "what am I invited to" has to name invitations by
something recoverable, and the id is it. Exposing the id costs nothing — acting
on one still requires proving you read the invited inbox.

WHY /mine EXISTS AT ALL
-----------------------
Because an invitee who signs in WITHOUT clicking the link has nowhere to be:
they hold no membership yet, and services/organizations.ensure_membership
deliberately does not mint them a personal workspace while an invitation is
outstanding (see there). Every org-scoped request they make is a 403 until they
answer. This route is how the dashboard finds the invitation and sends them to
it instead of showing them an empty product.
"""
import logging
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.config import roles_registry as roles
from src.db.models import Account, Organization, OrganizationInvitation
from src.deps import (
    account_id_from_claims,
    db_dependency,
    ensure_account,
    get_current_user_or_api_key,
    get_db,
)
from src.rate_limit import limiter
from src.services import invitations, organizations

logger = logging.getLogger(__name__)

router = APIRouter()


class InvitationView(BaseModel):
    """What an invitation looks like to the person holding its token.

    DELIBERATELY THIN. The lookup below is served without authentication, so
    every field here is one that already appears in the invitation email the
    recipient is reading. The org's member list, plan, size and module ceiling
    are all absent and must stay absent: a public endpoint keyed on a secret
    should not become a way to read anything about a tenant.
    """
    id: int
    email: str
    org_name: str
    role: str
    role_label: str
    invited_by: Optional[str] = None
    expires_at: Optional[datetime] = None
    # 'pending' | 'accepted' | 'declined' | 'revoked' | 'expired'. Computed, not
    # stored — see services/invitations. Distinguishes a dead invitation from a
    # made-up token, so the page can say what happened rather than "not found".
    status: str


class MyInvitationsResponse(BaseModel):
    invitations: List[InvitationView]


class AcceptResponse(BaseModel):
    org_id: int
    org_name: str
    role: str


def _status_of(invitation: OrganizationInvitation) -> str:
    if invitation.accepted_at:
        return "accepted"
    if invitation.declined_at:
        return "declined"
    if invitation.revoked_at:
        return "revoked"
    return "pending" if invitations.is_live(invitation) else "expired"


def _view(db: Session, invitation: OrganizationInvitation) -> InvitationView:
    org_name = (db.query(Organization.name)
                  .filter(Organization.id == invitation.org_id).scalar())
    inviter = None
    if invitation.invited_by_account_id is not None:
        inviter = (db.query(Account.email)
                     .filter(Account.id == invitation.invited_by_account_id)
                     .scalar())
    return InvitationView(
        id=invitation.id,
        email=invitation.email,
        org_name=org_name or "an organization",
        role=invitation.role,
        role_label=roles.label(invitation.role),
        invited_by=inviter,
        expires_at=invitation.expires_at,
        status=_status_of(invitation),
    )


# Registered BEFORE /{token}. "mine" is a perfectly good token as far as the
# router is concerned, so the literal has to be matched first or it never wins.
@router.get("/invitations/mine", response_model=MyInvitationsResponse)
@limiter.limit("60/minute")
async def my_invitations(
    request: Request,
    db: Session = Depends(get_db),
    auth: dict = Depends(get_current_user_or_api_key),
):
    """Every live invitation addressed to the signed-in account.

    NOT org-scoped, and cannot be: the caller may hold no membership at all —
    that is the state this route exists to resolve. The filter on their own
    email IS the whole boundary here, the same shape as mine.py, so nothing may
    be added below that is a fact about the ORG rather than about the
    invitation they were sent.
    """
    account_id = account_id_from_claims(auth)
    account = ensure_account(db, account_id)
    rows = invitations.pending_for_email(db, account.email)
    return MyInvitationsResponse(
        invitations=[_view(db, i) for i in rows],
    )


@router.get("/invitations/{token}", response_model=InvitationView)
@limiter.limit("60/minute")
async def read_invitation(token: str, db: db_dependency, request: Request):
    """What this token names. NO AUTHENTICATION — see the module header.

    Answers for dead invitations too, with a `status` saying which kind of dead.
    A page that could not tell "already accepted" from "never existed" would
    send people to support for the most ordinary outcome there is.
    """
    invitation = invitations.find_by_token(db, token)
    if invitation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="This invitation link is not valid.")
    return _view(db, invitation)


def _answerable(db: Session, invitation_id: int, auth: dict):
    """Load a live invitation the CALLER is entitled to answer.

    The three failures are deliberately different statuses, because they are
    different mistakes:

      404 no such invitation — nothing to say.

      403 wrong account — signed in, but as somebody else. Recoverable, and the
          message names the address to use. NOT a 404: pretending the
          invitation does not exist would be a lie to somebody holding a valid
          link, and would send them to support instead of the sign-out button.

      409 not live — the invitation is real and the answer is simply no longer
          available: already accepted, declined, revoked, or expired.
    """
    account_id = account_id_from_claims(auth)
    account = ensure_account(db, account_id)

    invitation = (db.query(OrganizationInvitation)
                    .filter(OrganizationInvitation.id == invitation_id)
                    .first())
    if invitation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="This invitation link is not valid.")

    if (account.email or "").strip().lower() != invitation.email:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"This invitation was sent to {invitation.email}. "
                   f"Sign in as that address to accept it.",
        )

    if not invitations.is_live(invitation):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This invitation is no longer open ({_status_of(invitation)}).",
        )

    return invitation, account


@router.post("/invitations/{invitation_id}/accept", response_model=AcceptResponse)
@limiter.limit("20/minute")
async def accept_invitation(
    invitation_id: int,
    request: Request,
    db: Session = Depends(get_db),
    auth: dict = Depends(get_current_user_or_api_key),
):
    """Join the org this invitation names.

    THE ONLY PLACE A MEMBERSHIP IS CREATED FROM AN INVITE. Inviting writes no
    membership row; this does. See services/invitations.accept.
    """
    invitation, account = _answerable(db, invitation_id, auth)
    org_id = invitation.org_id
    member = invitations.accept(db, invitation, account)
    role = member.role
    db.commit()

    org_name = (db.query(Organization.name)
                  .filter(Organization.id == org_id).scalar())
    logger.info("[ORG] account %s accepted their invitation to org %s as %s",
                account.id, org_id, role)
    return AcceptResponse(org_id=org_id,
                          org_name=org_name or "an organization",
                          role=role)


@router.post("/invitations/{invitation_id}/decline",
             status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("20/minute")
async def decline_invitation(
    invitation_id: int,
    request: Request,
    db: Session = Depends(get_db),
    auth: dict = Depends(get_current_user_or_api_key),
):
    """Refuse the invitation, and make sure the decliner still has somewhere to be.

    THE SECOND HALF IS NOT OPTIONAL. ensure_membership does not create a
    personal workspace for an account with an outstanding invitation — that is
    the fix for the org-per-invitee bug — so somebody who signed in only to say
    no would be left with no org and a 403 on every screen.

    Ordered so the refusal is committed FIRST: ensure_membership reads the
    pending state, and asking it to act while this invitation is still live
    would have it correctly decline to create anything.
    """
    invitation, account = _answerable(db, invitation_id, auth)
    org_id = invitation.org_id
    invitations.decline(db, invitation, account)
    db.commit()

    organizations.ensure_membership(db, account)
    logger.info("[ORG] account %s declined their invitation to org %s",
                account.id, org_id)

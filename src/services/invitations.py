"""
Invitations: the offer of a membership, and the answer to it.

THE ONE RULE THIS FILE EXISTS TO ENFORCE
----------------------------------------
A membership row is written on ACCEPT and at no earlier moment. Sending an
invitation gives nobody access to anything — not a row, not a module, not a
place in a member list. Everything here is arranged around keeping that true,
because the moment somewhere writes the membership early, the invitation stops
being consent and becomes paperwork attached to something that already
happened.

WHAT IS AND IS NOT A SECRET
---------------------------
The raw token is a secret and lives in exactly two places: the invitation email
and the URL the recipient clicks. What is STORED is its sha256, the same
treatment accounts.login_otp gets, so a database leak hands over no live
invitations.

But the token is deliberately NOT the authorization. Accepting requires a
signed-in session whose email matches the invitation, so a forwarded link
cannot put a stranger in somebody's org — it only shows them a page saying who
was invited. That is why the lookup below can be served unauthenticated (the
accept page must render before sign-in) while accept and decline cannot.

The security argument in routers/organizations/members.py — that a token is a
second secret sent to an inbox that already controls access — is correct, and
this file does not pretend otherwise. The token identifies WHICH invitation is
being answered. The OTP still decides who is answering.

EXPIRY IS A READ-TIME QUESTION
------------------------------
There is no sweeper and no `status` column. An invitation is live when it has
no accepted_at, declined_at or revoked_at and its expires_at is in the future,
which is one predicate every reader shares (`_live` / `PENDING_SQL`). A stored
status would be a cache of a comparison against the clock, and it would be
wrong for exactly as long as nobody ran the sweeper.
"""
import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from src.db.models import Account, OrganizationInvitation, OrganizationMember
from src.services import audit

logger = logging.getLogger(__name__)

# Long enough that guessing is not a strategy, short enough to survive an email
# client that wraps long URLs. 32 bytes of urlsafe base64 is 43 characters.
TOKEN_BYTES = 32

# Two weeks. Long enough to survive a holiday, short enough that a forwarded
# link found in an old mailbox is usually already dead. Re-inviting refreshes
# it, so the cost of being wrong is one click by the owner.
TTL_DAYS = 14


def _hash(token: str) -> str:
    """sha256 hex. Same treatment as accounts.login_otp — see the header."""
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _live_filter():
    """The predicate for "this invitation is still awaiting an answer".

    One definition, used by every reader here. Answered-ness and expiry are
    both read at query time; see the header for why neither is stored.
    """
    return and_(
        OrganizationInvitation.accepted_at.is_(None),
        OrganizationInvitation.declined_at.is_(None),
        OrganizationInvitation.revoked_at.is_(None),
        OrganizationInvitation.expires_at > _now(),
    )


def _unanswered_filter():
    """Answered-ness ALONE, ignoring the clock.

    Distinct from _live_filter on purpose. The partial unique index covers
    unanswered rows whether or not they have expired, so the invite path has to
    find an expired-but-unanswered row in order to refresh it rather than
    colliding with it. Readers who are deciding whether somebody may act want
    _live_filter; writers reconciling with the index want this.
    """
    return and_(
        OrganizationInvitation.accepted_at.is_(None),
        OrganizationInvitation.declined_at.is_(None),
        OrganizationInvitation.revoked_at.is_(None),
    )


def is_live(invitation: OrganizationInvitation) -> bool:
    """The same question as _live_filter, asked of a row already in hand."""
    if invitation is None:
        return False
    if invitation.accepted_at or invitation.declined_at or invitation.revoked_at:
        return False
    expires = invitation.expires_at
    # Rows read back from Postgres are timezone-aware; ones just constructed in
    # a test may not be. Treat a naive timestamp as UTC rather than raising.
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires > _now()


def find_by_token(db: Session, token: str) -> OrganizationInvitation | None:
    """The invitation this token names, live or not.

    Returns answered and expired rows too, deliberately: the accept page has to
    tell somebody "this invitation was already accepted" or "this expired on
    the 4th", and it cannot do that if a dead token is indistinguishable from a
    made-up one. Callers gate on is_live() before acting.
    """
    if not token:
        return None
    return (db.query(OrganizationInvitation)
              .filter(OrganizationInvitation.token_hash == _hash(token))
              .first())


def pending_for_email(db: Session, email: str) -> list[OrganizationInvitation]:
    """Every live invitation addressed to `email`, oldest first."""
    if not email:
        return []
    return (db.query(OrganizationInvitation)
              .filter(OrganizationInvitation.email == email.strip().lower())
              .filter(_live_filter())
              .order_by(OrganizationInvitation.created_at.asc())
              .all())


def has_pending_for_email(db: Session, email: str) -> bool:
    """Whether this address has been offered a membership it has not answered.

    Read by services/organizations.ensure_membership, which uses it to decide
    NOT to mint a personal workspace for somebody who already has somewhere to
    go. See there for why that matters.
    """
    if not email:
        return False
    return db.query(
        db.query(OrganizationInvitation)
          .filter(OrganizationInvitation.email == email.strip().lower())
          .filter(_live_filter())
          .exists()
    ).scalar()


def pending_for_org(db: Session, org_id: int) -> list[OrganizationInvitation]:
    """Every live invitation this org has outstanding, newest first.

    What the members screen lists under the roster. Expired rows are excluded:
    an owner looking at "who is about to join" is not helped by an offer that
    can no longer be accepted, and the fix for one is to invite again, which
    refreshes the row in place.
    """
    return (db.query(OrganizationInvitation)
              .filter(OrganizationInvitation.org_id == org_id)
              .filter(_live_filter())
              .order_by(OrganizationInvitation.created_at.desc())
              .all())


def issue(db: Session, *, org_id: int, email: str, role: str,
          invited_by_account_id: int | None) -> tuple[OrganizationInvitation, str]:
    """Create or REFRESH the invitation for (org, email). Caller commits.

    Returns the row and the RAW token, which the caller must put in the email
    and then forget — it is unrecoverable from the row.

    Refreshing rather than inserting a second row is the point of the partial
    unique index: two live invitations to one address is how somebody ends up
    clicking the dead one. A re-invite mints a new token (so the previous email
    stops working, which is what "resend" should mean), extends the expiry, and
    may change the role — an owner who mis-picked the role fixes it by inviting
    again, not by revoking first.

    Matches on UNANSWERED rather than live rows, because that is what the index
    covers: an expired-but-unanswered invitation must be refreshed here rather
    than collided with.
    """
    email = (email or "").strip().lower()
    token = secrets.token_urlsafe(TOKEN_BYTES)

    invitation = (db.query(OrganizationInvitation)
                    .filter(OrganizationInvitation.org_id == org_id,
                            OrganizationInvitation.email == email)
                    .filter(_unanswered_filter())
                    .first())

    if invitation is None:
        invitation = OrganizationInvitation(org_id=org_id, email=email)
        db.add(invitation)

    invitation.role = role
    invitation.token_hash = _hash(token)
    invitation.invited_by_account_id = invited_by_account_id
    invitation.expires_at = _now() + timedelta(days=TTL_DAYS)
    db.flush()
    return invitation, token


def accept(db: Session, invitation: OrganizationInvitation,
           account: Account) -> OrganizationMember:
    """Turn a live invitation into a membership. Caller commits.

    THE CALLER HAS ALREADY CHECKED that the invitation is live and that
    `account` is the one it names — those are authorization decisions and they
    belong in the router with the HTTP status codes that express them. What is
    here is the part that must not vary between callers.

    Pinning the plan is not incidental. This org is becoming a TEAM, and
    pin_plan freezes it on the plan its owner is on now, so the people joining
    do not lose modules the day the owner's card expires. It happens on accept
    rather than on invite because an unanswered invitation has not made
    anything a team yet.
    """
    # Imported here rather than at module scope: services/organizations imports
    # this module for has_pending_for_email, and a top-level import in both
    # directions is a cycle.
    from src.services.organizations import GRANTED_BY_GRANT, pin_plan
    from src.db.models import Organization

    existing = (db.query(OrganizationMember)
                  .filter(OrganizationMember.org_id == invitation.org_id,
                          OrganizationMember.account_id == account.id)
                  .first())

    if existing is None:
        organization = (db.query(Organization)
                          .filter(Organization.id == invitation.org_id).one())
        pin_plan(db, organization)

        existing = OrganizationMember(
            org_id=invitation.org_id,
            account_id=account.id,
            role=invitation.role,
            # Never 'subscription'. Their access comes from this org, so a
            # Stripe event on their personal account must never revoke it.
            granted_by=GRANTED_BY_GRANT,
        )
        db.add(existing)

        # The ordinary member.add, written by accepting rather than by
        # inviting. See services/audit: the log records access being GAINED,
        # and until now nothing had been.
        audit.record_membership(
            db, event_type=audit.MEMBER_ADD, org_id=invitation.org_id,
            account_id=account.id, role=invitation.role,
            actor_account_id=account.id,
        )

    # Send them into the org they just joined. Somebody who already had a home
    # keeps it and finds this org in the switcher — being invited somewhere is
    # not a reason to move their dashboard.
    if account.default_org_id is None:
        account.default_org_id = invitation.org_id

    invitation.accepted_at = _now()
    db.flush()
    return existing


def decline(db: Session, invitation: OrganizationInvitation,
            account: Account | None) -> None:
    """Refuse an invitation. Caller commits.

    Recorded rather than deleted. "They were invited and said no" is a
    different fact from "they were never invited", and an owner re-inviting
    somebody who declined deserves to know which one they are doing.

    Does NOT give the decliner a workspace — that is the router's job, because
    it is the caller that knows whether this decline left them with nowhere to
    be. See routers/organizations/invitations.py.
    """
    invitation.declined_at = _now()
    audit.record_invitation(
        db, event_type=audit.MEMBER_INVITE_DECLINE, org_id=invitation.org_id,
        email=invitation.email, role=invitation.role,
        actor_account_id=account.id if account else None,
    )
    db.flush()


def revoke(db: Session, invitation: OrganizationInvitation,
           actor_account_id: int | None) -> None:
    """Withdraw an invitation before it is answered. Caller commits.

    Takes effect on the invitee's very next click: accept re-reads the row and
    gates on is_live(), so there is no token to expire and no email to unsend.
    """
    invitation.revoked_at = _now()
    audit.record_invitation(
        db, event_type=audit.MEMBER_INVITE_REVOKE, org_id=invitation.org_id,
        email=invitation.email, role=invitation.role,
        actor_account_id=actor_account_id,
    )
    db.flush()

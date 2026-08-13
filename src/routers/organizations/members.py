"""
Members of an organization: who is in it, in what role, and who put them there.

THE INVITE, AND WHY IT NOW HAS A TOKEN
--------------------------------------
This section used to be titled "why it has no token", and argued that adding
someone by email should write their membership immediately and mail them the
ordinary sign-in code. The security half of that argument was right and is
unchanged: the platform authenticates by OTP, so reaching an org already
requires controlling the invitee's inbox, and a token is a second secret sent
to the same place. Nothing here is safer than it was.

It was revisited for the reason the old note itself gave:

    "What that DOES cost is consent: an account that already exists is added
    immediately, without being asked. That is the right trade for colleagues
    you already work with and the wrong one for strangers, and it is the thing
    to revisit when orgs start inviting people they have not met."

So inviting now writes an organization_invitations row and mails an INVITATION.
The membership is written when the invitee accepts, and at no earlier moment —
see services/invitations, which exists to keep that true.

The second thing it fixed is what the invitee received. With no invitation
there was no invitation email, so an invite sent a bare eight-digit sign-in
code with no sender, no org and no reason. Correct as a credential, useless as
a message. There is a sender for this now:
auth/background_tasks/send_org_invitation_email_ses.

A member is added with granted_by='grant'. Their access comes from the ORG's
standing, never from a personal subscription they were never asked to buy, and
no Stripe webhook will ever touch their row.

NO ACCOUNT IS CREATED BY INVITING. It used to be: an invite INSERTed an Account
for an address that had proved nothing, so a typo left a permanent account row
nobody controlled. An invitation names an ADDRESS, and the account is created
by /request-code when its owner shows up.

NO NAMING STEP
--------------
Inviting somebody used to require claiming a permanent public slug first, on
the reasoning that a team is a thing with a name. Organizations no longer have
slugs — an id identifies them everywhere — so the gate is gone and inviting is
one step. The DISPLAY name is still editable, and now it is the only name there
is.
"""
import logging
import re
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator

from src.config import roles_registry as roles
from src.db.models import Account, Organization, OrganizationMember
from src.deps import db_dependency, named_org_dependency, org_dependency
from src.rate_limit import limiter
from src.services import audit, invitations
from src.services.invitation_mail import send_invitation

logger = logging.getLogger(__name__)

router = APIRouter()


class MemberResponse(BaseModel):
    account_id: int
    email: str
    # Their role in THIS org: 'manager' | 'community_member'. Inert when
    # is_owner is true — an owner bypasses roles entirely.
    role: str
    is_owner: bool
    # 'grant' | 'subscription'. Invited members are always granted: their
    # access rides on the org, not on a subscription they never bought.
    granted_by: str
    created_at: Optional[datetime] = None


class PendingInvitationResponse(BaseModel):
    """Somebody who has been asked and has not answered.

    Deliberately NOT folded into MemberResponse as a "pending member". They are
    not a member: no row, no access, nothing counted. A list that mixed the two
    would make "who can see this org's data?" — the question the roster exists
    to answer — require reading a status column to answer correctly.
    """
    id: int
    email: str
    role: str
    invited_by: Optional[str] = None
    created_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None


class MembersPageResponse(BaseModel):
    org_id: int
    org_name: str
    can_manage: bool
    members: List[MemberResponse]
    # Outstanding invitations, newest first. Everyone in the org sees them, on
    # the same reasoning that everyone sees the roster: who is about to be able
    # to read your org's data is not a secret from the people already in it.
    # Only an owner can act on them — that is `can_manage`.
    invitations: List[PendingInvitationResponse] = []
    # Role keys an invite may choose from, so the dropdown cannot offer one
    # this org does not have.
    role_keys: List[str]


# Deliberately not pydantic's EmailStr: that pulls in email-validator, which
# this service does not ship, and full RFC validation is not what protects
# anything here. An invite to a malformed address simply never reaches anyone —
# access requires reading the inbox.
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class InviteRequest(BaseModel):
    email: str
    role: str = Field(description="Which role the new member joins in")

    @field_validator("email")
    @classmethod
    def _looks_like_an_address(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if not EMAIL_PATTERN.match(v):
            raise ValueError("Enter a valid email address.")
        return v


class RenameOrgRequest(BaseModel):
    name: str = Field(description="Display name. The only name an org has.")

    @field_validator("name")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("Give the organization a name.")
        if len(v) > 255:
            raise ValueError("That name is too long (255 characters max).")
        return v


class UpdateMemberRequest(BaseModel):
    role: str


def _require_owner(org):
    """Only an owner shapes their org's membership.

    404 rather than 403, matching require_module: a
    member who cannot manage the org should not have its admin endpoints
    confirm they exist.
    """
    if not org.is_owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


def _load_org(db, org_id: int) -> Organization:
    return db.query(Organization).filter(Organization.id == org_id).one()


def _owner_account_id(db, org_id: int) -> int | None:
    """Who owns this org. One column, one answer.

    Replaced _owner_count(), which counted is_owner rows in order to ask "would
    removing this person leave the org ownerless?". That question had a
    plural-sounding answer only because ownership was stored per membership;
    an org names exactly one owner, so the check is now an equality.
    """
    return (db.query(Organization.owner_account_id)
              .filter(Organization.id == org_id).scalar())


def _members_payload(db, org) -> MembersPageResponse:
    """Everyone in ONE org, given an already-validated context.

    Takes an OrgContext, never a bare org id — the only way to hold one is to
    have passed the membership gate in deps._org_context_for.

    `can_manage` is org.is_owner, which describes THE ORG IN THE CONTEXT. That
    matters more here than it looks: this payload is served for orgs the caller
    is merely a member of, so the flag has to say "may I manage THIS one" and
    not "am I an owner somewhere". It is what the UI hides the invite and
    remove controls behind.
    """
    organization = _load_org(db, org.org_id)
    owner_id = organization.owner_account_id
    rows = (
        db.query(OrganizationMember, Account)
        .join(Account, Account.id == OrganizationMember.account_id)
        .filter(OrganizationMember.org_id == org.org_id)
        # Owner first, then alphabetical. Ordered against the org's owner
        # column rather than a per-row flag, which is now the only place
        # that knows.
        .order_by((OrganizationMember.account_id == owner_id).desc(),
                  Account.email.asc())
        .all()
    )
    pending = invitations.pending_for_org(db, org.org_id)
    inviter_emails = dict(
        db.query(Account.id, Account.email)
          .filter(Account.id.in_([i.invited_by_account_id for i in pending
                                  if i.invited_by_account_id] or [0])).all()
    )

    return MembersPageResponse(
        org_id=org.org_id,
        org_name=organization.name,
        can_manage=org.is_owner,
        invitations=[
            PendingInvitationResponse(
                id=i.id, email=i.email, role=i.role,
                invited_by=inviter_emails.get(i.invited_by_account_id),
                created_at=i.created_at, expires_at=i.expires_at,
            )
            for i in pending
        ],
        members=[
            MemberResponse(
                account_id=m.account_id, email=a.email, role=m.role,
                is_owner=(m.account_id == owner_id), granted_by=m.granted_by,
                created_at=m.created_at,
            )
            for m, a in rows
        ],
        # A constant, not a query: every org offers the same roles.
        role_keys=list(roles.ROLE_KEYS),
    )


@router.get("/members", response_model=MembersPageResponse)
@limiter.limit("60/minute")
async def list_members(db: db_dependency, org: org_dependency, request: Request):
    """Everyone in the caller's ACTIVE org.

    Readable by any member: knowing who your
    colleagues are is not privileged inside a team, and hiding it would make
    "who can see my contacts?" unanswerable from inside the product.
    """
    return _members_payload(db, org)


@router.get("/{org_id}/members", response_model=MembersPageResponse)
@limiter.limit("60/minute")
async def list_members_for_org(db: db_dependency, org: named_org_dependency,
                               request: Request):
    """The same roster for an org named in the PATH, active or not.

    NO OWNER GATE, deliberately — the same call the active-org route makes.
    A member may see who else is in an org they belong to, and belonging is
    exactly what named_org_dependency has already proven. Adding a gate here
    that the sibling route does not have would mean the same person sees their
    colleagues on one screen and not on another.

    There is no longer a tiers MATRIX for it to leak — what each role opens is
    a platform-wide constant in config/roles_registry, the same in every org and
    not a secret. `role_keys` below is the invite picker's option list.
    """
    return _members_payload(db, org)


def _rename(body: RenameOrgRequest, db, org) -> MembersPageResponse:
    """Change the display name of ONE already-validated org.

    The only name an org has, now that slugs are gone. It is a label — what
    members see in the switcher and what the audit log snapshots — not an
    identity, so renaming is free and the id is what anything durable points
    at.
    """
    _require_owner(org)
    organization = _load_org(db, org.org_id)

    before = organization.name
    if before != body.name:
        organization.name = body.name
        # Worth a log line: the audit trail snapshots names at write time so
        # entries survive a rename, which only reads correctly if the rename
        # itself is recorded. Otherwise history shows a name changing with
        # nothing saying when or by whom.
        audit.record_org_change(
            db, event_type=audit.ORG_RENAME, org_id=org.org_id,
            detail=f"{before!r} -> {body.name!r}",
            actor_account_id=org.account_id,
        )
    db.commit()
    # The refreshed roster, read straight from the payload builder rather
    # than by calling the list route — which is rate-limit decorated, so
    # invoking it here would charge a rename against the read budget too.
    return _members_payload(db, org)


@router.put("/name", response_model=MembersPageResponse)
@limiter.limit("30/minute")
async def rename_organization(
    body: RenameOrgRequest, db: db_dependency, org: org_dependency, request: Request,
):
    """Rename the caller's ACTIVE org."""
    return _rename(body, db, org)


@router.put("/{org_id}/name", response_model=MembersPageResponse)
@limiter.limit("30/minute")
async def rename_organization_by_id(
    body: RenameOrgRequest, db: db_dependency, org: named_org_dependency,
    request: Request,
):
    """The same rename, for an org named in the PATH. See invite_member_for_org
    for why naming the org relaxes nothing."""
    return _rename(body, db, org)


async def _invite(body: InviteRequest, db, org,
                  background_tasks: BackgroundTasks) -> PendingInvitationResponse:
    """Offer somebody a membership in ONE org, in a chosen role.

    Takes an already-validated OrgContext, never a bare org id — the same rule
    _members_payload follows, and the reason both mountings below are safe: the
    only way to hold a context is to have passed the membership gate in
    deps._org_context_for, and `org.is_owner` describes THAT org.

    WRITES NO MEMBERSHIP AND NO ACCOUNT. It writes an invitation and sends an
    email; the membership appears when the invitee accepts, and their account
    when they sign in. See this module's header for why that changed, and
    services/invitations for the rules.
    """
    _require_owner(org)
    _load_org(db, org.org_id)

    # The role must be one this platform defines. Without this an invite
    # could carry a typo and the member would resolve to no modules at all,
    # which looks like a permissions bug rather than a typo. The database
    # would refuse it too (ck_org_invitation_role) — this turns a 500 into
    # a 422 that says what was wrong.
    if not roles.is_valid(body.role):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="Unknown role.")

    email = body.email.strip().lower()

    # Already in? Then there is nothing to offer. Checked by ADDRESS via
    # the account, because an invitation names an address and a membership
    # names an account — this is the one place the two have to be lined up.
    account = db.query(Account).filter(Account.email == email).first()
    if account is not None:
        existing = (db.query(OrganizationMember)
                      .filter(OrganizationMember.org_id == org.org_id,
                              OrganizationMember.account_id == account.id)
                      .first())
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="They are already in this organization.")

    # Refreshes an unanswered invitation in place rather than adding a
    # second — see services/invitations.issue. So "invite again" is also
    # how an owner fixes a mis-picked role or revives an expired offer.
    invitation, token = invitations.issue(
        db, org_id=org.org_id, email=email, role=body.role,
        invited_by_account_id=org.account_id,
    )

    # NOT pin_plan. The org is not a team until somebody actually joins,
    # and pinning on the offer would freeze a solo owner's plan because
    # they typed an address once. It happens on accept instead — see
    # services/invitations.accept.

    audit.record_invitation(
        db, event_type=audit.MEMBER_INVITE, org_id=org.org_id,
        email=email, role=body.role, actor_account_id=org.account_id,
    )
    db.commit()
    db.refresh(invitation)

    # After commit, so a failed write never produces an email about an
    # invitation that does not exist.
    send_invitation(db, background_tasks, invitation, token)

    logger.info("[ORG] account %s invited %s to org %s as %s",
                org.account_id, email, org.org_id, body.role)
    return PendingInvitationResponse(
        id=invitation.id, email=invitation.email, role=invitation.role,
        invited_by=_email_for(db, invitation.invited_by_account_id),
        created_at=invitation.created_at, expires_at=invitation.expires_at,
    )


def _email_for(db, account_id: int | None) -> str | None:
    if account_id is None:
        return None
    return db.query(Account.email).filter(Account.id == account_id).scalar()


def _revoke_invitation(invitation_id: int, db, org) -> None:
    """Withdraw an invitation this org sent. Owner only.

    Scoped to org.org_id as well as the id, so a revoke cannot reach into
    another org's invitations by guessing a number.
    """
    _require_owner(org)

    from src.db.models import OrganizationInvitation
    invitation = (db.query(OrganizationInvitation)
                    .filter(OrganizationInvitation.id == invitation_id,
                            OrganizationInvitation.org_id == org.org_id)
                    .first())
    if invitation is None or not invitations.is_live(invitation):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="No such invitation.")

    invitations.revoke(db, invitation, org.account_id)
    db.commit()
    logger.info("[ORG] account %s revoked the invitation for %s to org %s",
                org.account_id, invitation.email, org.org_id)


def _resend_invitation(invitation_id: int, db, org,
                       background_tasks: BackgroundTasks
                       ) -> PendingInvitationResponse:
    """Send the invitation email again, with a FRESH token.

    Not a re-send of the original message: issue() mints a new token and
    extends the expiry, which retires the link in the first email. That is the
    behaviour to want — two live tokens for one invitation is how somebody
    clicks the dead one — and it is why this goes through the same function
    inviting does rather than just calling the mailer again.
    """
    _require_owner(org)

    from src.db.models import OrganizationInvitation
    invitation = (db.query(OrganizationInvitation)
                    .filter(OrganizationInvitation.id == invitation_id,
                            OrganizationInvitation.org_id == org.org_id)
                    .first())
    if invitation is None or not invitations.is_live(invitation):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="No such invitation.")

    invitation, token = invitations.issue(
        db, org_id=org.org_id, email=invitation.email,
        role=invitation.role, invited_by_account_id=org.account_id,
    )
    audit.record_invitation(
        db, event_type=audit.MEMBER_INVITE_RESEND, org_id=org.org_id,
        email=invitation.email, role=invitation.role,
        actor_account_id=org.account_id,
    )
    db.commit()
    db.refresh(invitation)

    send_invitation(db, background_tasks, invitation, token)
    return PendingInvitationResponse(
        id=invitation.id, email=invitation.email, role=invitation.role,
        invited_by=_email_for(db, invitation.invited_by_account_id),
        created_at=invitation.created_at, expires_at=invitation.expires_at,
    )


@router.post("/members", status_code=status.HTTP_201_CREATED,
             response_model=PendingInvitationResponse)
@limiter.limit("20/minute")
async def invite_member(
    body: InviteRequest,
    db: db_dependency,
    org: org_dependency,
    request: Request,
    background_tasks: BackgroundTasks,
):
    """Invite somebody to the caller's ACTIVE org."""
    return await _invite(body, db, org, background_tasks)


@router.post("/{org_id}/members", status_code=status.HTTP_201_CREATED,
             response_model=PendingInvitationResponse)
@limiter.limit("20/minute")
async def invite_member_for_org(
    body: InviteRequest,
    db: db_dependency,
    org: named_org_dependency,
    request: Request,
    background_tasks: BackgroundTasks,
):
    """The same invite, for an org named in the PATH rather than the cookie.

    So an owner of several orgs can staff any of them from the account-settings
    Organizations page without switching the whole dashboard into it first —
    reading about an org should not move you into it, and neither should adding
    somebody to it.

    NOTHING IS RELAXED BY NAMING THE ORG. named_org_dependency re-checks
    membership against organization_members exactly as the cookie path does,
    and _require_owner inside _invite reads `is_owner` for THE ORG IN THE
    CONTEXT — which _org_context_for derives from that org's owner column, not
    from whether the caller owns something somewhere. A member of this org who
    owns a different one gets the same 404 here as they would there.
    """
    return await _invite(body, db, org, background_tasks)


@router.delete("/invitations/{invitation_id}",
               status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("30/minute")
async def revoke_invitation(
    invitation_id: int, db: db_dependency, org: org_dependency, request: Request,
):
    """Withdraw an invitation from the caller's ACTIVE org."""
    _revoke_invitation(invitation_id, db, org)


@router.delete("/{org_id}/invitations/{invitation_id}",
               status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("30/minute")
async def revoke_invitation_for_org(
    invitation_id: int, db: db_dependency, org: named_org_dependency,
    request: Request,
):
    """The same revoke, for an org named in the PATH. See invite_member_for_org
    for why naming the org relaxes nothing."""
    _revoke_invitation(invitation_id, db, org)


@router.post("/invitations/{invitation_id}/resend",
             response_model=PendingInvitationResponse)
@limiter.limit("20/minute")
async def resend_invitation(
    invitation_id: int, db: db_dependency, org: org_dependency,
    request: Request, background_tasks: BackgroundTasks,
):
    """Re-send an invitation from the caller's ACTIVE org, with a fresh token."""
    return _resend_invitation(invitation_id, db, org, background_tasks)


@router.post("/{org_id}/invitations/{invitation_id}/resend",
             response_model=PendingInvitationResponse)
@limiter.limit("20/minute")
async def resend_invitation_for_org(
    invitation_id: int, db: db_dependency, org: named_org_dependency,
    request: Request, background_tasks: BackgroundTasks,
):
    """The same resend, for an org named in the PATH. See invite_member_for_org
    for why naming the org relaxes nothing."""
    return _resend_invitation(invitation_id, db, org, background_tasks)


async def _update_role(account_id: int, body: UpdateMemberRequest,
                       db, org) -> MemberResponse:
    """Move a member to a different role, in ONE already-validated org."""
    _require_owner(org)

    if not roles.is_valid(body.role):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="Unknown role.")

    row = (db.query(OrganizationMember, Account)
             .join(Account, Account.id == OrganizationMember.account_id)
             .filter(OrganizationMember.org_id == org.org_id,
                     OrganizationMember.account_id == account_id).first())
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Not a member of this organization.")
    member, account = row

    if member.role != body.role:
        member.role = body.role
        audit.record_membership(
            db, event_type=audit.MEMBER_ROLE_CHANGE, org_id=org.org_id,
            account_id=account_id, role=body.role,
            actor_account_id=org.account_id,
        )
    db.commit()
    db.refresh(member)
    return MemberResponse(
        account_id=account_id, email=account.email, role=member.role,
        is_owner=(account_id == _owner_account_id(db, org.org_id)),
        granted_by=member.granted_by,
        created_at=member.created_at,
    )


@router.put("/members/{account_id}", response_model=MemberResponse)
@limiter.limit("30/minute")
async def update_member_role(
    account_id: int, body: UpdateMemberRequest,
    db: db_dependency, org: org_dependency, request: Request,
):
    """Move a member of the ACTIVE org to a different role."""
    return await _update_role(account_id, body, db, org)


@router.put("/{org_id}/members/{account_id}", response_model=MemberResponse)
@limiter.limit("30/minute")
async def update_member_role_for_org(
    account_id: int, body: UpdateMemberRequest,
    db: db_dependency, org: named_org_dependency, request: Request,
):
    """The same role change, for an org named in the PATH. See
    invite_member_for_org for why naming the org relaxes nothing."""
    return await _update_role(account_id, body, db, org)


def _remove(account_id: int, db, org) -> None:
    """Remove somebody from ONE already-validated org.

    Takes effect on their VERY NEXT request: get_org_context re-checks
    membership every time, so there is no token to revoke and no cache to
    invalidate.

    Their authored rows stay, still attributed to them. Deleting a departing
    colleague's contacts and notes would be an unrecoverable answer to a
    reversible problem, and account_id has been attribution rather than
    ownership since org scoping landed.
    """
    _require_owner(org)

    member = (db.query(OrganizationMember)
                .filter(OrganizationMember.org_id == org.org_id,
                        OrganizationMember.account_id == account_id).first())
    if not member:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Not a member of this organization.")

    # The owner cannot be removed, including by themselves. An org whose
    # owner is not in it has nobody who can invite, set roles, or hand it
    # over — it would need a super admin to become usable again, and it is
    # exactly the half-state this collapse exists to make unreachable.
    #
    # No longer "the LAST owner": an org names one owner, so there is never
    # a second one to fall back on. The count that used to be here only
    # looked plural because ownership was stored per membership row.
    if account_id == _owner_account_id(db, org.org_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This is the organization's owner and cannot be "
                   "removed.",
        )

    audit.record_membership(
        db, event_type=audit.MEMBER_REMOVE, org_id=org.org_id,
        account_id=account_id, role=member.role,
        actor_account_id=org.account_id,
    )
    db.delete(member)

    # Do not leave them pointed at an org they can no longer open: their
    # next request would 403 with no way back. Send them home to the
    # workspace they own.
    account = db.query(Account).filter(Account.id == account_id).first()
    if account and account.default_org_id == org.org_id:
        own = (db.query(Organization.id)
                 .filter(Organization.owner_account_id == account_id)
                 .first())
        account.default_org_id = own[0] if own else None

    db.commit()
    logger.info("[ORG] account %s removed %s from org %s",
                org.account_id, account_id, org.org_id)


@router.delete("/members/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("30/minute")
async def remove_member(
    account_id: int, db: db_dependency, org: org_dependency, request: Request,
):
    """Remove somebody from the caller's ACTIVE org."""
    _remove(account_id, db, org)


@router.delete("/{org_id}/members/{account_id}",
               status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("30/minute")
async def remove_member_for_org(
    account_id: int, db: db_dependency, org: named_org_dependency,
    request: Request,
):
    """The same removal, for an org named in the PATH. See
    invite_member_for_org for why naming the org relaxes nothing."""
    _remove(account_id, db, org)

"""
Members of an organization: who is in it, in what role, and who put them there.

THE INVITE, AND WHY IT HAS NO TOKEN
-----------------------------------
Adding someone by email creates their account if it does not exist and emails
them the ordinary sign-in code. There is no invite token, no accept step, and
no pending state — because none of them would add security. The platform
authenticates by OTP, so access already requires controlling that inbox; a
token would be a second secret sent to the same place, and a pending state
would be a row to expire, resend, and explain.

What that DOES cost is consent: an account that already exists is added
immediately, without being asked. That is the right trade for colleagues you
already work with and the wrong one for strangers, and it is the thing to
revisit when orgs start inviting people they have not met.

A member is added with granted_by='grant'. Their access comes from the ORG's
standing, never from a personal subscription they were never asked to buy, and
no Stripe webhook will ever touch their row.

NO NAMING STEP
--------------
Inviting somebody used to require claiming a permanent public slug first, on
the reasoning that a team is a thing with a name. Organizations no longer have
slugs — an id identifies them everywhere — so the gate is gone and inviting is
one step. The DISPLAY name is still editable, and now it is the only name there
is.
"""
import logging
import random
import re
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator

from src.config import roles_registry as roles
from src.db.models import Account, Organization, OrganizationMember
from src.deps import db_dependency, named_org_dependency, org_dependency
from src.rate_limit import limiter
from src.routers.auth.background_tasks.send_login_code_email_ses import (
    send_login_code_email_ses,
)
from src.services import audit
from src.services.organizations import GRANTED_BY_GRANT, pin_plan
from src.utils.errors import handle_db_error

logger = logging.getLogger(__name__)

router = APIRouter()

OTP_TTL_MINUTES = 10


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


class MembersPageResponse(BaseModel):
    org_id: int
    org_name: str
    can_manage: bool
    members: List[MemberResponse]
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
    return MembersPageResponse(
        org_id=org.org_id,
        org_name=organization.name,
        can_manage=org.is_owner,
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
    try:
        return _members_payload(db, org)
    except HTTPException:
        raise
    except Exception as e:
        raise handle_db_error(e, "[LIST MEMBERS]")


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
    try:
        return _members_payload(db, org)
    except HTTPException:
        raise
    except Exception as e:
        raise handle_db_error(e, "[LIST MEMBERS]")


def _rename(body: RenameOrgRequest, db, org) -> MembersPageResponse:
    """Change the display name of ONE already-validated org.

    The only name an org has, now that slugs are gone. It is a label — what
    members see in the switcher and what the audit log snapshots — not an
    identity, so renaming is free and the id is what anything durable points
    at.
    """
    try:
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
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise handle_db_error(e, "[RENAME ORG]")


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
                  background_tasks: BackgroundTasks) -> MemberResponse:
    """Add somebody to ONE org in a chosen role.

    Takes an already-validated OrgContext, never a bare org id — the same rule
    _members_payload follows, and the reason both mountings below are safe: the
    only way to hold a context is to have passed the membership gate in
    deps._org_context_for, and `org.is_owner` describes THAT org.
    """
    try:
        _require_owner(org)
        organization = _load_org(db, org.org_id)

        # The role must be one this platform defines. Without this an invite
        # could carry a typo and the member would resolve to no modules at all,
        # which looks like a permissions bug rather than a typo. The database
        # would refuse it too (ck_org_member_role) — this turns a 500 into a
        # 422 that says what was wrong.
        if not roles.is_valid(body.role):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail="Unknown role.")

        email = body.email.strip().lower()
        account = db.query(Account).filter(Account.email == email).first()

        created = False
        if account is None:
            account = Account(email=email)
            db.add(account)
            db.flush()
            created = True

        existing = (db.query(OrganizationMember)
                      .filter(OrganizationMember.org_id == org.org_id,
                              OrganizationMember.account_id == account.id).first())
        if existing:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                detail="They are already in this organization.")

        # This workspace is becoming a TEAM. Pin it to the plan the owner is
        # on, so the people about to join do not lose modules when the owner's
        # card expires. Idempotent for an org that is already pinned.
        pin_plan(db, organization)

        member = OrganizationMember(
            org_id=org.org_id,
            account_id=account.id,
            role=body.role,
            # Never 'subscription'. Their access comes from this org, so a
            # Stripe event on their personal account must never revoke it.
            granted_by=GRANTED_BY_GRANT,
            # No is_owner to set false: an invitee is not named in
            # organizations.owner_account_id, so they are not the owner. The
            # rule that used to need stating is now the only thing that can
            # happen.
        )
        db.add(member)

        # A brand-new account has nowhere else to land, so send them in here.
        # Someone who already had an account keeps their own default and finds
        # this org in the switcher.
        if account.default_org_id is None:
            account.default_org_id = org.org_id

        audit.record_membership(
            db, event_type=audit.MEMBER_ADD, org_id=org.org_id,
            account_id=account.id, role=body.role,
            actor_account_id=org.account_id,
        )
        db.commit()
        db.refresh(member)

        if created:
            # The ordinary sign-in code — there is no separate invite mail,
            # because there is no separate secret. Sent after commit so a
            # failed write never produces an email about access that does not
            # exist.
            code = str(random.randint(10000000, 99999999))
            from src.routers.auth.router import _hash_otp
            account.login_otp = _hash_otp(code)
            account.login_otp_expires_at = (
                datetime.now(timezone.utc) + timedelta(minutes=OTP_TTL_MINUTES))
            db.commit()
            background_tasks.add_task(send_login_code_email_ses, account.email, code)

        logger.info("[ORG] account %s invited %s to org %s as %s",
                    org.account_id, account.id, org.org_id, body.role)
        return MemberResponse(
            account_id=account.id, email=account.email, role=member.role,
            # Derived like everywhere else rather than hardcoded false. An
            # invitee is not normally the owner, but "normally" is what the
            # stored copy relied on: an owner who was somehow not a member
            # would have been reported as a non-owner on the way back in.
            is_owner=(account.id == _owner_account_id(db, org.org_id)),
            granted_by=member.granted_by,
            created_at=member.created_at,
        )
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise handle_db_error(e, "[INVITE MEMBER]")


@router.post("/members", status_code=status.HTTP_201_CREATED,
             response_model=MemberResponse)
@limiter.limit("20/minute")
async def invite_member(
    body: InviteRequest,
    db: db_dependency,
    org: org_dependency,
    request: Request,
    background_tasks: BackgroundTasks,
):
    """Add somebody to the caller's ACTIVE org."""
    return await _invite(body, db, org, background_tasks)


@router.post("/{org_id}/members", status_code=status.HTTP_201_CREATED,
             response_model=MemberResponse)
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


async def _update_role(account_id: int, body: UpdateMemberRequest,
                       db, org) -> MemberResponse:
    """Move a member to a different role, in ONE already-validated org."""
    try:
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
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise handle_db_error(e, "[UPDATE MEMBER]")


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
    try:
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
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise handle_db_error(e, "[REMOVE MEMBER]")


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

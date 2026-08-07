"""
Members of an organization: who is in it, on what tier, and who put them there.

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

from src.db.models import Account, Organization, OrganizationMember, OrganizationTier
from src.deps import db_dependency, org_dependency
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
    tier_key: str
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
    # Tier keys an invite may choose from, so the dropdown cannot offer a tier
    # this org does not have.
    tier_keys: List[str]


# Deliberately not pydantic's EmailStr: that pulls in email-validator, which
# this service does not ship, and full RFC validation is not what protects
# anything here. An invite to a malformed address simply never reaches anyone —
# access requires reading the inbox.
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class InviteRequest(BaseModel):
    email: str
    tier_key: str = Field(description="Which tier the new member joins on")

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
    tier_key: str


def _require_owner(org):
    """Only an owner shapes their org's membership.

    404 rather than 403, matching require_module and the tiers surface: a
    member who cannot manage the org should not have its admin endpoints
    confirm they exist.
    """
    if not org.is_owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


def _load_org(db, org_id: int) -> Organization:
    return db.query(Organization).filter(Organization.id == org_id).one()


def _owner_count(db, org_id: int) -> int:
    return (db.query(OrganizationMember)
              .filter(OrganizationMember.org_id == org_id,
                      OrganizationMember.is_owner.is_(True)).count())


@router.get("/members", response_model=MembersPageResponse)
@limiter.limit("60/minute")
async def list_members(db: db_dependency, org: org_dependency, request: Request):
    """Everyone in the caller's active org.

    Readable by any member, unlike the tiers matrix: knowing who your
    colleagues are is not privileged inside a team, and hiding it would make
    "who can see my contacts?" unanswerable from inside the product.
    """
    try:
        organization = _load_org(db, org.org_id)
        rows = (
            db.query(OrganizationMember, Account)
            .join(Account, Account.id == OrganizationMember.account_id)
            .filter(OrganizationMember.org_id == org.org_id)
            .order_by(OrganizationMember.is_owner.desc(), Account.email.asc())
            .all()
        )
        tiers = (db.query(OrganizationTier.tier_key)
                   .filter(OrganizationTier.org_id == org.org_id)
                   .order_by(OrganizationTier.id.asc()).all())

        return MembersPageResponse(
            org_id=org.org_id,
            org_name=organization.name,
            can_manage=org.is_owner,
            members=[
                MemberResponse(
                    account_id=m.account_id, email=a.email, tier_key=m.tier_key,
                    is_owner=m.is_owner, granted_by=m.granted_by,
                    created_at=m.created_at,
                )
                for m, a in rows
            ],
            tier_keys=[t[0] for t in tiers],
        )
    except HTTPException:
        raise
    except Exception as e:
        raise handle_db_error(e, "[LIST MEMBERS]")


@router.put("/name", response_model=MembersPageResponse)
@limiter.limit("30/minute")
async def rename_organization(
    body: RenameOrgRequest, db: db_dependency, org: org_dependency, request: Request,
):
    """Change the display name.

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
        return await list_members(db=db, org=org, request=request)
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise handle_db_error(e, "[RENAME ORG]")


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
    """Add somebody to this org on a chosen tier."""
    try:
        _require_owner(org)
        organization = _load_org(db, org.org_id)

        # The tier must exist HERE. Without this an invite could name a tier
        # from another org — or a typo — and the member would resolve to no
        # modules at all, which looks like a permissions bug rather than a
        # typo.
        tier = (db.query(OrganizationTier)
                  .filter(OrganizationTier.org_id == org.org_id,
                          OrganizationTier.tier_key == body.tier_key).first())
        if not tier:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail="Unknown tier for this organization.")

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
            tier_key=body.tier_key,
            # Never 'subscription'. Their access comes from this org, so a
            # Stripe event on their personal account must never revoke it.
            granted_by=GRANTED_BY_GRANT,
            is_owner=False,
        )
        db.add(member)

        # A brand-new account has nowhere else to land, so send them in here.
        # Someone who already had an account keeps their own default and finds
        # this org in the switcher.
        if account.default_org_id is None:
            account.default_org_id = org.org_id

        audit.record_membership(
            db, event_type=audit.MEMBER_ADD, org_id=org.org_id,
            account_id=account.id, tier_key=body.tier_key,
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

        logger.info("[ORG] account %s invited %s to org %s on tier %s",
                    org.account_id, account.id, org.org_id, body.tier_key)
        return MemberResponse(
            account_id=account.id, email=account.email, tier_key=member.tier_key,
            is_owner=False, granted_by=member.granted_by,
            created_at=member.created_at,
        )
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise handle_db_error(e, "[INVITE MEMBER]")


@router.put("/members/{account_id}", response_model=MemberResponse)
@limiter.limit("30/minute")
async def update_member_tier(
    account_id: int, body: UpdateMemberRequest,
    db: db_dependency, org: org_dependency, request: Request,
):
    """Move a member to a different tier."""
    try:
        _require_owner(org)

        tier = (db.query(OrganizationTier)
                  .filter(OrganizationTier.org_id == org.org_id,
                          OrganizationTier.tier_key == body.tier_key).first())
        if not tier:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail="Unknown tier for this organization.")

        row = (db.query(OrganizationMember, Account)
                 .join(Account, Account.id == OrganizationMember.account_id)
                 .filter(OrganizationMember.org_id == org.org_id,
                         OrganizationMember.account_id == account_id).first())
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="Not a member of this organization.")
        member, account = row

        if member.tier_key != body.tier_key:
            member.tier_key = body.tier_key
            audit.record_membership(
                db, event_type=audit.MEMBER_TIER_CHANGE, org_id=org.org_id,
                account_id=account_id, tier_key=body.tier_key,
                actor_account_id=org.account_id,
            )
        db.commit()
        db.refresh(member)
        return MemberResponse(
            account_id=account_id, email=account.email, tier_key=member.tier_key,
            is_owner=member.is_owner, granted_by=member.granted_by,
            created_at=member.created_at,
        )
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise handle_db_error(e, "[UPDATE MEMBER]")


@router.delete("/members/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("30/minute")
async def remove_member(
    account_id: int, db: db_dependency, org: org_dependency, request: Request,
):
    """Remove somebody from this org.

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

        # The last owner cannot be removed, including by themselves. An org
        # with no owner has nobody who can invite, set tiers, or hand it over —
        # it would need super admin intervention to become usable again.
        if member.is_owner and _owner_count(db, org.org_id) <= 1:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This is the only owner. Make somebody else an owner "
                       "first.",
            )

        audit.record_membership(
            db, event_type=audit.MEMBER_REMOVE, org_id=org.org_id,
            account_id=account_id, tier_key=member.tier_key,
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

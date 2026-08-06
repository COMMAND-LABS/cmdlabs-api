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

WHY A SLUG IS REQUIRED FIRST
----------------------------
A personal workspace has no slug. Inviting somebody turns it into a team, and a
team is a thing with a name — it appears in a switcher, in an audit log, and
eventually at /@{slug}. Asking for it once, here, is better than generating one
nobody chose and can never change.
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
from src.services.organizations import GRANTED_BY_GRANT
from src.utils.errors import handle_db_error

logger = logging.getLogger(__name__)

router = APIRouter()

# Lowercase, starts alphanumeric, 2-63 chars. Matches what a subdomain would
# allow, so `acme.cmdlabs.io` stays possible without a second rule later.
SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")

# Slugs that must never become an org, because they would collide with a route
# or impersonate the platform. The @ sigil in /@{slug} makes collision with
# app routes structurally impossible, so this list only has to cover names that
# would be misleading rather than every page that might ever exist.
RESERVED_SLUGS = {
    "root", "admin", "administrator", "cmdlabs", "cmd-labs", "support",
    "help", "billing", "security", "official", "staff", "system", "api",
}

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
    # None until the workspace is named. The UI asks for one before inviting.
    org_slug: Optional[str] = None
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


class NameOrgRequest(BaseModel):
    slug: str = Field(description="Public identifier, lowercase. Immutable.")
    name: Optional[str] = None


class SlugAvailability(BaseModel):
    slug: str
    available: bool
    # Why not, in the same words the write path would use. None when available.
    reason: Optional[str] = None


class RenameOrgRequest(BaseModel):
    name: str = Field(description="Display name. Editable, unlike the slug.")

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


def _slug_problem(db, slug: str) -> Optional[tuple]:
    """Why `slug` cannot be taken, as (status_code, message), or None.

    ONE function for the availability check and the write, so the form can
    never call something available that the PUT then refuses. The status code
    travels with the message because the two callers need different ones —
    422 for a malformed slug, 409 for one that is reserved or already someone
    else's — and splitting the rules to preserve that distinction is exactly
    how the two would drift apart.
    """
    if not SLUG_PATTERN.match(slug):
        return (status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Use 2-63 characters: lowercase letters, numbers and hyphens, "
                "starting with a letter or number.")
    if slug in RESERVED_SLUGS:
        return (status.HTTP_409_CONFLICT, "That name is reserved.")
    if db.query(Organization.id).filter(Organization.slug == slug).first():
        return (status.HTTP_409_CONFLICT, "That name is taken.")
    return None


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
            org_slug=organization.slug,
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


@router.get("/slug/available", response_model=SlugAvailability)
@limiter.limit("30/minute")
async def check_slug(
    slug: str, db: db_dependency, org: org_dependency, request: Request,
):
    """Whether a name can still be claimed — for the naming form.

    Deliberately narrow. Only an owner of a STILL-UNNAMED org may ask, because
    that is the only caller with a use for the answer, and answering it for
    anyone else would turn an immutable public identifier into a directory
    anybody could enumerate one guess at a time. An org that already has a slug
    gets the same 404 as a non-owner: naming happens once, so the question is
    moot the moment it is answered.

    Rate limited at a third of the write path's neighbours for the same reason.
    The check is advisory in any case — PUT /slug re-validates, and the unique
    constraint settles a race between two owners typing the same name.
    """
    try:
        _require_owner(org)
        organization = _load_org(db, org.org_id)
        if organization.slug is not None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="Not found")

        candidate = (slug or "").strip().lower()
        problem = _slug_problem(db, candidate)
        return SlugAvailability(
            slug=candidate,
            available=problem is None,
            reason=None if problem is None else problem[1],
        )
    except HTTPException:
        raise
    except Exception as e:
        raise handle_db_error(e, "[CHECK SLUG]")


@router.put("/slug", response_model=MembersPageResponse)
@limiter.limit("10/minute")
async def name_organization(
    body: NameOrgRequest, db: db_dependency, org: org_dependency, request: Request,
):
    """Give a personal workspace a public identity. Once only.

    IMMUTABLE by design. A slug is the org's public name — it goes in URLs,
    emails, and eventually /@{slug} — so letting it change would break every
    link that ever pointed at it and would let one org quietly assume a name
    another had built a reputation on. Renaming the DISPLAY name stays free.
    """
    try:
        _require_owner(org)
        organization = _load_org(db, org.org_id)

        if organization.slug is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This organization already has a name and it cannot be "
                       "changed. Its display name can.",
            )

        slug = (body.slug or "").strip().lower()
        problem = _slug_problem(db, slug)
        if problem:
            raise HTTPException(status_code=problem[0], detail=problem[1])

        organization.slug = slug
        if body.name:
            organization.name = body.name.strip()

        audit.record_org_change(
            db, event_type=audit.ORG_CREATE, org_id=org.org_id,
            detail=f"named '{slug}'", actor_account_id=org.account_id,
        )
        db.commit()
        return await list_members(db=db, org=org, request=request)
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise handle_db_error(e, "[NAME ORG]")


@router.put("/name", response_model=MembersPageResponse)
@limiter.limit("30/minute")
async def rename_organization(
    body: RenameOrgRequest, db: db_dependency, org: org_dependency, request: Request,
):
    """Change the display name. Never the slug.

    The two are deliberately different kinds of thing. The SLUG is identity —
    public, in URLs, immutable, and the reason renaming cannot quietly let one
    org assume a name another built a reputation on. The NAME is a label: it is
    what members see in the switcher, and being stuck with a typo in it forever
    would be a silly thing to enforce.

    The API promised this in the 409 it returns from /slug ("its display name
    can [be changed]") before anything implemented it. This is that promise.
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

        if organization.slug is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Name your organization before inviting people to it.",
            )

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
        # it would need staff intervention to become usable again.
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
                     .filter(Organization.owner_account_id == account_id,
                             Organization.slug.is_(None)).first())
            account.default_org_id = own[0] if own else None

        db.commit()
        logger.info("[ORG] account %s removed %s from org %s",
                    org.account_id, account_id, org.org_id)
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise handle_db_error(e, "[REMOVE MEMBER]")

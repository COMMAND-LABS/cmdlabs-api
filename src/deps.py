import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated
from sqlalchemy.orm import Session
from fastapi import Depends, HTTPException, status, Request
from passlib.context import CryptContext
from jose import jwt, JWTError
import os
from .config import plans_registry as plans
from .db.database import SessionLocal
from .db.models import ApiKey, Account, ApiKeyStatus
from .utils.api_key_utils import verify_api_key
from sqlalchemy import func

logger = logging.getLogger(__name__)

SECRET_KEY = os.getenv('AUTH_SECRET_KEY')
ALGORITHM = os.getenv('AUTH_ALGORITHM')

def get_db():
    """
    Database session dependency.
    
    The engine is configured with pool_pre_ping=True and a checkout
    event listener that validates SSL connections, so stale connections
    are automatically replaced before being handed out.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

db_dependency = Annotated[Session, Depends(get_db)]
bcrypt_context = CryptContext(schemes=["sha256_crypt"])

async def get_current_user(request: Request):
    try:
        token = request.cookies.get("jwt")
        auth_header = request.headers.get("Authorization", "")

        logger.info("[AUTH] %s %s | cookie_jwt: %s | auth_header: %s",
                    request.method, request.url.path,
                    token[:20] + "..." if token else "None",
                    auth_header[:30] + "..." if auth_header else "None")

        if not token:
            if auth_header.startswith("Bearer "):
                token = auth_header.replace("Bearer ", "").strip()

        if not token:
            logger.warning("[AUTH] No token found — rejecting %s %s", request.method, request.url.path)
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated - no JWT token found in cookies or Authorization header")

        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])

        email: str | None = payload.get('sub')
        account_id: str = payload.get('id')
        
        if email is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Could not validate user - email not found in token')
        
        return {'email': email, 'id': account_id}
    except JWTError as e:
        logger.warning("JWT validation failed: %s", e)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f'Could not validate user: {str(e)}')
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unexpected error in get_current_user")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f'Could not validate user: {str(e)}')
    
jwt_dependency = Annotated[dict, Depends(get_current_user)]


async def get_current_user_or_api_key(
    request: Request,
    db: Session = Depends(get_db)
) -> dict:
    """
    Unified authentication: tries JWT first, then API key.
    Returns same format: {'email': str, 'id': int, 'auth_type': 'jwt'|'api_key'}
    """
    try:
        token = request.cookies.get("jwt")
        auth_header = request.headers.get("Authorization", "")

        logger.info("[AUTH-UNIFIED] %s %s | cookie_jwt: %s | auth_header: %s",
                    request.method, request.url.path,
                    token[:20] + "..." if token else "None",
                    auth_header[:30] + "..." if auth_header else "None")

        if not token:
            if auth_header.startswith("Bearer "):
                bearer_value = auth_header.replace("Bearer ", "").strip()
                if not bearer_value.startswith("kalygo_"):
                    token = bearer_value

        if token:
            payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
            email = payload.get('sub')
            account_id = payload.get('id')
            if email:
                logger.info("[AUTH-UNIFIED] JWT valid for %s", email)
                return {
                    'email': email,
                    'id': int(account_id) if isinstance(account_id, str) else account_id,
                    'auth_type': 'jwt'
                }
    except (JWTError, KeyError, ValueError) as e:
        logger.warning("[AUTH-UNIFIED] JWT decode failed: %s", e)

    api_key = None
    
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        api_key = auth_header.replace("Bearer ", "").strip()
    
    if not api_key:
        api_key = request.headers.get("X-API-Key", "").strip()
    
    if api_key and api_key.startswith("kalygo_"):
        key_prefix = api_key[:20] if len(api_key) >= 20 else api_key
        
        api_key_record = db.query(ApiKey).filter(
            ApiKey.key_prefix == key_prefix,
            ApiKey.status == ApiKeyStatus.ACTIVE
        ).first()
        
        if api_key_record:
            if verify_api_key(api_key, api_key_record.key_hash):
                api_key_record.last_used_at = func.now()
                db.commit()
                
                account = db.query(Account).filter(Account.id == api_key_record.account_id).first()
                if account:
                    return {
                        'email': account.email,
                        'id': api_key_record.account_id,
                        'auth_type': 'api_key',
                        'api_key_id': api_key_record.id,
                    }
    
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required. Provide JWT cookie or API key in Authorization/X-API-Key header."
    )


auth_dependency = Annotated[dict, Depends(get_current_user_or_api_key)]


def account_id_from_claims(claims: dict) -> int:
    """Return the integer account id from a JWT / API-key claims dict.

    The ``id`` field may arrive as a string (raw JWT payload) or an int
    (already-coerced unified auth / API-key path); normalize to int.
    """
    account_id = claims['id']
    return int(account_id) if isinstance(account_id, str) else account_id


def ensure_account(db: Session, account_id: int) -> Account:
    """Fetch an account by id, raising 404 if it does not exist."""
    account = db.query(Account).filter(Account.id == account_id).first()
    if not account:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Account not found",
        )
    return account


# --------------------------------------------------------------------------
# Organization context
# --------------------------------------------------------------------------

# Name of the cookie carrying the caller's active organization id.
#
# A cookie rather than a header, because ~11 fetch call sites in the UI bypass
# the shared request() wrapper; every one of them already sends
# credentials:"include", so a cookie reaches all of them for free while a
# header would silently fall back to the wrong org wherever it was missed.
#
# A cookie rather than a JWT claim, because the JWT lives 7 days and removing
# someone from an org has to take effect on their very next request.
ORG_COOKIE_NAME = "cmdlabs_org"


@dataclass(frozen=True)
class OrgContext:
    """Who is asking, in which org, and what that org allows.

    `org_id` is the ONLY thing that decides which rows a request may see.
    `role` decides which modules it may open. Those two axes are kept strictly
    separate: a role never widens or narrows row visibility, so a misconfigured
    role is a wrong menu rather than a data leak. That separation is also the
    limit of what a role can express — see config/roles_registry.

    `is_super_admin` bypasses MODULE gating only. It never bypasses org_id —
    platform super admins read an org's data by joining it, which leaves a
    membership row anyone in that org can see. An invisible read bypass would
    make the audit log meaningless and would give every query two behaviors.

    """
    account_id: int
    org_id: int
    # 'manager' | 'community_member'. NEVER 'owner' — ownership is a column on
    # organizations and is carried by is_owner below, derived per request.
    role: str
    # Both default to FALSE, which is the safe direction: a context built
    # without them is less privileged, never more. get_org_context always
    # passes both; the defaults exist for the test helpers that assemble a
    # context by hand, and for the same reason read_only and plan have them.
    is_owner: bool = False
    is_super_admin: bool = False
    # True during the GRACE window after the owner's subscription lapsed:
    # everything still opens, nothing may be changed. Derived per request from
    # the owner's accounts.subscription_lapsed_at, never stored — see
    # config/plans_registry and services/modules.org_entitlement.
    read_only: bool = False
    # When read-only becomes a downgrade to free. Passed to the UI so the
    # banner can say WHEN rather than just "soon". None unless read_only.
    grace_ends_at: datetime | None = None
    # The plan THIS ORG has — 'free' | 'premium'. Pinned by staff, or derived
    # from the owner's subscription. It gates the platform course catalog and
    # nothing else; module access still comes from ceiling ∩ role, and row
    # access still comes from org_id alone.
    #
    # THE ORG'S PLAN, NOT THE CALLER'S. It used to be plan_for_account(account),
    # which meant a free account invited into a paid org kept its own free plan
    # and was refused the premium catalog courses the org had already paid for.
    # That contradicted the layer directly above it: the module CEILING has
    # always come from the org (services/modules.org_entitlement), so the same
    # member could already open Contacts and Deals while being told a premium
    # course was not for them. One container, one answer.
    #
    # Widening, not a hole. Reaching an org still requires an OrganizationMember
    # row, which only its owner can create — so nobody can put themselves inside
    # a paid org, and org_id still decides every row either way.
    #
    # Defaulted so the handful of test helpers that build a context by hand
    # keep working.
    plan: str = plans.PLAN_FREE

    @property
    def is_read_only(self) -> bool:
        """Kept as the name every call site already reads."""
        return self.read_only


def _org_context_for(db: Session, account, account_id: int,
                     target_org_id: int) -> OrgContext:
    """Build a validated OrgContext for ONE named org. The membership gate.

    THE SINGLE PLACE that answers "may this account act in this org, and with
    what?". Both entry points below funnel through it: get_org_context, which
    takes the org from the cookie, and get_named_org_context, which takes it
    from a path parameter.

    They are deliberately not two implementations. The difference between them
    is one line — WHERE the target org id comes from — and everything after it
    (membership re-check, ownership derivation, entitlement) is the part that
    must never diverge. Two copies of a membership gate is two chances to fix a
    bug in one of them.
    """
    from src.db.models import Organization, OrganizationMember

    row = (
        db.query(OrganizationMember, Organization)
        .join(Organization, Organization.id == OrganizationMember.org_id)
        .filter(
            OrganizationMember.account_id == account_id,
            OrganizationMember.org_id == target_org_id,
        )
        .first()
    )

    if row is None:
        # Fail closed, and give the same answer whether the org does not exist
        # or the caller is simply not in it — otherwise this is an oracle for
        # enumerating which org ids are real.
        logger.warning(
            "[ORG] account %s is not a member of org %s — refusing",
            account_id, target_org_id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not a member of this organization.",
        )

    member, org = row
    # One query, and it decides BOTH what opens and whether it may be written.
    # Resolved here rather than at each call site so a request cannot see a
    # ceiling computed at one instant and a writability computed at another.
    from src.services import modules as modules_service

    entitlement = modules_service.org_entitlement(db, org.id)

    return OrgContext(
        account_id=account_id,
        org_id=org.id,
        role=member.role,
        # DERIVED, not stored. An org names its owner in one column; a second
        # copy on the membership row was a cache with no invalidation, and it
        # drifted — orgs whose owner_account_id named somebody who held no
        # is_owner row, and so could not open the org they owned.
        #
        # The Organization is already joined for the membership check above, so
        # this costs nothing and cannot disagree with itself.
        is_owner=(org.owner_account_id == account_id),
        is_super_admin=account.is_super_admin,
        read_only=entitlement.read_only,
        grace_ends_at=entitlement.grace_ends_at,
        # From the same one-query entitlement as the ceiling and read_only, so
        # a request cannot see a plan resolved at one instant and a ceiling at
        # another — and so there is one place that decides what an org has.
        plan=entitlement.plan,
    )


async def get_named_org_context(
    org_id: int,
    db: Session = Depends(get_db),
    auth: dict = Depends(get_current_user_or_api_key),
) -> OrgContext:
    """Resolve and VALIDATE an org named in the PATH, ignoring the cookie.

    For the handful of reads that are legitimately about an org the caller is
    not currently acting in — the account-settings Organizations page, which
    shows a tab per membership and must not re-scope the whole dashboard to
    render one.

    THE PATH IS NO MORE TRUSTED THAN THE COOKIE. Both are just an org id from
    the client, and both are checked against organization_members on every
    request by the same function. An id the caller is not a member of gets 403,
    exactly as a tampered cookie does.

    WRITES ARE PERMITTED, narrowly, and this used to say they were not. The
    membership-management routes in routers/organizations/members.py are now
    mounted twice — once on the cookie context, once here — so an owner of
    several orgs can staff any of them without switching the dashboard into it
    first. What changed is the ROUTE SET, not the gate: both mountings call the
    same implementation, which re-derives `is_owner` from THIS org's owner
    column via _org_context_for below.

    The original objection was that _refuse_writes_while_read_only hangs off
    the cookie context, so a write mounted here would skip it. That turned out
    to be true of the cookie path as well: the check runs inside require_module,
    and /api/organizations sits in ALWAYS_ALLOWED_PREFIXES, so org-configuration
    writes have always been exempt (see that function's own note). Mounting
    them here is therefore parity, not a new hole — but if that exemption is
    ever closed, close it for BOTH mountings, since they are one implementation.

    Still not a general-purpose write context. A route belongs here only when
    the org is the SUBJECT of the request — membership administration — rather
    than the scope the caller happens to be working in. Anything touching
    customer records should keep taking the org from the cookie, so "which org
    am I acting in" stays a single answer the user chose with the switcher.
    """
    account_id = account_id_from_claims(auth)
    account = ensure_account(db, account_id)
    return _org_context_for(db, account, account_id, org_id)


async def get_org_context(
    request: Request,
    db: Session = Depends(get_db),
    auth: dict = Depends(get_current_user_or_api_key),
) -> OrgContext:
    """Resolve and VALIDATE the caller's active organization.

    The cookie is never trusted. It names an org, and membership in that org is
    re-checked against the database on every single request — so revoking a
    membership takes effect immediately, with no token to re-issue and no cache
    to invalidate.
    """
    account_id = account_id_from_claims(auth)
    account = ensure_account(db, account_id)

    requested_org_id: int | None = None
    # On the API-key path the cookie is ignored outright. A key is a
    # long-lived credential that carries no org of its own yet, so honouring a
    # cookie alongside it would let a key issued for one org be pointed at
    # another just by setting a header. Falls back to the account default.
    if auth.get("auth_type") != "api_key":
        raw = request.cookies.get(ORG_COOKIE_NAME)
        if raw and raw.isdigit():
            requested_org_id = int(raw)

    target_org_id = requested_org_id or account.default_org_id
    if target_org_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No organization for this account.",
        )

    # Fail closed, in _org_context_for. Notably we do NOT silently fall back to
    # the default org when a cookie names an org the caller is not in: that
    # would turn a revoked membership into "you are quietly somewhere else"
    # instead of a visible error, and would mask a tampered cookie entirely.
    return _org_context_for(db, account, account_id, target_org_id)


org_dependency = Annotated[OrgContext, Depends(get_org_context)]
# For READS about an org named in the path rather than the cookie. Same
# membership gate; see get_named_org_context for why it is not a write path.
named_org_dependency = Annotated[OrgContext, Depends(get_named_org_context)]


def require_module(module_key: str):
    """Dependency factory: refuse the request unless `ctx` may open this module.

    This is what makes a role an authorization boundary rather than a menu
    filter. Without it an account whose role excludes Deals still reaches
    GET /api/deals by typing the URL — which is exactly the state
    cmdlabs-ui/src/config/roles.ts documents about the pre-org system in its
    own header comment.

    Attached in main.py from the module registry rather than hand-written on
    each router, so a new router either maps to a module and is gated, or is
    absent from the registry and visibly always-allowed. There is no third
    state where someone simply forgot.

    Note this gates SCREENS, not rows. org_id still decides what is visible
    within a module; the two axes never substitute for each other.
    """
    async def _check(
        request: Request,
        db: Session = Depends(get_db),
        ctx: "OrgContext" = Depends(get_org_context),
    ) -> None:
        from src.services import modules as modules_service

        if not modules_service.can_open(db, ctx, module_key):
            logger.info(
                "[MODULE] account %s (org %s, role %s) denied %s %s — %s not enabled",
                ctx.account_id, ctx.org_id, ctx.role,
                request.method, request.url.path, module_key,
            )
            # 404 rather than 403: a module the caller's role excludes should
            # look absent, not forbidden. Telling someone precisely which paid
            # features exist behind a wall is an invitation to probe them.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Not found",
            )

        _refuse_writes_while_read_only(ctx, request)

    return _check


def _refuse_writes_while_read_only(ctx: "OrgContext", request: Request) -> None:
    """During the grace window after a lapse: read everything, change nothing.

    The middle ground between "still fully paid" and "dropped to free". Nothing
    is deleted and nothing disappears from the screen — losing a team's data,
    or even the sight of it, over a failed card is not a recoverable mistake.
    Refusing writes is.

    COVERAGE IS THE MODULE REGISTRY'S, deliberately and imperfectly. This runs
    inside require_module, so the routes it guards are exactly the module-gated
    ones. Everything in ALWAYS_ALLOWED_PREFIXES is exempt, and the important
    half of that is right: /api/billing MUST stay writable or a read-only org
    could not pay its way out, and /api/auth must keep working. The half that
    is merely tolerable is that /api/organizations writes — renaming the org,
    inviting a member — also slip through. They are org configuration rather
    than customer data, so nothing is lost or corrupted; it is simply looser
    than the banner implies. Fixing it means guarding those routes by hand,
    which is a decision to take on purpose rather than by widening this.
    """
    if ctx.is_read_only and request.method not in ("GET", "HEAD", "OPTIONS"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This workspace is read-only while the subscription is "
                   "lapsed. Your data is all still here — update the payment "
                   "method to make changes again.",
        )


async def require_super_admin(
    db: Session = Depends(get_db),
    auth: dict = Depends(get_current_user_or_api_key),
) -> Account:
    """Platform super admins only. Set out of band via scripts/super_admin.py.

    Deliberately NOT built on OrgContext: administering the platform is not an
    action inside any one org, so requiring an active-org membership would be
    both wrong and circular (super admins would need to belong to an org to
    discover which orgs exist).

    What this permits is administration — listing orgs, setting a module
    ceiling, suspending. It does NOT grant access to any org's DATA. Reading
    another org's rows still requires joining that org, which leaves a
    membership row its members can see. Keeping those apart is what lets
    `org_id == ctx.org_id` hold with zero exceptions.
    """
    account = ensure_account(db, account_id_from_claims(auth))
    if not account.is_super_admin:
        # 404 rather than 403: the admin surface should not confirm its own
        # existence to a non-super-admin caller.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Not found",
        )
    return account


super_admin_dependency = Annotated[Account, Depends(require_super_admin)]
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
    `tier_key` decides which modules it may open. Those two axes are kept
    strictly separate: a tier never widens or narrows row visibility, so a
    misconfigured tier is a wrong menu rather than a data leak.

    `is_super_admin` bypasses MODULE gating only. It never bypasses org_id —
    platform super admins read an org's data by joining it, which leaves a
    membership row anyone in that org can see. An invisible read bypass would
    make the audit log meaningless and would give every query two behaviors.

    """
    account_id: int
    org_id: int
    tier_key: str
    is_owner: bool
    is_super_admin: bool
    # True during the GRACE window after the owner's subscription lapsed:
    # everything still opens, nothing may be changed. Derived per request from
    # the owner's accounts.subscription_lapsed_at, never stored — see
    # config/plans_registry and services/modules.org_entitlement.
    read_only: bool = False
    # When read-only becomes a downgrade to free. Passed to the UI so the
    # banner can say WHEN rather than just "soon". None unless read_only.
    grace_ends_at: datetime | None = None
    # The self-serve plan this ACCOUNT is on, per Stripe — 'free' | 'premium'.
    # A third axis, and the narrowest: it gates the platform course catalog and
    # nothing else. Module access still comes from ceiling ∩ tier, and row
    # access still comes from org_id alone. Defaulted so the handful of test
    # helpers that build a context by hand keep working.
    plan: str = plans.PLAN_FREE

    @property
    def is_read_only(self) -> bool:
        """Kept as the name every call site already reads."""
        return self.read_only


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
    from src.db.models import Organization, OrganizationMember

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
        # Fail closed. Notably we do NOT silently fall back to the default org
        # when a cookie names an org the caller is not in: that would turn a
        # revoked membership into "you are quietly somewhere else" instead of a
        # visible error, and would mask a tampered cookie entirely.
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
        tier_key=member.tier_key,
        is_owner=member.is_owner,
        is_super_admin=account.is_super_admin,
        read_only=entitlement.read_only,
        grace_ends_at=entitlement.grace_ends_at,
        plan=plans.plan_for_account(account),
    )


org_dependency = Annotated[OrgContext, Depends(get_org_context)]


def require_module(module_key: str):
    """Dependency factory: refuse the request unless `ctx` may open this module.

    This is what makes the tiers matrix an authorization boundary rather than a
    menu filter. Without it an account whose tier excludes Deals still reaches
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
                "[MODULE] account %s (org %s, tier %s) denied %s %s — %s not enabled",
                ctx.account_id, ctx.org_id, ctx.tier_key,
                request.method, request.url.path, module_key,
            )
            # 404 rather than 403: a module the caller has no tier for should
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
    """Platform super admins only. Granted out of band via
    scripts/grant_super_admin.py.

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
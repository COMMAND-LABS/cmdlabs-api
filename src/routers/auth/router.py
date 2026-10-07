from datetime import timedelta, datetime, timezone
import hashlib
import logging
import random
from typing import Optional
from fastapi import APIRouter, HTTPException, status, Header, Response, BackgroundTasks, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator
from jose import jwt
import os
from src.db.models import Account, UsageCredits
from src.routers.auth.background_tasks.record_login import record_login
from src.routers.auth.background_tasks.send_login_code_email_ses import send_login_code_email_ses
from src.deps import db_dependency, jwt_dependency

from src.services import organizations as organizations_service
from src.services.organizations import ensure_membership
from src.rate_limit import limiter

logger = logging.getLogger(__name__)

router = APIRouter()

SECRET_KEY = os.getenv("AUTH_SECRET_KEY")
ALGORITHM = os.getenv("AUTH_ALGORITHM")

# Optional ceiling on the number of accounts, for throttling a launch. Unset or
# 0 means no cap, and the count query is skipped entirely. This used to be a
# hardcoded 400, which would have silently stopped all signups at account 400.
SIGNUP_CAP = int(os.getenv("SIGNUP_CAP", "0") or 0)

def _canonical_email(v: str) -> str:
    """Canonical email form used for storage and lookups (lowercase + trimmed)."""
    return v.strip().lower()

class CurrentUserResponse(BaseModel):
    email: str

class RequestCodeBody(BaseModel):
    email: str

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, v: str) -> str:
        return _canonical_email(v)

class VerifyCodeBody(BaseModel):
    email: str
    code: str
    # The org whose sign-in page (/org/<slug>/login) this came from, if any.
    # Decides where the session LANDS, never whether it belongs anywhere: see
    # verify_login_code.
    org_slug: Optional[str] = None

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, v: str) -> str:
        return _canonical_email(v)

    @field_validator("org_slug")
    @classmethod
    def _normalize_slug(cls, v: Optional[str]) -> Optional[str]:
        return organizations_service.normalize_slug(v)


class RequestedOrg(BaseModel):
    """The org named by `org_slug`, and whether this account is in it."""
    slug: str
    name: str
    is_member: bool


class VerifyCodeResponse(BaseModel):
    ok: bool = True
    # The org the session should act in, set ONLY when they are a member of
    # the requested org. The UI writes it to the org cookie alongside the jwt.
    landing_org_id: Optional[int] = None
    # None when no slug was sent or no org has that address.
    requested_org: Optional[RequestedOrg] = None

OTP_TTL_MINUTES = 10

def _hash_otp(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()

def _issue_jwt_cookie(response: Response, token: str) -> Response:
    response.set_cookie(
        key="jwt",
        value=token,
        httponly=True,
        expires=60 * 60 * 24 * 7,
        secure=True,
        samesite="None",
        domain=os.getenv("COOKIE_DOMAIN"),
        path="/",
    )
    return response

def create_access_token(email: str, user_id: int, expires_delta: timedelta):
    encode = {'sub': email, 'id': user_id}
    expires = datetime.now(timezone.utc) + expires_delta
    encode.update({'exp': expires})
    return jwt.encode(encode, SECRET_KEY, algorithm=ALGORITHM)

@router.get('/validate-token')
async def validate_token(request: Request, authorization: str = Header(...)):
    try:
        token = authorization.split(" ")[1]
        jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return {'access_token': authorization, 'token_type': 'bearer'}
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token has expired")

@router.get('/me', response_model=CurrentUserResponse)
async def get_current_user_info(current_user: jwt_dependency, request: Request):
    return CurrentUserResponse(email=current_user['email'])

@router.delete("/log-out")
@limiter.limit("5/minute")
def logout(request: Request, response: Response):
    response.delete_cookie(
        key="jwt",
        domain=os.getenv("COOKIE_DOMAIN"),
        path="/"
    )
    return {"message": "Logged out successfully"}

# POST /request-password-reset and /reset-password sat here. Removed
# 2026-09-29: sign-in is by emailed one-time code, so the password they set was
# never read, the emailed link pointed at a page that did not exist, and the
# request route was unauthenticated, unthrottled and answered 404 for unknown
# emails (an account-existence check that also sent SES mail on every call).
# The accounts.hashed_password / reset_token columns remain for now.

@router.post("/request-code", status_code=status.HTTP_200_OK)
@limiter.limit("5/minute")
async def request_login_code(body: RequestCodeBody, db: db_dependency, request: Request, background_tasks: BackgroundTasks):
    """
    Step 1 of OTP login/signup.
    Creates the account if it doesn't exist, then emails a 6-digit code.
    Always returns 200 to avoid leaking whether the email is registered.
    """
    account = db.query(Account).filter(Account.email == body.email).first()

    if not account:
        if SIGNUP_CAP and db.query(Account).count() >= SIGNUP_CAP:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Account creation is currently limited.",
            )
        # No Stripe customer yet. It used to be created here, before the email
        # was even verified, so every typo and bot got one. Checkout creates it
        # on first purchase (routers/billing/checkout.py).
        account = Account(
            email=body.email,
        )
        db.add(account)
        db.flush()

        try:
            usage_credits = UsageCredits(account_id=account.id, amount=1.00)
            db.add(usage_credits)
        except Exception:
            pass

        db.commit()
        db.refresh(account)

    code = str(random.randint(10000000, 99999999))
    account.login_otp = _hash_otp(code)
    account.login_otp_expires_at = datetime.now(timezone.utc) + timedelta(minutes=OTP_TTL_MINUTES)
    db.commit()

    background_tasks.add_task(send_login_code_email_ses, account.email, code)
    return {"detail": "Code sent"}

@router.post("/verify-code")
@limiter.limit("10/minute")
async def verify_login_code(body: VerifyCodeBody, db: db_dependency, request: Request, background_tasks: BackgroundTasks):
    """
    Step 2 of OTP login/signup.
    Validates the 6-digit code and issues a JWT session cookie.
    """
    invalid_exc = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired code")

    account = db.query(Account).filter(Account.email == body.email).first()
    if not account or not account.login_otp or not account.login_otp_expires_at:
        raise invalid_exc

    if datetime.now(timezone.utc) > account.login_otp_expires_at:
        raise invalid_exc

    if account.login_otp != _hash_otp(body.code):
        raise invalid_exc

    account.login_otp = None
    account.login_otp_expires_at = None
    db.commit()

    # Place the account in an org on first VERIFIED login.
    #
    # Deliberately here and not in /request-code, which creates the account
    # before the OTP is checked: an unverified squatter would otherwise get a
    # membership for an email they do not control. They can never obtain a JWT,
    # so they never need one.
    #
    # Idempotent, and non-fatal by design — a failure here must not cost a user
    # their login. The membership is created on their next sign-in instead.
    try:
        ensure_membership(db, account)
    except Exception:
        db.rollback()
        logger.exception("[VERIFY CODE] Could not ensure org membership for account %s", account.id)

    # The org whose sign-in page they used, if any. This decides where they
    # LAND, never whether they belong: a member is sent into the org and it
    # becomes their default; anybody else is told so and lands in their own
    # default. Nothing here joins anyone to anything — that is what
    # invitations are for.
    #
    # Non-fatal, like the membership step above: an address that has since
    # been cleared must not cost a login the email has just proven.
    landing_org_id: int | None = None
    requested_org: RequestedOrg | None = None
    if body.org_slug:
        try:
            org, is_member = organizations_service.membership_for_slug(
                db, account.id, body.org_slug)
            if org is not None:
                requested_org = RequestedOrg(slug=org.slug, name=org.name,
                                             is_member=is_member)
                if is_member:
                    landing_org_id = org.id
                    if account.default_org_id != org.id:
                        account.default_org_id = org.id
                        db.commit()
        except Exception:
            db.rollback()
            logger.exception("[VERIFY CODE] Could not resolve org %r for account %s",
                             body.org_slug, account.id)

    ip_address = request.client.host
    token = create_access_token(account.email, account.id, timedelta(days=7))
    background_tasks.add_task(record_login, account.id, ip_address)

    # A JSON body rather than the empty Response this used to return: the
    # org-aware sign-in needs to know where it landed. Every existing caller
    # ignored the body, so the empty case is a superset.
    response = JSONResponse(content=VerifyCodeResponse(
        landing_org_id=landing_org_id, requested_org=requested_org,
    ).model_dump())
    return _issue_jwt_cookie(response, token)


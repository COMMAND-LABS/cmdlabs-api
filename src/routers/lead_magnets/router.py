"""
Public lead magnet signup.

POST /api/lead-magnets/{slug}/signups — record the request, queue the email.

No auth: the /resources pages are reachable while logged out. The slug is
validated against the code registry, and the body carries only an email and
attribution — never content — so the server decides everything that gets
mailed (see src/config/lead_magnets_registry.py for why).
"""
import re
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator

from src.config import lead_magnets_registry as registry
from src.db.lead_magnet_signups import LeadMagnetSignup
from src.deps import db_dependency
from src.rate_limit import limiter

from .send_lead_magnet_email_ses import send_lead_magnet_email_ses

router = APIRouter()

# Deliberately not pydantic's EmailStr: that pulls in email-validator, which
# this service does not ship (see routers/organizations/members.py). A
# malformed address simply never receives anything.
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class LeadMagnetAttribution(BaseModel):
    """Where the visitor came from. The field list IS the whitelist: anything
    else the client sends is dropped by pydantic. Stored as data, never
    rendered into the email."""
    utm_source: Optional[str] = Field(default=None, max_length=200)
    utm_medium: Optional[str] = Field(default=None, max_length=200)
    utm_campaign: Optional[str] = Field(default=None, max_length=200)
    utm_term: Optional[str] = Field(default=None, max_length=200)
    utm_content: Optional[str] = Field(default=None, max_length=200)
    referrer: Optional[str] = Field(default=None, max_length=2048)
    landing_path: Optional[str] = Field(default=None, max_length=2048)


class LeadMagnetSignupBody(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    attribution: Optional[LeadMagnetAttribution] = None

    @field_validator("email")
    @classmethod
    def _canonical(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if not EMAIL_PATTERN.match(v):
            raise ValueError("Enter a valid email address.")
        return v


class LeadMagnetSignupResponse(BaseModel):
    status: str


@router.post(
    "/{slug}/signups",
    status_code=status.HTTP_201_CREATED,
    response_model=LeadMagnetSignupResponse,
)
@limiter.limit("5/minute")
async def create_signup(
    slug: str,
    body: LeadMagnetSignupBody,
    db: db_dependency,
    background_tasks: BackgroundTasks,
    request: Request,
):
    """Record a lead magnet request and email the links.

    Always the same 201 for a valid request, whether or not this email has
    asked before: a repeat simply gets the email again, and the response never
    confirms a prior signup. Rows are append-only; stats count distinct emails.
    """
    magnet = registry.get(slug)
    if magnet is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Lead magnet not found"
        )

    attribution = (
        body.attribution.model_dump(exclude_none=True) if body.attribution else None
    )
    signup = LeadMagnetSignup(
        slug=magnet.slug,
        email=body.email,
        attribution=attribution or None,
        user_agent=(request.headers.get("user-agent") or "")[:512] or None,
    )
    db.add(signup)
    db.commit()

    # Out-of-band so a slow or failing SES call never blocks the visitor; the
    # sender swallows and logs its own errors.
    background_tasks.add_task(send_lead_magnet_email_ses, to_email=body.email, slug=magnet.slug)

    return LeadMagnetSignupResponse(status="sent")

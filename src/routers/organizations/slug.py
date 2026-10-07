"""
An organization's public address: /org/<slug>/login on the UI side.

    GET /by-slug/{slug}      NO AUTH. The org sign-in page has to render for
                            somebody who has not signed in — that is who it is
                            for. Returns the id, the name and the slug. Nothing
                            about the org's members, plan or data.

    PUT /{org_id}/slug       OWNER. Set, change or clear the address. The same
                            gate as renaming (members.py), because the address
                            is shown to the public and is the owner's to pick.

Resolving a slug never joins anybody to anything. /verify-code uses
services.organizations.membership_for_slug to decide whether the person who
just signed in may LAND in the org; everyone else is told to ask for an invite.
"""
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from src.db.models import Organization
from src.deps import db_dependency, named_org_dependency, require_org_owner
from src.rate_limit import limiter
from src.services import organizations

router = APIRouter()


class PublicOrganization(BaseModel):
    """What an unauthenticated visitor may learn from an address: a name."""
    id: int
    name: str
    slug: str


class SetSlugRequest(BaseModel):
    # None or blank clears the address.
    slug: Optional[str] = None


class OrgSlugResponse(BaseModel):
    org_id: int
    slug: Optional[str]


@router.get("/by-slug/{slug}", response_model=PublicOrganization)
@limiter.limit("60/minute")
async def read_public_organization(slug: str, db: db_dependency,
                                   request: Request):
    """NO AUTHENTICATION — see the module header.

    Only ids, names and slugs leave here. Guessing an address reveals that an
    org with that name exists, which is what an address is for; the limiter is
    the same one the invitation lookup has.
    """
    normalized = organizations.normalize_slug(slug)
    org = organizations.find_by_slug(db, normalized) if normalized else None
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="No organization at this address.")
    return PublicOrganization(id=org.id, name=org.name, slug=org.slug)


@router.put("/{org_id}/slug", response_model=OrgSlugResponse)
@limiter.limit("30/minute")
async def set_organization_slug(body: SetSlugRequest, db: db_dependency,
                                org: named_org_dependency, request: Request):
    """Owner only, 404 to everyone else, matching the rename route.

    Rule violations are raised as HTTPExceptions with a plain `detail` string
    rather than as pydantic validators: main.py rewrites validator failures
    into a generic envelope, and the owner needs to be told WHICH rule the
    address broke.
    """
    require_org_owner(org)
    organization = (db.query(Organization)
                      .filter(Organization.id == org.org_id).one())
    try:
        organizations.set_slug(db, organization, body.slug,
                               actor_account_id=org.account_id)
    except organizations.SlugTakenError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except organizations.SlugError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=str(e))
    return OrgSlugResponse(org_id=organization.id, slug=organization.slug)

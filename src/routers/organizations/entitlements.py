"""
What the caller may open, in their active organization.

The UI hydrates its side menu from this instead of the hardcoded lists in
cmdlabs-ui/src/config/roles.ts, so a ceiling or tier change takes effect on the
next page load with no deploy.
"""
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.deps import db_dependency, org_dependency
from src.rate_limit import limiter
from src.services import modules
from src.utils.errors import handle_db_error

router = APIRouter()


class EntitlementsResponse(BaseModel):
    org_id: int
    org_slug: str
    tier_key: str
    is_owner: bool
    is_super_admin: bool
    data_scope: str
    org_status: str
    modules: List[str]
    # The org's whole ceiling, so an owner's admin UI can show what exists but
    # is not enabled. None for non-owners, who have no use for it.
    ceiling: Optional[List[str]] = None


@router.get("/me/entitlements", response_model=EntitlementsResponse)
@limiter.limit("120/minute")
async def my_entitlements(db: db_dependency, org: org_dependency, request: Request):
    try:
        return EntitlementsResponse(
            org_id=org.org_id,
            org_slug=org.org_slug,
            tier_key=org.tier_key,
            is_owner=org.is_owner,
            is_super_admin=org.is_super_admin,
            data_scope=org.data_scope,
            org_status=org.org_status,
            modules=modules.effective_modules(db, org),
            ceiling=(modules.ceiling_for(db, org.org_id)
                     if (org.is_owner or org.is_super_admin) else None),
        )
    except HTTPException:
        raise
    except Exception as e:
        raise handle_db_error(e, "[ENTITLEMENTS]")

"""
Tier management — the org owner's matrix.

A tier is a named bundle of modules. Tiers are NOT levels: each is an arbitrary
set, nothing requires one to be a superset of another, and two tiers may be
entirely disjoint. The only constraint is the org's ceiling, applied by
services.modules.clamp_to_ceiling.
"""
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from src.config.modules_registry import MODULES
from src.db.models import OrganizationTier
from src.deps import db_dependency, org_dependency
from src.rate_limit import limiter
from src.services import audit, modules
from src.utils.errors import handle_db_error

router = APIRouter()


class TierResponse(BaseModel):
    tier_key: str
    label: str
    modules: List[str]
    stripe_price_id: Optional[str] = None


class ModuleInfo(BaseModel):
    key: str
    label: str
    # False when the org's ceiling excludes it: the UI renders these disabled
    # with the reason, so an owner can see what exists and ask for it rather
    # than wondering why a module is missing.
    available: bool


class TiersPageResponse(BaseModel):
    org_id: int
    ceiling: List[str]
    all_modules: List[ModuleInfo]
    tiers: List[TierResponse]
    can_edit: bool


class UpdateTierModulesRequest(BaseModel):
    modules: List[str] = Field(description="Full replacement set of module keys")


def _require_owner(org):
    """Only an owner shapes their org's tiers.

    Platform super admins are deliberately NOT allowed here: administering an
    org means setting its ceiling, not reaching inside to redistribute what the
    owner chose to do with it.
    """
    if not org.is_owner:
        # 404 rather than 403 so the org admin surface does not confirm its own
        # existence to a member who cannot use it — the same choice
        # require_module and require_super_admin make.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Not found")


@router.get("/tiers", response_model=TiersPageResponse)
@limiter.limit("60/minute")
async def list_tiers(db: db_dependency, org: org_dependency, request: Request):
    """Owner only, matching the write path below.

    This returns the org's whole ceiling and every tier's module set. GET
    /me/entitlements deliberately withholds the ceiling from non-owners, so
    serving it here to any member would have made that restriction decorative —
    the same information by a different route.
    """
    try:
        _require_owner(org)
        ceiling = modules.ceiling_for(db, org.org_id)
        rows = (db.query(OrganizationTier)
                  .filter(OrganizationTier.org_id == org.org_id)
                  .order_by(OrganizationTier.id.asc()).all())
        return TiersPageResponse(
            org_id=org.org_id,
            ceiling=ceiling,
            all_modules=[
                ModuleInfo(key=m.key, label=m.label, available=m.key in set(ceiling))
                for m in MODULES
            ],
            tiers=[
                TierResponse(tier_key=t.tier_key, label=t.label,
                             modules=list(t.modules or []),
                             stripe_price_id=t.stripe_price_id)
                for t in rows
            ],
            can_edit=org.is_owner,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise handle_db_error(e, "[LIST TIERS]")


@router.put("/tiers/{tier_key}/modules", response_model=TierResponse)
@limiter.limit("30/minute")
async def set_tier_modules(
    tier_key: str,
    body: UpdateTierModulesRequest,
    db: db_dependency,
    org: org_dependency,
    request: Request,
):
    """Replace a tier's module set.

    A full replacement rather than add/remove: two owners editing the matrix
    concurrently would otherwise interleave into a set neither of them chose.
    """
    try:
        _require_owner(org)

        tier = (db.query(OrganizationTier)
                  .filter(OrganizationTier.org_id == org.org_id,
                          OrganizationTier.tier_key == tier_key).first())
        if not tier:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="Tier not found")

        before = list(tier.modules or [])
        tier.modules = modules.clamp_to_ceiling(db, org.org_id, body.modules)

        if tier.modules != before:
            audit.record(
                db,
                event_type=audit.TIER_MODULES_CHANGE,
                org_id=org.org_id,
                resource_type=audit.RESOURCE_TIER,
                resource_id=tier.id,
                resource_label=tier.tier_key,
                # What it BECAME, so the log answers "what changed to what"
                # rather than merely "something changed".
                detail=",".join(tier.modules) or "(none)",
                actor_account_id=org.account_id,
            )
        db.commit()
        db.refresh(tier)

        return TierResponse(tier_key=tier.tier_key, label=tier.label,
                            modules=list(tier.modules or []),
                            stripe_price_id=tier.stripe_price_id)
    except HTTPException:
        raise
    except Exception as e:
        raise handle_db_error(e, "[SET TIER MODULES]")

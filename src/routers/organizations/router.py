"""Organization-facing router: entitlements, overview, members, switcher.

The tiers router (the owner's tiers x modules matrix) is gone with
organization_tiers. What a member may open is now their ROLE, a constant in
config/roles_registry rather than a row any org could edit.
"""
from fastapi import APIRouter

from .entitlements import router as entitlements_router
from .members import router as members_router
from .mine import router as mine_router
from .overview import router as overview_router

router = APIRouter()

router.include_router(entitlements_router)
router.include_router(overview_router)
router.include_router(mine_router)
router.include_router(members_router)

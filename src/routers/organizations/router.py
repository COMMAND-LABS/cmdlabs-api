"""Organization-facing router: entitlements, overview, tiers, members, switcher."""
from fastapi import APIRouter

from .entitlements import router as entitlements_router
from .members import router as members_router
from .mine import router as mine_router
from .overview import router as overview_router
from .tiers import router as tiers_router

router = APIRouter()

router.include_router(entitlements_router)
router.include_router(overview_router)
router.include_router(mine_router)
router.include_router(tiers_router)
router.include_router(members_router)

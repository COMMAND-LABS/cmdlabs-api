"""Organization-facing router: entitlements and the owner's tiers matrix."""
from fastapi import APIRouter

from .entitlements import router as entitlements_router
from .tiers import router as tiers_router

router = APIRouter()

router.include_router(entitlements_router)
router.include_router(tiers_router)

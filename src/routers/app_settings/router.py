"""
App settings router: the authenticated caller's own application preferences,
scoped to the org they are currently acting in.
"""
from fastapi import APIRouter

from .get import router as get_router
from .update import router as update_router

router = APIRouter()

router.include_router(get_router)
router.include_router(update_router)

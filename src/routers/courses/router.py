"""Courses: per-org enablement of code-backed course experiences."""
from fastapi import APIRouter

from .crud import router as crud_router
from .grants import router as grants_router

router = APIRouter()

router.include_router(crud_router)
router.include_router(grants_router)

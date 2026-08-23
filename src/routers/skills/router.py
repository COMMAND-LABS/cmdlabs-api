"""
Skills router - combines all skill CRUD endpoints.
"""
from fastapi import APIRouter

from .create import router as create_router
from .list import router as list_router
from .get import router as get_router
from .update import router as update_router
from .delete import router as delete_router
from .templates import router as templates_router

router = APIRouter()

# Templates first: "/templates/..." must win over "/{skill_id}".
router.include_router(templates_router)
router.include_router(create_router)
router.include_router(list_router)
router.include_router(get_router)
router.include_router(update_router)
router.include_router(delete_router)

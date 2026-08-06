"""Spaces: the platform's second container. See crud.py for the boundary."""
from fastapi import APIRouter

from .crud import router as crud_router

router = APIRouter()
router.include_router(crud_router)

"""
Agents router for managing AI agents.
Allows creation and management of agents with their configurations.

/api/agents opens for the Agents OR the Agent Chat module (config/
modules_registry): someone who only chats with agents shared with them still
needs to list and read those agents. Authoring — create, edit, delete, share —
is the Agents module alone, gated here.
"""
from fastapi import APIRouter, Depends

from src.deps import require_module

# Import endpoint routers
from .create import router as create_router
from .list import router as list_router
from .get import router as get_router
from .update import router as update_router
from .delete import router as delete_router
from .grants.router import router as grants_router

router = APIRouter()

# Using agents: list the ones you can use, read one.
router.include_router(list_router)
router.include_router(get_router)

# Authoring agents: Agents module only.
authoring = APIRouter(dependencies=[Depends(require_module("agents"))])
authoring.include_router(create_router)
authoring.include_router(update_router)
authoring.include_router(delete_router)
authoring.include_router(grants_router)
router.include_router(authoring)

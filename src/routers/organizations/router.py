"""Organization-facing router: entitlements, overview, members, switcher.

The tiers router (the owner's tiers x modules matrix) is gone with
organization_tiers. What a member may open is now their ROLE, a constant in
config/roles_registry rather than a row any org could edit.
"""
from fastapi import APIRouter

from .active import router as active_router
from .entitlements import router as entitlements_router
from .invitations import router as invitations_router
from .members import router as members_router
from .mine import router as mine_router
from .overview import router as overview_router
from .slug import router as slug_router

router = APIRouter()

router.include_router(entitlements_router)
router.include_router(overview_router)
router.include_router(mine_router)
router.include_router(active_router)
# /by-slug/{slug} is the other unauthenticated route in this package; matched
# ahead of members for the same reason as invitations below.
router.include_router(slug_router)
# BEFORE members. Both declare paths under /invitations, and members' owner-side
# routes are the ones with a trailing verb, so nothing here is shadowed — but
# the invitee's routes are the ones an unauthenticated visitor hits, and having
# them matched first keeps that ordering a decision rather than an accident.
router.include_router(invitations_router)
router.include_router(members_router)

"""
Platform-admin router — super admin only.

Administration of orgs, not access to their data. Every endpoint here is gated
by require_super_admin, which is deliberately independent of OrgContext: super
admins would otherwise need to belong to an org to discover which orgs exist.
"""
from fastapi import APIRouter

from .organization_detail import router as org_detail_router
from .organizations import router as org_admin_router
from .list_organizations import router as list_organizations_router

router = APIRouter()

router.include_router(list_organizations_router)
router.include_router(org_admin_router)
router.include_router(org_detail_router)

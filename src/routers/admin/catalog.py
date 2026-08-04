"""
Catalog administration. Super admin only.

Publish a platform-owned agent or knowledge base, then grant it to client orgs
— or to one department inside a client org. One published item, many grants:
that is what makes a lesson have a single live version rather than a copy per
client.
"""
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from src.db.catalog_models import CatalogGrant, CatalogItem
from src.db.models import AccessGroup, Organization
from src.deps import db_dependency, super_admin_dependency
from src.rate_limit import limiter
from src.services import audit, catalog
from src.utils.errors import handle_db_error

router = APIRouter()


# ── models ───────────────────────────────────────────────────────────────────

class PublishRequest(BaseModel):
    resource_type: str = Field(description="'agent' or 'vector_store'")
    resource_id: int
    title: str
    description: Optional[str] = None


class GrantRequest(BaseModel):
    org_id: int
    group_id: Optional[int] = Field(
        default=None,
        description="Restrict to one access group inside that org. "
                    "Omit to grant to the whole org.")


class CatalogGrantResponse(BaseModel):
    id: int
    org_id: int
    org_name: Optional[str] = None
    group_id: Optional[int] = None
    group_name: Optional[str] = None
    granted_at: Optional[datetime] = None


class CatalogItemResponse(BaseModel):
    id: int
    resource_type: str
    resource_id: int
    title: str
    description: Optional[str] = None
    published_at: Optional[datetime] = None
    grants: List[CatalogGrantResponse] = []


def _serialize(db, item: CatalogItem) -> CatalogItemResponse:
    grants = db.query(CatalogGrant).filter(
        CatalogGrant.catalog_item_id == item.id).all()
    org_names = dict(db.query(Organization.id, Organization.name).all())
    group_names = dict(db.query(AccessGroup.id, AccessGroup.name).all()) if grants else {}
    return CatalogItemResponse(
        id=item.id,
        resource_type=item.resource_type,
        resource_id=item.resource_id,
        title=item.title,
        description=item.description,
        published_at=item.published_at,
        grants=[
            CatalogGrantResponse(
                id=g.id, org_id=g.org_id, org_name=org_names.get(g.org_id),
                group_id=g.group_id, group_name=group_names.get(g.group_id),
                granted_at=g.granted_at,
            )
            for g in grants
        ],
    )


# ── endpoints ────────────────────────────────────────────────────────────────

@router.get("/catalog", response_model=List[CatalogItemResponse])
@limiter.limit("30/minute")
async def list_catalog(db: db_dependency, staff: super_admin_dependency, request: Request):
    try:
        items = db.query(CatalogItem).order_by(CatalogItem.id.desc()).all()
        return [_serialize(db, i) for i in items]
    except HTTPException:
        raise
    except Exception as e:
        raise handle_db_error(e, "[CATALOG LIST]")


@router.post("/catalog", status_code=status.HTTP_201_CREATED,
             response_model=CatalogItemResponse)
@limiter.limit("20/minute")
async def publish_resource(
    body: PublishRequest,
    db: db_dependency,
    staff: super_admin_dependency,
    request: Request,
):
    """Publish a PLATFORM-owned resource to the catalog.

    The guard in catalog.assert_publishable is the one that matters: without
    it, a tenant-owned resource could be published and then granted to another
    tenant, which is exactly the cross-org path the boundary forbids.
    """
    try:
        item = catalog.publish(
            db,
            resource_type=body.resource_type,
            resource_id=body.resource_id,
            title=body.title,
            description=body.description,
            published_by_account_id=staff.id,
        )
        db.flush()
        audit.record_catalog(db, event_type=audit.CATALOG_PUBLISH,
                             item_id=item.id, title=item.title,
                             actor_account_id=staff.id)
        db.commit()
        db.refresh(item)
        return _serialize(db, item)
    except catalog.NotPublishableError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=str(exc))
    except HTTPException:
        raise
    except Exception as e:
        raise handle_db_error(e, "[CATALOG PUBLISH]")


@router.delete("/catalog/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("20/minute")
async def unpublish(item_id: int, db: db_dependency,
                    staff: super_admin_dependency, request: Request):
    """Unpublish. Every grant goes with it (FK cascade), so access stops
    everywhere at once — the underlying resource is untouched."""
    try:
        item = db.query(CatalogItem).filter(CatalogItem.id == item_id).first()
        if not item:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="Catalog item not found")
        audit.record_catalog(db, event_type=audit.CATALOG_UNPUBLISH,
                             item_id=item.id, title=item.title,
                             actor_account_id=staff.id)
        db.delete(item)
        db.commit()
    except HTTPException:
        raise
    except Exception as e:
        raise handle_db_error(e, "[CATALOG UNPUBLISH]")


@router.post("/catalog/{item_id}/grants", status_code=status.HTTP_201_CREATED,
             response_model=CatalogGrantResponse)
@limiter.limit("30/minute")
async def grant_catalog_item(
    item_id: int,
    body: GrantRequest,
    db: db_dependency,
    staff: super_admin_dependency,
    request: Request,
):
    """Grant a published item to an org, or to one department inside it."""
    try:
        item = db.query(CatalogItem).filter(CatalogItem.id == item_id).first()
        if not item:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="Catalog item not found")

        grant = catalog.grant_to_org(
            db, item=item, org_id=body.org_id, group_id=body.group_id,
            granted_by_account_id=staff.id,
        )
        db.flush()
        audit.record_catalog(db, event_type=audit.CATALOG_GRANT,
                             item_id=item.id, title=item.title,
                             target_org_id=grant.org_id, group_id=grant.group_id,
                             actor_account_id=staff.id)
        db.commit()
        db.refresh(grant)

        org_name = db.query(Organization.name).filter(
            Organization.id == grant.org_id).scalar()
        group_name = db.query(AccessGroup.name).filter(
            AccessGroup.id == grant.group_id).scalar() if grant.group_id else None
        return CatalogGrantResponse(
            id=grant.id, org_id=grant.org_id, org_name=org_name,
            group_id=grant.group_id, group_name=group_name,
            granted_at=grant.granted_at,
        )
    except catalog.NotPublishableError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=str(exc))
    except HTTPException:
        raise
    except Exception as e:
        raise handle_db_error(e, "[CATALOG GRANT]")


@router.delete("/catalog/{item_id}/grants/{grant_id}",
               status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("30/minute")
async def revoke_catalog_grant(
    item_id: int, grant_id: int, db: db_dependency,
    staff: super_admin_dependency, request: Request,
):
    try:
        grant = db.query(CatalogGrant).filter(
            CatalogGrant.id == grant_id,
            CatalogGrant.catalog_item_id == item_id,
        ).first()
        if not grant:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="Grant not found")
        item = db.query(CatalogItem).filter(CatalogItem.id == item_id).first()
        audit.record_catalog(db, event_type=audit.CATALOG_REVOKE,
                             item_id=item_id,
                             title=item.title if item else None,
                             target_org_id=grant.org_id, group_id=grant.group_id,
                             actor_account_id=staff.id)
        db.delete(grant)
        db.commit()
    except HTTPException:
        raise
    except Exception as e:
        raise handle_db_error(e, "[CATALOG REVOKE]")

"""
List access grants for a knowledge base (index owner only). Reads AccessGrant.
"""
from fastapi import APIRouter, HTTPException, status, Request
from typing import List
from src.deps import org_dependency, db_dependency, jwt_dependency, account_id_from_claims
from src.services.org_scope import AGENT, VECTOR_STORE, resource_predicate, scoped_resources
from src.db.models import VectorStore, AccessGrant
from src.services import access
from src.services.access_admin import grant_label
from .models import VectorStoreAccessGrantResponse
from src.utils.errors import handle_db_error
from src.rate_limit import limiter

router = APIRouter()


@router.get("/grants", response_model=List[VectorStoreAccessGrantResponse])
@limiter.limit("30/minute")
async def list_grants(
    index_name: str,
    db: db_dependency,
    jwt: jwt_dependency,
    org: org_dependency,
    request: Request,
):
    """List the people this knowledge base is shared with. Index owner only.

    Space shares were not listed here either: they belonged to the space.
    Spaces are gone, so grants are now the whole list.
    """
    try:
        account_id = account_id_from_claims(jwt)
        index_name = index_name.strip()

        store = db.query(VectorStore).filter(
            resource_predicate(VectorStore, org),
            VectorStore.index_name == index_name,
        ).first()
        if not store:
            return []

        grants = (
            db.query(AccessGrant)
            .filter(
                AccessGrant.resource_type == access.VECTOR_STORE,
                AccessGrant.resource_id == store.id,
            )
            .order_by(AccessGrant.created_at.desc())
            .all()
        )

        return [
            VectorStoreAccessGrantResponse(
                id=g.id,
                owner_account_id=account_id,
                index_name=index_name,
                grantee_account_id=g.principal_id,
                label=grant_label(db, g),
                role=g.role,
                created_at=g.created_at,
            )
            for g in grants
        ]
    except HTTPException:
        raise
    except Exception as e:
        raise handle_db_error(e, "[LIST VS GRANTS]")

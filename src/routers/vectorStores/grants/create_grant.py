"""
Share a knowledge base with one named person (index owner only).

Writes a unified AccessGrant (resource_type='vector_store', role 'read'|'write')
keyed by the VectorStore row id.
"""
from fastapi import APIRouter, HTTPException, status, Request
from src.deps import org_dependency, db_dependency, jwt_dependency, account_id_from_claims
from src.db.models import AccessGrant
from src.services import access
from src.services.access_admin import resolve_grantee, upsert_grant, record_access_event
from ..helpers import get_or_create_vector_store
from .models import CreateVectorStoreGrantRequest, VectorStoreAccessGrantResponse
from src.rate_limit import limiter

router = APIRouter()


@router.post("/grants", response_model=VectorStoreAccessGrantResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("10/minute")
async def create_grant(
    body: CreateVectorStoreGrantRequest,
    db: db_dependency,
    jwt: jwt_dependency,
    org: org_dependency,
    request: Request,
):
    """
    Share one of your knowledge bases with one other person in your org.

    You can only share an index reachable by your own Pinecone key (you are the
    owner). role 'read' = view; 'write' = ingest/edit.

    Sharing with a GROUP of people meant putting the knowledge base in a SPACE.
    Spaces are gone, so there is no such arm today. It was read-only and crossed org
    boundaries, which is exactly why it is a different endpoint.
    """
    account_id = account_id_from_claims(jwt)
    index_name = body.index_name.strip()
    if not index_name:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="index_name is required")

    principal_type, principal_id, label = resolve_grantee(
        db,
        caller_account_id=account_id,
        grantee_email=body.granteeEmail,
    )

    store = get_or_create_vector_store(db, account_id, index_name, org_id=org.org_id)

    existing = db.query(AccessGrant).filter(
        AccessGrant.principal_type == principal_type,
        AccessGrant.principal_id == principal_id,
        AccessGrant.resource_type == access.VECTOR_STORE,
        AccessGrant.resource_id == store.id,
    ).first()
    if existing and existing.role == body.role:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This knowledge base is already shared with that person at that role")

    grant = upsert_grant(
        db,
        org_id=org.org_id,
        principal_type=principal_type,
        principal_id=principal_id,
        resource_type=access.VECTOR_STORE,
        resource_id=store.id,
        role=body.role,
    )
    record_access_event(
        db,
        event_type="role_change" if existing else "create",
        actor_account_id=account_id,
        resource_type=access.VECTOR_STORE,
        resource_id=store.id,
        principal_type=principal_type,
        principal_id=principal_id,
        role=body.role,
    )
    db.commit()
    db.refresh(grant)

    return VectorStoreAccessGrantResponse(
        id=grant.id,
        owner_account_id=account_id,
        index_name=index_name,
        grantee_account_id=principal_id,
        label=label,
        role=grant.role,
        created_at=grant.created_at,
    )

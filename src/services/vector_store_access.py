"""
Knowledge-base (vector store) access control — over the unified resolver.

Access is now stored as AccessGrant rows (resource_type='vector_store',
role 'read'|'write') keyed by the VectorStore row id, and resolved by
services/access.py. These helpers keep the index-centric signatures the
vectorStores endpoints use (index_name + owner_account_id), translating to the
VectorStore id under the hood.

Permission is explicit per grant (read vs write). Every vector-store endpoint
still funnels through authorize_vector_store, which decides WHICH account's
resources the request runs against (always the owner) and whether the caller is
allowed.

A knowledge base can also be reached by SPACE membership, and that arm is
read-only by construction — it is consulted for read and never for write, so a
space owner sharing their KB is offering it to be consulted, not edited. See
org_scope.shares_resource.
"""
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.db.models import VectorStore
from src.services import access
from src.services.org_scope import VECTOR_STORE as _VECTOR_STORE, shares_resource


def _vector_store_id(db: Session, owner_account_id: int, index_name: str):
    row = (
        db.query(VectorStore.id)
        .filter(
            VectorStore.owner_account_id == owner_account_id,
            VectorStore.index_name == index_name,
        )
        .first()
    )
    return row[0] if row else None


def _space_shared_ids(db: Session, account_id: int) -> set:
    """VectorStore ids reaching this account through any space it is in."""
    from src.db.space_models import SpaceMember, SpaceResource

    rows = (db.query(SpaceResource.resource_id)
              .join(SpaceMember, SpaceMember.space_id == SpaceResource.space_id)
              .filter(SpaceResource.resource_type == _VECTOR_STORE,
                      SpaceMember.account_id == account_id)
              .all())
    return {r[0] for r in rows}


def authorize_vector_store(
    db: Session,
    caller_account_id: int,
    index_name: str,
    owner_account_id: int | None,
    *,
    require_write: bool,
    org_id: int | None = None,
) -> int:
    """Authorize access to knowledge base ``index_name`` and return its OWNER id.

    - owner_account_id None / == caller -> the caller's OWN KB: full access.
    - otherwise -> a SHARED KB: the caller must hold a read grant on the
      VectorStore, or belong to a space it has been shared into. Write needs
      a write GRANT — a space share never confers it.

    404 if no read access, 403 if read-only but write required.

    ``org_id`` confines the grant lookup to one organization; pass it whenever a
    request context exists. Without it a grant recorded in another org still
    resolves, and this helper is the gate every vector-store endpoint funnels
    through.
    """
    if owner_account_id is None or owner_account_id == caller_account_id:
        return caller_account_id

    vs_id = _vector_store_id(db, owner_account_id, index_name)
    if vs_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Knowledge base not found")

    via_space = shares_resource(db, caller_account_id, _VECTOR_STORE, vs_id)
    if not via_space and not access.can_access(
        db, caller_account_id, access.VECTOR_STORE, vs_id, required="read",
        org_id=org_id
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Knowledge base not found")

    if require_write and not access.can_access(
        db, caller_account_id, access.VECTOR_STORE, vs_id, required="write", org_id=org_id
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You have view-only access to this shared knowledge base.",
        )

    return owner_account_id


def list_shared_vector_stores(db: Session, account_id: int,
                              org_id: int | None = None) -> list[dict]:
    """Knowledge bases shared with ``account_id`` — by grant, or by a space.

    Returns one entry per (owner, index) the caller can reach, with ``can_write``
    True when the caller holds a write grant. A space share is never writable,
    so anything reached only that way comes back read-only.

    ``org_id`` confines the GRANT arm to that organization. The space arm is
    deliberately not confined: its whole purpose is to reach across orgs, and
    it is safe because only a resource's owner can put it in a space.
    """
    readable_ids = access.accessible_resource_ids(db, account_id, access.VECTOR_STORE,
                                                  required="read", org_id=org_id)
    readable_ids = set(readable_ids) | _space_shared_ids(db, account_id)
    if not readable_ids:
        return []
    writable_ids = access.accessible_resource_ids(db, account_id, access.VECTOR_STORE,
                                                  required="write", org_id=org_id)

    rows = (
        db.query(VectorStore.id, VectorStore.owner_account_id, VectorStore.index_name)
        .filter(VectorStore.id.in_(readable_ids))
        .all()
    )
    return [
        {
            "owner_account_id": owner_account_id,
            "index_name": index_name,
            "can_write": vs_id in writable_ids,
        }
        for vs_id, owner_account_id, index_name in rows
    ]

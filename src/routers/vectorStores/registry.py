"""
The caller's knowledge bases as the PLATFORM knows them.

Distinct from GET /indexes, which asks Pinecone what indexes exist under the
caller's key. This asks the database which of them the platform has a row for —
and that row id is what every sharing mechanism refers to.

The distinction is not pedantry. An index is a thing in Pinecone; a VectorStore
is the platform's handle on it, carrying its org, its credential bindings and
its grants. You cannot share what has no handle, so a UI offering "put this in
a container" has to pick from THIS list rather than from Pinecone's.

Read-only, and deliberately so. It would be easy to make this create a missing
row on the way past, which is exactly how a GET starts writing.
"""
import logging
from typing import List

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.db.models import VectorStore
from src.deps import db_dependency, org_dependency
from src.rate_limit import limiter
from src.services.org_scope import resource_predicate
from src.utils.errors import handle_db_error

logger = logging.getLogger(__name__)

router = APIRouter()


class RegisteredIndex(BaseModel):
    id: int
    index_name: str
    is_owner: bool


@router.get("/registry", response_model=List[RegisteredIndex])
@limiter.limit("60/minute")
async def registered_indexes(db: db_dependency, org: org_dependency,
                             request: Request):
    """Knowledge bases in this org the caller can see, with their row ids.

    Scoped by resource_predicate, the same rule the knowledge-base list uses:
    this org, and either marked org-visible or created by the caller. `is_owner`
    says which of them the caller may actually share — sharing requires
    ownership and the server re-checks it, so this is a hint for the UI, never
    the decision.
    """
    try:
        rows = (db.query(VectorStore.id, VectorStore.index_name,
                         VectorStore.owner_account_id)
                  .filter(resource_predicate(VectorStore, org))
                  .order_by(VectorStore.index_name.asc())
                  .all())
        return [
            RegisteredIndex(id=vid, index_name=name,
                            is_owner=(owner == org.account_id))
            for vid, name, owner in rows
        ]
    except HTTPException:
        raise
    except Exception as e:
        raise handle_db_error(e, "[LIST KB REGISTRY]")

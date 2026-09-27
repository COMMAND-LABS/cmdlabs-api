import logging
from fastapi import APIRouter, Request
from src.core.schemas.DeleteVectorsRequest import DeleteVectorsRequest
import os
from src.core.clients import pc
from src.deps import jwt_dependency, account_id_from_claims
from src.rate_limit import limiter
from src.routers.vectorStores.list_namespace_files import collect_ids_where
from ._shared import SIMILARITY_SEARCH_NAMESPACE, is_upload_owned_by

logger = logging.getLogger(__name__)

router = APIRouter()

# Pinecone accepts at most 1000 ids per delete call.
DELETE_BATCH = 1000


@router.delete("/delete-vectors")
@limiter.limit("10/minute")
def delete_vectors_in_namespace(request_body: DeleteVectorsRequest, decoded_jwt: jwt_dependency, request: Request):
    """
    Delete the CALLER's uploaded vectors from the similarity_search namespace.

    The namespace is shared by every account, so this never deletes the whole
    namespace: it enumerates it and deletes only the vectors the caller
    uploaded (see _shared.py). `request_body.namespace` is not consulted; this
    endpoint only ever touches similarity_search.

    `truncated` is True when the namespace was larger than the scan cap, in
    which case some of the caller's vectors may remain and the call can be
    repeated.
    """
    namespace = SIMILARITY_SEARCH_NAMESPACE
    account_id = account_id_from_claims(decoded_jwt)
    try:
        index = pc.Index(os.getenv("PINECONE_ALL_MINILM_L6_V2_INDEX"))

        ids, truncated = collect_ids_where(
            index, namespace, lambda meta: is_upload_owned_by(meta, account_id)
        )
        for i in range(0, len(ids), DELETE_BATCH):
            index.delete(ids=ids[i:i + DELETE_BATCH], namespace=namespace)

        logger.info(
            "[DELETE VECTORS] account %s deleted %d vectors from %s (truncated=%s)",
            account_id, len(ids), namespace, truncated,
        )
        return {
            "success": True,
            "deleted_count": len(ids),
            "namespace": namespace,
            "truncated": truncated,
        }
    except Exception as e:
        logger.error("[DELETE VECTORS] %s: %s", type(e).__name__, e)
        return {
            "success": False,
            "namespace": request_body.namespace,
            "error": "An unexpected error occurred. Please try again.",
        }

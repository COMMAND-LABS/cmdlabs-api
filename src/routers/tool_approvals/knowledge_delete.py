"""Execute an approved knowledgeDelete: remove the vectors from the knowledge base.

A whole document is re-enumerated here rather than trusting ids captured when
the agent asked: whatever passages the document has at approval time are the
ones removed. Specific passages are re-fetched, so ones already gone are
skipped and each removed passage is counted against its own document.

Every delete is logged like the knowledge base page's own per-file delete
(routers/vectorStores/delete_file_vectors.py): operation DELETE with the
filename set and vectors_deleted counted, one row per document. The filename
must always be set: a DELETE row without filenames is read as "the whole
section was wiped" by the ingestion-log fallback (list_namespace_files).

Authorization is re-checked at approval time, as for knowledgeWrite.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter

from fastapi import HTTPException
from pinecone import Pinecone
from sqlalchemy.orm import Session

from src.db.models import PendingToolApproval
from src.routers.vectorStores.delete_file_vectors import DELETE_BATCH
from src.routers.vectorStores.helpers import get_pinecone_api_key_for_index
from src.routers.vectorStores.list_namespace_files import (
    collect_ids_for_filename,
    filename_of,
    invalidate_namespace_cache,
    metadata_by_id,
)
from src.services.ingestion_log import record_ingestion_log
from src.services.vector_store_access import authorize_vector_store

logger = logging.getLogger(__name__)

MAX_PASSAGES = 20


class _TooLarge(Exception):
    pass


def delete_target(index, namespace: str, *, filename: str | None,
                  passage_ids: list[str] | None) -> Counter:
    """Delete the target's vectors. Returns {filename: vectors deleted}."""
    if filename:
        ids, truncated = collect_ids_for_filename(index, namespace, filename)
        if truncated:
            raise _TooLarge()
        per_file = Counter({filename: len(ids)}) if ids else Counter()
    else:
        found = metadata_by_id(index, namespace, passage_ids)
        ids = [vid for vid in passage_ids if vid in found]
        per_file = Counter(filename_of(found[vid]) for vid in ids)

    for i in range(0, len(ids), DELETE_BATCH):
        index.delete(ids=ids[i:i + DELETE_BATCH], namespace=namespace)
    return per_file


async def execute_knowledge_delete(
    db: Session,
    approval: PendingToolApproval,
    *,
    account_id: int,
    user_email: str,
) -> str:
    """Delete the vectors. Marks the approval approved on success and returns
    the user-facing message. Raises HTTPException on failure and leaves the
    approval pending so it can be retried or rejected."""
    payload = approval.payload or {}
    index_name = payload.get("index")
    namespace = payload.get("namespace")
    owner_account_id = payload.get("owner_account_id")
    filename = payload.get("filename")
    passage_ids = payload.get("passage_ids")
    reason = (payload.get("reason") or "").strip()

    passages_ok = (isinstance(passage_ids, list) and 0 < len(passage_ids) <= MAX_PASSAGES
                   and all(isinstance(p, str) and p for p in passage_ids))
    if not all([index_name, namespace, owner_account_id]) or (
            bool(filename) == bool(passage_ids)) or (passage_ids and not passages_ok):
        raise HTTPException(status_code=422, detail="Approval payload is incomplete.")

    authorize_vector_store(db, account_id, index_name, owner_account_id, require_write=True)
    api_key = get_pinecone_api_key_for_index(db, owner_account_id, index_name)

    def _run() -> Counter:
        index = Pinecone(api_key=api_key).Index(index_name)
        return delete_target(index, namespace, filename=filename, passage_ids=passage_ids)

    try:
        per_file = await asyncio.to_thread(_run)
    except _TooLarge:
        raise HTTPException(status_code=409, detail=(
            "This knowledge base is too large to remove the document from chat. "
            "Delete it on the knowledge base page instead."))
    except Exception:
        logger.exception("knowledgeDelete approval %s: delete failed", approval.id)
        raise HTTPException(status_code=500, detail="Failed to remove it. Please try again.")

    approval.status = "approved"
    db.commit()
    invalidate_namespace_cache(owner_account_id, index_name, namespace)
    deleted = sum(per_file.values())
    logger.info("knowledgeDelete approval %s removed %d vectors from %s/%s",
                approval.id, deleted, index_name, namespace)

    # Logged after the approval commits, best effort, as knowledgeWrite does:
    # the vectors are already gone, so a logging failure must not leave the
    # approval pending.
    for name, count in per_file.items():
        record_ingestion_log(
            db,
            log_prefix=f"knowledgeDelete approval {approval.id}:",
            account_id=owner_account_id,
            provider="pinecone",
            index_name=index_name,
            namespace=namespace,
            filenames=[name],
            comment=(f"knowledge_delete by {user_email} (approval {approval.id}): {reason}")[:1000],
            vectors_added=0,
            vectors_deleted=count,
            vectors_failed=0,
            operation_type="DELETE",
            status="SUCCESS",
        )

    if not deleted:
        return "Nothing to remove: it was already gone from the knowledge base."
    if filename:
        return f"Removed '{filename}' ({deleted} passages) from {index_name}/{namespace}."
    return f"Removed {deleted} passage(s) from {index_name}/{namespace}."

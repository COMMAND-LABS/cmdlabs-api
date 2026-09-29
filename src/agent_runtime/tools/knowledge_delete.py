"""knowledgeDelete — remove information from a knowledge base, after a human says yes.

The counterpart of knowledgeWrite. The model names what to remove, either
  - a whole document (``filename``: every passage that came from it, e.g. a
    note knowledge_write saved or a PDF someone uploaded), or
  - specific passages (``passage_ids``: the ``id`` of vector_search results),
and the tool queues that for the person in the chat. Nothing is deleted here:
routers/tool_approvals/knowledge_delete.py does it once they approve.

The tool looks the target up before queuing, so the approver sees the actual
text that would go (not just an id), and a name or id that matches nothing is
an error the model can correct instead of an empty approval.

Why approval: a delete from a knowledge base the whole team searches cannot
be undone from the app, and the model decides what to delete from whatever the
current user said. Authorization is the same as writing: the KB owner always
may; a shared member needs a write grant, checked at build time and again at
approval time.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import HTTPException
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.routers.vectorStores.list_namespace_files import (
    NO_FILENAME,
    collect_ids_for_filename,
    filename_of,
    metadata_by_id,
)
from src.services.vector_store_access import authorize_vector_store

from .hitl_email_base import queue_tool_approval
from .pinecone_helpers import load_pinecone_index
from .sessions import resolve_session_factory

logger = logging.getLogger(__name__)

TOOL_TYPE = "knowledgeDelete"
TOOL_NAME = "knowledge_delete"
MAX_PASSAGES = 20
PREVIEW_PASSAGES = 5
SNIPPET_CHARS = 600


class KnowledgeTargetError(ValueError):
    """What to delete can't be resolved; the message is written for the model."""


class DeleteInput(BaseModel):
    reason: str = Field(min_length=1, max_length=1000,
                        description="Why it should be removed, e.g. outdated or wrong. Shown to the approver.")
    filename: str | None = Field(
        default=None, min_length=1, max_length=512,
        description="Remove a whole document: the `filename` in a search result's metadata.")
    passage_ids: list[str] | None = Field(
        default=None, min_length=1, max_length=MAX_PASSAGES,
        description="Remove only these passages: the `id` of search results. "
                    "Use instead of filename when only part of a document is wrong.")


def _snippet(meta: dict | None) -> str:
    text = (meta or {}).get("content") or (meta or {}).get("text") or ""
    text = str(text).strip()
    return text if len(text) <= SNIPPET_CHARS else text[:SNIPPET_CHARS].rstrip() + "…"


def _passage(vid: str, meta: dict | None) -> dict[str, Any]:
    return {"id": vid, "filename": filename_of(meta), "text": _snippet(meta)}


def resolve_target(index, namespace: str, *, filename: str | None,
                   passage_ids: list[str] | None) -> dict[str, Any]:
    """What a delete would remove, looked up in Pinecone: {"target": {...},
    "passages": [...], "count": n}. Raises KnowledgeTargetError when the target
    is ambiguous, missing or too large to enumerate."""
    if bool(filename) == bool(passage_ids):
        raise KnowledgeTargetError("Give either filename (a whole document) or passage_ids, not both.")

    if passage_ids:
        wanted = list(dict.fromkeys(passage_ids))
        found = metadata_by_id(index, namespace, wanted)
        if not found:
            raise KnowledgeTargetError("None of those passages are in this knowledge base. "
                             "Use the `id` of a search result.")
        ids = [vid for vid in wanted if vid in found]
        return {"target": {"passage_ids": ids},
                "passages": [_passage(vid, found[vid]) for vid in ids],
                "count": len(ids),
                "missing": [vid for vid in wanted if vid not in found]}

    if filename == NO_FILENAME:
        raise KnowledgeTargetError("Passages without a document name can only be removed by passage_ids.")
    ids, truncated = collect_ids_for_filename(index, namespace, filename)
    if truncated:
        raise KnowledgeTargetError("This knowledge base is too large to remove a whole document from "
                         "chat. Remove specific passages by id, or delete the document on the "
                         "knowledge base page.")
    if not ids:
        raise KnowledgeTargetError(f"No document named '{filename}' in this knowledge base. "
                         "Use the `filename` from a search result's metadata.")
    sample = metadata_by_id(index, namespace, ids[:PREVIEW_PASSAGES])
    return {"target": {"filename": filename},
            "passages": [_passage(vid, sample.get(vid)) for vid in ids[:PREVIEW_PASSAGES]],
            "count": len(ids),
            "missing": []}


async def create_knowledge_delete_tool(
    tool_config: dict[str, Any],
    account_id: int,
    db: Session,
    auth_token: str | None = None,
    **kwargs,
) -> StructuredTool:
    index_name = tool_config.get("index")
    namespace = tool_config.get("namespace")
    if (tool_config.get("provider") or "").lower() != "pinecone":
        raise ValueError("knowledgeDelete: provider must be 'pinecone'")
    if not index_name or not namespace:
        raise ValueError("knowledgeDelete: index and namespace are required")

    owner_account_id = kwargs.get("agent_owner_account_id", account_id)
    org_scope = kwargs.get("org_scope")
    try:
        authorize_vector_store(
            db, account_id, index_name, owner_account_id,
            require_write=True,
            org_id=getattr(org_scope, "org_id", None),
        )
    except HTTPException as exc:
        raise ValueError(f"knowledgeDelete: caller may not write to '{index_name}' ({exc.detail})") from exc

    setup = load_pinecone_index(tool_config, account_id, db, **kwargs)
    if not setup:
        raise ValueError(f"knowledgeDelete: could not open '{index_name}'")
    index = setup[0]

    agent_id: int | None = kwargs.get("agent_id")
    chat_session_id: int | None = kwargs.get("chat_session_id_pk")
    session_factory = resolve_session_factory(kwargs)

    async def _delete(reason: str, filename: str | None = None,
                      passage_ids: list[str] | None = None) -> str:
        try:
            found = await asyncio.to_thread(
                resolve_target, index, namespace, filename=filename, passage_ids=passage_ids)
        except KnowledgeTargetError as exc:
            return json.dumps({"error": str(exc)})
        except Exception:
            logger.exception("[KNOWLEDGE DELETE] lookup failed in %s/%s", index_name, namespace)
            return json.dumps({"error": "Could not look that up in the knowledge base. Try again."})

        what = (f"document '{filename}' ({found['count']} passages)" if filename
                else f"{found['count']} passage(s)")
        missing = found["missing"]
        return await queue_tool_approval(
            # The CALLER approves, as for knowledgeWrite.
            account_id=account_id,
            agent_id=agent_id,
            chat_session_id=chat_session_id,
            session_factory=session_factory,
            tool_type=TOOL_TYPE,
            payload={
                "owner_account_id": owner_account_id,
                "index": index_name,
                "namespace": namespace,
                "reason": reason.strip(),
                **found["target"],
            },
            preview={
                "index": index_name,
                "namespace": namespace,
                "reason": reason.strip(),
                "filename": filename,
                "passage_count": found["count"],
                "passages": found["passages"],
            },
            message=(
                f"Removing {what} is waiting for the user's approval. Nothing has been "
                "removed yet."
                + (f" Not found, so left out: {', '.join(missing)}." if missing else "")
            ),
        )

    return StructuredTool.from_function(
        coroutine=_delete,
        name=TOOL_NAME,
        description=tool_config.get("description") or (
            "Remove outdated or wrong information from the team's knowledge base: a whole "
            "document (by filename) or specific passages (by id), both taken from search "
            "results, so search first. The user reviews exactly what will be removed and "
            "must approve it before anything is deleted."
        ),
        args_schema=DeleteInput,
    )

"""knowledgeWrite — save a note to a knowledge base, after a human says yes.

The prototype's `add_knowledge` appended to a markdown file. Here the note
goes through the same approval queue as outgoing email (queue_tool_approval),
and on approval becomes a markdown file in the KB's bucket that the text-ingest
pipeline turns into vectors — see routers/tool_approvals/knowledge_write.py.

Why approval: the model writes to a knowledge base the whole team searches,
based on whatever the current user told it. That is exactly the shape of
action a person should see before it lands.

Who may add the tool to an agent is one question; who may WRITE to this KB is
another. The second is checked at build time against the vector-store grants:
the KB owner always may; a shared member needs a write grant.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import HTTPException
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field, create_model
from sqlalchemy.orm import Session

from src.services.vector_store_access import authorize_vector_store

from .hitl_email_base import queue_tool_approval
from .sessions import resolve_session_factory

logger = logging.getLogger(__name__)

TOOL_TYPE = "knowledgeWrite"
TOOL_NAME = "knowledge_write"


def _args_schema(topics: list[str]) -> type[BaseModel]:
    """`topic` is a closed enum of the configured topics, so the model cannot
    file a note anywhere the owner did not name."""
    return create_model(
        "KnowledgeWriteInput",
        topic=(Literal[tuple(topics)],  # type: ignore[valid-type]
               Field(description="Where to file the note: one of " + ", ".join(topics) + ".")),
        text=(str, Field(min_length=1, max_length=20_000,
                         description="The note, in the user's words. Self-contained: it will be read later without this conversation.")),
    )


async def create_knowledge_write_tool(
    tool_config: dict[str, Any],
    account_id: int,
    db: Session,
    auth_token: str | None = None,
    **kwargs,
) -> StructuredTool:
    index_name = tool_config.get("index")
    namespace = tool_config.get("namespace")
    topics = [t for t in (tool_config.get("topics") or []) if isinstance(t, str) and t]
    if (tool_config.get("provider") or "").lower() != "pinecone":
        raise ValueError("knowledgeWrite: provider must be 'pinecone'")
    if not index_name or not namespace or not topics:
        raise ValueError("knowledgeWrite: index, namespace and a non-empty topics list are required")

    owner_account_id = kwargs.get("agent_owner_account_id", account_id)
    org_scope = kwargs.get("org_scope")
    try:
        authorize_vector_store(
            db, account_id, index_name, owner_account_id,
            require_write=True,
            org_id=getattr(org_scope, "org_id", None),
        )
    except HTTPException as exc:
        # Skipped, not refused: the factory logs and the model never sees a
        # tool this caller may not use.
        raise ValueError(f"knowledgeWrite: caller may not write to '{index_name}' ({exc.detail})") from exc

    agent_id: int | None = kwargs.get("agent_id")
    chat_session_id: int | None = kwargs.get("chat_session_id_pk")
    session_factory = resolve_session_factory(kwargs)

    async def _write(topic: str, text: str) -> str:
        return await queue_tool_approval(
            # The CALLER approves: the queue is listed per account and this is
            # the person in the chat who can vouch for the note.
            account_id=account_id,
            agent_id=agent_id,
            chat_session_id=chat_session_id,
            session_factory=session_factory,
            tool_type=TOOL_TYPE,
            payload={
                "owner_account_id": owner_account_id,
                "index": index_name,
                "namespace": namespace,
                "topic": topic,
                "text": text,
            },
            preview={"topic": topic, "text": text, "index": index_name, "namespace": namespace},
            message=(
                f"Note filed under '{topic}' is waiting for the user's approval. "
                "Once approved it is ingested into the knowledge base; that takes a few "
                "minutes, so it will not show up in search immediately."
            ),
        )

    return StructuredTool.from_function(
        coroutine=_write,
        name=TOOL_NAME,
        description=tool_config.get("description") or (
            "Save something the user told you so the team can find it later. The note is "
            "reviewed by the user before it is stored. Topics: " + ", ".join(topics) + "."
        ),
        args_schema=_args_schema(topics),
    )

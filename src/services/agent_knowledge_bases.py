"""
The knowledge bases an agent searches, and who may read them through it.

An agent's vector-search tools only ever search its OWNER's knowledge bases
(the tools resolve the owner's credentials — see agent_runtime/tools/
pinecone_helpers.py). Reading one through the agent takes what reading it
directly takes: ownership or a read grant on it. Sharing the agent does NOT
share its knowledge bases; the share flow offers to grant them alongside it
(routers/agents/grants/create_grant.py), so the grant is explicit and visible.

Without the grant the agent still runs, minus its vector-search tools; the chat
tells the person which knowledge bases they are missing (unreadable_indexes).
"""
from sqlalchemy.orm import Session

from src.db.models import Agent
from src.services.vector_store_access import can_read_vector_store

VECTOR_TOOL_TYPES = frozenset({"vectorSearch", "vectorSearchWithReranking"})


def vector_tool_configs(agent_config: dict | None) -> list[dict]:
    """The agent's vector-search tool configs that name an index."""
    tools = ((agent_config or {}).get("data") or {}).get("tools") or []
    return [
        t for t in tools
        if isinstance(t, dict) and t.get("type") in VECTOR_TOOL_TYPES and t.get("index")
    ]


def agent_kb_indexes(agent_config: dict | None) -> list[str]:
    """Index names the agent searches, in config order, without repeats."""
    return list(dict.fromkeys(t["index"] for t in vector_tool_configs(agent_config)))


def unreadable_indexes(db: Session, account_id: int, agent: Agent,
                       org_id: int | None) -> list[str]:
    """The agent's knowledge bases *account_id* may not read. Empty for the owner."""
    return [
        index for index in agent_kb_indexes(agent.config)
        if not can_read_vector_store(db, account_id, index, agent.account_id, org_id=org_id)
    ]

"""
Pydantic request/response models for agent access grants.
"""
from pydantic import BaseModel, ConfigDict
from datetime import datetime
from typing import List


class CreateGrantRequest(BaseModel):
    """Share an agent with ONE named person.

    Sharing with a SET of people was putting the agent in a SPACE
    — a different table, because a space's
    audience deliberately crosses org boundaries and a grant may not.
    """
    granteeEmail: str
    # Also give them view-only access to the knowledge bases the agent
    # searches. Without a read grant the agent runs for them minus search.
    grantKnowledgeBases: bool = False


class AgentAccessGrantResponse(BaseModel):
    id: int
    agent_id: int
    grantee_account_id: int
    label: str
    created_at: datetime
    # Index names newly granted read by this share (grantKnowledgeBases).
    knowledge_bases_granted: List[str] = []

    model_config = ConfigDict(from_attributes=True)

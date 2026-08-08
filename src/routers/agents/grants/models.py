"""
Pydantic request/response models for agent access grants.
"""
from pydantic import BaseModel, ConfigDict
from datetime import datetime


class CreateGrantRequest(BaseModel):
    """Share an agent with ONE named person.

    Sharing with a SET of people was putting the agent in a SPACE
    — a different table, because a space's
    audience deliberately crosses org boundaries and a grant may not.
    """
    granteeEmail: str


class AgentAccessGrantResponse(BaseModel):
    id: int
    agent_id: int
    grantee_account_id: int
    label: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

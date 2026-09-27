"""
Shared Pydantic models for the agents router.
"""
from pydantic import BaseModel, ConfigDict
from typing import Optional, Dict, Any


class CreateAgentRequest(BaseModel):
    name: str
    config: Dict[str, Any]
    
    model_config = ConfigDict(populate_by_name=True)


class UpdateAgentRequest(BaseModel):
    name: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    
    model_config = ConfigDict(populate_by_name=True)


class AgentResponse(BaseModel):
    id: int
    name: str
    config: Optional[Dict[str, Any]] = None
    is_owner: Optional[bool] = None

    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def from_agent(cls, agent, is_owner: Optional[bool] = None) -> "AgentResponse":
        """The one field-by-field mapping every agents route returns.

        create/update leave is_owner unset (null), as they always have.
        """
        return cls(
            id=agent.id,
            name=agent.name,
            config=agent.config,
            is_owner=is_owner,
        )

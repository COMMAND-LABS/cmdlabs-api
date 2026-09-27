"""
List agents endpoint.

THE RULE: you see an agent exactly when you can use it — you own it, or its
owner shared it with you — and only in the org you are acting in. This is the
same rule GET /api/agents/{id} and the chat stream apply (access.can_access),
so nothing appears in the list that then 404s when opened.
"""
from fastapi import APIRouter, Request
from typing import List
from src.deps import org_dependency, db_dependency, jwt_dependency, account_id_from_claims
from sqlalchemy import or_
from src.db.models import Agent
from src.services.agent_access import get_accessible_agent_ids
from .models import AgentResponse
from src.rate_limit import limiter

router = APIRouter()

@router.get("/", response_model=List[AgentResponse])
@limiter.limit("30/minute")
async def list_agents(
    db: db_dependency,
    jwt: jwt_dependency,
    org: org_dependency,
    request: Request
):
    """
    Agents the caller owns or that were shared with them, in this org.
    Each item carries ``is_owner`` so the UI can offer edit/delete/share on
    owned agents only.
    """
    account_id = account_id_from_claims(jwt)
    shared_ids = get_accessible_agent_ids(db, account_id, org_id=org.org_id)

    agents = (
        db.query(Agent)
        .filter(
            Agent.org_id == org.org_id,
            or_(Agent.account_id == account_id, Agent.id.in_(shared_ids)),
        )
        .order_by(Agent.id.desc())
        .all()
    )

    return [
        AgentResponse.from_agent(agent, is_owner=(agent.account_id == account_id))
        for agent in agents
    ]

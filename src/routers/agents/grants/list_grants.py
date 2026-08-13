"""
List access grants for an agent (agent owner only). Reads unified AccessGrant.
"""
from fastapi import APIRouter, Request
from typing import List
from src.deps import org_dependency, db_dependency, jwt_dependency, account_id_from_claims
from src.services.org_scope import get_resource_or_404
from src.db.models import Agent, AccessGrant
from src.services import access
from src.services.access_admin import grant_label
from .models import AgentAccessGrantResponse
from src.rate_limit import limiter

router = APIRouter()

@router.get("/{agent_id}/access-grants", response_model=List[AgentAccessGrantResponse])
@limiter.limit("30/minute")
async def list_grants(
    agent_id: int,
    db: db_dependency,
    jwt: jwt_dependency,
    org: org_dependency,
    request: Request,
):
    """List the people this agent is shared with. Agent owner only.

    Space shares were not listed here either: they belonged to the space and
    were managed from it. Spaces are gone, so grants are now the whole list.
    """
    account_id = account_id_from_claims(jwt)

    get_resource_or_404(db, Agent, agent_id, org)

    grants = (
        db.query(AccessGrant)
        .filter(
            AccessGrant.resource_type == access.AGENT,
            AccessGrant.resource_id == agent_id,
        )
        .order_by(AccessGrant.created_at.desc())
        .all()
    )

    return [
        AgentAccessGrantResponse(
            id=g.id,
            agent_id=agent_id,
            grantee_account_id=g.principal_id,
            label=grant_label(db, g),
            created_at=g.created_at,
        )
        for g in grants
    ]

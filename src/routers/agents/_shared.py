"""Shared lookups for the agents routes."""
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.db.models import Agent


def owned_agent_or_404(db: Session, agent_id: int, org) -> Agent:
    """The agent, if the caller OWNS it in the org they are acting in; else 404.

    Managing an agent — editing, deleting, seeing who it is shared with — is
    its owner's alone. Being able to see or use an agent (a share) never
    extends to managing it. 404 rather than 403, as for anything the caller
    cannot reach.
    """
    agent = db.query(Agent).filter(
        Agent.id == agent_id,
        Agent.org_id == org.org_id,
        Agent.account_id == org.account_id,
    ).first()
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    return agent

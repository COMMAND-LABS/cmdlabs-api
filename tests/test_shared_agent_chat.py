"""A community member can USE an agent shared with them, and cannot change it.

Agent Chat needs the read-and-run surface under /api/agents (list, get,
stream), /api/tool-approvals and /api/files. Authoring — create, edit, delete,
share — stays with the Agents module, which community members hold since
2026-09-28 for their OWN agents. And a shared agent keeps its owner's
knowledge-base tools, while CRM and email tools still follow the chatter's
own role.
"""
import pytest
from sqlalchemy.orm import Session

from src.agent_runtime.tool_entitlement import agent_tool_modules
from src.config import plans_registry as plans
from src.config import roles_registry as roles
from src.config.modules_registry import modules_for_path
from src.config.roles_registry import ROLE_COMMUNITY_MEMBER
from src.db.models import Agent, Organization
from tests.org_isolation import client_for, make_tenant

CONFIG = {"schema": "agent_config", "version": 4,
          "data": {"systemPrompt": "hi", "tools": []}}


@pytest.fixture()
def owner(db: Session):
    t = make_tenant(db, slug="shared-chat", account_id=9201)
    org = db.query(Organization).filter(Organization.id == t.org_id).one()
    org.pinned_plan = plans.PLAN_PREMIUM
    db.flush()
    return t


@pytest.fixture()
def member(db: Session, owner):
    return make_tenant(db, slug="shared-chat", account_id=9202,
                       role=ROLE_COMMUNITY_MEMBER, is_owner=False)


@pytest.fixture()
async def shared_agent(db: Session, _override_db, owner, member):
    agent = Agent(org_id=owner.org_id, account_id=owner.account_id,
                  name="Analyst", visibility="private", config=CONFIG)
    db.add(agent)
    db.flush()
    async with client_for(owner) as c:
        resp = await c.post(f"/api/agents/{agent.id}/access-grants",
                            json={"granteeEmail": member.account.email})
    assert resp.status_code == 201, resp.text
    return agent


async def test_a_community_member_can_list_and_open_a_shared_agent(member, shared_agent):
    async with client_for(member) as c:
        listed = await c.get("/api/agents/")
        opened = await c.get(f"/api/agents/{shared_agent.id}")
        approvals = await c.get("/api/tool-approvals/")

    assert listed.status_code == 200, listed.text
    assert [a["id"] for a in listed.json()] == [shared_agent.id]
    assert opened.status_code == 200, opened.text
    assert approvals.status_code == 200, approvals.text


async def test_a_community_member_authors_their_own_but_not_a_shared_agent(
    member, shared_agent
):
    """Community members hold the Agents module (since 2026-09-28), so they
    build their own. An agent shared WITH them stays its owner's to change."""
    async with client_for(member) as c:
        created = await c.post("/api/agents/", json={"name": "Mine", "config": CONFIG})
        responses = [
            await c.put(f"/api/agents/{shared_agent.id}", json={"name": "Renamed"}),
            await c.delete(f"/api/agents/{shared_agent.id}"),
            await c.post(f"/api/agents/{shared_agent.id}/access-grants",
                         json={"granteeEmail": "x@example.com"}),
        ]
    assert created.status_code == 201, created.text
    assert [r.status_code for r in responses] == [404, 404, 404]


def test_agent_routes_open_for_agents_or_agent_chat():
    assert modules_for_path("/api/agents") == ("agents", "agent_chat")
    assert modules_for_path("/api/tool-approvals") == ("agents", "agent_chat")
    assert modules_for_path("/api/files") == ("agents", "agent_chat")
    assert modules_for_path("/api/contacts") == ("contacts",)


def test_a_shared_agent_keeps_its_owners_knowledge_base_tools(
    db: Session, owner, member, monkeypatch
):
    # Community members now hold knowledge_bases; take it away so this keeps
    # testing the rule (a shared agent's KB tools follow its OWNER), not the
    # current contents of the allowlist.
    monkeypatch.setattr(roles, "COMMUNITY_MODULES",
                        tuple(m for m in roles.COMMUNITY_MODULES if m != "knowledge_bases"))
    own = agent_tool_modules(db, member.account_id, member.org_id,
                             agent_owner_account_id=member.account_id)
    shared = agent_tool_modules(db, member.account_id, member.org_id,
                                agent_owner_account_id=owner.account_id)

    assert "knowledge_bases" not in own, "the member has no Knowledge Bases module"
    assert "knowledge_bases" in shared, "but the owner's agent keeps its KB tools"
    for key in ("contacts", "email_campaigns"):
        assert key not in shared, f"{key} tools must still follow the member's role"


@pytest.fixture()
def teammate(db: Session, owner):
    """A manager in the same org: holds the Agents module, owns nothing here."""
    return make_tenant(db, slug="shared-chat", account_id=9203, is_owner=False)


def _org_visible_agent(db: Session, owner):
    agent = Agent(org_id=owner.org_id, account_id=owner.account_id,
                  name="Team agent", visibility="org", config=CONFIG)
    db.add(agent)
    db.flush()
    return agent


async def test_you_see_an_agent_exactly_when_you_can_use_it(
    db: Session, _override_db, owner, teammate
):
    """Org visibility is not a share: listing it would show an agent that then
    404s when opened, because opening and chatting require owner-or-grant."""
    agent = _org_visible_agent(db, owner)
    async with client_for(teammate) as c:
        listed = await c.get("/api/agents/")
        opened = await c.get(f"/api/agents/{agent.id}")
    assert agent.id not in [a["id"] for a in listed.json()]
    assert opened.status_code == 404


async def test_only_the_owner_manages_an_agent(db: Session, _override_db, owner, teammate):
    agent = _org_visible_agent(db, owner)
    async with client_for(teammate) as c:
        responses = [
            await c.put(f"/api/agents/{agent.id}", json={"name": "Mine now"}),
            await c.get(f"/api/agents/{agent.id}/access-grants"),
            await c.delete(f"/api/agents/{agent.id}"),
        ]
    assert [r.status_code for r in responses] == [404, 404, 404]
    db.refresh(agent)
    assert agent.name == "Team agent"

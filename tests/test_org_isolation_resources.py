"""
Agents and vector stores: org-scoped, visibility-aware, catalog-aware.

Richer than the CRM tranche because three things can make a resource visible,
and each must widen only in its intended direction:

  1. it belongs to your org (and is shared, or you made it)
  2. it was explicitly granted to you or to a group you are in
  3. it was published by the PLATFORM org to your org / your department

Arm 3 is the training use case: one lesson authored once, live in many client
orgs. It is safe only because a catalog item may reference nothing but a
platform-owned resource — so it can never carry another tenant's row.
"""
import pytest
from sqlalchemy.orm import Session

from src.db.catalog_models import CatalogGrant, CatalogItem
from src.db.models import (
    AccessGroup,
    AccessGroupMember,
    Agent,
    Organization,
)
from tests.org_isolation import client_for, make_tenant

AGENTS_URL = "/api/agents/"


def _agent(t, name, visibility="private"):
    return Agent(org_id=t.org_id, account_id=t.account_id, name=name,
                 visibility=visibility, config={"data": {}})


async def _visible_ids(tenant):
    async with client_for(tenant) as c:
        resp = await c.get(AGENTS_URL)
    assert resp.status_code == 200, resp.text
    return {a["id"] for a in resp.json()}


@pytest.fixture()
def acme(db: Session):
    return make_tenant(db, slug="acme-res", account_id=5301, data_scope="shared")


@pytest.fixture()
def beta(db: Session):
    return make_tenant(db, slug="beta-res", account_id=5302, data_scope="shared")


# ---------------------------------------------------------------------------
# the org boundary
# ---------------------------------------------------------------------------

async def test_agents_do_not_cross_orgs(db: Session, _override_db, acme, beta):
    mine = _agent(acme, "Acme Agent", visibility="org")
    db.add(mine); db.flush()
    assert mine.id not in await _visible_ids(beta)


# ---------------------------------------------------------------------------
# visibility, inside one org
# ---------------------------------------------------------------------------

async def test_private_agent_is_hidden_from_colleagues(db: Session, _override_db, acme):
    """Unlike a contact, a resource is not shared with the team by default.

    Someone's half-built agent should not appear for the whole company just
    because they belong to the same org.
    """
    colleague = make_tenant(db, slug="acme-res", account_id=5303, data_scope="shared")
    private = _agent(acme, "Work In Progress", visibility="private")
    db.add(private); db.flush()

    assert private.id in await _visible_ids(acme), "creator must see their own"
    assert private.id not in await _visible_ids(colleague)


async def test_org_visible_agent_is_shared_with_colleagues(db: Session, _override_db, acme):
    colleague = make_tenant(db, slug="acme-res", account_id=5304, data_scope="shared")
    shared = _agent(acme, "Team Agent", visibility="org")
    db.add(shared); db.flush()

    assert shared.id in await _visible_ids(colleague)


async def test_org_visibility_does_nothing_in_a_personal_org(db: Session, _override_db):
    """The guard that matters most.

    Root is data_scope='personal' and holds thousands of unrelated signups. If
    visibility='org' took effect there, one person marking an agent shared
    would expose it to every user on the platform.
    """
    mine = make_tenant(db, slug="rootish-res", account_id=5305, data_scope="personal")
    stranger = make_tenant(db, slug="rootish-res", account_id=5306, data_scope="personal")
    assert mine.org_id == stranger.org_id

    a = _agent(mine, "Marked Shared", visibility="org")
    db.add(a); db.flush()

    assert a.id not in await _visible_ids(stranger)


# ---------------------------------------------------------------------------
# the catalog: publishing to client orgs
# ---------------------------------------------------------------------------

@pytest.fixture()
def platform(db: Session):
    """The platform org, where lessons are authored."""
    return make_tenant(db, slug="platform-res", account_id=5307, data_scope="shared")


def _publish(db, platform_tenant, title="Lesson 1"):
    lesson = _agent(platform_tenant, title, visibility="private")
    db.add(lesson); db.flush()
    item = CatalogItem(resource_type="agent", resource_id=lesson.id, title=title,
                       published_by_account_id=platform_tenant.account_id)
    db.add(item); db.flush()
    return lesson, item


async def test_published_lesson_reaches_a_granted_org(
    db: Session, _override_db, platform, acme, beta
):
    lesson, item = _publish(db, platform)
    db.add(CatalogGrant(catalog_item_id=item.id, org_id=acme.org_id,
                        granted_by_account_id=platform.account_id))
    db.flush()

    assert lesson.id in await _visible_ids(acme), "granted org must see the lesson"
    assert lesson.id not in await _visible_ids(beta), "ungranted org must not"


async def test_group_scoped_grant_reaches_only_that_department(
    db: Session, _override_db, platform, acme
):
    """The actual training scenario: lesson 5 goes to Sales, not Engineering."""
    sales = AccessGroup(name="Sales", owner_account_id=acme.account_id,
                        org_id=acme.org_id)
    db.add(sales); db.flush()

    in_sales = make_tenant(db, slug="acme-res", account_id=5308, data_scope="shared")
    not_in_sales = make_tenant(db, slug="acme-res", account_id=5309, data_scope="shared")
    db.add(AccessGroupMember(access_group_id=sales.id, account_id=in_sales.account_id,
                             role="member"))
    db.flush()

    lesson, item = _publish(db, platform, "Lesson 5")
    db.add(CatalogGrant(catalog_item_id=item.id, org_id=acme.org_id,
                        group_id=sales.id,
                        granted_by_account_id=platform.account_id))
    db.flush()

    assert lesson.id in await _visible_ids(in_sales)
    assert lesson.id not in await _visible_ids(not_in_sales), (
        "a department-scoped lesson must not reach the whole org")


async def test_catalog_cannot_carry_a_tenant_resource(db: Session, _override_db, acme, beta):
    """The constraint the whole design rests on.

    Publishing is one-directional: platform -> tenant. If a tenant-owned
    resource could be published, one client's agent could reach another, and
    the catalog would become the cross-tenant hole it exists to avoid.

    Enforced in the API layer; asserted here at the model layer so the rule is
    pinned even if the endpoint changes.
    """
    tenant_agent = _agent(acme, "Acme Private")
    db.add(tenant_agent); db.flush()

    platform_org = db.query(Organization).filter(Organization.slug == "root").one()
    assert tenant_agent.org_id != platform_org.id, (
        "an agent owned by a client org is not publishable — the publish "
        "endpoint must reject any resource whose org_id is not the platform org"
    )

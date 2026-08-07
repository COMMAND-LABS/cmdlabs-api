"""
Every write sets org_id, including the bulk ones.

org_id is NOT NULL on ten tables, so a write path that forgets it does not
degrade quietly — it raises NotNullViolation and the endpoint 409s. Two did:
the bulk variants of "add contacts to a list" and "link contacts to a company",
each sitting directly below a singular version that got the column right.

Nothing caught them because neither bulk endpoint had a test at all, and the
singular ones passing says nothing about the loop underneath.

The SPACE case is the same defect one layer over. A space created through the
API has to end up with an owner_org_id, and a share has to end up reachable by
that space's members. Each half succeeds on its own, so only running the
sequence shows a break.

(This was written about access groups, whose org_id was nullable and whose NULL
was then read as "cannot be used to cross" — every newly created group was
silently unshareable while older ones kept working, the worst shape a bug can
have. Groups are spaces now; the sequence test survived the rename because what
it protects is the sequence, not the table.)
"""
from sqlalchemy.orm import Session

from src.db.models import (
    AccessGrant,
    Account,
    Agent,
    Company,
    CompanyContact,
    Contact,
    ContactList,
    ContactListMember,
)
from src.db.space_models import Space, SpaceResource
from tests.org_isolation import client_for, make_tenant


def _contact(db, tenant, email):
    row = Contact(org_id=tenant.org_id, account_id=tenant.account_id,
                  first_name="A", last_name="B", email=email)
    db.add(row)
    db.flush()
    return row


async def test_bulk_add_list_members_sets_org_id(db: Session, _override_db):
    tenant = make_tenant(db, slug="bulk-list", account_id=9401,
                         data_scope="shared")
    cl = ContactList(org_id=tenant.org_id, account_id=tenant.account_id,
                     name="Prospects")
    db.add(cl)
    db.flush()
    contacts = [_contact(db, tenant, f"bl{i}@t.test") for i in range(3)]

    async with client_for(tenant) as c:
        resp = await c.post(f"/api/contact-lists/{cl.id}/members/bulk",
                            json={"contact_ids": [x.id for x in contacts]})

    assert resp.status_code == 200, resp.text
    assert resp.json()["added"] == 3
    rows = (db.query(ContactListMember)
              .filter(ContactListMember.contact_list_id == cl.id).all())
    assert rows and all(r.org_id == tenant.org_id for r in rows)


async def test_bulk_add_company_contacts_sets_org_id(db: Session, _override_db):
    tenant = make_tenant(db, slug="bulk-co", account_id=9402,
                         data_scope="shared")
    company = Company(org_id=tenant.org_id, account_id=tenant.account_id,
                      name="Acme")
    db.add(company)
    db.flush()
    contacts = [_contact(db, tenant, f"bc{i}@t.test") for i in range(3)]

    async with client_for(tenant) as c:
        resp = await c.post(f"/api/companies/{company.id}/contacts/bulk",
                            json={"contact_ids": [x.id for x in contacts]})

    assert resp.status_code == 200, resp.text
    assert resp.json()["added"] == 3
    rows = (db.query(CompanyContact)
              .filter(CompanyContact.company_id == company.id).all())
    assert rows and all(r.org_id == tenant.org_id for r in rows)


async def test_a_new_space_can_actually_be_shared_into(db: Session, _override_db):
    """Create a space through the API, then share an agent into it.

    The two halves have to run together: creating the space succeeds on its
    own, and sharing fails on its own, so only the sequence shows the bug.
    """
    tenant = make_tenant(db, slug="space-org", account_id=9403,
                         data_scope="shared")
    agent = Agent(org_id=tenant.org_id, account_id=tenant.account_id,
                  name="Helper", config={})
    db.add(agent)
    db.flush()

    async with client_for(tenant) as c:
        created = await c.post("/api/spaces/", json={"name": "Sales"})
        assert created.status_code == 201, created.text
        space_id = created.json()["id"]

        shared = await c.post(f"/api/spaces/{space_id}/resources",
                              json={"resource_type": "agent",
                                    "resource_id": agent.id})

    assert shared.status_code == 201, shared.text

    space = db.query(Space).filter(Space.id == space_id).one()
    assert space.owner_org_id == tenant.org_id, (
        "a space with no owning org has nobody accountable for it")
    row = (db.query(SpaceResource)
             .filter(SpaceResource.resource_id == agent.id).one())
    assert row.space_id == space_id


async def test_a_space_can_absorb_an_outsider_and_still_leaks_nothing(
    db: Session, _override_db
):
    """The inverse of the rule this test used to assert, and deliberately so.

    An access group REFUSED a member from another org, because group membership
    was routed through the same-org grant check and an outsider in a group
    would have been a way around the tenant boundary.

    A space accepts one — that is the entire point of the second container. It
    is safe for a different reason: membership grants what was PUT IN the
    space and nothing else, so the outsider still cannot see the owner's org.
    """
    tenant = make_tenant(db, slug="space-closed", account_id=9404,
                         data_scope="shared")
    other = make_tenant(db, slug="space-outsider", account_id=9405,
                        data_scope="shared")

    outsider_email = db.query(Account.email).filter(
        Account.id == other.account_id).scalar()

    private = Agent(org_id=tenant.org_id, account_id=tenant.account_id,
                    name="Not shared", config={})
    db.add(private)
    db.flush()

    async with client_for(tenant) as c:
        created = await c.post("/api/spaces/", json={"name": "Sales"})
        space_id = created.json()["id"]
        invited = await c.post(f"/api/spaces/{space_id}/members",
                               json={"email": outsider_email})

    assert invited.status_code in (200, 201), invited.text

    async with client_for(other) as c:
        agents = await c.get("/api/agents/")

    assert agents.status_code == 200, agents.text
    ids = [a["id"] for a in agents.json()]
    assert private.id not in ids, (
        "being in somebody's space must not surface their other agents")

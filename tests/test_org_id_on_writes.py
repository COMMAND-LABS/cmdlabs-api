"""
Every write sets org_id, including the bulk ones.

org_id is NOT NULL on ten tables, so a write path that forgets it does not
degrade quietly — it raises NotNullViolation and the endpoint 409s. Two did:
the bulk variants of "add contacts to a list" and "link contacts to a company",
each sitting directly below a singular version that got the column right.

Nothing caught them because neither bulk endpoint had a test at all, and the
singular ones passing says nothing about the loop underneath.

The group case is the same defect one layer over: a NULL org_id on an
AccessGroup is accepted by the database and then rejected by
access.assert_same_org, which treats an unclassified org as "cannot be used to
cross". So every newly created group was silently unshareable while every group
created before the migration kept working — the worst shape a bug can have.
"""
from sqlalchemy.orm import Session

from src.db.models import (
    AccessGrant,
    AccessGroup,
    Account,
    Agent,
    Company,
    CompanyContact,
    Contact,
    ContactList,
    ContactListMember,
)
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


async def test_a_new_group_can_actually_be_granted(db: Session, _override_db):
    """Create a group through the API, then share an agent with it.

    The two halves have to run together: creating the group succeeded on its
    own, and granting failed on its own, so only the sequence shows the bug.
    """
    tenant = make_tenant(db, slug="grp-org", account_id=9403,
                         data_scope="shared")
    agent = Agent(org_id=tenant.org_id, account_id=tenant.account_id,
                  name="Helper", config={})
    db.add(agent)
    db.flush()

    async with client_for(tenant) as c:
        created = await c.post("/api/access-groups/", json={"name": "Sales"})
        assert created.status_code == 201, created.text
        group_id = created.json()["id"]

        granted = await c.post(f"/api/agents/{agent.id}/access-grants",
                               json={"accessGroupId": group_id})

    assert granted.status_code == 201, granted.text

    group = db.query(AccessGroup).filter(AccessGroup.id == group_id).one()
    assert group.org_id == tenant.org_id, "a group with no org cannot be granted"
    grant = (db.query(AccessGrant)
               .filter(AccessGrant.resource_id == agent.id).one())
    assert grant.org_id == tenant.org_id


async def test_a_group_cannot_absorb_an_outsider(db: Session, _override_db):
    """Group membership is how resources reach people, so adding someone from
    another org here would route around the tenant boundary — the grant check
    validates the GROUP's org, never each member's."""
    tenant = make_tenant(db, slug="grp-closed", account_id=9404,
                         data_scope="shared")
    other = make_tenant(db, slug="grp-outsider", account_id=9405,
                        data_scope="shared")

    outsider_email = db.query(Account.email).filter(
        Account.id == other.account_id).scalar()

    async with client_for(tenant) as c:
        created = await c.post("/api/access-groups/", json={"name": "Sales"})
        group_id = created.json()["id"]
        resp = await c.post(f"/api/access-groups/{group_id}/members",
                            json={"email": outsider_email})

    assert resp.status_code == 404, (
        "an account from another org must not be addable to this org's group")

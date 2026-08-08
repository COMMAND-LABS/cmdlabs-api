"""
Every write sets org_id, including the bulk ones.

org_id is NOT NULL on ten tables, so a write path that forgets it does not
degrade quietly — it raises NotNullViolation and the endpoint 409s. Two did:
the bulk variants of "add contacts to a list" and "link contacts to a company",
each sitting directly below a singular version that got the column right.

Nothing caught them because neither bulk endpoint had a test at all, and the
singular ones passing says nothing about the loop underneath.

The SPACE case was the same defect one layer over, and it is worth recording
because the shape recurs. A space created through the API had to end up with an
owner_org_id, and a share had to end up reachable by that space's members. Each
half succeeded on its own, so only running the SEQUENCE showed the break.

(That was originally written about access groups, whose org_id was nullable and
whose NULL was then read as "cannot be used to cross" — every newly created
group was silently unshareable while older ones kept working, the worst shape a
bug can have. Groups became spaces, and the sequence test survived the rename
because what it protected was the sequence, not the table. Spaces are now gone
and the two tests went with them; write the sequence test first if a
create-then-share flow ever comes back.)
"""
from sqlalchemy.orm import Session

from src.db.models import (
    AccessGrant,
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

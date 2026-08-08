"""
Unit tests for the tenancy predicate.

Every scoped query in the application routes through `tenant_predicate`, so a
bug here is a bug everywhere at once. These tests exercise it directly against
the database rather than through HTTP, so a failure points at the predicate
instead of at whichever route happened to surface it.
"""
import pytest
from sqlalchemy.orm import Session

from src.db.models import Account, Contact, Organization, OrganizationMember, VectorStore
from src.services.org_scope import (
    created_by_column,
    resource_predicate,
    scoped,
    tenant_predicate,
)
from tests.org_isolation import make_tenant


def _ctx(tenant, *, account_id=None):
    """The pieces of OrgContext the predicate actually reads."""
    from src.deps import OrgContext
    return OrgContext(
        account_id=account_id or tenant.account_id,
        org_id=tenant.org_id,
        tier_key="member",
        is_super_admin=False)


def _contact(org_id, account_id, email):
    return Contact(org_id=org_id, account_id=account_id,
                   first_name="T", last_name="X", email=email)


# ---------------------------------------------------------------------------
# the org boundary
# ---------------------------------------------------------------------------

def test_rows_from_another_org_are_never_visible(db: Session):
    a = make_tenant(db, slug="pred-a", account_id=6001, data_scope="shared")
    b = make_tenant(db, slug="pred-b", account_id=6002, data_scope="shared")

    mine = _contact(a.org_id, a.account_id, "mine@pred.test")
    theirs = _contact(b.org_id, b.account_id, "theirs@pred.test")
    db.add_all([mine, theirs]); db.flush()

    visible = scoped(db, Contact, _ctx(a)).all()
    assert mine in visible
    assert theirs not in visible


def test_shared_org_shows_every_members_rows(db: Session):
    """A real team: two people, one workspace, everything visible to both."""
    owner = make_tenant(db, slug="team-co", account_id=6003, data_scope="shared")
    colleague = make_tenant(db, slug="team-co", account_id=6004, data_scope="shared")
    assert owner.org_id == colleague.org_id

    row = _contact(owner.org_id, owner.account_id, "shared@pred.test")
    db.add(row); db.flush()

    assert row in scoped(db, Contact, _ctx(colleague)).all()


def test_colleagues_in_one_org_see_each_others_rows(db: Session):
    """Two members of one org see each other's rows. That is what an org IS.

    This test used to assert the opposite, because the root org held every
    signup at once and needed a rule for strangers sharing a tenant. Now every
    account owns its own org, so two accounts in the SAME org are colleagues by
    definition and the old behaviour would be a bug — a team whose members
    cannot see the team's contacts.

    Isolation between unrelated people did not weaken; it moved to where it
    belongs. They are in different orgs, which
    test_rows_from_another_org_are_never_visible covers.
    """
    owner = make_tenant(db, slug="team-rows", account_id=6005)
    colleague = make_tenant(db, slug="team-rows", account_id=6006)
    assert owner.org_id == colleague.org_id

    mine = _contact(owner.org_id, owner.account_id, "mine@team.test")
    theirs = _contact(owner.org_id, colleague.account_id, "theirs@team.test")
    db.add_all([mine, theirs]); db.flush()

    visible = scoped(db, Contact, _ctx(owner)).all()
    assert mine in visible and theirs in visible

    # Attribution survives: account_id still records WHO, it just no longer
    # decides who sees.
    assert theirs.account_id == colleague.account_id


# ---------------------------------------------------------------------------
# privilege never widens the boundary
# ---------------------------------------------------------------------------

def test_owner_does_not_see_other_orgs(db: Session):
    a = make_tenant(db, slug="own-a", account_id=6007, data_scope="shared")
    b = make_tenant(db, slug="own-b", account_id=6008, data_scope="shared")
    row = _contact(b.org_id, b.account_id, "b@own.test")
    db.add(row); db.flush()

    ctx = _ctx(a)
    ctx = ctx.__class__(**{**ctx.__dict__, "is_owner": True})
    assert row not in scoped(db, Contact, ctx).all()


def test_super_admin_does_not_see_other_orgs(db: Session):
    """Super admins bypass MODULES, never org_id.

    If this ever fails, the audit trail is a lie: super admins would be able to
    read any tenant's data without leaving a membership row behind.
    """
    a = make_tenant(db, slug="sa-a", account_id=6009, data_scope="shared")
    b = make_tenant(db, slug="sa-b", account_id=6010, data_scope="shared")
    row = _contact(b.org_id, b.account_id, "b@sa.test")
    db.add(row); db.flush()

    ctx = _ctx(a)
    ctx = ctx.__class__(**{**ctx.__dict__, "is_super_admin": True})
    assert row not in scoped(db, Contact, ctx).all()


# ---------------------------------------------------------------------------
# the odd column name
# ---------------------------------------------------------------------------

def test_vector_stores_use_owner_account_id(db: Session):
    """vector_stores spells its creator column differently; getting this wrong
    would raise AttributeError in personal scope only — i.e. in the root org,
    in production, and not in a shared-org test."""
    assert created_by_column(VectorStore).key == "owner_account_id"
    assert created_by_column(Contact).key == "account_id"


def test_resources_stay_private_to_their_creator(db: Session):
    """RESOURCES are narrower than CRM rows, and deliberately so.

    tenant_predicate shows a colleague every contact in the org. A vector store
    is not a contact: it carries credentials and reaches documents, so it stays
    private to whoever made it until they mark it visibility='org'. That is why
    resource_predicate exists as a separate expression rather than a flag.
    """
    mine = make_tenant(db, slug="vs-team", account_id=6011)
    colleague = make_tenant(db, slug="vs-team", account_id=6012)

    ours = VectorStore(org_id=mine.org_id, owner_account_id=mine.account_id,
                       index_name="mine-idx", visibility="private")
    theirs = VectorStore(org_id=mine.org_id, owner_account_id=colleague.account_id,
                         index_name="their-idx", visibility="private")
    shared = VectorStore(org_id=mine.org_id, owner_account_id=colleague.account_id,
                         index_name="shared-idx", visibility="org")
    db.add_all([ours, theirs, shared]); db.flush()

    visible = db.query(VectorStore).filter(
        resource_predicate(VectorStore, _ctx(mine))).all()
    assert ours in visible, "your own resource is always yours"
    assert shared in visible, "one marked 'org' reaches the team"
    assert theirs not in visible, "a colleague's private resource stays private"


# ---------------------------------------------------------------------------
# composability
# ---------------------------------------------------------------------------

def test_predicate_composes_with_further_filters(db: Session):
    t = make_tenant(db, slug="compose-co", account_id=6013, data_scope="shared")
    keep = _contact(t.org_id, t.account_id, "keep@compose.test")
    drop = _contact(t.org_id, t.account_id, "drop@compose.test")
    db.add_all([keep, drop]); db.flush()

    rows = (db.query(Contact)
              .filter(tenant_predicate(Contact, _ctx(t)))
              .filter(Contact.email == "keep@compose.test")
              .all())
    assert rows == [keep]

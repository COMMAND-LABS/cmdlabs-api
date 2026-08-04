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
        org_slug=tenant.org.slug,
        tier_key="member",
        is_owner=False,
        is_super_admin=False,
        data_scope=tenant.org.data_scope,
        org_status="active",
    )


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


def test_personal_org_hides_other_members_rows(db: Session):
    """The root org: thousands of unrelated signups, each seeing only their own.

    This is the clause that makes flipping reads a no-op — in a personal org
    the predicate reduces to `created_by == me`, which is the pre-org
    behaviour exactly.
    """
    mine = make_tenant(db, slug="root-like", account_id=6005, data_scope="personal")
    stranger = make_tenant(db, slug="root-like", account_id=6006, data_scope="personal")
    assert mine.org_id == stranger.org_id

    my_row = _contact(mine.org_id, mine.account_id, "mine@personal.test")
    their_row = _contact(mine.org_id, stranger.account_id, "theirs@personal.test")
    db.add_all([my_row, their_row]); db.flush()

    visible = scoped(db, Contact, _ctx(mine)).all()
    assert my_row in visible
    assert their_row not in visible, "personal scope must not leak between members"


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
    """Staff bypass MODULES, never org_id.

    If this ever fails, the audit trail is a lie: staff would be able to read
    any tenant's data without leaving a membership row behind.
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


def test_personal_scope_filters_vector_stores_by_owner(db: Session):
    mine = make_tenant(db, slug="vs-personal", account_id=6011, data_scope="personal")
    stranger = make_tenant(db, slug="vs-personal", account_id=6012, data_scope="personal")

    ours = VectorStore(org_id=mine.org_id, owner_account_id=mine.account_id,
                       index_name="mine-idx")
    theirs = VectorStore(org_id=mine.org_id, owner_account_id=stranger.account_id,
                         index_name="their-idx")
    db.add_all([ours, theirs]); db.flush()

    visible = scoped(db, VectorStore, _ctx(mine)).all()
    assert ours in visible and theirs not in visible


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

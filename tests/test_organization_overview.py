"""
The owner's console, and the slug-availability check that feeds its one form.

Two things are worth pinning here. The overview composes information from four
existing surfaces, so the rule that matters is that composing it did not widen
who may read it — a member of the org sees the same 404 the tiers matrix gives
them, and so does platform staff acting inside somebody else's org.

The availability check is the other. It answers a question about names that are
permanent and public, so it is deliberately reachable only by the one caller
with a use for it: an owner who has not named their workspace yet.
"""
import pytest
from sqlalchemy.orm import Session

from src.db.models import AccessGroup, Account, Organization, OrganizationTier
from tests.org_isolation import client_for, make_tenant

OVERVIEW = "/api/organizations/me/overview"
AVAILABLE = "/api/organizations/slug/available"


@pytest.fixture()
def team(db: Session):
    """A named org with an owner, a second member, and both tiers."""
    owner = make_tenant(db, slug="overview-co", account_id=9701,
                        tier_key="owner", is_owner=True)
    if not db.query(OrganizationTier).filter(
            OrganizationTier.org_id == owner.org_id,
            OrganizationTier.tier_key == "member").first():
        db.add(OrganizationTier(org_id=owner.org_id, tier_key="member",
                                label="Member", modules=["home", "contacts"]))
    db.flush()
    return owner


@pytest.fixture()
def solo(db: Session):
    """An owner whose workspace has never been named."""
    t = make_tenant(db, slug="unnamed-overview-co", account_id=9703,
                    tier_key="owner", is_owner=True)
    org = db.query(Organization).filter(Organization.id == t.org_id).one()
    org.slug = None
    db.flush()
    return t


# ---------------------------------------------------------------------------
# who may read it
# ---------------------------------------------------------------------------

async def test_an_owner_sees_their_own_organization(db: Session, _override_db,
                                                    team):
    org = db.query(Organization).filter(Organization.id == team.org_id).one()
    org.granted_modules = ["home", "contacts"]
    db.add(AccessGroup(name="Engineering", owner_account_id=team.account_id,
                       org_id=team.org_id))
    db.flush()

    async with client_for(team) as c:
        resp = await c.get(OVERVIEW)

    assert resp.status_code == 200
    body = resp.json()
    assert body["org_id"] == team.org_id
    assert body["slug"] == "overview-co"
    assert body["is_personal"] is False
    assert body["status"] == "active"
    assert body["member_count"] >= 1
    assert body["owner_count"] == 1
    assert body["group_count"] == 1
    # Labelled, never raw keys: module keys are stable identifiers and a UI
    # that renders them is one rename away from being wrong.
    assert {m["key"] for m in body["ceiling"]} == {"home", "contacts"}
    assert all(m["label"] for m in body["ceiling"])
    assert body["module_total"] > len(body["ceiling"])
    assert {t["tier_key"] for t in body["tiers"]} == {"owner", "member"}


async def test_a_member_gets_the_same_404_as_the_tiers_matrix(
    db: Session, _override_db, team,
):
    """Composing four owner-only surfaces must not create a fifth that leaks.

    404 rather than 403 so the console does not confirm its own existence to
    somebody who cannot use it.
    """
    member = make_tenant(db, slug="overview-co", account_id=9702,
                         tier_key="member", is_owner=False)

    async with client_for(member) as c:
        resp = await c.get(OVERVIEW)
    assert resp.status_code == 404


async def test_platform_staff_do_not_get_the_owners_console(
    db: Session, _override_db, team,
):
    """Staff administer an org by setting its ceiling, not from inside it.

    The admin surface (/api/admin/organizations/{id}) is where staff look, and
    it is deliberately configuration rather than the owner's view.
    """
    staff = make_tenant(db, slug="overview-co", account_id=9704,
                        tier_key="member", is_owner=False)
    account = db.query(Account).filter(Account.id == staff.account_id).one()
    account.role = "admin"
    db.flush()

    async with client_for(staff) as c:
        resp = await c.get(OVERVIEW)
    assert resp.status_code == 404


async def test_recent_members_are_newest_first_and_capped(
    db: Session, _override_db, team,
):
    for i in range(7):
        make_tenant(db, slug="overview-co", account_id=9710 + i,
                    tier_key="member", is_owner=False)

    async with client_for(team) as c:
        body = (await c.get(OVERVIEW)).json()

    assert body["member_count"] == 8
    # The overview reports; it is not a second, worse members table.
    assert len(body["recent_members"]) == 5
    stamps = [m["created_at"] for m in body["recent_members"]]
    assert stamps == sorted(stamps, reverse=True)


# ---------------------------------------------------------------------------
# the availability check
# ---------------------------------------------------------------------------

async def test_availability_agrees_with_what_the_write_path_would_do(
    db: Session, _override_db, solo, team,
):
    async with client_for(solo) as c:
        assert (await c.get(AVAILABLE, params={"slug": "brand-new-name"})
                ).json()["available"] is True

        taken = (await c.get(AVAILABLE, params={"slug": "overview-co"})).json()
        assert taken["available"] is False
        assert "taken" in taken["reason"].lower()

        reserved = (await c.get(AVAILABLE, params={"slug": "cmdlabs"})).json()
        assert reserved["available"] is False
        assert "reserved" in reserved["reason"].lower()

        malformed = (await c.get(AVAILABLE, params={"slug": "Has Spaces"})).json()
        assert malformed["available"] is False


async def test_a_named_org_cannot_probe_for_other_names(db: Session,
                                                        _override_db, team):
    """Naming happens once, so the question is moot afterwards.

    Answering it anyway would turn an immutable public identifier into a
    directory anyone could walk one guess at a time.
    """
    async with client_for(team) as c:
        resp = await c.get(AVAILABLE, params={"slug": "anything"})
    assert resp.status_code == 404


async def test_a_member_cannot_probe_for_names(db: Session, _override_db, team):
    member = make_tenant(db, slug="overview-co", account_id=9705,
                         tier_key="member", is_owner=False)
    async with client_for(member) as c:
        resp = await c.get(AVAILABLE, params={"slug": "anything"})
    assert resp.status_code == 404


async def test_the_check_is_advisory_and_the_write_still_validates(
    db: Session, _override_db, solo, team,
):
    """A name claimed between the check and the save is an ordinary conflict."""
    async with client_for(solo) as c:
        assert (await c.get(AVAILABLE, params={"slug": "race-name"})
                ).json()["available"] is True

        other = db.query(Organization).filter(
            Organization.id == team.org_id).one()
        other.slug = "race-name"
        db.flush()

        resp = await c.put("/api/organizations/slug", json={"slug": "race-name"})
    assert resp.status_code == 409

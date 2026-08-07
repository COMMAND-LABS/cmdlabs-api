"""
The owner's console for their own organization.

The overview composes information from four existing surfaces, so the rule that
matters is that composing it did not widen who may read it — a member of the
org sees the same 404 the tiers matrix gives them, and so does platform super
admins acting inside somebody else's org.

Organizations no longer have slugs, so the naming flow and its availability
check are gone with them: an id identifies an org everywhere, and the display
name is an editable label.
"""
import pytest
from sqlalchemy.orm import Session

from src.db.models import Account, Organization, OrganizationTier
from tests.org_isolation import client_for, make_tenant

OVERVIEW = "/api/organizations/me/overview"


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


# ---------------------------------------------------------------------------
# who may read it
# ---------------------------------------------------------------------------

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


async def test_platform_super_admin_do_not_get_the_owners_console(
    db: Session, _override_db, team,
):
    """Super admins administer an org by setting its ceiling, not from inside.

    The admin surface (/api/admin/organizations/{id}) is where super admins
    look, and it is deliberately configuration rather than the owner's view.
    """
    super_admin = make_tenant(db, slug="overview-co", account_id=9704,
                        tier_key="member", is_owner=False)
    account = db.query(Account).filter(Account.id == super_admin.account_id).one()
    account.is_super_admin = True
    db.flush()

    async with client_for(super_admin) as c:
        resp = await c.get(OVERVIEW)
    assert resp.status_code == 404


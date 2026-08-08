"""
The owner's console for one organization.

The overview composes information from four existing surfaces, so the rule that
matters is that composing it did not widen who may read it — a member of the
org sees the same 404 the tiers matrix gives them, and so do platform super
admins acting inside somebody else's org.

NOW AIMED AT THE PATH ROUTE. These cases were written against
GET /me/overview, which answered for whichever org the caller was ACTING in.
That route went with the page it fed; /{org_id}/overview replaced it. The tests
did not go with it, because what they assert is not about how the org was
named — it is that composing four owner-only surfaces did not produce a fifth
that leaks. That property has to hold wherever the console lives.

Membership of the named org is proven separately, in
tests/test_named_org_reads.py. Here the caller is always a member; the question
is only whether being a member is enough. It is not.

Organizations no longer have slugs, so the naming flow and its availability
check are gone with them: an id identifies an org everywhere, and the display
name is an editable label.
"""
import pytest
from sqlalchemy.orm import Session

from src.db.models import Account
from tests.org_isolation import client_for, make_tenant

def _overview(org_id: int) -> str:
    return f"/api/organizations/{org_id}/overview"


@pytest.fixture()
def team(db: Session):
    """A named org with an owner."""
    owner = make_tenant(db, slug="overview-co", account_id=9701,
                        role="manager", is_owner=True)
    db.flush()
    return owner


# ---------------------------------------------------------------------------
# who may read it
# ---------------------------------------------------------------------------

async def test_a_member_does_not_get_the_owners_console(
    db: Session, _override_db, team,
):
    """Composing owner-only surfaces must not create one more that leaks.

    404 rather than 403 so the console does not confirm its own existence to
    somebody who cannot use it.
    """
    member = make_tenant(db, slug="overview-co", account_id=9702,
                         role="manager", is_owner=False)

    async with client_for(member) as c:
        resp = await c.get(_overview(team.org_id))
    assert resp.status_code == 404


async def test_platform_super_admin_do_not_get_the_owners_console(
    db: Session, _override_db, team,
):
    """Super admins administer an org by setting its ceiling, not from inside.

    The admin surface (/api/admin/organizations/{id}) is where super admins
    look, and it is deliberately configuration rather than the owner's view.
    """
    super_admin = make_tenant(db, slug="overview-co", account_id=9704,
                        role="manager", is_owner=False)
    account = db.query(Account).filter(Account.id == super_admin.account_id).one()
    account.is_super_admin = True
    db.flush()

    async with client_for(super_admin) as c:
        resp = await c.get(_overview(team.org_id))
    assert resp.status_code == 404


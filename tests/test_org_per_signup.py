"""
Every account owns an org, and no two signups share one.

This is the invariant that replaced `data_scope`. Before org-per-signup, 274
unrelated people lived in the root org and one conditional in the tenancy
predicate stood between them and each other's contacts. Now they are never in
the same org, so the isolation is structural — there is no flag to get wrong.

The tests here guard the SIGNUP PATH specifically, because that is what could
quietly put people back in a shared org. tests/test_org_isolation_*.py cover
the boundary itself.

The migration that performed the split (e3f4a5b6c7d8) carries its own
postflight assertions and refuses to finish if it would strand an account, leave
a non-staff member in the platform org, or sever a grant. Those run against real
data; these run against the code that has to keep the property true afterwards.
"""
from sqlalchemy.orm import Session

from src.db.models import (
    Account,
    Organization,
    OrganizationMember,
    OrganizationTier,
)
from src.services import modules
from src.services.organizations import (
    FREE_CEILING,
    PREMIUM_CEILING,
    ensure_membership,
    personal_org_for,
    sync_ceiling_to_subscription,
)


def _account(db: Session, account_id: int, **kw) -> Account:
    acct = Account(id=account_id, email=f"s{account_id}@signup.test", **kw)
    db.add(acct)
    db.flush()
    return acct


# ---------------------------------------------------------------------------
# the workspace
# ---------------------------------------------------------------------------

def test_a_signup_owns_the_org_it_lands_in(db: Session):
    acct = _account(db, 8101)
    member = ensure_membership(db, acct)

    org = personal_org_for(db, acct.id)
    assert org is not None
    assert member.org_id == org.id
    assert member.is_owner is True
    assert org.owner_account_id == acct.id
    assert acct.default_org_id == org.id


def test_a_personal_workspace_has_no_public_slug(db: Session):
    """No auto-generated slug. A slug is the org's public identity and is
    immutable, so handing someone `user-273` would be a defect they can never
    trade for the name they actually want."""
    acct = _account(db, 8102)
    ensure_membership(db, acct)
    org = personal_org_for(db, acct.id)

    assert org.slug is None
    assert org.is_personal is True


def test_two_signups_never_share_an_org(db: Session):
    a, b = _account(db, 8103), _account(db, 8104)
    ma, mb = ensure_membership(db, a), ensure_membership(db, b)
    assert ma.org_id != mb.org_id


def test_signing_in_twice_does_not_make_a_second_workspace(db: Session):
    """ensure_membership runs on every verified login."""
    acct = _account(db, 8105)
    first = ensure_membership(db, acct)
    second = ensure_membership(db, acct)

    assert first.org_id == second.org_id
    assert db.query(Organization).filter(
        Organization.owner_account_id == acct.id).count() == 1


def test_tiers_are_seeded_so_the_workspace_can_become_a_team(db: Session):
    """Converting to a team should be picking a slug and inviting somebody —
    not first discovering the tiers page is empty."""
    acct = _account(db, 8106)
    ensure_membership(db, acct)
    org = personal_org_for(db, acct.id)

    tiers = {t.tier_key: t for t in db.query(OrganizationTier).filter(
        OrganizationTier.org_id == org.id).all()}
    assert set(tiers) == {"owner", "member"}
    assert tiers["owner"].modules == list(org.granted_modules)
    assert tiers["member"].modules == [], (
        "an invited member must get what the owner deliberately checks, "
        "never a default nobody chose")


# ---------------------------------------------------------------------------
# the ceiling is the entitlement
# ---------------------------------------------------------------------------

def test_a_free_signup_sees_exactly_the_free_modules(db: Session):
    acct = _account(db, 8107)
    ensure_membership(db, acct)
    org = personal_org_for(db, acct.id)

    assert list(org.granted_modules) == FREE_CEILING

    from src.deps import OrgContext
    ctx = OrgContext(account_id=acct.id, org_id=org.id, org_slug=None,
                     tier_key="owner", is_owner=True, is_super_admin=False,
                     org_status="active")
    # Owner bypass means the ceiling IS what they can open — the tier layer is
    # inert until this org has somebody in it who is not the owner.
    assert modules.effective_modules(db, ctx) == FREE_CEILING


def test_subscribing_raises_the_ceiling(db: Session):
    """The lever billing actually pulls.

    Before this, the webhook wrote accounts.role and nothing else. Under
    org-per-signup that would set role='premium' and change nothing the user
    could see, because entitlement resolves from the org.
    """
    acct = _account(db, 8108)
    ensure_membership(db, acct)
    org = personal_org_for(db, acct.id)
    assert list(org.granted_modules) == FREE_CEILING

    acct.subscription_status = "active"
    sync_ceiling_to_subscription(db, acct)
    db.flush()
    assert list(org.granted_modules) == PREMIUM_CEILING

    acct.subscription_status = "canceled"
    sync_ceiling_to_subscription(db, acct)
    db.flush()
    assert list(org.granted_modules) == FREE_CEILING, "a lapse takes it back"


def test_a_comped_ceiling_survives_billing(db: Session):
    """The comp, one level up from OrganizationMember.granted_by.

    A client given paid access without a subscription must not lose it the
    first time an unrelated Stripe event fires for their account.
    """
    acct = _account(db, 8109)
    ensure_membership(db, acct)
    org = personal_org_for(db, acct.id)

    # Staff raise it by hand; admin.set_ceiling flips the column.
    org.granted_modules = ["home", "agents", "contacts", "settings"]
    org.ceiling_managed_by = "grant"
    db.flush()

    acct.subscription_status = "canceled"
    sync_ceiling_to_subscription(db, acct)
    db.flush()

    assert list(org.granted_modules) == ["home", "agents", "contacts", "settings"], (
        "a webhook must never undo a staff grant")


def test_billing_only_touches_the_account_s_own_workspace(db: Session):
    """A subscription is between one account and Stripe. It must not move the
    ceiling of a team org that account merely belongs to."""
    from tests.org_isolation import make_tenant

    team = make_tenant(db, slug="billing-team", account_id=8110)
    before = list(team.org.granted_modules)

    acct = _account(db, 8111)
    ensure_membership(db, acct)
    db.add(OrganizationMember(org_id=team.org_id, account_id=acct.id,
                              tier_key="member", granted_by="grant",
                              is_owner=False))
    db.flush()

    acct.subscription_status = "active"
    sync_ceiling_to_subscription(db, acct)
    db.flush()

    assert list(team.org.granted_modules) == before
    assert list(personal_org_for(db, acct.id).granted_modules) == PREMIUM_CEILING

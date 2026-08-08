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
a non-super-admin member in the platform org, or sever a grant. Those run against real
data; these run against the code that has to keep the property true afterwards.
"""
from sqlalchemy.orm import Session

from src.db.models import (
    Account,
    Organization,
    OrganizationMember,
    OrganizationTier,
)
from src.config import plans_registry as plans
from src.services import modules

# Read from the registry rather than hardcoded: these are what the plans
# contain right now, and a test that restated them would just be a second
# place to update.
FREE_CEILING = plans.modules_for_plan(plans.PLAN_FREE)
PREMIUM_CEILING = plans.modules_for_plan(plans.PLAN_PREMIUM)
from src.services.organizations import (
    ceiling_for_account,
    ensure_membership,
    own_org_for,
    pin_plan,
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

    org = own_org_for(db, acct.id)
    assert org is not None
    assert member.org_id == org.id
    assert org.owner_account_id == acct.id, (
        "ownership is the org's column — the membership row no longer\n"
        "        carries a second copy of it")
    assert org.owner_account_id == acct.id
    assert acct.default_org_id == org.id


def test_a_signup_gets_no_public_identity(db: Session):
    """A workspace is identified by its id and nothing else.

    This used to assert `org.slug is None` — that a signup got no
    auto-generated public name, because a slug was permanent and inventing one
    would hand somebody an identity they never chose. Organizations no longer
    have slugs at all, so the property holds by construction: there is nothing
    to auto-generate.
    """
    acct = _account(db, 8102)
    ensure_membership(db, acct)
    org = own_org_for(db, acct.id)

    assert org is not None
    assert org.id is not None
    assert org.name, "it still has a display name, which is editable"

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
    """Converting to a team should be inviting somebody —
    not first discovering the tiers page is empty."""
    acct = _account(db, 8106)
    ensure_membership(db, acct)
    org = own_org_for(db, acct.id)

    tiers = {t.tier_key: t for t in db.query(OrganizationTier).filter(
        OrganizationTier.org_id == org.id).all()}
    assert set(tiers) == {"owner", "member"}
    assert tiers["owner"].modules == FREE_CEILING
    assert tiers["member"].modules == [], (
        "an invited member must get what the owner deliberately checks, "
        "never a default nobody chose")


# ---------------------------------------------------------------------------
# the ceiling is the entitlement
# ---------------------------------------------------------------------------

def test_a_free_signup_sees_exactly_the_free_modules(db: Session):
    acct = _account(db, 8107)
    ensure_membership(db, acct)
    org = own_org_for(db, acct.id)

    assert org.pinned_plan is None, "a signup follows their own subscription"
    assert modules.ceiling_for(db, org.id) == FREE_CEILING

    from src.deps import OrgContext
    ctx = OrgContext(account_id=acct.id, org_id=org.id, tier_key="owner", is_super_admin=False)
    # Owner bypass means the ceiling IS what they can open — the tier layer is
    # inert until this org has somebody in it who is not the owner.
    assert modules.effective_modules(db, ctx) == FREE_CEILING


def test_subscribing_widens_the_ceiling_and_a_lapse_takes_it_back(db: Session):
    """The lever billing actually pulls — now with nothing to pull.

    This used to call sync_ceiling_to_subscription() after every Stripe event,
    which wrote the plan's module list into organizations.granted_modules. That
    copy is gone: the ceiling is DERIVED from the owner's subscription, so
    changing the status is the whole update and the two cannot drift.
    """
    acct = _account(db, 8108)
    ensure_membership(db, acct)
    org = own_org_for(db, acct.id)
    assert modules.ceiling_for(db, org.id) == FREE_CEILING

    acct.subscription_status = "active"
    db.flush()
    assert modules.ceiling_for(db, org.id) == PREMIUM_CEILING

    acct.subscription_status = "canceled"
    db.flush()
    assert modules.ceiling_for(db, org.id) == FREE_CEILING, "a lapse takes it back"


def test_a_pinned_plan_survives_billing(db: Session):
    """The comp, one level up from OrganizationMember.granted_by.

    A client given paid access without a subscription must not lose it the
    first time an unrelated Stripe event fires. A pin is never recomputed —
    that asymmetry is the whole reason the column exists.
    """
    acct = _account(db, 8109)
    ensure_membership(db, acct)
    org = own_org_for(db, acct.id)

    org.pinned_plan = plans.PLAN_PREMIUM
    db.flush()

    acct.subscription_status = "canceled"
    db.flush()

    assert modules.ceiling_for(db, org.id) == PREMIUM_CEILING, (
        "billing must never undo a super admin pin")


def test_a_pinned_plan_follows_the_plan_as_it_grows(db: Session):
    """THE BUG THIS REPLACED, pinned as a test.

    A pin used to store the resolved module LIST, which made it a snapshot:
    every module added to the plan afterwards never reached the pinned org.
    All three pinned orgs on the platform lost `courses` and `spaces` that way,
    silently. Pinning the PLAN means a plan that grows reaches them too.
    """
    acct = _account(db, 8113)
    ensure_membership(db, acct)
    org = own_org_for(db, acct.id)
    org.pinned_plan = plans.PLAN_PREMIUM
    db.flush()

    before = modules.ceiling_for(db, org.id)

    original = plans.PLAN_MODULES[plans.PLAN_PREMIUM]
    try:
        # A module added to the product AFTER this org was pinned.
        plans.PLAN_MODULES[plans.PLAN_PREMIUM] = original + ("organization",)
        after = modules.ceiling_for(db, org.id)
    finally:
        plans.PLAN_MODULES[plans.PLAN_PREMIUM] = original

    assert "organization" not in before
    assert "organization" in after, (
        "a pinned org must pick up what its plan gains, with no backfill")


def test_billing_only_follows_the_owner_s_subscription(db: Session):
    """A subscription is between ONE account and Stripe.

    A member's card must not move the ceiling of a team they merely belong to —
    the derivation reads the org's OWNER, never whoever happens to be asking.
    """
    from tests.org_isolation import make_tenant

    team = make_tenant(db, slug="billing-team", account_id=8110)
    acct = _account(db, 8111)
    ensure_membership(db, acct)
    db.add(OrganizationMember(org_id=team.org_id, account_id=acct.id,
                              tier_key="member", granted_by="grant"))
    db.flush()

    before = modules.ceiling_for(db, team.org_id)
    acct.subscription_status = "active"
    db.flush()

    assert modules.ceiling_for(db, team.org_id) == before
    assert modules.ceiling_for(
        db, own_org_for(db, acct.id).id) == PREMIUM_CEILING


def test_becoming_a_team_pins_the_plan(db: Session):
    """A colleague must not lose Contacts because the founder's card expired.

    Until somebody else is let in, the plan follows the owner's subscription.
    The moment it stops being one person's workspace it is pinned — because the
    people it would now move are no longer the person paying.
    """
    acct = _account(db, 8112)
    acct.subscription_status = "active"
    ensure_membership(db, acct)
    org = own_org_for(db, acct.id)
    assert modules.ceiling_for(db, org.id) == PREMIUM_CEILING

    pin_plan(db, org)
    db.flush()

    assert org.pinned_plan == plans.PLAN_PREMIUM

    acct.subscription_status = "canceled"
    db.flush()
    assert modules.ceiling_for(db, org.id) == PREMIUM_CEILING, (
        "the team keeps the plan it had when it became a team")

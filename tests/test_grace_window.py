"""
What happens between "the card failed" and "you're on the free plan".

    ACTIVE   Stripe says paid            premium modules, writes allowed
    GRACE    within GRACE_DAYS           premium modules, READS ONLY
    LAPSED   past GRACE_DAYS             free modules,    writes allowed

The middle state is the point. Dropping straight to free the moment a payment
fails takes the paid modules off the screen entirely — a 404, not a warning —
so the first thing a customer learns about their expired card is that their
data appears to be gone. This suite pins down that it does not.

Everything here is derived from ONE stored value: accounts.subscription_
lapsed_at. There is no suspended flag and no scheduled job, so there is nothing
that can be missed and leave somebody locked out of a workspace they paid for.
The tests pass `now` explicitly for the same reason the production code accepts
it — a two-week window is not otherwise testable.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from src.config import plans_registry as plans
from src.db.models import Account, Organization
from src.services import modules

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def _at(days_ago: float) -> datetime:
    return NOW - timedelta(days=days_ago)


# ---------------------------------------------------------------------------
# the state machine
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", ["active", "trialing"])
def test_a_paying_subscription_is_active_whatever_is_on_record(status):
    """A stale lapse timestamp must never outrank a live subscription.

    The webhook clears it on the way back in, but a clear that failed to commit
    must not leave a paying customer read-only. The status wins outright.
    """
    assert plans.billing_state(status, _at(1), NOW) == plans.BILLING_ACTIVE


def test_a_fresh_lapse_opens_the_grace_window():
    assert plans.billing_state("canceled", _at(1), NOW) == plans.BILLING_GRACE


def test_the_window_closes_after_grace_days():
    assert plans.billing_state(
        "canceled", _at(plans.GRACE_DAYS + 1), NOW) == plans.BILLING_LAPSED


def test_the_boundary_belongs_to_the_lapsed_side():
    """Exactly GRACE_DAYS later is over, not still running.

    Pinned because an off-by-one here is a day of free premium for everybody,
    and nobody would ever notice it from the outside.
    """
    assert plans.billing_state(
        "canceled", _at(plans.GRACE_DAYS), NOW) == plans.BILLING_LAPSED
    assert plans.billing_state(
        "canceled", _at(plans.GRACE_DAYS - 0.01), NOW) == plans.BILLING_GRACE


def test_no_timestamp_means_no_grace():
    """A NULL is not a fresh lapse, and must never be read as one.

    Two populations have NULL: accounts that never subscribed, and accounts
    that lapsed before the column existed. Treating either as "just lapsed"
    would hand out a fortnight of premium to people who are not owed it, every
    time the column is added to a new environment.
    """
    assert plans.billing_state("canceled", None, NOW) == plans.BILLING_LAPSED
    assert plans.billing_state(None, None, NOW) == plans.BILLING_LAPSED


def test_a_naive_timestamp_does_not_blow_up():
    """This expression decides whether somebody may write to their own account.

    A naive datetime — from a hand-built row, or a driver that drops tzinfo —
    would otherwise raise TypeError comparing against an aware `now`, turning a
    billing question into a 500 on every request the account makes.
    """
    naive = _at(1).replace(tzinfo=None)
    assert plans.billing_state("canceled", naive, NOW) == plans.BILLING_GRACE


# ---------------------------------------------------------------------------
# what each state buys
# ---------------------------------------------------------------------------

def test_grace_keeps_the_paid_modules():
    assert plans.plan_for("canceled", _at(1), NOW) == plans.PLAN_PREMIUM


def test_past_grace_drops_to_free():
    assert plans.plan_for(
        "canceled", _at(plans.GRACE_DAYS + 1), NOW) == plans.PLAN_FREE


# ---------------------------------------------------------------------------
# resolved against a real org
# ---------------------------------------------------------------------------

def _org_owned_by(db: Session, account: Account, *, managed_by="subscription"):
    org = Organization(name="Lapsing Co", granted_modules=["home"],
                       owner_account_id=account.id,
                       ceiling_managed_by=managed_by)
    db.add(org)
    db.flush()
    return org


def test_an_org_in_grace_keeps_its_ceiling_and_refuses_writes(db: Session):
    owner = Account(id=7301, email="grace@x.test", subscription_status="canceled",
                    subscription_lapsed_at=_at(2))
    db.add(owner)
    db.flush()
    org = _org_owned_by(db, owner)

    entitlement = modules.org_entitlement(db, org.id, now=NOW)

    assert entitlement.read_only is True
    assert entitlement.ceiling == plans.modules_for_plan(plans.PLAN_PREMIUM), (
        "the modules stay — that is what makes this different from a downgrade")
    assert entitlement.grace_ends_at == _at(2) + timedelta(days=plans.GRACE_DAYS)


def test_past_the_window_the_org_is_writable_again_on_free(db: Session):
    """The end of grace is a DOWNGRADE, not a deeper lockout.

    They are an ordinary free user at this point and must be able to use the
    free plan — including writing to it. Leaving them read-only forever would
    be the worst of both.
    """
    owner = Account(id=7302, email="lapsed@x.test", subscription_status="canceled",
                    subscription_lapsed_at=_at(plans.GRACE_DAYS + 3))
    db.add(owner)
    db.flush()
    org = _org_owned_by(db, owner)

    entitlement = modules.org_entitlement(db, org.id, now=NOW)

    assert entitlement.read_only is False
    assert entitlement.ceiling == plans.modules_for_plan(plans.PLAN_FREE)
    assert entitlement.grace_ends_at is None


def test_a_comped_org_is_never_made_read_only_by_billing(db: Session):
    """The comp promise, restated one level up.

    Staff setting a ceiling by hand is a promise the billing path must not
    quietly withdraw — the same asymmetry ceiling_managed_by already encodes
    for the modules themselves. A comped org has no subscription to lapse, so
    a cancelled card on the owner's account says nothing about it.
    """
    owner = Account(id=7303, email="comped@x.test", subscription_status="canceled",
                    subscription_lapsed_at=_at(1))
    db.add(owner)
    db.flush()
    org = _org_owned_by(db, owner, managed_by="grant")

    entitlement = modules.org_entitlement(db, org.id, now=NOW)

    assert entitlement.read_only is False
    assert entitlement.ceiling == ["home"], "the granted ceiling, untouched"


def test_an_ownerless_org_is_not_locked(db: Session):
    """Nobody could fix the payment, so refusing writes would strand it."""
    org = Organization(name="Orphan", granted_modules=["home"],
                       owner_account_id=None,
                       ceiling_managed_by="subscription")
    db.add(org)
    db.flush()

    assert modules.org_entitlement(db, org.id, now=NOW).read_only is False


# ---------------------------------------------------------------------------
# the webhook, which is the only writer
# ---------------------------------------------------------------------------

def test_a_repeat_webhook_does_not_restart_the_window(db: Session):
    """Stripe retries. The clock must not.

    _apply_subscription stamps the timestamp on the TRANSITION out of an
    entitling status. If it stamped on every lapsed-status webhook instead,
    each retry of a failing charge would push the deadline out and premium
    would be free forever — while the customer saw a date they could not
    predict.
    """
    from src.routers.billing.webhook import _apply_subscription

    account = Account(id=7304, email="retry@x.test", subscription_status="active")
    db.add(account)
    db.flush()

    _apply_subscription(db, account, {"id": "sub_1", "status": "past_due"})
    first = account.subscription_lapsed_at
    assert first is not None

    _apply_subscription(db, account, {"id": "sub_1", "status": "past_due"})
    assert account.subscription_lapsed_at == first, "the deadline did not move"


def test_paying_again_clears_the_lapse(db: Session):
    from src.routers.billing.webhook import _apply_subscription

    account = Account(id=7305, email="recovered@x.test",
                      subscription_status="canceled",
                      subscription_lapsed_at=_at(3))
    db.add(account)
    db.flush()

    _apply_subscription(db, account, {"id": "sub_1", "status": "active"})

    assert account.subscription_lapsed_at is None, (
        "a stale timestamp would shorten their NEXT grace window")
    assert plans.plan_for_account(account, NOW) == plans.PLAN_PREMIUM

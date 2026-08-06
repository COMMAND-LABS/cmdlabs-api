"""
The self-serve plans: free and premium, and what each one includes.

THE SINGLE DEFINITION
---------------------
Before this file the two module sets sat as loose lists inside
services/organizations.py named FREE_CEILING / PREMIUM_CEILING — named, that
is, after the COLUMN they get written to rather than after the thing they are.
That made "what does premium include?" a question you answered by reading the
signup path. It is a product fact and it belongs in one place, next to
modules_registry.py, which is where the module keys themselves live.

PLAN vs TIER vs CEILING — three words, three different axes
-----------------------------------------------------------
    PLAN     what the PLATFORM sells to a customer      free | premium
    CEILING  where a plan is stored on an org           organizations.granted_modules
    TIER     how an org OWNER divides their ceiling     organization_tiers

A plan is not a tier, and the difference is not cosmetic. Tier rows are
editable by the org's own owner (PUT /api/organizations/tiers/{key}/modules),
and every self-serve signup owns their personal workspace. If free vs premium
were expressed as a tier, a free user could rewrite their own tier and
self-upgrade; the only reason they cannot today is that clamp_to_ceiling pins
them to the plan stored on the ceiling. So the plan is stored one level ABOVE
what the customer controls, and tiers stay what they are good at: an org owner
dividing what they bought among their own people.

WHY THE SUBSCRIPTION DECIDES, NOT accounts.role
-----------------------------------------------
Same reason the ceiling backfills used it: a role column that has drifted out
of agreement with Stripe would otherwise hand out paid modules that no webhook
would ever take back. The subscription is the fact; role is a cache of it.
"""
from src.config.modules_registry import normalize

PLAN_FREE = "free"
PLAN_PREMIUM = "premium"
PLAN_KEYS = (PLAN_FREE, PLAN_PREMIUM)

# Stripe statuses that count as paid. Kept here rather than imported from
# db.models so this module stays importable by config-level code with no ORM
# dependency; db.models holds the same tuple for the Account property.
ACTIVE_SUBSCRIPTION_STATUSES = ("active", "trialing")

PLAN_LABELS = {
    PLAN_FREE: "Free",
    PLAN_PREMIUM: "Premium",
}

# What each plan includes. Keys must exist in modules_registry.MODULES.
#
# `courses` is in BOTH: the catalog is how somebody on the free plan sees what
# the paid one contains, so gating the browser itself would defeat the point.
# Which COURSES they can open is decided per course by Course.required_plan,
# not by hiding the module.
PLAN_MODULES = {
    PLAN_FREE: ("home", "courses", "membership", "settings"),
    PLAN_PREMIUM: ("agents", "agent_chat", "credentials", "courses",
                   "membership", "settings"),
}


def is_valid(plan: str) -> bool:
    return plan in PLAN_KEYS


def modules_for_plan(plan: str) -> list:
    """The module keys a plan includes, in registry order.

    Normalized rather than returned verbatim so a key removed from
    modules_registry degrades to "no longer included" instead of being handed
    out as an unknown grant.
    """
    return normalize(PLAN_MODULES.get(plan, PLAN_MODULES[PLAN_FREE]))


def plan_for_subscription_status(subscription_status: str | None) -> str:
    """Which plan a Stripe status buys. The only place that mapping lives."""
    return (PLAN_PREMIUM
            if subscription_status in ACTIVE_SUBSCRIPTION_STATUSES
            else PLAN_FREE)


def plan_for_account(account) -> str:
    """The plan this account is on right now, per Stripe."""
    return plan_for_subscription_status(
        getattr(account, "subscription_status", None))


def includes(plan: str, required_plan: str) -> bool:
    """Whether `plan` satisfies a requirement of `required_plan`.

    The one place plans are ORDERED. Everywhere else treats them as opaque
    keys, so adding a third plan later means editing this function and nothing
    that calls it.
    """
    if required_plan == PLAN_FREE:
        return True
    return plan == PLAN_PREMIUM

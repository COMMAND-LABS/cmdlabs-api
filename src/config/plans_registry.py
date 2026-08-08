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

PLAN vs ROLE vs CEILING — three words, three different axes
-----------------------------------------------------------
    PLAN     what the PLATFORM sells to a customer      free | premium
    CEILING  the modules a plan opens for an org        derived from the plan
    ROLE     what a PERSON is inside an org             config/roles_registry

A CEILING IS ALWAYS A PLAN. It is either the plan the owner pays for, or one
pinned by super admins (organizations.pinned_plan) so billing cannot take it
away. There is no third form and, in particular, no stored list of modules: see
PLAN_MODULES below for why that mattered.

A PLAN IS NOT A ROLE, and the difference is not cosmetic: a plan is what the
org BOUGHT, a role is who a person IS inside it. The cap runs one way — a role
can never open something the plan does not include.

This distinction used to carry more weight than it does now, and the reason is
worth keeping. The right-hand column above was TIER: organization_tiers rows,
editable by the org's own owner. Since every self-serve signup owns their
personal workspace, expressing free vs premium as a tier would have let a free
user rewrite their own tier and self-upgrade — the only thing stopping them was
that the plan lived one level ABOVE what the customer controlled.

Roles are constants in code, editable by nobody, so that particular hole is
closed by construction rather than by a clamp. Keep the levels separate anyway:
the moment anything customer-editable reappears on the module axis, the
argument above comes straight back.

WHY THE SUBSCRIPTION DECIDES, NOT accounts.role
-----------------------------------------------
Same reason the ceiling backfills used it: a role column that has drifted out
of agreement with Stripe would otherwise hand out paid modules that no webhook
would ever take back. The subscription is the fact; role is a cache of it.

WHAT HAPPENS WHEN A CARD FAILS
------------------------------
Three states, decided by ONE stored timestamp (accounts.subscription_lapsed_at)
and the clock:

    ACTIVE   Stripe says paid            premium modules, writes allowed
    GRACE    lapsed, within GRACE_DAYS   premium modules, READS ONLY
    LAPSED   lapsed, past GRACE_DAYS     free modules,    writes allowed

The middle state is the whole point. Dropping straight from premium to free the
moment a payment fails takes Contacts and Deals off the screen entirely — a
404, not a warning — so the first thing a customer learns about their expired
card is that their data appears to be gone. Read-only says the opposite: it is
all still here, and here is the one thing you need to do.

Grace is DERIVED, never stored as a state. There is no "suspend the org" job to
run, nothing to backfill, and no second column that can disagree with Stripe —
the same argument that removed the stored ceiling. A lapse is one timestamp;
everything downstream is a comparison against it.
"""
from datetime import datetime, timedelta, timezone

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
# THE ONLY PLACE THIS IS WRITTEN DOWN, and it has to stay that way. It used to
# have a rival: an org could carry a frozen LIST of modules instead, which is
# what "comp this client" was implemented as. A list is a snapshot, so every
# module added to a plan afterwards simply never reached the comped orgs —
# all three of them ended up missing `courses` (and, then, `spaces`) without anybody
# doing anything wrong. Comping now pins a PLAN (organizations.pinned_plan),
# so it tracks this table as it grows.
#
# PREMIUM IS A STRICT SUPERSET OF FREE. Nothing is only-on-free; upgrading can
# never take a screen away. Asserted in tests/test_course_plans.py.
#
# `courses` is in BOTH: the catalog is how somebody on the free plan sees what
# the paid one contains, so gating the browser itself would defeat the point.
# Which COURSES they can open is decided per course by Course.required_plan, not
# by hiding the module. (`spaces` was in both for the same reason, until spaces
# were removed.)
#
# TWO MODULE KEYS ARE DELIBERATELY IN NEITHER PLAN, and that is not an
# oversight — it is the same oversight this table just stopped having, so it
# is written down:
#
#   membership    how you PAY. Gating it would let a lapsed account be unable
#                 to fix its own billing. Always-visible in the UI
#                 (roles.ts ALWAYS_VISIBLE) and always-allowed on the API
#                 (/api/billing in modules_registry.ALWAYS_ALLOWED_PREFIXES).
#   organization  the owner's console. Gated on OWNERSHIP rather than on the
#                 plan — routers/organizations/overview.py uses
#                 _require_owner — so an owner whose plan happened to omit it
#                 could not administer their own org while the API served
#                 them perfectly well.
#
# Neither has any route_prefixes in modules_registry, so nothing on the API
# consults them at all.
PLAN_MODULES = {
    PLAN_FREE: ("home", "courses", "prompts", "settings"),
    PLAN_PREMIUM: ("home", "agents", "agent_chat", "contacts", "contact_lists",
                   "companies", "deals", "prompts", "knowledge_bases", "access",
                   "credentials", "email_templates", "email_campaigns",
                   "courses", "analytics", "settings"),
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


# How long a lapsed subscription keeps its modules, read-only, before dropping
# to free. Long enough to notice an email and re-enter a card; short enough
# that it is not a way to keep premium for nothing.
GRACE_DAYS = 14

BILLING_ACTIVE = "active"
BILLING_GRACE = "grace"
BILLING_LAPSED = "lapsed"


def _aware(moment: datetime | None) -> datetime | None:
    """Treat a naive timestamp as UTC.

    The column is timestamptz, so Postgres hands back aware values — but a row
    built by hand in a test, or read through SQLite, may not. Comparing the two
    raises TypeError, and this expression decides whether somebody can write to
    their own account: it may not blow up over a tzinfo.
    """
    if moment is None or moment.tzinfo is not None:
        return moment
    return moment.replace(tzinfo=timezone.utc)


def grace_ends_at(lapsed_at: datetime | None) -> datetime | None:
    """When read-only turns into a downgrade. None if nothing has lapsed."""
    lapsed_at = _aware(lapsed_at)
    return None if lapsed_at is None else lapsed_at + timedelta(days=GRACE_DAYS)


def billing_state(subscription_status: str | None,
                  lapsed_at: datetime | None,
                  now: datetime | None = None) -> str:
    """ACTIVE, GRACE or LAPSED — the only place this is decided.

    `now` is injectable so the grace window can be tested without waiting two
    weeks or freezing the system clock.
    """
    if subscription_status in ACTIVE_SUBSCRIPTION_STATUSES:
        return BILLING_ACTIVE

    ends = grace_ends_at(lapsed_at)
    if ends is None:
        # Never subscribed, or lapsed before the timestamp existed. Not a
        # grace period — grace is a courtesy extended AT the moment of lapse,
        # and there is no moment on record to extend it from.
        return BILLING_LAPSED

    return BILLING_GRACE if (now or _utcnow()) < ends else BILLING_LAPSED


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def plan_for_state(state: str) -> str:
    """Which plan a billing state buys.

    Grace buys premium. That is the point: the modules stay, the writes stop.
    """
    return PLAN_PREMIUM if state in (BILLING_ACTIVE, BILLING_GRACE) else PLAN_FREE


def plan_for(subscription_status: str | None,
             lapsed_at: datetime | None,
             now: datetime | None = None) -> str:
    """Which plan these billing facts buy. The only place that mapping lives."""
    return plan_for_state(billing_state(subscription_status, lapsed_at, now))


def plan_for_account(account, now: datetime | None = None) -> str:
    """The plan this account is on right now."""
    return plan_for(getattr(account, "subscription_status", None),
                    getattr(account, "subscription_lapsed_at", None),
                    now)


def includes(plan: str, required_plan: str) -> bool:
    """Whether `plan` satisfies a requirement of `required_plan`.

    The one place plans are ORDERED. Everywhere else treats them as opaque
    keys, so adding a third plan later means editing this function and nothing
    that calls it.
    """
    if required_plan == PLAN_FREE:
        return True
    return plan == PLAN_PREMIUM

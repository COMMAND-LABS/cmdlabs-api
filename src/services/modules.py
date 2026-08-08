"""
Module entitlement: which screens a caller may open.

    effective = org.ceiling  ∩  role.modules

Two levels, and only two:

  - the CEILING — the plan this org may use at all, either pinned by a super
    admin (organizations.pinned_plan) or derived from the owner's subscription.
  - the ROLE (config/roles_registry) — what this person is in the org. A
    manager tracks the whole ceiling; a community member gets a fixed
    allowlist.

ROLES ARE NOT LEVELS EITHER, but they are now platform-wide. This used to read
"the TIER (organization_tiers.modules)", an arbitrary per-org set that the org
owner edited. Roles replaced it: the same intersection, but the right-hand side
is a constant in code rather than a row a customer can rewrite.

That closes a hole this file's cap existed to hold shut. Because tiers were
owner-editable and every signup owns their workspace, a free user could rewrite
their own tier; only clamp_to_ceiling stopped them self-upgrading. A role is
not editable at all, so the cap now guards against nothing but a stale plan.

INTERSECTED AT READ TIME, never cascaded. Lowering a ceiling takes effect on
the caller's next request without rewriting anything — so a revocation can
never be half-applied, and there is no stale grant left behind to re-widen
access later.

Distinct from the DATA axis: modules decide which screens open, org_id decides
which rows are visible. A misconfigured role is a wrong menu; it can never be a
data leak — and it can never be a partial view of the CRM either, which is why
a community member has no CRM module at all rather than a narrowed one.
"""
import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from src.config import plans_registry as plans
from src.config.modules_registry import MODULE_KEYS, normalize
from src.config import roles_registry as roles
from src.db.models import Account, Organization

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OrgEntitlement:
    """What an org may open, and whether it may be changed.

    ONE OBJECT because it is one derivation. Both answers come from the same
    two facts — whether the org has a pinned plan, and the owner's billing —
    and computing them separately is how they end up disagreeing: an org
    showing premium modules while refusing every write, or the reverse.
    """
    # The plan in force: 'free' | 'premium'.
    plan: str
    ceiling: list
    # True during the grace window after a lapse: the modules stay, the writes
    # stop. Never true for a pinned org — see below.
    read_only: bool
    # When read-only turns into a downgrade to free. None when nothing lapsed.
    grace_ends_at: datetime | None


def org_entitlement(db: Session, org_id: int,
                    now: datetime | None = None) -> OrgEntitlement:
    """What this org may use, and whether it may write.

    A CEILING IS ALWAYS A PLAN — either one pinned by super admins, or the one
    the owner is paying for. Nothing is stored except which of those two it is.

    This used to store the answer instead of the question: a `granted_modules`
    list, plus a `ceiling_managed_by` flag saying whether billing was allowed
    to rewrite it. Deriving the billing case removed one copy; the comped case
    kept the other, and a copy with no invalidation path is a value that
    drifts. It did: every module added to a plan after an org was comped never
    reached it, and all three comped orgs on the platform quietly lost
    `courses` without anyone touching them.

    A PIN IS NEVER RECOMPUTED DOWNWARD, AND NEVER GOES READ-ONLY. A super
    admin giving a client a plan is a promise this function must not withdraw
    — the same asymmetry OrganizationMember.granted_by encodes one level down.
    A pinned org has no subscription to lapse, so billing has nothing to say
    about it in either direction.
    """
    row = (db.query(Organization.pinned_plan, Organization.owner_account_id)
             .filter(Organization.id == org_id).first())
    if row is None:
        return OrgEntitlement(plan=plans.PLAN_FREE, ceiling=[],
                              read_only=False, grace_ends_at=None)

    pinned_plan, owner_account_id = row
    if pinned_plan is not None:
        return OrgEntitlement(plan=pinned_plan,
                              ceiling=plans.modules_for_plan(pinned_plan),
                              read_only=False, grace_ends_at=None)

    # An org with no owner (the account was deleted) gets the free plan and is
    # never locked: there is nobody who could fix a payment, so refusing writes
    # would strand it for good.
    if owner_account_id is None:
        return OrgEntitlement(plan=plans.PLAN_FREE,
                              ceiling=plans.modules_for_plan(plans.PLAN_FREE),
                              read_only=False, grace_ends_at=None)

    owner = (db.query(Account.subscription_status,
                      Account.subscription_lapsed_at)
               .filter(Account.id == owner_account_id).first())
    if owner is None:
        return OrgEntitlement(plan=plans.PLAN_FREE,
                              ceiling=plans.modules_for_plan(plans.PLAN_FREE),
                              read_only=False, grace_ends_at=None)

    subscription_status, lapsed_at = owner
    state = plans.billing_state(subscription_status, lapsed_at, now)
    plan = plans.plan_for_state(state)
    return OrgEntitlement(
        plan=plan,
        ceiling=plans.modules_for_plan(plan),
        read_only=(state == plans.BILLING_GRACE),
        grace_ends_at=(plans.grace_ends_at(lapsed_at)
                       if state == plans.BILLING_GRACE else None),
    )


def ceiling_for(db: Session, org_id: int) -> list:
    """What this org may use at all. See org_entitlement."""
    return org_entitlement(db, org_id).ceiling


def effective_modules(db: Session, ctx) -> list:
    """The modules `ctx` may open, in registry order.

    An OWNER gets the org's whole ceiling regardless of the role on their
    membership row. That is a bypass, not a stored grant — it is why an owner's
    role is inert, and why no screen should present it as though it granted
    them anything. It originally existed so a bad save in the tier matrix could
    not lock an owner out of the screen that undid it; with roles fixed in code
    there is no such save, but the bypass stays because ownership outranking
    role is the simpler rule to reason about.

    PLATFORM SUPER ADMINS bypass both layers and get every module that exists.

    That is wider than it was — super admins used to be capped by the ceiling
    of the org they were acting in — and the reason is that the cap never did
    any work. It only meant super admins had to be sitting in an org whose
    ceiling happened to be full, which is the entire job the platform org
    existed to do. Removing the cap removes the need for a special org.

    Safe because of the axis this file is about: modules decide which SCREENS
    open, org_id decides which ROWS are visible. Super admins reading a
    tenant's data still requires JOINING that tenant, which leaves a membership
    row its members can see and a super_admin.join entry naming who and when.
    The widest this goes is super admins opening a screen for an org that never
    bought that module, where they find that org's own (empty) rows.
    """
    if getattr(ctx, "is_super_admin", False):
        return list(MODULE_KEYS)

    ceiling = ceiling_for(db, ctx.org_id)

    if getattr(ctx, "is_owner", False):
        return ceiling

    # Capped by the ceiling inside modules_for: a role can never open something
    # the org has not paid for.
    return roles.modules_for(getattr(ctx, "role", roles.DEFAULT_ROLE), ceiling)


def can_open(db: Session, ctx, module_key: str) -> bool:
    return module_key in set(effective_modules(db, ctx))


def clamp_to_ceiling(db: Session, org_id: int, requested) -> list:
    """Restrict a set of module keys to what the org's ceiling allows.

    Written for the tier matrix, whose saves it silently trimmed rather than
    rejected so an owner's save could not exceed what they had been given. The
    matrix is gone — roles are constants now — and roles_registry.modules_for
    applies the same cap on the read path. Kept because it is the one place
    that expresses "these keys, capped by this org", which any future
    module-granting surface will want.
    """
    ceiling = set(ceiling_for(db, org_id))
    asked = set(normalize(requested))
    dropped = asked - ceiling
    if dropped:
        logger.info(
            "[MODULES] org %s: dropped %s from a tier — outside the org ceiling",
            org_id, sorted(dropped),
        )
    return [k for k in MODULE_KEYS if k in asked and k in ceiling]

"""
Organization membership: every account owns one, from its first verified login.

THE RULE, AND IT HAS NO EXCEPTIONS
----------------------------------
An organization is the tenant, and every account has one of its own. A signup
gets a PERSONAL WORKSPACE — an org with a single member who is its owner — and
a team is the very same object with more members in it. Nothing is a special
case, which is what lets the tenancy rule be `org_id == ctx.org_id` and nothing
else (see services/org_scope.tenant_predicate).

It did not start that way. Every account used to land in the root org, and a
`data_scope='personal'` flag stopped those strangers seeing each other's
contacts. That flag existed for exactly one row and was the only reason row
visibility depended on anything besides org_id. Migrations e3f4a5b6c7d8 and
f4a5b6c7d8e9 split the orgs apart and removed it.

There is no longer a special org. Root used to be the platform's own — where
catalog content lived and where super admins had to be placed to work at all.
Super admins now bypass the module ceiling wherever they are, and publishing
became a Space (itself since removed), so the platform's org is an ordinary tenant like any
customer's.

WHY THE CEILING, NOT THE TIER
-----------------------------
A personal org's member is its owner, and an owner bypasses the tier layer
(services/modules.effective_modules) so that one bad save in the matrix can
never lock them out of the screen that fixes it. The consequence is that for a
personal org the CEILING is the whole entitlement — it is what billing raises
and lowers. Tiers only start meaning anything once an org contains somebody who
is not its owner.

CANONICAL FILE. Keep in sync with cmdlabs-agent-api via ./sync-schemas.sh if
that service ever needs to create memberships (it currently only reads them).
"""
import logging

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.config import plans_registry as plans
from src.config import roles_registry as roles
from src.db.models import Account, Organization, OrganizationMember
from src.services import audit

logger = logging.getLogger(__name__)

# NOTHING IS SEEDED PER ORG ANY MORE. Every org used to get two
# organization_tiers rows — 'owner' and 'member' — because a tier was a per-org,
# owner-editable set of modules and an org with none had an empty matrix. Roles
# are platform-wide constants (config/roles_registry), so there is no per-org
# row to create, nothing to seed wrong, and no way for one org's vocabulary to
# drift from another's.
#
# The tier names that came before are worth remembering as a list of mistakes
# this vocabulary should not repeat:
#
#   - `org_owner` named OWNERSHIP, which is organizations.owner_account_id and
#     never belonged on the module axis at all. It is why 'owner' is not a role
#     value today.
#   - `free` and `premium` borrowed the PLAN axis's vocabulary, which is the one
#     thing this must not be confused with. A plan is what the org bought; a
#     role is who a person is inside it.

# Membership provenance. Unrelated to the org's PLAN, which is now a single
# nullable column (Organization.pinned_plan) rather than a flag over a stored
# module list.
GRANTED_BY_SUBSCRIPTION = "subscription"
GRANTED_BY_GRANT = "grant"

ACTIVE_SUBSCRIPTION_STATUSES = plans.ACTIVE_SUBSCRIPTION_STATUSES

def ceiling_for_account(account: Account) -> list:
    """The modules this account's plan opens, right now.

    A thin read of config/plans_registry — the one answer to "what does premium
    include?". Two module-set constants used to live here instead, named after
    the COLUMN they were written to rather than the thing they were; the column
    is gone and so are they.
    """
    return plans.modules_for_plan(plans.plan_for_account(account))



def pin_plan(db: Session, org: Organization) -> None:
    """Pin an org to the plan it is on right now. Caller commits.

    Called when a workspace becomes a TEAM — the moment somebody who is not the
    owner is let in. Until then the plan is read from the owner's subscription;
    afterwards it must not be, because the people it would move are no longer
    the person paying. A colleague should not lose Contacts because the
    founder's card expired.

    PINS THE PLAN, NOT THE MODULE LIST. This used to write down the resolved
    modules and set a flag saying billing could no longer touch them, which
    made the pin a snapshot: every module added to a plan afterwards never
    reached the org. All three pinned orgs on the platform lost `courses` and
    `courses` that way, silently, and it read as a missing menu item rather than
    as a stale cache. A pinned plan tracks PLAN_MODULES as it grows.

    Idempotent: an org that is already pinned is left exactly as it is.
    """
    if org.pinned_plan is not None:
        return

    from src.services import modules

    plan = modules.org_entitlement(db, org.id).plan
    org.pinned_plan = plan
    audit.record_org_change(
        db, event_type=audit.ORG_CEILING_CHANGE, org_id=org.id,
        detail=f"pinned to the {plan} plan on becoming a team",
    )
    logger.info("[ORG] %s pinned to the %s plan — now a team", org.id, plan)


def is_solo(db: Session, org_id: int) -> bool:
    """True when this org has exactly one member.

    `Organization.is_personal` used to answer this by testing `slug IS NULL`,
    which actually meant "has not been named yet" — a different question that
    happened to give the same answer while naming was required before inviting.
    It stopped being true the moment super admins could join an unnamed org.

    Counted rather than stored. It is one indexed count, it cannot drift, and
    the alternative is a column that has to be maintained on every membership
    change for the sake of a label.
    """
    return (db.query(OrganizationMember)
              .filter(OrganizationMember.org_id == org_id).count()) == 1


def own_org_for(db: Session, account_id: int) -> Organization | None:
    """The workspace this account owns, if it has one.

    Was `personal_org_for`, and keyed on `slug IS NULL` — which meant "has not
    been named yet" and was read as "is a workspace of one". Those came apart
    the moment anything could add a member without naming the org. Ownership is
    the honest key: an account owns the org created for it at signup, whether
    or not anybody else has since joined.
    """
    return (
        db.query(Organization)
        .filter(Organization.owner_account_id == account_id)
        .first()
    )


# Old name, kept briefly for readers. Prefer own_org_for.
personal_org_for = own_org_for


def ensure_membership(db: Session, account: Account,
                      org: Organization | None = None) -> OrganizationMember | None:
    """Ensure `account` can act somewhere. Idempotent.

    With no `org`, this creates (or finds) the account's own personal
    workspace. Passing an org joins that one instead — which is what an invite
    will do.

    Called at first VERIFIED login rather than at account creation, because
    accounts are INSERTed in /request-code, before the OTP is checked. An
    unverified squatter therefore gets an account row and no org, and since
    they can never obtain a JWT they never need one — and no empty workspace is
    created for an address nobody controls.
    """
    if org is None:
        org = personal_org_for(db, account.id)
        if org is None:
            org = _create_personal_org(db, account)

    existing = (
        db.query(OrganizationMember)
        .filter(OrganizationMember.org_id == org.id,
                OrganizationMember.account_id == account.id)
        .first()
    )
    if existing:
        if account.default_org_id is None:
            account.default_org_id = org.id
            db.commit()
        return existing

    # Ownership is not written here — it is organizations.owner_account_id, and
    # this row no longer carries a copy. The owner's role is INERT (they bypass
    # it in modules.effective_modules), so it is set to the same default as
    # anybody else rather than to a special value that would imply otherwise.
    role = roles.DEFAULT_ROLE
    member = OrganizationMember(
        org_id=org.id,
        account_id=account.id,
        role=role,
        # Never 'subscription': a membership is not what billing acts on. For a
        # personal org billing moves the CEILING, and for a team org the member
        # is there because an owner put them there.
        granted_by=GRANTED_BY_GRANT,
    )
    db.add(member)
    if account.default_org_id is None:
        account.default_org_id = org.id

    # Joining an org is the moment someone gains access to a tenant's data —
    # the single most consequential event on the platform, and it was
    # previously unlogged.
    audit.record_membership(
        db, event_type=audit.MEMBER_ADD, org_id=org.id,
        account_id=account.id, role=role,
        actor_account_id=account.id,
    )

    try:
        db.commit()
    except IntegrityError:
        # Two concurrent /verify-code calls for the same account raced on
        # uq_org_member. Whichever lost re-reads the winner's row rather than
        # failing the login.
        db.rollback()
        return (
            db.query(OrganizationMember)
            .filter(OrganizationMember.org_id == org.id,
                    OrganizationMember.account_id == account.id)
            .first()
        )

    db.refresh(member)
    return member


def _create_personal_org(db: Session, account: Account) -> Organization:
    """A workspace of one, owned by its only member.

    No public identity to invent. Orgs used to carry an immutable `slug`, and
    this deliberately left it NULL rather than generating `user-273` — a
    permanent public name nobody chose. The column is gone, so the property is
    now structural: an org is its id, and its display name is a label the owner
    can change.
    """
    name = (account.email or "").split("@")[0] or "Workspace"
    org = Organization(
        name=name,
        owner_account_id=account.id,
        # pinned_plan stays NULL: this workspace follows its owner's
        # subscription, which is what every self-serve signup should do. A
        # super admin pins a plan (admin.set_plan), and the moment somebody
        # else is let in pin_plan() does it automatically — see there for why.
    )
    db.add(org)
    db.flush()

    audit.record_org_change(
        db, event_type=audit.ORG_CREATE, org_id=org.id,
        detail=f"created on the {plans.plan_for_account(account)} plan",
        actor_account_id=account.id,
    )
    return org





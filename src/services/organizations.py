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
catalog content lived and where staff had to be placed to work at all. Staff
now bypass the module ceiling wherever they are, and publishing became a Space,
so the platform's org is an ordinary tenant like any customer's.

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
from src.db.models import Account, Organization, OrganizationMember, OrganizationTier
from src.services import audit

logger = logging.getLogger(__name__)

TIER_OWNER = "owner"
TIER_MEMBER = "member"

# The tier staff hold in the platform org. TIER_FREE / TIER_PREMIUM lived here
# too and are gone: nothing read them, and a plan is not a tier.
TIER_ORG_OWNER = "org_owner"

# Entitlement provenance. 'subscription' is owned by the Stripe webhook and
# lapses with billing; 'grant' is set by a human and is NEVER written by any
# webhook — that asymmetry is what lets staff comp a client into paid access.
GRANTED_BY_SUBSCRIPTION = "subscription"
GRANTED_BY_GRANT = "grant"

ACTIVE_SUBSCRIPTION_STATUSES = plans.ACTIVE_SUBSCRIPTION_STATUSES

# The plan module sets, by their old names.
#
# These used to be literal lists here, named after the COLUMN they are written
# to rather than the thing they are. The definition now lives in
# config/plans_registry.py — one answer to "what does premium include?" — and
# these stay as aliases because a handful of tests and readers still reach for
# them. Migration e3f4a5b6c7d8 seeded its own point-in-time copy and must not
# follow either of them as the product changes.
FREE_CEILING = plans.modules_for_plan(plans.PLAN_FREE)
PREMIUM_CEILING = plans.modules_for_plan(plans.PLAN_PREMIUM)


def ceiling_for_account(account: Account) -> list:
    """The modules a new personal workspace starts with — i.e. their plan.

    Derived from the subscription rather than accounts.role, for the same
    reason the backfill migrations do it that way: a row whose role has drifted
    out of agreement with Stripe would otherwise be handed paid modules that no
    webhook would ever take back.

    For a personal workspace the ceiling IS the plan. It is stored one level
    above the tier layer on purpose — tiers are editable by the org's own
    owner, and every self-serve signup owns their workspace, so a plan
    expressed as a tier would be a plan the customer could rewrite. See
    config/plans_registry.py.
    """
    return plans.modules_for_plan(plans.plan_for_account(account))


def freeze_ceiling(db: Session, org: Organization) -> None:
    """Pin an org's ceiling at what it currently resolves to. Caller commits.

    Called when a workspace becomes a TEAM — the moment somebody who is not the
    owner is let in. Until then the ceiling is derived from the owner's plan and
    follows their subscription; afterwards it must not, because the people it
    would move are no longer the person paying. A colleague should not lose
    Contacts because the founder's card expired.

    So the derived value is written down and ownership of the column passes to
    'grant', which no automated path ever rewrites. Idempotent: an org that is
    already granted is left exactly as it is.
    """
    if org.ceiling_managed_by != GRANTED_BY_SUBSCRIPTION:
        return

    from src.services import modules

    frozen = modules.ceiling_for(db, org.id)
    org.granted_modules = frozen
    org.ceiling_managed_by = GRANTED_BY_GRANT
    audit.record_org_change(
        db, event_type=audit.ORG_CEILING_CHANGE, org_id=org.id,
        detail=f"frozen on becoming a team: {','.join(frozen) or '(none)'}",
    )
    logger.info("[ORG] %s ceiling frozen at %s — now a team", org.id, frozen)


def is_solo(db: Session, org_id: int) -> bool:
    """True when this org has exactly one member.

    `Organization.is_personal` used to answer this by testing `slug IS NULL`,
    which actually meant "has not been named yet" — a different question that
    happened to give the same answer while naming was required before
    inviting. It stopped being true the moment staff could join an unnamed org.

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

    is_owner = (org.owner_account_id == account.id)
    tier_key = TIER_OWNER if is_owner else TIER_MEMBER
    member = OrganizationMember(
        org_id=org.id,
        account_id=account.id,
        tier_key=tier_key,
        # Never 'subscription': a membership is not what billing acts on. For a
        # personal org billing moves the CEILING, and for a team org the member
        # is there because an owner put them there.
        granted_by=GRANTED_BY_GRANT,
        is_owner=is_owner,
    )
    db.add(member)
    if account.default_org_id is None:
        account.default_org_id = org.id

    # Joining an org is the moment someone gains access to a tenant's data —
    # the single most consequential event on the platform, and it was
    # previously unlogged.
    audit.record_membership(
        db, event_type=audit.MEMBER_ADD, org_id=org.id,
        account_id=account.id, tier_key=tier_key,
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
        granted_modules=ceiling_for_account(account),
        # Billing owns this org's ceiling until staff overrides it, at which
        # point admin.set_ceiling flips this to 'grant' and the webhook stops
        # touching it. That is the comp mechanism, one level up from
        # OrganizationMember.granted_by.
        ceiling_managed_by=GRANTED_BY_SUBSCRIPTION,
    )
    db.add(org)
    db.flush()

    # Seeded so that converting this workspace into a team is picking a slug
    # and inviting somebody — not first discovering the tiers page is empty.
    # `member` starts with nothing: an invited person gets what the owner
    # deliberately checks in the matrix, never a default they did not choose.
    db.add(OrganizationTier(org_id=org.id, tier_key=TIER_OWNER, label="Owner",
                            modules=list(org.granted_modules)))
    db.add(OrganizationTier(org_id=org.id, tier_key=TIER_MEMBER, label="Member",
                            modules=[]))
    db.flush()

    audit.record_org_change(
        db, event_type=audit.ORG_CREATE, org_id=org.id,
        detail=",".join(org.granted_modules) or "(none)",
        actor_account_id=account.id,
    )
    return org





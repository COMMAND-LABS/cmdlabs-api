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

THE PLAN FOLLOWS THE OWNER, SOLO OR TEAM
----------------------------------------
An org's plan is its owner's subscription unless a super admin pinned one
(services/modules.org_entitlement). Owners bypass roles
(services/modules.effective_modules), so for a personal org the plan is the
whole entitlement. Roles only start meaning anything once an org contains
somebody who is not its owner.

Accepting an invitation used to pin the plan the org was on at that moment.
That froze teams in both directions: an owner who cancelled kept premium for
free, and a free owner who later paid stayed on free. Teams now track the
owner's billing like everyone else, and the 14-day read-only grace window
(config/plans_registry.billing_state) is what protects colleagues when a card
fails.

"""
import logging
import re

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
GRANTED_BY_GRANT = "grant"


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



def ensure_membership(db: Session, account: Account,
                      org: Organization | None = None) -> OrganizationMember | None:
    """Ensure `account` can act somewhere. Idempotent.

    With no `org`, this finds the account somewhere to be, creating a personal
    workspace only if it has nowhere else. Passing an org joins that one
    instead.

    Called at first VERIFIED login rather than at account creation, because
    accounts are INSERTed in /request-code, before the OTP is checked. An
    unverified squatter therefore gets an account row and no org, and since
    they can never obtain a JWT they never need one — and no empty workspace is
    created for an address nobody controls.

    "NOWHERE ELSE" IS FOUR QUESTIONS, AND IT USED TO BE ONE
    -------------------------------------------------------
    This function used to jump straight to `personal_org_for(...) or
    _create_personal_org(...)`, which asks only "do they own an org?". Every
    invited person answered no, so every invited person got a workspace of
    their own minted at their first sign-in — an empty org named after their
    email address, which they owned, on top of the team they had actually been
    invited to. They were then dropped into whichever one default_org_id
    happened to name.

    So the order below is the fix, and each step is a different reason not to
    create anything:

      1. Already a member somewhere  — creating a second home for somebody who
                                       has one is the bug above.
      2. Owns an org but holds no    — drift, not a missing workspace. Join the
         membership row in it          org they already own rather than making
                                       a second one they also own.
      3. Has an outstanding          — they have somewhere to go and have not
         invitation                    answered yet. Minting a workspace here
                                       is the same bug as (1), just two minutes
                                       earlier. Returns None: they are
                                       org-less on purpose until they answer,
                                       and the dashboard routes them to the
                                       invitation (routers/organizations/
                                       invitations.py, /invitations/mine).
      4. None of the above           — an ordinary self-serve signup. Create
                                       the workspace.

    Step 3 is the one with a consequence worth stating: between signing in and
    answering, every org-scoped request they make is a 403. That is deliberate
    — there is genuinely no org they may act in yet — and declining is what
    resolves it, which is why decline_invitation calls back into here.
    """
    if org is None:
        # 1. Anywhere at all. The cheapest question and the one that was never
        #    asked; an invitee who has accepted lands here.
        existing_anywhere = (
            db.query(OrganizationMember)
            .filter(OrganizationMember.account_id == account.id)
            .first()
        )
        if existing_anywhere:
            if account.default_org_id is None:
                account.default_org_id = existing_anywhere.org_id
                db.commit()
            return existing_anywhere

        # 2. An org they own but are somehow not a member of. Historically this
        #    drift existed and left owners unable to open their own org, so it
        #    is repaired rather than papered over with a second workspace.
        org = own_org_for(db, account.id)

        if org is None:
            # 3. Imported here rather than at module scope: services/
            #    invitations imports GRANTED_BY_GRANT from this module, and a
            #    top-level import both ways is a cycle.
            from src.services import invitations

            if invitations.has_pending_for_email(db, account.email):
                logger.info(
                    "[ORG] account %s has a pending invitation — no personal "
                    "workspace created", account.id,
                )
                return None

            # 4.
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
        # subscription, and keeps following it after other people join. Only
        # a super admin pins a plan (admin.set_plan), to comp an org.
    )
    db.add(org)
    db.flush()

    audit.record_org_change(
        db, event_type=audit.ORG_CREATE, org_id=org.id,
        detail=f"created on the {plans.plan_for_account(account)} plan",
        actor_account_id=account.id,
    )
    return org






# ---------------------------------------------------------------------------
# Public address (slug)
# ---------------------------------------------------------------------------
#
# An org MAY have a slug, so that /org/<slug>/login can show its name and land
# a member in it. It is an address and nothing more: every route still keys on
# the id, personal workspaces are never assigned one, and the owner can change
# or clear it. The earlier, immutable slug and why it went: migration
# f4a5b6c7d8f0.

SLUG_MIN_LEN = 3
SLUG_MAX_LEN = 63
# Lowercase letters, digits and hyphens, never a hyphen at either end.
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])?$")

# Route segments and words that would mislead as an org's public address.
# Reserved outright, because the address is shown to people who have not
# signed in and have no other way to tell our pages from an org's.
RESERVED_SLUGS = frozenset({
    "login", "signup", "sign-in", "sign-up", "logout", "register",
    "admin", "api", "app", "dashboard", "invite", "invitations",
    "org", "orgs", "organization", "organizations",
    "pricing", "settings", "billing", "account", "accounts", "me", "mine",
    "new", "by-slug", "www", "support", "help", "docs", "blog", "about",
    "resources", "static", "public", "root",
    "cmdlabs", "command-labs", "commandlabs",
})


class SlugError(ValueError):
    """An address the owner may not have. The message is written for them."""


class SlugTakenError(SlugError):
    """Another org already holds this address."""


def normalize_slug(raw: str | None) -> str | None:
    """Trim and lowercase; an empty value means "no address"."""
    if raw is None:
        return None
    value = raw.strip().lower()
    return value or None


def validate_slug(slug: str) -> None:
    """Raise SlugError unless `slug` (already normalized) is one we allow."""
    if len(slug) < SLUG_MIN_LEN:
        raise SlugError(
            f"That address is too short ({SLUG_MIN_LEN} characters minimum).")
    if len(slug) > SLUG_MAX_LEN:
        raise SlugError(
            f"That address is too long ({SLUG_MAX_LEN} characters max).")
    if not SLUG_RE.fullmatch(slug):
        raise SlugError("Use lowercase letters, digits and hyphens, starting "
                        "and ending with a letter or digit.")
    if slug.isdigit():
        # An all-digit address reads as an id, and ids are how every other
        # route names an org.
        raise SlugError("An address needs at least one letter.")
    if slug in RESERVED_SLUGS:
        raise SlugError("That address is reserved.")


def find_by_slug(db: Session, slug: str) -> Organization | None:
    return db.query(Organization).filter(Organization.slug == slug).first()


def set_slug(db: Session, org: Organization, slug: str | None, *,
             actor_account_id: int) -> Organization:
    """Set, change or clear an org's public address. Commits.

    Raises SlugError for anything the owner can fix — shape, reserved words —
    and SlugTakenError when another org already holds the address.
    """
    slug = normalize_slug(slug)
    if slug is not None:
        validate_slug(slug)
        taken = (db.query(Organization.id)
                   .filter(Organization.slug == slug,
                           Organization.id != org.id)
                   .first())
        if taken:
            raise SlugTakenError("That address is already taken.")

    before = org.slug
    if before == slug:
        return org

    org.slug = slug
    audit.record_org_change(
        db, event_type=audit.ORG_SLUG_CHANGE, org_id=org.id,
        detail=f"{before!r} -> {slug!r}",
        actor_account_id=actor_account_id,
    )
    try:
        db.commit()
    except IntegrityError:
        # Two owners raced for the same address between the check above and
        # the commit; uq_organizations_slug picked a winner and this one lost.
        db.rollback()
        raise SlugTakenError("That address is already taken.")
    db.refresh(org)
    return org


def membership_for_slug(db: Session, account_id: int,
                        slug: str) -> tuple[Organization | None, bool]:
    """The org at `slug`, and whether `account_id` is a member of it.

    (None, False) when no org has that address. Membership is the ONLY
    question asked here; nothing joins anybody to anything.
    """
    org = find_by_slug(db, slug)
    if org is None:
        return None, False
    is_member = (
        db.query(OrganizationMember.id)
          .filter(OrganizationMember.org_id == org.id,
                  OrganizationMember.account_id == account_id)
          .first()
    ) is not None
    return org, is_member

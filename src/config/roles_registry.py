"""
The role registry: what a member IS in an org, and what that opens.

Three roles, and only three. Two are stored on the membership row; the third is
not stored at all.

    OWNER              organizations.owner_account_id, and nowhere else
    MANAGER            the core team — everything the org's plan allows
    COMMUNITY_MEMBER   people the org SERVES — an explicit, small allowlist

OWNER IS NOT IN THIS FILE'S ENUM, DELIBERATELY
----------------------------------------------
It is tempting to make ownership a third value of `role`, so that one column
answers "what is this person". Do not. Ownership is already a column —
organizations.owner_account_id — and the platform has run this experiment: an
`is_owner` flag lived on organization_members alongside it, the two drifted,
and orgs ended up with an owner_account_id naming somebody who held no is_owner
row and therefore could not open the org they owned. See db/models
.OrganizationMember.

So ownership stays exactly one fact in exactly one place, and is DERIVED for
display. A row's `role` says what they are when they are not the owner.

WHY MANAGER IS THE CEILING AND COMMUNITY IS A LIST
--------------------------------------------------
The asymmetry below is the whole design, and it is not an oversight:

    MANAGER            -> whatever the plan allows, resolved at read time
    COMMUNITY_MEMBER   -> a fixed tuple of module keys

A manager collaborates on everything the org bought, so their access has to
TRACK the plan. Writing their modules out as a list would make it a snapshot,
and every module added to a plan afterwards would silently never reach them —
which is precisely the bug config/plans_registry documents about frozen
`granted_modules` lists, where three comped orgs quietly lost `courses`.

A community member's role exists to WITHHOLD. An allowlist is therefore the
correct shape: a module added to the platform tomorrow must not reach them
because somebody forgot to exclude it. Default-deny for the restrictive role,
default-track for the permissive one.

WHAT THIS FILE CANNOT DO
------------------------
Roles gate MODULES, which are screens and route prefixes. They do not narrow
ROWS. The tenancy rule is `org_id == ctx.org_id` with no exceptions, and the
CRM tables carry no visibility column — so a role that opens `contacts` opens
EVERY contact in the org, and a role that does not open it opens none.

That is why COMMUNITY_MODULES contains no CRM key and must not acquire one. If
a community member should see "some contacts but not others", this file is the
wrong place to express it: that needs a visibility column on the rows
themselves, the way agents and vector stores already have one.

REPLACED organization_tiers, which was a per-org, owner-editable matrix of
arbitrary module sets. Three platform-wide roles lose the ability for an owner
to define or sell their own bundles, and gain an answer to "what can this
person do?" that is the same in every org and cannot be edited into a
self-upgrade.

CANONICAL FILE. Mirrored into cmdlabs-agent-api via ./sync-schemas.sh.
"""

# Stored on organization_members.role. Stable identifiers, never display names:
# a rename in the UI must not change what a persisted row means.
ROLE_MANAGER = "manager"
ROLE_COMMUNITY_MEMBER = "community_member"

# The values the column admits. Kept in step with ck_org_member_role.
ROLE_KEYS = (ROLE_MANAGER, ROLE_COMMUNITY_MEMBER)

# What a new member joins as when nobody says otherwise. The SMALLER role, on
# the same principle as the seeded 'member' tier it replaces: an invited person
# gets what the owner deliberately chose, never a default they did not.
DEFAULT_ROLE = ROLE_COMMUNITY_MEMBER

ROLE_LABELS = {
    ROLE_MANAGER: "Manager",
    ROLE_COMMUNITY_MEMBER: "Community Member",
}

# For admin UIs and invite pickers. Not used for matching — see the key note.
ROLE_DESCRIPTIONS = {
    ROLE_MANAGER:
        "Collaborates on the business — full access to everything the "
        "organization's plan includes.",
    ROLE_COMMUNITY_MEMBER:
        "Someone the organization serves. Can take courses and talk to "
        "agents; cannot see contacts, deals, or anything the team is "
        "building.",
}

# The community member's ENTIRE surface. An allowlist, and it stays one.
#
# Note what is in and what is not, because the line is the point:
#
#   agent_chat  IN  — using an agent someone else configured
#   agents      OUT — authoring them; an agent carries credentials and reaches
#                     knowledge bases, so building one is a team activity
#   courses     IN  — published material is what "serving people" means here
#   home        IN  — there has to be somewhere to land
#
# Everything else is out, and adding to this tuple is a decision about who sees
# your customers' data. `contacts`, `contact_lists`, `companies`, `deals`,
# `credentials`, `access`, `analytics`, `email_*` may not appear here.
COMMUNITY_MODULES = ("home", "courses", "agent_chat")


def is_valid(role: str) -> bool:
    return role in ROLE_KEYS


def label(role: str) -> str:
    """Display name for a role key, falling back to the key itself.

    Falls back rather than raising: a row holding a role this build does not
    know about is a deployment-order problem, not a reason to fail the request
    that was only trying to render somebody's name.
    """
    return ROLE_LABELS.get(role, role)


def modules_for(role: str, ceiling) -> list:
    """The modules a NON-OWNER holding `role` may open, capped by the ceiling.

    `ceiling` is the org's plan, already resolved. Both branches are capped by
    it, so a role can never open something the org has not paid for — the cap
    is what makes this safe to reason about independently of billing.

    Owners do not come through here. They bypass roles entirely
    (services/modules.effective_modules), which is why an owner's role is inert
    and never shown as though it granted anything.
    """
    if role == ROLE_MANAGER:
        # Tracks the plan rather than enumerating it. See the module docstring.
        return list(ceiling)
    return [k for k in ceiling if k in COMMUNITY_MODULES]

"""
The module registry: stable keys, and what each one gates.

A module is a top-level area of the product. Two things reference it:

  - the UI side menu (cmdlabs-ui/src/config/navigation.tsx)
  - the API, via the route prefixes below

KEYS ARE STABLE IDENTIFIERS, NOT DISPLAY NAMES. Grants are persisted by key, so
renaming "Deals" to "Pipeline" in the UI must not revoke anybody's access. That
is the whole reason this file exists rather than matching on menu labels.

The route prefixes are what make a module grant mean something on the server.
Without them the tiers matrix only hides menu items, and anyone who crafts a
request by hand reaches the data anyway — which is precisely the state
cmdlabs-ui/src/config/roles.ts documents in its own header comment.

Keep in step with ALL_MODULES in migration e8f9a0b1c2d3, which seeded the first
ceilings. That list is a point-in-time snapshot; this file is the live one.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Module:
    key: str
    label: str                      # for admin UIs; never used for matching
    route_prefixes: tuple = field(default_factory=tuple)


# Routes every authenticated caller reaches regardless of entitlement.
# Gating any of these would be self-defeating: an account with no modules could
# not see its own settings, could not pay to get more, and could not sign out.
ALWAYS_ALLOWED_PREFIXES = (
    "/api/auth",
    "/api/accounts",
    "/api/billing",
    "/api/logins",
    "/api/feedback",
    "/api/waitlist",
    "/api/api-keys",
    "/api/organizations",   # your own org + entitlements
    "/api/admin",           # separately gated by require_super_admin
    "/t",                   # public email tracking
    "/healthcheck",
)


MODULES = (
    Module("home", "Home", ()),
    # /api/files is the CHAT file surface, not the knowledge-base one: upload,
    # signed-url and source-url are called from exactly one place in the UI
    # (services/uploadChatFile.ts) — attaching a file to a chat and opening the
    # document behind a citation. source_url.py gates on can_access_agent, so
    # it is agent-scoped by construction. Classifying it under knowledge_bases
    # took chat file upload and citation links away from every tier that has
    # Agents but not Knowledge Bases, which is what the seeded premium tier is.
    Module("agents", "Agents", ("/api/agents", "/api/tool-approvals", "/api/files")),
    Module("agent_chat", "Agent Chat", ("/api/chat-sessions",)),
    Module("contacts", "Contacts", ("/api/contacts",)),
    Module("contact_lists", "Contact Lists", ("/api/contact-lists",)),
    Module("companies", "Companies", ("/api/companies",)),
    Module("deals", "Deals", ("/api/deals",)),
    Module("prompts", "Prompts", ("/api/prompts",)),
    Module("knowledge_bases", "Knowledge Bases",
           ("/api/vector-stores", "/api/similarity-search")),
    Module("access", "Access", ("/api/access-groups", "/api/access")),
    Module("credentials", "Credentials", ("/api/credentials",)),
    Module("email_templates", "Email Templates", ("/api/email-templates",)),
    Module("email_campaigns", "Email Campaigns",
           ("/api/email-campaigns", "/api/emails", "/api/email-events")),
    # Courses are gated like any other product area. Banding free vs paid
    # courseware is therefore a TIER question, not a per-course one — module
    # keys are platform-wide and stable, while tier_key is a per-org string
    # that means nothing in a second org.
    Module("courses", "Courses", ("/api/courses",)),
    # Spaces are the SECOND container — shared content across orgs. Gated like
    # any other product area: the module decides whether you see the feature at
    # all, and SpaceMember decides which spaces you can actually reach. Two
    # different questions, and the module answer is never the access answer.
    Module("spaces", "Spaces", ("/api/spaces",)),
    Module("analytics", "Analytics", ()),
    Module("membership", "Membership", ()),
    Module("settings", "Settings", ()),
    Module("organization", "Organization", ()),
)

MODULE_KEYS = tuple(m.key for m in MODULES)
BY_KEY = {m.key: m for m in MODULES}


def is_valid(key: str) -> bool:
    return key in BY_KEY


def normalize(keys) -> list:
    """Drop unknown keys, de-duplicate, and return in registry order.

    Unknown keys are DISCARDED rather than rejected: a stored grant naming a
    module that has since been removed should degrade to "no longer available",
    not break every request for that member.
    """
    given = set(keys or ())
    return [k for k in MODULE_KEYS if k in given]


def module_for_path(path: str) -> Module | None:
    """The module gating `path`, or None when the path is always allowed.

    Longest prefix wins, so a more specific route cannot be shadowed by a
    shorter one that happens to share its opening segments.
    """
    for prefix in ALWAYS_ALLOWED_PREFIXES:
        if path.startswith(prefix):
            return None

    best = None
    best_len = 0
    for module in MODULES:
        for prefix in module.route_prefixes:
            if path.startswith(prefix) and len(prefix) > best_len:
                best, best_len = module, len(prefix)
    return best

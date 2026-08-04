"""
The tiers the migration actually seeded must serve the screens they grant.

WHY THIS FILE EXISTS
--------------------
Enforcement turned module grants from a menu filter into an authorization
boundary. That changed the meaning of the seeded tier lists without changing
the lists: they were copied from the pre-org UI, where hiding a menu item was
all a tier did and every endpoint answered regardless. Under enforcement, a
module a tier does not name is a 404.

The premium tier grants Agents but not Knowledge Bases. /api/files — chat file
upload and the source document behind a citation — was classified under
knowledge_bases, so shipping enforcement would have taken chat attachments and
citation links away from every paying subscriber. Nothing caught it: the module
suite narrows a tier by hand to prove the mechanism works, and conftest's
fixture org grants every tier every module, so no test ever ran a caller on a
tier a real account actually holds.

So the invariant here is not "gating works" — test_module_enforcement.py covers
that. It is "the tiers we shipped are internally consistent": a tier that grants
a surface must also grant everything that surface calls.

Adding a cross-module call from a UI surface means adding it to
SURFACE_DEPENDENCIES below. If the tier lists cannot satisfy it, that is a
pricing decision to make deliberately — which is the point of failing here
rather than in a customer's console.
"""
import pytest
from sqlalchemy.orm import Session

from src.config.modules_registry import module_for_path
from src.db.models import Organization, OrganizationTier
from tests.org_isolation import client_for, make_tenant

# Copied from migration e8f9a0b1c2d3, which seeded the root org's tiers. Copied
# rather than imported: the migration is a point-in-time snapshot, and pinning
# the values here means a later edit to either one shows up as a diff in this
# file instead of quietly agreeing with itself.
SEEDED_TIERS = {
    "free": ["home", "membership", "settings"],
    "premium": ["agents", "agent_chat", "credentials", "membership", "settings"],
    "org_owner": ["agents", "agent_chat", "credentials", "membership",
                  "settings", "organization"],
}

# Route paths each module's UI surface calls, INCLUDING calls that belong to
# another module's prefix. The second kind is the whole point — a dependency
# inside your own module can never be the thing that breaks.
SURFACE_DEPENDENCIES = {
    # services/uploadChatFile.ts: attaching a file to a chat, and opening the
    # document behind a citation. Both are agent-scoped (source_url.py gates on
    # can_access_agent) despite living under /api/files.
    "agents": ["/api/files/signed-url"],
    "agent_chat": ["/api/files/signed-url"],
}


def _tier_reaches(module_keys: list[str], path: str) -> bool:
    module = module_for_path(path)
    return module is None or module.key in module_keys


@pytest.mark.parametrize("tier_key", sorted(SEEDED_TIERS))
def test_seeded_tier_covers_its_own_surfaces(tier_key):
    """Every path a granted module's UI calls is inside that same tier.

    A pure-data check with no database: the failure it guards against is a
    classification mistake, and the message needs to name the tier, the module
    and the path so the fix is obvious.
    """
    granted = SEEDED_TIERS[tier_key]
    missing = []
    for module_key in granted:
        for path in SURFACE_DEPENDENCIES.get(module_key, []):
            if not _tier_reaches(granted, path):
                owner = module_for_path(path)
                missing.append(
                    f"{path} (needs {owner.key!r}) — called by {module_key!r}"
                )

    assert not missing, (
        f"The {tier_key!r} tier grants a surface it cannot fully serve:\n  "
        + "\n  ".join(missing)
        + "\nEither the route is classified under the wrong module in "
          "src/config/modules_registry.py, or the tier needs widening — the "
          "latter is a pricing decision, not a bug fix."
    )


async def test_a_real_premium_member_can_use_chat_files(db: Session, _override_db):
    """The end-to-end version of the above, on the tier as actually seeded.

    Runs the request rather than reasoning about the registry, so a future
    change that gates /api/files some other way (a dependency added to the
    router, say) still fails here.
    """
    tenant = make_tenant(db, slug="tier-premium", account_id=9310,
                         data_scope="personal", tier_key="premium",
                         is_owner=False)
    org = db.query(Organization).filter(Organization.id == tenant.org_id).one()
    org.granted_modules = sorted(
        {m for mods in SEEDED_TIERS.values() for m in mods}
    )
    tier = (db.query(OrganizationTier)
              .filter(OrganizationTier.org_id == tenant.org_id).one_or_none())
    if tier is None:
        tier = OrganizationTier(org_id=tenant.org_id, tier_key="premium",
                                label="Premium")
        db.add(tier)
    tier.tier_key = "premium"
    tier.modules = SEEDED_TIERS["premium"]
    db.flush()

    async with client_for(tenant) as c:
        resp = await c.get("/api/files/signed-url?path=chat/x.pdf")

    assert resp.status_code != 404, (
        "a premium subscriber lost chat file upload and citation source links — "
        "/api/files is gated by a module the premium tier does not hold"
    )

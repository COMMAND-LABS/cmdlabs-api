"""
Every plan we sell must serve the screens it grants.

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
that. It is "the plans we sell are internally consistent": a plan that grants a
surface must also grant everything that surface calls.

Keyed to PLANS rather than to the three tiers the 2024 migration seeded. Those
were retired in b3c4d5e6f7a8, and a tier was never the right unit anyway — it
is per-org and chosen by that org's owner, so there is no fixed list to check.
A plan is fixed and is what somebody actually buys.

Adding a cross-module call from a UI surface means adding it to
SURFACE_DEPENDENCIES below. If a plan cannot satisfy it, that is a pricing
decision to make deliberately — which is the point of failing here
rather than in a customer's console.
"""
import pytest
from sqlalchemy.orm import Session

from src.config import plans_registry as plans
from src.config.modules_registry import module_for_path
from src.db.models import Organization
from tests.org_isolation import client_for, make_tenant

# The PLANS, read from the registry that defines them.
#
# This used to be a hand-copy of the three tiers migration e8f9a0b1c2d3 seeded
# — 'free', 'premium' and 'org_owner'. Two problems with that, both fixed by
# looking at plans instead:
#
#   - b3c4d5e6f7a8 retired those three keys, so the literal described a
#     vocabulary the product no longer has. It kept passing because it was a
#     literal, which is the worst way for a test to be wrong.
#   - a TIER is per-org and chosen by that org's owner, so there is no fixed
#     list to check. A PLAN is fixed, is what a customer actually buys, and is
#     therefore the thing whose internal consistency matters.
#
# Imported rather than copied now, for the opposite reason to before: there is
# one definition of what premium includes, and a test that disagreed with it
# would be testing its own copy.
SHIPPED_PLANS = {key: list(plans.modules_for_plan(key)) for key in plans.PLAN_KEYS}

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


def _plan_reaches(module_keys: list[str], path: str) -> bool:
    module = module_for_path(path)
    return module is None or module.key in module_keys


@pytest.mark.parametrize("plan_key", sorted(SHIPPED_PLANS))
def test_each_plan_covers_its_own_surfaces(plan_key):
    """Every path a granted module's UI calls is inside that same plan.

    A pure-data check with no database: the failure it guards against is a
    classification mistake, and the message needs to name the plan, the module
    and the path so the fix is obvious.
    """
    granted = SHIPPED_PLANS[plan_key]
    missing = []
    for module_key in granted:
        for path in SURFACE_DEPENDENCIES.get(module_key, []):
            if not _plan_reaches(granted, path):
                owner = module_for_path(path)
                missing.append(
                    f"{path} (needs {owner.key!r}) — called by {module_key!r}"
                )

    assert not missing, (
        f"The {plan_key!r} plan grants a surface it cannot fully serve:\n  "
        + "\n  ".join(missing)
        + "\nEither the route is classified under the wrong module in "
          "src/config/modules_registry.py, or the plan needs widening — the "
          "latter is a pricing decision, not a bug fix."
    )


async def test_a_real_premium_member_can_use_chat_files(db: Session, _override_db):
    """The end-to-end version of the above, on a member of a premium org.

    Runs the request rather than reasoning about the registry, so a future
    change that gates /api/files some other way (a dependency added to the
    router, say) still fails here.

    A NON-OWNER deliberately: an owner takes the org's whole ceiling and would
    pass this whatever the tier said, which is exactly the way to write a test
    that cannot fail.
    """
    tenant = make_tenant(db, slug="plan-premium-surface", account_id=9310,
                         role="manager", is_owner=False)
    org = db.query(Organization).filter(Organization.id == tenant.org_id).one()
    # The org is on the premium PLAN — that is the ceiling. It used to set
    # org.granted_modules here, a column dropped in d8e9f0a1b2c4; assigning it
    # on an unmapped attribute is a silent no-op, so that line had been doing
    # nothing while looking load-bearing.
    org.pinned_plan = plans.PLAN_PREMIUM

    # And the member is a MANAGER, whose modules track the whole plan — so the
    # intersection is the plan itself and the request under test is the only
    # thing that can fail. This used to narrow a tier row to the plan's module
    # list by hand; a manager IS that, by definition.
    db.flush()

    async with client_for(tenant) as c:
        resp = await c.get("/api/files/signed-url?path=chat/x.pdf")

    assert resp.status_code != 404, (
        "a premium subscriber lost chat file upload and citation source links — "
        "/api/files is gated by a module the premium plan does not include"
    )

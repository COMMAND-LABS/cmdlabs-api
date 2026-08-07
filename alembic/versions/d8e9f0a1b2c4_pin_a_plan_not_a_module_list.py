"""a ceiling is a plan, not a stored module list

Revision ID: d8e9f0a1b2c4
Revises: c7d8e9f0a1b3

THE LAST STORED COPY OF A DERIVABLE VALUE.

`granted_modules` held the modules an org may open, and `ceiling_managed_by`
said whether billing was allowed to rewrite it. Deriving the billing case
(d2e3f4a5b6c8) removed one copy and left the other: an org marked 'grant' kept
a frozen LIST, and a frozen list is a snapshot.

Every module added to a plan after an org was comped therefore never reached
it. All three comped orgs on this platform silently ended up without `courses`
and `spaces` — nobody did anything wrong, and it surfaced as a missing menu
item rather than as a stale cache. It is the same bug that hit the ceiling
three times before, in the one place that had been left able to have it.

So the pin becomes a PLAN. `pinned_plan` is NULL for an org that follows its
owner's subscription, and 'free' | 'premium' for one staff has given a plan
to. What that opens is read from config/plans_registry.PLAN_MODULES on every
request, so a plan that grows reaches pinned orgs too and there is nothing left
to backfill.

WHAT THE THREE PINNED ORGS GET. All of them are mapped to 'premium', and it is
a strict widening for every one — the premium plan is a superset of what each
currently holds:

    CMD LABS  17 modules  -> premium (gains courses, spaces)
    Bolay      6 modules  -> premium (gains the CRM, courses, spaces, ...)
    C2 P2      6 modules  -> premium

`membership` and `organization` are in no plan and so appear to be lost here.
They are not: neither has any route_prefixes, so nothing on the API consults
them, and the UI reaches both by other means — membership via
roles.ts ALWAYS_VISIBLE (you must always be able to pay), organization via
OWNER_ONLY (it is gated on ownership, not on a plan).

MAPPING BY CONTENT RATHER THAN BY NAME. An org is pinned to premium if what it
holds today is a subset of premium, and to free otherwise — computed per row
rather than assumed, so an org holding something premium does not sell would
be caught here rather than quietly narrowed. On this database every row maps to
premium; the branch exists so that stays a fact rather than a hope.

Create Date: 2026-08-07
"""
import json

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = 'd8e9f0a1b2c4'
down_revision = 'c7d8e9f0a1b3'
branch_labels = None
depends_on = None

# Read from the registry at migration time ON PURPOSE. This is a one-shot
# mapping of existing rows, and it must reflect what the plans contained when
# it ran — not follow them afterwards, which is what the rest of the platform
# now does and what makes a later re-run meaningless.
_PREMIUM = {
    "home", "agents", "agent_chat", "contacts", "contact_lists", "companies",
    "deals", "prompts", "knowledge_bases", "access", "credentials",
    "email_templates", "email_campaigns", "courses", "spaces", "analytics",
    "settings",
}
# Not sold by any plan, and deliberately so — see the header. Ignored when
# deciding which plan a row's contents fit inside.
_UNSOLD = {"membership", "organization"}


def upgrade():
    conn = op.get_bind()

    op.add_column('organizations',
                  sa.Column('pinned_plan', sa.String(20), nullable=True))

    rows = conn.execute(sa.text("""
        SELECT id, name, granted_modules
          FROM organizations
         WHERE ceiling_managed_by <> 'subscription'
    """)).fetchall()

    for org_id, name, granted in rows:
        held = set(granted or []) - _UNSOLD
        plan = 'premium' if held <= _PREMIUM else 'free'
        lost = sorted(held - _PREMIUM) if plan == 'premium' else []
        conn.execute(
            sa.text("UPDATE organizations SET pinned_plan = :plan "
                    "WHERE id = :id"),
            {"plan": plan, "id": org_id},
        )
        print(f"[pin] org {org_id} ({name}): {len(held)} modules -> {plan}"
              + (f"  LOST {lost}" if lost else ""))

    op.drop_constraint('ck_org_ceiling_managed_by', 'organizations',
                       type_='check')
    op.create_check_constraint(
        'ck_org_pinned_plan', 'organizations',
        "pinned_plan IN ('free','premium')")

    op.drop_column('organizations', 'ceiling_managed_by')
    op.drop_column('organizations', 'granted_modules')


def downgrade():
    """Restores the shape and a plausible list, never the original one.

    The stored module lists are gone, and the whole point of this migration is
    that they were the wrong thing to store. What comes back is each pinned
    org's plan expanded to modules — which is what the list SHOULD have said.
    """
    op.add_column('organizations',
                  sa.Column('granted_modules', postgresql.JSONB(),
                            nullable=False, server_default='[]'))
    op.add_column('organizations',
                  sa.Column('ceiling_managed_by', sa.String(20),
                            nullable=False, server_default='subscription'))
    op.drop_constraint('ck_org_pinned_plan', 'organizations', type_='check')
    op.create_check_constraint(
        'ck_org_ceiling_managed_by', 'organizations',
        "ceiling_managed_by IN ('subscription','grant')")

    conn = op.get_bind()
    for plan, modules in (
        ('premium', sorted(_PREMIUM)),
        ('free', ["home", "courses", "spaces", "prompts", "settings"]),
    ):
        conn.execute(sa.text("""
            UPDATE organizations
               SET ceiling_managed_by = 'grant',
                   granted_modules = CAST(:modules AS jsonb)
             WHERE pinned_plan = :plan
        """), {"modules": json.dumps(modules), "plan": plan})

    op.drop_column('organizations', 'pinned_plan')

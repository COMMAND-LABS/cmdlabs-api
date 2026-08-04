"""Give every signup its own org; root becomes the platform org

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
Create Date: 2026-08-04 16:00:00.000000

WHY
---
Until now every account lived in the root org, and `data_scope='personal'` was
the flag that stopped 274 strangers seeing each other's contacts. That flag
existed for exactly one org — the one org that was not really an org. It also
forced root to be two things at once: the platform's own content org AND the
public lobby, which is why assert_publishable needed a second condition to tell
a staff-authored lesson from a stranger's private agent.

After this migration:

    root        the PLATFORM org. Staff only. Owns published content.
    personal    one per signup. Exactly one member, who owns it.
    team        a real shared org (see PROMOTED_GROUPS below).

Every org then means the same thing, `org_id == ctx.org_id` is the whole
tenancy rule, and the next migration drops data_scope along with the branch it
guarded — the most security-sensitive expression in the codebase gets simpler
rather than more clever.

THE ONE THING THAT COULD HAVE GONE WRONG
----------------------------------------
Splitting accounts apart severs any grant that crossed between them. Production
had exactly one live cluster: access group "Bolay" (4 members) holding grants on
agent 48 and vector store 1, both owned by staff and living in root. Left alone,
those four people would have lost access with no error and no log line.

They are re-homed here through the CATALOG rather than by moving the resources.
Direction is what makes that safe: the agent stays in the platform org and is
PUBLISHED outward, so one master copy can be granted to the next cohort too.
Moving it into the client's org would have made a second cohort need a copy.

Note those four accounts are role='free', so under module enforcement their
effective modules are [home, membership, settings] — they could not open the
Agents screen at all, and the grants were already unreachable. The team ceiling
below is what actually makes the training work.

REVERSIBLE. downgrade() puts every row back in root and restores data_scope
semantics, because root's id is known and nothing is destroyed on the way out.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e3f4a5b6c7d8'
down_revision: Union[str, None] = 'd2e3f4a5b6c7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


PLATFORM_SLUG = 'root'

# Access groups promoted to real organizations, confirmed against production
# before writing this. A group with live grants MUST appear here or its members
# lose access when the orgs split — the postflight below fails the migration
# rather than letting that happen quietly.
PROMOTED_GROUPS = {
    2: ('bolay', 'Bolay'),
    1: ('c2-p2', 'C2 P2'),
}

# Ceilings. For a PERSONAL org the ceiling is the whole entitlement: its member
# is its owner, and an owner bypasses the tier layer. So the ceiling is what
# billing raises and lowers, and organization_tiers only starts mattering when
# an org has more than one member.
FREE_CEILING = ['home', 'membership', 'settings']
PREMIUM_CEILING = ['agents', 'agent_chat', 'credentials', 'membership', 'settings']

# What a training org needs to open a shared lesson: the agent, the chat that
# runs it, and the knowledge base behind it.
TEAM_CEILING = ['home', 'agents', 'agent_chat', 'knowledge_bases',
                'membership', 'settings']

# Tables scoped by whoever created the row.
OWNED_TABLES = {
    'contacts': 'account_id',
    'companies': 'account_id',
    'contact_lists': 'account_id',
    'deals': 'account_id',
    'agents': 'account_id',
    'vector_stores': 'owner_account_id',
}

# Child tables. Their org comes from the PARENT, never from their own
# account_id: the parent is what decides the tenant, and deriving a child's org
# independently is how a child ends up in a different org from its parent.
CHILD_TABLES = {
    'contact_events': ('contacts', 'contact_id'),
    'career_timeline': ('contacts', 'contact_id'),
    'company_contacts': ('companies', 'company_id'),
    'contact_list_members': ('contact_lists', 'contact_list_id'),
}


def _json(values) -> str:
    inner = ','.join('"%s"' % v for v in values)
    return '[%s]' % inner


def upgrade() -> None:
    conn = op.get_bind()

    # ---------------------------------------------------------------- schema
    # A personal workspace has no public page, so it needs no slug. NULL is
    # "not publicly addressable" — and because Postgres treats NULLs as
    # distinct, the UNIQUE constraint keeps working untouched. This also keeps
    # the slug genuinely immutable: nobody is stuck with an auto-generated
    # `user-273` they can never trade for `/@acme`.
    op.alter_column('organizations', 'slug',
                    existing_type=sa.String(64), nullable=True)

    # Who owns the ceiling column. 'subscription' means the Stripe webhook
    # writes it; 'grant' means a human did and billing must never undo it.
    # Same asymmetry as organization_members.granted_by, one level up — which
    # is where it now has to live, because for a personal org the ceiling IS
    # the entitlement.
    op.add_column('organizations', sa.Column(
        'ceiling_managed_by', sa.String(20), nullable=False,
        server_default='subscription'))
    op.create_check_constraint(
        'ck_org_ceiling_managed_by', 'organizations',
        "ceiling_managed_by IN ('subscription','grant')")

    root_id = conn.execute(sa.text(
        "SELECT id FROM organizations WHERE slug = :s"), {'s': PLATFORM_SLUG}
    ).scalar()
    if root_id is None:
        # A fresh database that has never held accounts. Nothing to move.
        return

    # Root's own ceiling is staff-set, not billed.
    conn.execute(sa.text(
        "UPDATE organizations SET ceiling_managed_by = 'grant' WHERE id = :id"),
        {'id': root_id})

    # ------------------------------------------------------------ team orgs
    # Promoted groups first, so that when personal orgs are created below every
    # account already knows which team (if any) it belongs to.
    for group_id, (slug, name) in PROMOTED_GROUPS.items():
        exists = conn.execute(sa.text(
            "SELECT 1 FROM access_groups WHERE id = :g"), {'g': group_id}).scalar()
        if not exists:
            continue

        org_id = conn.execute(sa.text("""
            INSERT INTO organizations
                (slug, name, owner_account_id, data_scope, granted_modules,
                 status, ceiling_managed_by)
            SELECT :slug, :name,
                   (SELECT owner_account_id FROM access_groups WHERE id = :g),
                   'shared', CAST(:ceiling AS jsonb), 'active', 'grant'
             WHERE NOT EXISTS (SELECT 1 FROM organizations WHERE slug = :slug)
            RETURNING id
        """), {'slug': slug, 'name': name, 'g': group_id,
               'ceiling': _json(TEAM_CEILING)}).scalar()
        if org_id is None:
            org_id = conn.execute(sa.text(
                "SELECT id FROM organizations WHERE slug = :s"), {'s': slug}).scalar()

        for tier_key, label, modules in (('owner', 'Owner', TEAM_CEILING),
                                         ('member', 'Member', TEAM_CEILING)):
            conn.execute(sa.text("""
                INSERT INTO organization_tiers (org_id, tier_key, label, modules)
                SELECT :o, :k, :l, CAST(:m AS jsonb)
                 WHERE NOT EXISTS (SELECT 1 FROM organization_tiers
                                    WHERE org_id = :o AND tier_key = :k)
            """), {'o': org_id, 'k': tier_key, 'l': label, 'm': _json(modules)})

        # The group's members become the org's members.
        #
        # NOBODY is made is_owner. Ownership of a customer's org is theirs to
        # hold, and guessing which of them should control tiers and billing
        # from a group role would be inventing authority. Staff administer it
        # from the platform admin surface until it is handed over deliberately.
        conn.execute(sa.text("""
            INSERT INTO organization_members
                (org_id, account_id, tier_key, granted_by, is_owner)
            SELECT :o, m.account_id, 'member', 'grant', false
              FROM access_group_members m
             WHERE m.access_group_id = :g
               AND NOT EXISTS (SELECT 1 FROM organization_members om
                                WHERE om.org_id = :o AND om.account_id = m.account_id)
        """), {'o': org_id, 'g': group_id})

        # The group itself moves with its members.
        conn.execute(sa.text(
            "UPDATE access_groups SET org_id = :o WHERE id = :g"),
            {'o': org_id, 'g': group_id})

        # --- grants that would have been severed -> catalog publications ---
        # The resource stays in the platform org and is published outward. That
        # is the one direction that does not puncture tenancy, and it keeps a
        # single master copy that the next cohort can be granted as well.
        severed = conn.execute(sa.text("""
            SELECT id, resource_type, resource_id, role
              FROM access_grants
             WHERE principal_type = 'group' AND principal_id = :g
        """), {'g': group_id}).fetchall()

        for grant_id, resource_type, resource_id, _role in severed:
            if resource_type not in ('agent', 'vector_store'):
                continue

            title = conn.execute(sa.text(
                "SELECT name FROM agents WHERE id = :r" if resource_type == 'agent'
                else "SELECT index_name FROM vector_stores WHERE id = :r"),
                {'r': resource_id}).scalar()
            if title is None:
                continue

            item_id = conn.execute(sa.text("""
                INSERT INTO catalog_items
                    (resource_type, resource_id, title, description,
                     published_by_account_id)
                SELECT :t, :r, :title,
                       'Published automatically when organizations were split.',
                       (SELECT owner_account_id FROM organizations WHERE id = :root)
                 WHERE NOT EXISTS (SELECT 1 FROM catalog_items
                                    WHERE resource_type = :t AND resource_id = :r)
                RETURNING id
            """), {'t': resource_type, 'r': resource_id, 'title': title,
                   'root': root_id}).scalar()
            if item_id is None:
                item_id = conn.execute(sa.text(
                    "SELECT id FROM catalog_items "
                    "WHERE resource_type = :t AND resource_id = :r"),
                    {'t': resource_type, 'r': resource_id}).scalar()

            # Granted to the GROUP rather than the whole org: access is defined
            # by group membership today, so that is the faithful translation,
            # and it keeps working if the org later holds people who should not
            # get this particular lesson.
            conn.execute(sa.text("""
                INSERT INTO catalog_grants
                    (catalog_item_id, org_id, group_id, granted_by_account_id)
                SELECT :i, :o, :g,
                       (SELECT owner_account_id FROM organizations WHERE id = :root)
                 WHERE NOT EXISTS (SELECT 1 FROM catalog_grants
                                    WHERE catalog_item_id = :i AND org_id = :o
                                      AND group_id = :g)
            """), {'i': item_id, 'o': org_id, 'g': group_id, 'root': root_id})

            # The original grant now names a resource in another org, so it can
            # never resolve again. Removing it stops it lingering as a row that
            # looks like access and is not.
            conn.execute(sa.text("DELETE FROM access_grants WHERE id = :i"),
                         {'i': grant_id})

    # -------------------------------------------------------- personal orgs
    # One per non-staff account, named after the local part of their email so
    # the switcher shows something they recognize. No slug: a personal
    # workspace has no public page until its owner chooses to create one.
    conn.execute(sa.text("""
        INSERT INTO organizations
            (slug, name, owner_account_id, data_scope, granted_modules, status,
             ceiling_managed_by)
        SELECT NULL,
               COALESCE(NULLIF(split_part(a.email, '@', 1), ''), 'Workspace'),
               a.id,
               'shared',
               CASE WHEN a.subscription_status IN ('active','trialing')
                    THEN CAST(:premium AS jsonb) ELSE CAST(:free AS jsonb) END,
               'active',
               'subscription'
          FROM accounts a
         WHERE a.role <> 'admin'
           AND NOT EXISTS (SELECT 1 FROM organizations o
                            WHERE o.owner_account_id = a.id AND o.slug IS NULL)
    """), {'premium': _json(PREMIUM_CEILING), 'free': _json(FREE_CEILING)})

    # Its owner is its only member. is_owner is what makes the ceiling the
    # entitlement — an owner bypasses the tier, so a personal org needs no
    # tier maintenance at all.
    conn.execute(sa.text("""
        INSERT INTO organization_members
            (org_id, account_id, tier_key, granted_by, is_owner)
        SELECT o.id, o.owner_account_id, 'owner', 'grant', true
          FROM organizations o
         WHERE o.slug IS NULL
           AND NOT EXISTS (SELECT 1 FROM organization_members m
                            WHERE m.org_id = o.id AND m.account_id = o.owner_account_id)
    """))

    # Seeded so converting a personal workspace into a team is picking a slug
    # and inviting someone, not first discovering the tiers page is empty.
    for tier_key, label in (('owner', 'Owner'), ('member', 'Member')):
        conn.execute(sa.text("""
            INSERT INTO organization_tiers (org_id, tier_key, label, modules)
            SELECT o.id, :k, :l,
                   CASE WHEN :k = 'owner' THEN o.granted_modules ELSE '[]'::jsonb END
              FROM organizations o
             WHERE o.slug IS NULL
               AND NOT EXISTS (SELECT 1 FROM organization_tiers t
                                WHERE t.org_id = o.id AND t.tier_key = :k)
        """), {'k': tier_key, 'l': label})

    # ------------------------------------------------------------ move rows
    for table, owner_col in OWNED_TABLES.items():
        conn.execute(sa.text(f"""
            UPDATE {table} t
               SET org_id = o.id
              FROM organizations o
             WHERE o.slug IS NULL
               AND o.owner_account_id = t.{owner_col}
               AND t.org_id = :root
        """), {'root': root_id})

    for table, (parent_table, fk) in CHILD_TABLES.items():
        conn.execute(sa.text(f"""
            UPDATE {table} c
               SET org_id = p.org_id
              FROM {parent_table} p
             WHERE p.id = c.{fk}
               AND c.org_id IS DISTINCT FROM p.org_id
        """))

    # Access groups that were not promoted follow their owner.
    conn.execute(sa.text("""
        UPDATE access_groups g
           SET org_id = o.id
          FROM organizations o
         WHERE o.slug IS NULL
           AND o.owner_account_id = g.owner_account_id
           AND g.org_id = :root
           AND g.id <> ALL(:promoted)
    """), {'root': root_id, 'promoted': list(PROMOTED_GROUPS) or [0]})

    # Surviving grants follow their resource, which is what decides their org.
    conn.execute(sa.text("""
        UPDATE access_grants gr SET org_id = a.org_id
          FROM agents a WHERE a.id = gr.resource_id AND gr.resource_type = 'agent'
    """))
    conn.execute(sa.text("""
        UPDATE access_grants gr SET org_id = v.org_id
          FROM vector_stores v
         WHERE v.id = gr.resource_id AND gr.resource_type = 'vector_store'
    """))

    # --------------------------------------------------------- memberships
    # Non-staff leave root. This is the step that makes root safe to treat as
    # an ordinary org once data_scope is gone — root still holds the platform's
    # own contacts, and anyone left behind would see all of them.
    conn.execute(sa.text("""
        DELETE FROM organization_members m
         USING accounts a
         WHERE a.id = m.account_id
           AND m.org_id = :root
           AND a.role <> 'admin'
    """), {'root': root_id})

    # Default org: the team they were invited into if there is one, else their
    # own workspace. Someone promoted into a team org came for the team.
    conn.execute(sa.text("""
        UPDATE accounts a SET default_org_id = COALESCE(
            (SELECT m.org_id FROM organization_members m
               JOIN organizations o ON o.id = m.org_id
              WHERE m.account_id = a.id AND o.slug IS NOT NULL
                AND o.slug <> :root_slug
              ORDER BY m.org_id LIMIT 1),
            (SELECT o.id FROM organizations o
              WHERE o.owner_account_id = a.id AND o.slug IS NULL LIMIT 1),
            a.default_org_id)
         WHERE a.role <> 'admin'
    """), {'root_slug': PLATFORM_SLUG})

    # Every org now means the same thing.
    conn.execute(sa.text("UPDATE organizations SET data_scope = 'shared'"))

    _postflight(conn, root_id)


def _postflight(conn, root_id: int) -> None:
    """Refuse to finish on any of the outcomes this migration exists to avoid.

    A data migration that half-worked is worse than one that failed: the first
    looks finished. Each check below names the rows it found, because "it
    failed" without the offending ids is a second investigation.
    """
    stray = conn.execute(sa.text("""
        SELECT a.id, a.email FROM organization_members m
          JOIN accounts a ON a.id = m.account_id
         WHERE m.org_id = :root AND a.role <> 'admin'
    """), {'root': root_id}).fetchall()
    if stray:
        raise RuntimeError(
            f"Non-staff accounts left in the platform org: {stray}. They would "
            f"see the platform's own CRM once data_scope is dropped.")

    orphan = conn.execute(sa.text("""
        SELECT a.id, a.email FROM accounts a
         WHERE NOT EXISTS (SELECT 1 FROM organization_members m
                            WHERE m.account_id = a.id)
    """)).fetchall()
    if orphan:
        raise RuntimeError(f"Accounts left with no organization at all: {orphan}")

    for table, (parent_table, fk) in CHILD_TABLES.items():
        mismatched = conn.execute(sa.text(f"""
            SELECT c.id FROM {table} c JOIN {parent_table} p ON p.id = c.{fk}
             WHERE c.org_id IS DISTINCT FROM p.org_id LIMIT 5
        """)).fetchall()
        if mismatched:
            raise RuntimeError(
                f"{table} rows in a different org from their parent: {mismatched}")

    # A grant whose principal cannot reach its resource is dead weight that
    # still reads as access on the sharing screen.
    dead = conn.execute(sa.text("""
        SELECT g.id, g.principal_type, g.principal_id, g.resource_type, g.resource_id
          FROM access_grants g
         WHERE (g.principal_type = 'group'
                AND NOT EXISTS (SELECT 1 FROM access_groups ag
                                 WHERE ag.id = g.principal_id AND ag.org_id = g.org_id))
            OR (g.principal_type = 'account'
                AND NOT EXISTS (SELECT 1 FROM organization_members m
                                 WHERE m.account_id = g.principal_id
                                   AND m.org_id = g.org_id))
    """)).fetchall()
    if dead:
        raise RuntimeError(
            f"These grants were severed by the split and were not re-homed: "
            f"{dead}. Add the owning group to PROMOTED_GROUPS, or convert the "
            f"grant to a catalog publication.")


def downgrade() -> None:
    conn = op.get_bind()
    root_id = conn.execute(sa.text(
        "SELECT id FROM organizations WHERE slug = :s"), {'s': PLATFORM_SLUG}
    ).scalar()

    if root_id is not None:
        for table in list(OWNED_TABLES) + list(CHILD_TABLES):
            conn.execute(sa.text(f"UPDATE {table} SET org_id = :root"),
                         {'root': root_id})
        for table in ('access_groups', 'access_grants', 'access_grant_events'):
            conn.execute(sa.text(f"UPDATE {table} SET org_id = :root"),
                         {'root': root_id})

        conn.execute(sa.text("""
            INSERT INTO organization_members
                (org_id, account_id, tier_key, granted_by, is_owner)
            SELECT :root, a.id,
                   CASE WHEN a.role = 'admin' THEN 'org_owner'
                        WHEN a.subscription_status IN ('active','trialing')
                        THEN 'premium' ELSE 'free' END,
                   CASE WHEN a.role <> 'admin'
                         AND a.subscription_status IN ('active','trialing')
                        THEN 'subscription' ELSE 'grant' END,
                   (a.role = 'admin')
              FROM accounts a
             WHERE NOT EXISTS (SELECT 1 FROM organization_members m
                                WHERE m.org_id = :root AND m.account_id = a.id)
        """), {'root': root_id})

        conn.execute(sa.text("UPDATE accounts SET default_org_id = :root"),
                     {'root': root_id})
        conn.execute(sa.text(
            "UPDATE organizations SET data_scope = 'personal' WHERE id = :root"),
            {'root': root_id})

        # Catalog publications created by the upgrade are removed with the orgs
        # they were granted to (catalog_grants cascades on org_id).
        conn.execute(sa.text("""
            DELETE FROM organization_tiers WHERE org_id IN
                (SELECT id FROM organizations WHERE id <> :root)
        """), {'root': root_id})
        conn.execute(sa.text("""
            DELETE FROM organization_members WHERE org_id <> :root
        """), {'root': root_id})
        conn.execute(sa.text("DELETE FROM organizations WHERE id <> :root"),
                     {'root': root_id})

    op.drop_constraint('ck_org_ceiling_managed_by', 'organizations', type_='check')
    op.drop_column('organizations', 'ceiling_managed_by')
    op.alter_column('organizations', 'slug',
                    existing_type=sa.String(64), nullable=False)

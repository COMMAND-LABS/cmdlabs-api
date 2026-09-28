"""backfill knowledge-base read grants for existing agent shares

Revision ID: d948d7bf156f
Revises: b49167452008
Create Date: 2026-09-27

Searching an agent's knowledge base now takes a read grant on it, as reading
the knowledge base directly does (services/agent_knowledge_bases.py). Until
now sharing the agent was enough, so everyone an agent is shared with today
would silently lose its search. This gives each of them a read grant on every
knowledge base the agent searches, keeping what they can do exactly as it was.

Data only; the same rules the share endpoint applies:
  - a person who already holds a grant on the knowledge base keeps it as it is
    (ON CONFLICT DO NOTHING — a write grant is never downgraded);
  - the agent's org, the knowledge base's org and the grantee's membership must
    agree (access.assert_same_org);
  - a knowledge base that predates the vector_stores table gets its row first
    (get_or_create_vector_store).
Each new grant is logged in access_grant_events like any other grant.

Downgrade is a no-op: a backfilled grant is indistinguishable from one an owner
made since, and revoking the latter would take access nobody meant to remove.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'd948d7bf156f'
down_revision: Union[str, None] = 'b49167452008'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (agent grant, vector-search tool) pairs: every person an agent is shared
# with, against every index that agent's vector-search tools name.
_FROM_SHARED_AGENT_TOOLS = """
    FROM access_grants g
    JOIN agents a ON g.resource_type = 'agent' AND g.resource_id = a.id
    CROSS JOIN LATERAL jsonb_array_elements(
        CASE WHEN jsonb_typeof(a.config::jsonb -> 'data' -> 'tools') = 'array'
             THEN a.config::jsonb -> 'data' -> 'tools' ELSE '[]'::jsonb END
    ) AS t(tool)
"""
_WHERE_SHARED_AGENT_TOOLS = """
    WHERE g.principal_type = 'account'
      AND g.principal_id <> a.account_id
      AND t.tool ->> 'type' IN ('vectorSearch', 'vectorSearchWithReranking')
      AND coalesce(t.tool ->> 'index', '') <> ''
"""

CREATE_MISSING_STORES = f"""
INSERT INTO vector_stores (org_id, owner_account_id, index_name, visibility, created_at, updated_at)
SELECT DISTINCT a.org_id, a.account_id, t.tool ->> 'index', 'private', now(), now()
{_FROM_SHARED_AGENT_TOOLS}
{_WHERE_SHARED_AGENT_TOOLS}
ON CONFLICT (owner_account_id, index_name) DO NOTHING
"""

GRANT_READ = f"""
WITH wanted AS (
    SELECT DISTINCT a.org_id, g.principal_id, vs.id AS store_id
    {_FROM_SHARED_AGENT_TOOLS}
    JOIN vector_stores vs
      ON vs.owner_account_id = a.account_id
     AND vs.index_name = t.tool ->> 'index'
     AND vs.org_id = a.org_id
    JOIN organization_members m
      ON m.org_id = a.org_id AND m.account_id = g.principal_id
    {_WHERE_SHARED_AGENT_TOOLS}
      AND (g.org_id IS NULL OR g.org_id = a.org_id)
), inserted AS (
    INSERT INTO access_grants
        (org_id, principal_type, principal_id, resource_type, resource_id, role, created_at, updated_at)
    SELECT org_id, 'account', principal_id, 'vector_store', store_id, 'read', now(), now()
    FROM wanted
    ON CONFLICT (principal_type, principal_id, resource_type, resource_id) DO NOTHING
    RETURNING org_id, principal_id, resource_id
)
INSERT INTO access_grant_events
    (org_id, event_type, resource_type, resource_id, resource_label,
     principal_type, principal_id, principal_label, role, detail, created_at)
SELECT i.org_id, 'create', 'vector_store', i.resource_id, vs.index_name,
       'account', i.principal_id, coalesce(acc.email, 'account #' || i.principal_id), 'read',
       'Backfilled: the agent was already shared with them, which used to include its knowledge bases.',
       now()
FROM inserted i
JOIN vector_stores vs ON vs.id = i.resource_id
LEFT JOIN accounts acc ON acc.id = i.principal_id
"""


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text(CREATE_MISSING_STORES))
    bind.execute(sa.text(GRANT_READ))


def downgrade() -> None:
    pass

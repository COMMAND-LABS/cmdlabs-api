"""add_skills_table

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-08-15

Adds skills — org-owned Agent Skills (the SKILL.md pattern): a named markdown
instruction package agents reference by id in their config and load on demand
at runtime through the load_skill tool.

Tenancy shape matches agents/vector_stores: org_id NOT NULL (the tenant key),
visibility 'private'|'org', account_id as created_by attribution. (org_id, name)
is unique because name is the handle the model passes to load_skill.
"""
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = 'd0e1f2a3b4c5'
down_revision: Union[str, None] = 'c9d0e1f2a3b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'skills',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('org_id', sa.Integer(),
                  sa.ForeignKey('organizations.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('visibility', sa.String(20), nullable=False,
                  server_default='private'),
        sa.Column('account_id', sa.Integer(),
                  sa.ForeignKey('accounts.id'),
                  nullable=False),
        # Kebab-case handle, ≤64 chars (enforced at the routes).
        sa.Column('name', sa.String(64), nullable=False),
        # What the model reads in the system-prompt index when deciding
        # whether to load this skill.
        sa.Column('description', sa.String(1024), nullable=False),
        # Markdown body, front matter stripped at write time. ≤64 KB
        # (enforced at the routes).
        sa.Column('content', sa.Text(), nullable=False),
        # Parsed YAML front matter, kept for SKILL.md round-tripping.
        sa.Column('frontmatter', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.UniqueConstraint('org_id', 'name', name='uq_skill_org_name'),
    )
    op.create_index('ix_skills_id', 'skills', ['id'])
    op.create_index('ix_skills_org_id', 'skills', ['org_id'])
    op.create_index('ix_skills_account_id', 'skills', ['account_id'])


def downgrade() -> None:
    op.drop_index('ix_skills_account_id', 'skills')
    op.drop_index('ix_skills_org_id', 'skills')
    op.drop_index('ix_skills_id', 'skills')
    op.drop_table('skills')

"""rename email_address to primary_recipient and make nullable

Revision ID: u1v2w3x4y5z6
Revises: t0u1v2w3x4y5
Create Date: 2026-04-02
"""
from alembic import op
import sqlalchemy as sa

revision = 'u1v2w3x4y5z6'
down_revision = 't0u1v2w3x4y5'
branch_labels = None
depends_on = None


def _has_column(name: str) -> bool:
    return bool(op.get_bind().execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'email_events' AND column_name = :c"
        ),
        {"c": name},
    ).scalar())


def upgrade() -> None:
    # Guarded because the chain is not replayable from empty otherwise: the
    # preceding migration (t0u1v2w3x4y5, "add email_events table") was edited
    # after the fact to create this column already named `primary_recipient`.
    # Databases built incrementally still have `email_address` here and need the
    # rename; a database built from scratch never had it and must skip.
    #
    # Both paths converge on the same schema, which is the only thing that
    # matters. Do NOT "simplify" this back to an unconditional alter_column —
    # that is what broke `alembic upgrade head` on a fresh database.
    if _has_column('email_address'):
        op.alter_column(
            'email_events',
            'email_address',
            new_column_name='primary_recipient',
            existing_type=sa.String(320),
            existing_nullable=False,
            nullable=True,
        )


def downgrade() -> None:
    if _has_column('primary_recipient'):
        op.alter_column(
            'email_events',
            'primary_recipient',
            new_column_name='email_address',
            existing_type=sa.String(320),
            existing_nullable=True,
            nullable=False,
        )

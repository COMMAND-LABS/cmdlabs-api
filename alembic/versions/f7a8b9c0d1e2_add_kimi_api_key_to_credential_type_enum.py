"""add_kimi_api_key_to_credential_type_enum

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
Create Date: 2026-08-08

Adds KIMI_API_KEY to the credential_type_enum type for storing Moonshot /
Kimi API keys, which fund turn completions for agents on the `kimi` provider
(e.g. the kimi-k2.6 model).

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f7a8b9c0d1e2'
down_revision: Union[str, None] = 'e6f7a8b9c0d1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_enum
                WHERE enumlabel = 'KIMI_API_KEY'
                AND enumtypid = (SELECT oid FROM pg_type WHERE typname = 'credential_type_enum')
            ) THEN
                ALTER TYPE credential_type_enum ADD VALUE 'KIMI_API_KEY';
            END IF;
        END $$;
    """)


def downgrade() -> None:
    # Postgres does not support removing enum values directly.
    # Manual intervention required if a downgrade is needed.
    pass

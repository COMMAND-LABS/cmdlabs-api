"""drop tariff-and-duty (tnd_*) tables

Revision ID: e5b7c9d1a2f3
Revises: c7a1d2e3f4b5
Create Date: 2026-10-06

The Tariffs feature was removed from the product: the review page and menu
entry in cmdlabs-ui, and routers/tariffs, db/tnd_models.py, services/tnd_*,
the tariffMeasureSearch/tariffMeasureUpdate agent tools and the `tariffs`
module key here. Nothing in this codebase reads or writes these tables any
more, so they go.

ONE WRITER LIVES OUTSIDE THIS REPO. The scheduled research job in
tariff-n-duty-scraper (`tnd_agent`) inserts into tnd_research_runs,
tnd_source_documents and tnd_measures. Decommission that job before this
migration runs in an environment where it is scheduled, or its next run fails
on a missing table.

THIS DROPS DATA. Every proposed, approved and rejected rate, and the record of
which official documents were examined, goes with the tables. Take a backup
first if any org's review history matters. The downgrade recreates the three
tables exactly as c7a1d2e3f4b5 did, but empty.

Dropped in dependency order: tnd_measures references both other tables, and
tnd_source_documents references tnd_research_runs.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = 'e5b7c9d1a2f3'
down_revision: Union[str, None] = 'c7a1d2e3f4b5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index('ix_tnd_measures_lookup', table_name='tnd_measures')
    op.drop_table('tnd_measures')
    op.drop_table('tnd_source_documents')
    op.drop_table('tnd_research_runs')


# ── Downgrade: the tables as c7a1d2e3f4b5 created them ───────────────────────

def _org_id():
    return sa.Column('org_id', sa.Integer(),
                     sa.ForeignKey('organizations.id', ondelete='CASCADE'),
                     nullable=False, index=True)


def downgrade() -> None:
    op.create_table(
        'tnd_research_runs',
        sa.Column('id', sa.Integer(), primary_key=True),
        _org_id(),
        sa.Column('started_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('window_start', sa.Date(), nullable=False),
        sa.Column('window_end', sa.Date(), nullable=False),
        sa.Column('docs_fetched', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('docs_relevant', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('measures_proposed', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('status', sa.String(20), nullable=False, server_default='running'),
        sa.Column('error', sa.Text(), nullable=True),
        sa.CheckConstraint("status IN ('running','succeeded','failed')",
                           name='ck_tnd_run_status'),
    )

    op.create_table(
        'tnd_source_documents',
        sa.Column('id', sa.Integer(), primary_key=True),
        _org_id(),
        sa.Column('run_id', sa.Integer(),
                  sa.ForeignKey('tnd_research_runs.id', ondelete='SET NULL'),
                  nullable=True, index=True),
        sa.Column('source', sa.String(40), nullable=False),
        sa.Column('external_id', sa.String(100), nullable=False),
        sa.Column('title', sa.Text(), nullable=False),
        sa.Column('doc_type', sa.String(40), nullable=True),
        sa.Column('agencies', JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column('published_on', sa.Date(), nullable=True),
        sa.Column('effective_on', sa.Date(), nullable=True),
        sa.Column('url', sa.Text(), nullable=False),
        sa.Column('raw_text_url', sa.Text(), nullable=True),
        sa.Column('content_hash', sa.String(64), nullable=True),
        sa.Column('status', sa.String(20), nullable=False, server_default='new'),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('fetched_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.UniqueConstraint('org_id', 'source', 'external_id', name='uq_tnd_doc_source_id'),
        sa.CheckConstraint("status IN ('new','irrelevant','extracted','failed')",
                           name='ck_tnd_doc_status'),
    )

    op.create_table(
        'tnd_measures',
        sa.Column('id', sa.Integer(), primary_key=True),
        _org_id(),
        sa.Column('run_id', sa.Integer(),
                  sa.ForeignKey('tnd_research_runs.id', ondelete='SET NULL'),
                  nullable=True),
        sa.Column('source_document_id', sa.Integer(),
                  sa.ForeignKey('tnd_source_documents.id', ondelete='SET NULL'),
                  nullable=True, index=True),
        sa.Column('jurisdiction', sa.String(2), nullable=False, server_default='US'),
        sa.Column('hts_code', sa.String(10), nullable=True),
        sa.Column('origin_country', sa.String(2), nullable=True),
        sa.Column('program', sa.String(40), nullable=False),
        sa.Column('rate_type', sa.String(20), nullable=False),
        sa.Column('ad_valorem_rate', sa.Numeric(7, 4), nullable=True),
        sa.Column('specific_rate', sa.Numeric(14, 4), nullable=True),
        sa.Column('specific_unit', sa.String(20), nullable=True),
        sa.Column('effective_from', sa.Date(), nullable=False),
        sa.Column('effective_to', sa.Date(), nullable=True),
        sa.Column('excluded_hts', JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('source_quote', sa.Text(), nullable=True),
        sa.Column('quote_verified', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('confidence', sa.Numeric(3, 2), nullable=True),
        sa.Column('extracted_by', sa.String(80), nullable=True),
        sa.Column('status', sa.String(20), nullable=False, server_default='proposed'),
        sa.Column('reviewed_by_account_id', sa.Integer(),
                  sa.ForeignKey('accounts.id', ondelete='SET NULL'), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('review_note', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("status IN ('proposed','approved','rejected')",
                           name='ck_tnd_measure_status'),
        sa.CheckConstraint("rate_type IN ('ad_valorem','specific','compound')",
                           name='ck_tnd_measure_rate_type'),
        sa.CheckConstraint("hts_code IS NULL OR hts_code ~ '^[0-9]{2,10}$'",
                           name='ck_tnd_measure_hts_digits'),
        sa.CheckConstraint("effective_to IS NULL OR effective_to > effective_from",
                           name='ck_tnd_measure_dates'),
    )
    op.create_index('ix_tnd_measures_lookup', 'tnd_measures',
                    ['org_id', 'status', 'jurisdiction', 'effective_from'])

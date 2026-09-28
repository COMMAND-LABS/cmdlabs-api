"""add tariff-and-duty (tnd_*) tables

Revision ID: c7a1d2e3f4b5
Revises: d948d7bf156f
Create Date: 2026-09-28

The tables the duty-and-tariff research agent
(research-agent-duty-n-tariff-updates) writes to, and that duty calculations
read from:

  tnd_research_runs      one row per scheduled run; `window_end` of the last
                         successful run is where the next one starts
  tnd_source_documents   every official document a run looked at, relevant or
                         not, so a document is never processed twice
  tnd_measures           the rate table: one rate, for one tariff code (or all
                         codes), from one origin (or all origins), over one
                         date range. The agent writes 'proposed' rows only; a
                         person approves them, and ONLY approved rows are used
                         in calculations (services/tnd_rates.py).

Rates live here and not in a knowledge base: a duty figure has to come from an
exact match on code, origin and date, not from a similarity search.

Every table is org-scoped like the rest of the platform. Approving a rate is
the customer's compliance decision, so one org's approval never changes
another org's numbers.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = 'c7a1d2e3f4b5'
down_revision: Union[str, None] = 'd948d7bf156f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _org_id():
    return sa.Column('org_id', sa.Integer(),
                     sa.ForeignKey('organizations.id', ondelete='CASCADE'),
                     nullable=False, index=True)


def upgrade() -> None:
    op.create_table(
        'tnd_research_runs',
        sa.Column('id', sa.Integer(), primary_key=True),
        _org_id(),
        sa.Column('started_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        # The publication dates this run covered, inclusive.
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
        sa.Column('source', sa.String(40), nullable=False),        # 'federal_register'
        sa.Column('external_id', sa.String(100), nullable=False),  # FR document_number
        sa.Column('title', sa.Text(), nullable=False),
        sa.Column('doc_type', sa.String(40), nullable=True),
        sa.Column('agencies', JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column('published_on', sa.Date(), nullable=True),
        sa.Column('effective_on', sa.Date(), nullable=True),
        sa.Column('url', sa.Text(), nullable=False),
        sa.Column('raw_text_url', sa.Text(), nullable=True),
        sa.Column('content_hash', sa.String(64), nullable=True),
        # new -> irrelevant | extracted | failed
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
        # Digits only, 2-10 of them, matched as a prefix. NULL = every code.
        sa.Column('hts_code', sa.String(10), nullable=True),
        # ISO 3166 alpha-2. NULL = every origin.
        sa.Column('origin_country', sa.String(2), nullable=True),
        # e.g. 'section_301', 'section_232', 'section_338', 'ieepa', 'mfn'.
        # Free text on purpose: new legal authorities keep appearing.
        sa.Column('program', sa.String(40), nullable=False),
        sa.Column('rate_type', sa.String(20), nullable=False),
        sa.Column('ad_valorem_rate', sa.Numeric(7, 4), nullable=True),   # 0.2500 = 25%
        sa.Column('specific_rate', sa.Numeric(14, 4), nullable=True),
        sa.Column('specific_unit', sa.String(20), nullable=True),        # 'kg', 'each'
        sa.Column('effective_from', sa.Date(), nullable=False),
        sa.Column('effective_to', sa.Date(), nullable=True),             # exclusive; NULL = open
        # Tariff-code prefixes carved out of this measure.
        sa.Column('excluded_hts', JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('source_quote', sa.Text(), nullable=True),
        # True when source_quote was found verbatim in the document text.
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


def downgrade() -> None:
    op.drop_index('ix_tnd_measures_lookup', table_name='tnd_measures')
    op.drop_table('tnd_measures')
    op.drop_table('tnd_source_documents')
    op.drop_table('tnd_research_runs')

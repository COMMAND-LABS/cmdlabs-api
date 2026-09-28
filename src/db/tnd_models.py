"""
Tariff-and-duty (tnd_*) tables: what the duty-and-tariff research agent found,
and the rate table duty calculations read.

Written by research-agent-duty-n-tariff-updates (a separate scheduled job that
connects to the same database); reviewed through /api/tariffs; read by
services/tnd_rates.py. Kept in step with migration c7a1d2e3f4b5.

The agent only ever writes status='proposed'. A person approves or rejects each
measure, and calculations use approved measures only: a wrong rate is a wrong
number that finance acts on, so nothing the agent extracted counts until
somebody has checked it against its source quote.
"""
from sqlalchemy import (Boolean, CheckConstraint, Column, Date, DateTime, ForeignKey,
                        Index, Integer, Numeric, String, Text, UniqueConstraint, func, text)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from .database import Base

RUN_STATUSES = ('running', 'succeeded', 'failed')
DOCUMENT_STATUSES = ('new', 'irrelevant', 'extracted', 'failed')
MEASURE_STATUSES = ('proposed', 'approved', 'rejected')
RATE_TYPES = ('ad_valorem', 'specific', 'compound')


def _org_id():
    return Column(Integer, ForeignKey('organizations.id', ondelete='CASCADE'),
                  nullable=False, index=True)


class TndResearchRun(Base):
    """One scheduled run. The next run starts after the last successful
    run's window_end."""
    __tablename__ = 'tnd_research_runs'

    id = Column(Integer, primary_key=True)
    org_id = _org_id()
    started_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    window_start = Column(Date, nullable=False)
    window_end = Column(Date, nullable=False)
    docs_fetched = Column(Integer, nullable=False, server_default='0')
    docs_relevant = Column(Integer, nullable=False, server_default='0')
    measures_proposed = Column(Integer, nullable=False, server_default='0')
    status = Column(String(20), nullable=False, server_default='running')
    error = Column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint("status IN ('running','succeeded','failed')", name='ck_tnd_run_status'),
    )


class TndSourceDocument(Base):
    """An official document a run looked at, relevant or not. Unique per
    (org, source, external_id), so no document is processed twice."""
    __tablename__ = 'tnd_source_documents'

    id = Column(Integer, primary_key=True)
    org_id = _org_id()
    run_id = Column(Integer, ForeignKey('tnd_research_runs.id', ondelete='SET NULL'),
                    nullable=True, index=True)
    source = Column(String(40), nullable=False)
    external_id = Column(String(100), nullable=False)
    title = Column(Text, nullable=False)
    doc_type = Column(String(40), nullable=True)
    agencies = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    published_on = Column(Date, nullable=True)
    effective_on = Column(Date, nullable=True)
    url = Column(Text, nullable=False)
    raw_text_url = Column(Text, nullable=True)
    content_hash = Column(String(64), nullable=True)
    status = Column(String(20), nullable=False, server_default='new')
    note = Column(Text, nullable=True)
    fetched_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint('org_id', 'source', 'external_id', name='uq_tnd_doc_source_id'),
        CheckConstraint("status IN ('new','irrelevant','extracted','failed')",
                        name='ck_tnd_doc_status'),
    )


class TndMeasure(Base):
    """One rate on one tariff code (NULL = all) from one origin (NULL = all),
    over [effective_from, effective_to)."""
    __tablename__ = 'tnd_measures'

    id = Column(Integer, primary_key=True)
    org_id = _org_id()
    run_id = Column(Integer, ForeignKey('tnd_research_runs.id', ondelete='SET NULL'),
                    nullable=True)
    source_document_id = Column(Integer,
                                ForeignKey('tnd_source_documents.id', ondelete='SET NULL'),
                                nullable=True, index=True)
    jurisdiction = Column(String(2), nullable=False, server_default='US')
    hts_code = Column(String(10), nullable=True)
    origin_country = Column(String(2), nullable=True)
    program = Column(String(40), nullable=False)
    rate_type = Column(String(20), nullable=False)
    ad_valorem_rate = Column(Numeric(7, 4), nullable=True)
    specific_rate = Column(Numeric(14, 4), nullable=True)
    specific_unit = Column(String(20), nullable=True)
    effective_from = Column(Date, nullable=False)
    effective_to = Column(Date, nullable=True)
    excluded_hts = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    description = Column(Text, nullable=False)
    source_quote = Column(Text, nullable=True)
    quote_verified = Column(Boolean, nullable=False, server_default=text('false'))
    confidence = Column(Numeric(3, 2), nullable=True)
    extracted_by = Column(String(80), nullable=True)
    status = Column(String(20), nullable=False, server_default='proposed')
    reviewed_by_account_id = Column(Integer, ForeignKey('accounts.id', ondelete='SET NULL'),
                                    nullable=True)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    review_note = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    source_document = relationship('TndSourceDocument')

    __table_args__ = (
        CheckConstraint("status IN ('proposed','approved','rejected')",
                        name='ck_tnd_measure_status'),
        CheckConstraint("rate_type IN ('ad_valorem','specific','compound')",
                        name='ck_tnd_measure_rate_type'),
        CheckConstraint("hts_code IS NULL OR hts_code ~ '^[0-9]{2,10}$'",
                        name='ck_tnd_measure_hts_digits'),
        CheckConstraint("effective_to IS NULL OR effective_to > effective_from",
                        name='ck_tnd_measure_dates'),
        Index('ix_tnd_measures_lookup', 'org_id', 'status', 'jurisdiction', 'effective_from'),
    )

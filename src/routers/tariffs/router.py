"""
Tariff & duty updates — review what the research agent found, and calculate
duty from what was approved. Mounted at /api/tariffs (module 'tariffs').

The research agent (research-agent-duty-n-tariff-updates) writes runs,
documents and PROPOSED measures straight to the database; nothing here creates
them. This router is the human half: approve or reject each measure, and look
up the duty an entry line would pay under the approved ones.
"""
import datetime as dt
import re
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func
from sqlalchemy.orm import joinedload

from src.db.tnd_models import TndMeasure, TndResearchRun, TndSourceDocument
from src.deps import auth_dependency, db_dependency, org_dependency
from src.rate_limit import limiter
from src.services import tnd_rates
from src.services.org_scope import get_scoped_or_404, tenant_predicate

router = APIRouter()


class RunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    started_at: dt.datetime
    finished_at: dt.datetime | None
    window_start: dt.date
    window_end: dt.date
    docs_fetched: int
    docs_relevant: int
    measures_proposed: int
    status: str
    error: str | None


class SourceDocumentSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    source: str
    external_id: str
    title: str
    url: str
    published_on: dt.date | None


class MeasureResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    jurisdiction: str
    hts_code: str | None
    origin_country: str | None
    program: str
    rate_type: str
    ad_valorem_rate: Decimal | None
    specific_rate: Decimal | None
    specific_unit: str | None
    effective_from: dt.date
    effective_to: dt.date | None
    excluded_hts: list[str]
    description: str
    source_quote: str | None
    quote_verified: bool
    confidence: Decimal | None
    extracted_by: str | None
    status: str
    reviewed_by_account_id: int | None
    reviewed_at: dt.datetime | None
    review_note: str | None
    created_at: dt.datetime
    source_document: SourceDocumentSummary | None


class PreviousRate(BaseModel):
    """The approved rate a measure changes (tnd_rates.previous_rate)."""
    model_config = ConfigDict(from_attributes=True)
    rate_type: str
    ad_valorem_rate: Decimal | None
    specific_rate: Decimal | None
    specific_unit: str | None
    hts_code: str | None
    effective_from: dt.date


class MeasureListItem(MeasureResponse):
    # What changed and for which product, for the review cards.
    previous_rate: PreviousRate | None = None
    product_description: str | None = None


class MeasureListResponse(BaseModel):
    measures: list[MeasureListItem]
    total: int
    counts: dict[str, int]


class ReviewRequest(BaseModel):
    decision: Literal["approved", "rejected"]
    note: str | None = Field(default=None, max_length=2000)


@router.get("/runs", response_model=list[RunResponse])
@limiter.limit("60/minute")
async def list_runs(db: db_dependency, auth: auth_dependency, org: org_dependency,
                    request: Request, limit: int = Query(10, ge=1, le=100)):
    return (db.query(TndResearchRun)
            .filter(tenant_predicate(TndResearchRun, org))
            .order_by(TndResearchRun.started_at.desc())
            .limit(limit).all())


@router.get("/measures", response_model=MeasureListResponse)
@limiter.limit("60/minute")
async def list_measures(
    db: db_dependency, auth: auth_dependency, org: org_dependency, request: Request,
    status: Literal["proposed", "approved", "rejected"] | None = Query(default="proposed"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    base = db.query(TndMeasure).filter(tenant_predicate(TndMeasure, org))
    counts = {s: 0 for s in ("proposed", "approved", "rejected")}
    for s, n in (db.query(TndMeasure.status, func.count(TndMeasure.id))
                 .filter(tenant_predicate(TndMeasure, org))
                 .group_by(TndMeasure.status).all()):
        counts[s] = n

    query = base.options(joinedload(TndMeasure.source_document))
    if status:
        query = query.filter(TndMeasure.status == status)
    total = query.count()
    rows = (query.order_by(TndMeasure.effective_from.desc(), TndMeasure.id.desc())
            .offset(offset).limit(limit).all())

    # Context for the cards, one query each for the whole page (no N+1).
    approved = base.filter(TndMeasure.status == "approved").all() if rows else []
    products = _hts_descriptions(db, org) if rows else {}
    items = []
    for m in rows:
        item = MeasureListItem.model_validate(m)
        prev = tnd_rates.previous_rate(approved, m)
        item.previous_rate = PreviousRate.model_validate(prev) if prev else None
        item.product_description = _product_description(m, products)
        items.append(item)
    return MeasureListResponse(measures=items, total=total, counts=counts)


_HTS_TITLE = re.compile(r"^\s*HTS\s+([\d.]+)\s*:\s*(.+)$", re.DOTALL)


def _parse_hts_title(title: str | None) -> tuple[str, str] | None:
    """'HTS 8544.30.00: Ignition wiring sets…' -> ('85443000', 'Ignition wiring sets…')."""
    match = _HTS_TITLE.match(title or "")
    if not match:
        return None
    code, text = tnd_rates.normalize_hts(match.group(1)), match.group(2).strip()
    return (code, text) if code and text else None


def _hts_descriptions(db, org) -> dict[str, str]:
    """HTS number (digits) -> product description, from this org's 'hts'
    source documents (the scraper titles them 'HTS <no>: <description>').
    The newest document wins when a code was fetched more than once."""
    out: dict[str, str] = {}
    for (title,) in (db.query(TndSourceDocument.title)
                     .filter(tenant_predicate(TndSourceDocument, org),
                             TndSourceDocument.source == "hts")
                     .order_by(TndSourceDocument.id).all()):
        parsed = _parse_hts_title(title)
        if parsed:
            out[parsed[0]] = parsed[1]
    return out


def _product_description(m: TndMeasure, products: dict[str, str]) -> str | None:
    """The measure's own HTS document if it came from one; else the code
    itself, its nearest parent, or its nearest child (a base rate stored at
    the parent level, e.g. '8703' found via 8703.23.01)."""
    code = m.hts_code
    if not code:
        return None
    doc = m.source_document
    if doc is not None and doc.source == "hts" and doc.org_id == m.org_id:
        parsed = _parse_hts_title(doc.title)
        if parsed:
            return parsed[1]
    for n in range(len(code), 1, -1):
        if code[:n] in products:
            return products[code[:n]]
    children = sorted((c for c in products if c.startswith(code)), key=lambda c: (len(c), c))
    return products[children[0]] if children else None


@router.post("/measures/{measure_id}/review", response_model=MeasureResponse)
@limiter.limit("60/minute")
async def review_measure(measure_id: int, body: ReviewRequest, db: db_dependency,
                         auth: auth_dependency, org: org_dependency, request: Request):
    """Approve or reject a measure. Reversible: a later review replaces an
    earlier one, and the row always records who decided last and when."""
    measure = get_scoped_or_404(db, TndMeasure, measure_id, org, label="Measure")
    measure.status = body.decision
    measure.review_note = (body.note or "").strip() or None
    measure.reviewed_by_account_id = org.account_id
    measure.reviewed_at = dt.datetime.now(dt.timezone.utc)
    db.commit()
    db.refresh(measure)
    return measure


@router.get("/duty")
@limiter.limit("60/minute")
async def calculate_duty(
    db: db_dependency, auth: auth_dependency, org: org_dependency, request: Request,
    hts_code: str = Query(..., min_length=2, max_length=20),
    customs_value: Decimal = Query(..., ge=0),
    origin: str | None = Query(default=None, min_length=2, max_length=2),
    entry_date: dt.date | None = Query(default=None, description="Defaults to today"),
    quantity: Decimal | None = Query(default=None, ge=0),
):
    """Duty on one entry line under this org's APPROVED measures."""
    return tnd_rates.calculate_duty(
        db, org.org_id, hts_code=hts_code, origin=origin,
        on_date=entry_date or dt.date.today(), customs_value=customs_value,
        quantity=quantity)


# Historical data for the forecast tool (Tariffs -> Upload Data).
from .datasets import router as datasets_router  # noqa: E402

router.include_router(datasets_router)

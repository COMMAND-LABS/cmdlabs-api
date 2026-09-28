"""
Tariff & duty updates — review what the research agent found, and calculate
duty from what was approved. Mounted at /api/tariffs (module 'tariffs').

The research agent (research-agent-duty-n-tariff-updates) writes runs,
documents and PROPOSED measures straight to the database; nothing here creates
them. This router is the human half: approve or reject each measure, and look
up the duty an entry line would pay under the approved ones.
"""
import datetime as dt
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func
from sqlalchemy.orm import joinedload

from src.db.tnd_models import TndMeasure, TndResearchRun
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


class MeasureListResponse(BaseModel):
    measures: list[MeasureResponse]
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
    return MeasureListResponse(measures=rows, total=total, counts=counts)


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

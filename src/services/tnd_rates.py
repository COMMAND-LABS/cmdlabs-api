"""
Duty on one entry line, from APPROVED tariff measures only (db/tnd_models.py).

    duty = Σ over programs (base rate, Section 301, 232, 338, IEEPA, ...)
           of that program's rate in force on the entry date

Programs stack: an ad valorem rate applies to the customs value, a specific
rate to the quantity, a compound rate to both. Within ONE program only one
measure applies, chosen in this order:

  1. the most specific tariff code (a 10-digit match beats a chapter match,
     and any code beats "all codes");
  2. a named origin over "all origins";
  3. the latest effective_from, then the latest id: a newer approved
     measure replaces an older one without anybody editing the older row.

Known gaps, reported as warnings rather than guessed at:
  - no approved base ('mfn') rate for the code: the total is additional duties only;
  - a specific rate without a quantity cannot be computed;
  - free-trade-agreement rates, in-transit exemptions and non-stacking rules
    between programs are not modelled; the reviewer notes them per measure.
"""
from __future__ import annotations

import datetime as dt
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import or_
from sqlalchemy.orm import Session

from src.db.tnd_models import TndMeasure

CENT = Decimal("0.01")


def normalize_hts(code: str | None) -> str:
    """'8703.23.01' -> '87032301'."""
    return "".join(ch for ch in (code or "") if ch.isdigit())


def _matches(measure: TndMeasure, hts: str, origin: str | None) -> bool:
    if measure.hts_code and not hts.startswith(measure.hts_code):
        return False
    if measure.origin_country and measure.origin_country != origin:
        return False
    return not any(hts.startswith(normalize_hts(x)) for x in (measure.excluded_hts or []))


def _specificity(m: TndMeasure) -> tuple:
    return (len(m.hts_code or ""), m.origin_country is not None, m.effective_from, m.id)


def applicable_measures(db: Session, org_id: int, hts_code: str, origin: str | None,
                        on_date: dt.date, jurisdiction: str = "US") -> list[TndMeasure]:
    """One approved measure per program, in force on `on_date` for this line."""
    hts = normalize_hts(hts_code)
    origin = (origin or "").upper() or None
    candidates = (
        db.query(TndMeasure)
        .filter(
            TndMeasure.org_id == org_id,
            TndMeasure.status == "approved",
            TndMeasure.jurisdiction == jurisdiction,
            TndMeasure.effective_from <= on_date,
            or_(TndMeasure.effective_to.is_(None), TndMeasure.effective_to > on_date),
        )
        .all()
    )
    best: dict[str, TndMeasure] = {}
    for m in candidates:
        if not _matches(m, hts, origin):
            continue
        if m.program not in best or _specificity(m) > _specificity(best[m.program]):
            best[m.program] = m
    # Base rate first, then the additional duties alphabetically: a stable
    # order for the breakdown people read.
    return sorted(best.values(), key=lambda m: (m.program != "mfn", m.program))


def calculate_duty(db: Session, org_id: int, *, hts_code: str, origin: str | None,
                   on_date: dt.date, customs_value: Decimal,
                   quantity: Decimal | None = None) -> dict:
    measures = applicable_measures(db, org_id, hts_code, origin, on_date)
    lines, warnings = [], []
    total = Decimal("0")

    for m in measures:
        amount = Decimal("0")
        computable = True
        if m.rate_type in ("ad_valorem", "compound") and m.ad_valorem_rate is not None:
            amount += customs_value * m.ad_valorem_rate
        if m.rate_type in ("specific", "compound") and m.specific_rate is not None:
            if quantity is None:
                computable = False
                warnings.append(
                    f"{m.program}: specific rate {m.specific_rate} per {m.specific_unit or 'unit'} "
                    "needs a quantity; not included in the total.")
            else:
                amount += quantity * m.specific_rate
        amount = amount.quantize(CENT, rounding=ROUND_HALF_UP)
        if computable:
            total += amount
        lines.append({
            "measure_id": m.id,
            "program": m.program,
            "hts_code": m.hts_code,
            "origin_country": m.origin_country,
            "rate_type": m.rate_type,
            "ad_valorem_rate": m.ad_valorem_rate,
            "specific_rate": m.specific_rate,
            "specific_unit": m.specific_unit,
            "effective_from": m.effective_from,
            "effective_to": m.effective_to,
            "description": m.description,
            "amount": amount if computable else None,
        })

    if not any(m.program == "mfn" for m in measures):
        warnings.append("No approved base (MFN) rate for this code; the total covers "
                        "additional duties only.")
    if not measures:
        warnings.append("No approved measures apply to this code, origin and date.")

    return {
        "hts_code": normalize_hts(hts_code),
        "origin_country": (origin or "").upper() or None,
        "entry_date": on_date,
        "customs_value": customs_value,
        "quantity": quantity,
        "total_duty": total.quantize(CENT, rounding=ROUND_HALF_UP),
        "effective_rate": (total / customs_value).quantize(Decimal("0.0001"))
        if customs_value else None,
        "lines": lines,
        "warnings": warnings,
    }

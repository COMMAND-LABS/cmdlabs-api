"""Tariff & duty updates: the review endpoints and the duty calculation.

The research agent writes PROPOSED measures; only APPROVED ones may reach a
duty figure. Within a program the most specific approved measure in force on
the entry date wins, and programs stack.
"""
import datetime as dt
from decimal import Decimal

from sqlalchemy.orm import Session

from src.db.models import Organization
from src.db.tnd_models import TndMeasure, TndSourceDocument
from src.services.tnd_rates import calculate_duty

D = dt.date


def _measure(db, org, *, program="mfn", hts="870323", origin=None, rate="0.025",
             frm=D(2026, 1, 1), to=None, status="approved", **kw):
    m = TndMeasure(org_id=org.id, program=program, hts_code=hts, origin_country=origin,
                   rate_type=kw.pop("rate_type", "ad_valorem"),
                   ad_valorem_rate=Decimal(rate) if rate is not None else None,
                   effective_from=frm, effective_to=to, status=status,
                   description=kw.pop("description", f"{program} {rate}"), **kw)
    db.add(m)
    db.flush()
    return m


def _duty(db, org, value="10000", on=D(2026, 10, 1), origin="CA", hts="8703.23.01", qty=None):
    return calculate_duty(db, org.id, hts_code=hts, origin=origin, on_date=on,
                          customs_value=Decimal(value),
                          quantity=Decimal(qty) if qty else None)


def test_programs_stack(db: Session, test_org):
    _measure(db, test_org)                                                 # 2.5% base
    _measure(db, test_org, program="section_338", hts="8703", origin="CA", rate="0.25")
    out = _duty(db, test_org)
    assert out["total_duty"] == Decimal("2750.00")
    assert [line["program"] for line in out["lines"]] == ["mfn", "section_338"]
    assert out["warnings"] == []


def test_only_approved_measures_count(db: Session, test_org):
    _measure(db, test_org)
    _measure(db, test_org, program="section_338", rate="0.25", status="proposed")
    _measure(db, test_org, program="ieepa", rate="0.10", status="rejected")
    assert _duty(db, test_org)["total_duty"] == Decimal("250.00")


def test_dates_origin_and_exclusions(db: Session, test_org):
    _measure(db, test_org)
    _measure(db, test_org, program="section_338", origin="CA", rate="0.25",
             frm=D(2026, 8, 19), to=D(2026, 12, 1))
    _measure(db, test_org, program="ieepa", rate="0.10", excluded_hts=["8703.23"])
    assert _duty(db, test_org, on=D(2026, 8, 18))["total_duty"] == Decimal("250.00")   # before
    assert _duty(db, test_org, on=D(2026, 8, 19))["total_duty"] == Decimal("2750.00")  # in force
    assert _duty(db, test_org, on=D(2026, 12, 1))["total_duty"] == Decimal("250.00")   # ended
    assert _duty(db, test_org, origin="MX")["total_duty"] == Decimal("250.00")         # other origin


def test_most_specific_then_newest_wins_within_a_program(db: Session, test_org):
    _measure(db, test_org, program="section_338", hts="87", rate="0.10")
    specific = _measure(db, test_org, program="section_338", hts="870323", rate="0.25")
    assert _duty(db, test_org)["lines"][-1]["measure_id"] == specific.id

    newer = _measure(db, test_org, program="section_338", hts="870323", rate="0.15",
                     frm=D(2026, 9, 1))
    out = _duty(db, test_org)
    assert out["lines"][-1]["measure_id"] == newer.id
    assert out["total_duty"] == Decimal("1500.00")


def test_specific_rate_needs_a_quantity(db: Session, test_org):
    _measure(db, test_org, rate_type="specific", rate=None,
             specific_rate=Decimal("0.044"), specific_unit="kg")
    out = _duty(db, test_org)
    assert out["total_duty"] == Decimal("0.00")
    assert any("needs a quantity" in w for w in out["warnings"])
    assert _duty(db, test_org, qty="1000")["total_duty"] == Decimal("44.00")


def test_missing_base_rate_is_reported(db: Session, test_org):
    _measure(db, test_org, program="section_338", rate="0.25")
    assert any("No approved base" in w for w in _duty(db, test_org)["warnings"])


def test_other_orgs_measures_never_apply(db: Session, test_org):
    other = Organization(name="Other Co")
    db.add(other)
    db.flush()
    _measure(db, other, rate="0.50")
    assert _duty(db, test_org)["total_duty"] == Decimal("0.00")


async def test_list_and_review(authed_client, db: Session, test_org, test_account):
    doc = TndSourceDocument(org_id=test_org.id, source="federal_register",
                            external_id="2026-18839", title="Proclamation 11065",
                            url="https://www.federalregister.gov/d/2026-18839")
    db.add(doc)
    db.flush()
    m = _measure(db, test_org, program="section_338", rate="0.25", status="proposed",
                 source_document_id=doc.id, source_quote="25 percent")
    _measure(db, test_org)

    resp = await authed_client.get("/api/tariffs/measures")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [x["id"] for x in body["measures"]] == [m.id]           # proposed by default
    assert body["counts"] == {"proposed": 1, "approved": 1, "rejected": 0}
    assert body["measures"][0]["source_document"]["external_id"] == "2026-18839"

    resp = await authed_client.post(f"/api/tariffs/measures/{m.id}/review",
                                    json={"decision": "approved", "note": "checked"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "approved"
    assert resp.json()["reviewed_by_account_id"] == test_account.id

    resp = await authed_client.get("/api/tariffs/duty", params={
        "hts_code": "8703.23.01", "customs_value": "10000", "origin": "CA",
        "entry_date": "2026-10-01"})
    assert resp.status_code == 200, resp.text
    assert Decimal(resp.json()["total_duty"]) == Decimal("2750.00")


async def test_cannot_review_another_orgs_measure(authed_client, db: Session):
    other = Organization(name="Other Co")
    db.add(other)
    db.flush()
    m = _measure(db, other, status="proposed")
    resp = await authed_client.post(f"/api/tariffs/measures/{m.id}/review",
                                    json={"decision": "approved"})
    assert resp.status_code == 404
    db.refresh(m)
    assert m.status == "proposed"


# ---------------------------------------------------------------------------
# Upload Data: historical CSVs for the forecast tool
# ---------------------------------------------------------------------------

MOCK_CSV = open("data/mock/duty_spend.csv", "rb").read()


def test_inspect_the_mock_dataset():
    from src.routers.tariffs.datasets import inspect_csv, tool_config
    info = inspect_csv(MOCK_CSV)
    assert info["rows"] == 60
    assert info["date_column"] == "month"
    assert (info["first_period"], info["last_period"]) == ("2021-01-01", "2025-12-01")
    assert info["suggested"] == {"target_column": "import_value", "rate_column": "duty_rate"}
    assert tool_config("datasets/duty_spend.csv", info) == {
        "type": "timeSeriesForecast", "name": "forecast_duty_spend",
        "dataset": {"gcsPath": "datasets/duty_spend.csv"},
        "dateColumn": "month", "targetColumn": "import_value",
        "rate": {"column": "duty_rate", "outputName": "duty_spend"}}


def test_inspect_rejects_unusable_files():
    import pytest
    from src.routers.tariffs.datasets import inspect_csv
    for data, msg in [
        (b"a,b\nx,y\n", "No date column"),
        (b"month,note\n2025-01-01,hi\n", "No numeric column"),
        (b"month,v\n2025-01-01,1\n2025-02-01,2\n", "at least 32"),
    ]:
        with pytest.raises(ValueError, match=msg):
            inspect_csv(data)


async def test_upload_stores_under_datasets_and_refuses_silent_overwrite(
    authed_client, test_account, monkeypatch
):
    from src.services import account_gcs_service as gcs
    stored = {}
    monkeypatch.setattr(gcs, "object_exists",
                        lambda db, acct, gcs_file_path: gcs_file_path in stored)

    def upload(db, acct, *, file_bytes, gcs_file_path, content_type=None):
        stored[gcs_file_path] = (acct, file_bytes)
        return {"gcs_bucket": "b", "gcs_file_path": gcs_file_path}
    monkeypatch.setattr(gcs, "upload_bytes", upload)

    files = {"file": ("duty_spend.csv", MOCK_CSV, "text/csv")}
    resp = await authed_client.post("/api/tariffs/datasets", files=files)
    assert resp.status_code == 201, resp.text
    assert resp.json()["tool_config"]["dataset"]["gcsPath"] == "datasets/duty_spend.csv"
    assert stored["datasets/duty_spend.csv"][0] == test_account.id   # the uploader's bucket

    again = await authed_client.post("/api/tariffs/datasets", files=files)
    assert again.status_code == 409
    replaced = await authed_client.post("/api/tariffs/datasets", files=files,
                                        data={"replace": "true"})
    assert replaced.status_code == 201

    bad = await authed_client.post("/api/tariffs/datasets",
                                   files={"file": ("../x.csv", MOCK_CSV, "text/csv")})
    assert bad.status_code == 400

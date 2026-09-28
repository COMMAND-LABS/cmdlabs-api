"""GET /api/deals/?stage= takes one stage or a comma-separated list.

The deals page filters by any combination of stages while keeping pagination
and the total count on the server, so the list route must accept several
stages in one request. A single stage must keep filtering exactly as before.
"""
from sqlalchemy.orm import Session

from src.db.models import Deal

SEEDED = [("a", "lead"), ("b", "proposal"), ("c", "paid"), ("d", "lost")]


def _seed(db: Session, test_org, test_account) -> None:
    for title, stage in SEEDED:
        db.add(Deal(org_id=test_org.id, account_id=test_account.id, title=title, stage=stage))
    db.flush()


async def _titles(authed_client, stage: str) -> tuple[set[str], int]:
    resp = await authed_client.get("/api/deals/", params={"stage": stage})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return {d["title"] for d in body["deals"]}, body["total"]


async def test_single_stage_filters_as_before(authed_client, db: Session, test_org, test_account):
    _seed(db, test_org, test_account)
    assert await _titles(authed_client, "lead") == ({"a"}, 1)


async def test_several_stages_match_any_of_them(authed_client, db: Session, test_org, test_account):
    _seed(db, test_org, test_account)
    assert await _titles(authed_client, "lead,proposal") == ({"a", "b"}, 2)


async def test_stage_list_tolerates_case_spaces_and_empty_items(
    authed_client, db: Session, test_org, test_account
):
    _seed(db, test_org, test_account)
    assert await _titles(authed_client, " Lead, PROPOSAL,,") == ({"a", "b"}, 2)


async def test_no_stage_returns_every_stage(authed_client, db: Session, test_org, test_account):
    _seed(db, test_org, test_account)
    resp = await authed_client.get("/api/deals/")
    assert resp.status_code == 200
    assert {d["title"] for d in resp.json()["deals"]} >= {t for t, _ in SEEDED}

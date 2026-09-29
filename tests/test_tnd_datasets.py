"""Upload Data, second half: read a stored file's columns back, and attach it
to an agent's Forecast ability without anyone copying JSON."""
import pytest
from google.api_core.exceptions import NotFound
from sqlalchemy.orm import Session

from src.db.models import Account, Agent

MOCK_CSV = open("data/mock/duty_spend.csv", "rb").read()


def _config(tools=None):
    data = {"systemPrompt": "You forecast duty."}
    if tools is not None:
        data["tools"] = tools
    return {"schema": "agent_config", "version": 4, "data": data}


def _agent(db: Session, org_id: int, account_id: int, config=None) -> Agent:
    agent = Agent(org_id=org_id, account_id=account_id, name="Duty Analyst",
                  config=config if config is not None else _config())
    db.add(agent)
    db.flush()
    return agent


@pytest.fixture()
def stored(monkeypatch):
    """A fake bucket: {(account_id, path): bytes}. Missing objects raise NotFound
    like the real download does."""
    from src.services import account_gcs_service as gcs
    files = {}

    def download(db, account_id, *, gcs_file_path, max_bytes=0):
        try:
            return files[(account_id, gcs_file_path)]
        except KeyError:
            raise NotFound("missing")
    monkeypatch.setattr(gcs, "download_bytes", download)
    return files


async def test_columns_of_a_stored_file(authed_client, test_account, stored):
    stored[(test_account.id, "datasets/duty_spend.csv")] = MOCK_CSV
    resp = await authed_client.get("/api/tariffs/datasets/columns",
                                   params={"path": "datasets/duty_spend.csv"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["path"] == "datasets/duty_spend.csv"
    assert body["date_column"] == "month"
    assert body["numeric_columns"] == ["import_value", "duty_rate", "duty_spend"]
    assert body["suggested"] == {"target_column": "import_value", "rate_column": "duty_rate"}
    assert body["rows"] == 60
    assert (body["first_period"], body["last_period"]) == ("2021-01-01", "2025-12-01")


async def test_columns_missing_file_and_bad_paths(authed_client, stored):
    resp = await authed_client.get("/api/tariffs/datasets/columns",
                                   params={"path": "datasets/nope.csv"})
    assert resp.status_code == 404
    for bad in ["other/x.csv", "datasets/../x.csv", "datasets/a/b.csv",
                "datasets/x.txt", "datasets/a..csv", ""]:
        resp = await authed_client.get("/api/tariffs/datasets/columns", params={"path": bad})
        assert resp.status_code == 400, bad


async def test_columns_unusable_file_is_400_with_reason(authed_client, test_account, stored):
    stored[(test_account.id, "datasets/short.csv")] = b"month,v\n2025-01-01,1\n"
    resp = await authed_client.get("/api/tariffs/datasets/columns",
                                   params={"path": "datasets/short.csv"})
    assert resp.status_code == 400
    assert "at least 32" in resp.json()["detail"]


async def test_attach_appends_a_forecast_tool(authed_client, db: Session, test_org,
                                              test_account, stored):
    stored[(test_account.id, "datasets/duty_spend.csv")] = MOCK_CSV
    agent = _agent(db, test_org.id, test_account.id)
    resp = await authed_client.post("/api/tariffs/datasets/attach", json={
        "path": "datasets/duty_spend.csv", "agent_id": agent.id})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["agent_id"] == agent.id and body["replaced"] is False
    assert body["tool"] == {
        "type": "timeSeriesForecast", "name": "forecast_duty_spend",
        "dataset": {"gcsPath": "datasets/duty_spend.csv"},
        "dateColumn": "month", "targetColumn": "import_value",
        "rate": {"column": "duty_rate", "outputName": "duty_spend"}}
    db.refresh(agent)
    assert agent.config["data"]["tools"] == [body["tool"]]
    assert agent.config["data"]["systemPrompt"] == "You forecast duty."


async def test_attach_replaces_the_same_named_tool(authed_client, db: Session, test_org,
                                                   test_account, stored):
    stored[(test_account.id, "datasets/duty_2026.csv")] = MOCK_CSV
    old = {"type": "timeSeriesForecast", "name": "forecast_duty_spend",
           "dataset": {"gcsPath": "datasets/duty_2025.csv"},
           "dateColumn": "month", "targetColumn": "import_value"}
    other = {**old, "name": "forecast_volume"}
    agent = _agent(db, test_org.id, test_account.id, _config([old, other]))

    resp = await authed_client.post("/api/tariffs/datasets/attach", json={
        "path": "datasets/duty_2026.csv", "agent_id": agent.id})
    assert resp.status_code == 200, resp.text
    assert resp.json()["replaced"] is True
    db.refresh(agent)
    tools = agent.config["data"]["tools"]
    assert [t["name"] for t in tools] == ["forecast_duty_spend", "forecast_volume"]
    assert tools[0]["dataset"]["gcsPath"] == "datasets/duty_2026.csv"
    assert tools[1] == other

    # A different name adds a second forecast ability.
    resp = await authed_client.post("/api/tariffs/datasets/attach", json={
        "path": "datasets/duty_2026.csv", "agent_id": agent.id, "name": "forecast_q2"})
    assert resp.status_code == 200 and resp.json()["replaced"] is False
    db.refresh(agent)
    assert len(agent.config["data"]["tools"]) == 3


async def test_attach_refuses_a_non_owner(authed_client, db: Session, test_org,
                                          test_account, stored):
    stored[(test_account.id, "datasets/duty_spend.csv")] = MOCK_CSV
    other = Account(id=9401, email="colleague@example.com", default_org_id=test_org.id)
    db.add(other)
    db.flush()
    agent = _agent(db, test_org.id, other.id)
    resp = await authed_client.post("/api/tariffs/datasets/attach", json={
        "path": "datasets/duty_spend.csv", "agent_id": agent.id})
    assert resp.status_code == 404
    db.refresh(agent)
    assert "tools" not in agent.config["data"]


async def test_attach_bad_path_bad_name_and_old_config(authed_client, db: Session, test_org,
                                                       test_account, stored):
    stored[(test_account.id, "datasets/duty_spend.csv")] = MOCK_CSV
    agent = _agent(db, test_org.id, test_account.id)
    resp = await authed_client.post("/api/tariffs/datasets/attach", json={
        "path": "../secrets.csv", "agent_id": agent.id})
    assert resp.status_code == 400

    resp = await authed_client.post("/api/tariffs/datasets/attach", json={
        "path": "datasets/duty_spend.csv", "agent_id": agent.id, "name": "Bad Name!"})
    assert resp.status_code == 400
    db.refresh(agent)
    assert "tools" not in agent.config["data"]            # nothing saved

    legacy = _agent(db, test_org.id, test_account.id, config={"data": {}})
    resp = await authed_client.post("/api/tariffs/datasets/attach", json={
        "path": "datasets/duty_spend.csv", "agent_id": legacy.id})
    assert resp.status_code == 400
    assert "older format" in resp.json()["detail"]

"""The runner-backed tools: timeSeriesForecast and codeExecution.

Both are thin: config in, dataset bytes fetched, one HTTP call to the runner,
answer out. What is pinned here is the contract around that call — what the
model is offered (names, arguments), what reaches the runner, and that every
failure comes back to the model as a result rather than a raised exception.
The runner itself is tested in runner/tests.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from jsonschema import ValidationError

from src.agent_runtime.runner_client import RunnerClient, RunnerError, resolve_runner_client
from src.agent_runtime.tool_entitlement import TOOL_MODULES
from src.agent_runtime.tools import code_execution, time_series_forecast
from src.agent_runtime.tools.code_execution import create_code_execution_tool
from src.agent_runtime.tools.registry import ToolRegistry
from src.agent_runtime.tools.time_series_forecast import create_time_series_forecast_tool
from src.schemas import validate_against_schema

CSV = b"month,import_value,duty_rate\n2021-01-01,100,0.05\n"
FORECAST_CFG = {
    "type": "timeSeriesForecast",
    "dataset": {"gcsPath": "datasets/duty_spend.csv"},
    "dateColumn": "month",
    "targetColumn": "import_value",
    "rate": {"column": "duty_rate", "outputName": "duty_spend"},
}
EXEC_CFG = {
    "type": "codeExecution",
    "datasets": [{"gcsPath": "datasets/duty_spend.csv"}],
    "timeoutSeconds": 30,
}


class FakeRunner:
    def __init__(self, forecast_result=None, execute_result=None, error=None):
        self.forecast_result = forecast_result or {"prediction": 1.0, "rate": {"value": 0.05}}
        self.execute_result = execute_result or {"stdout": "hi\n", "stderr": "", "returncode": 0, "timed_out": False}
        self.error = error
        self.calls = []

    async def forecast(self, csv_bytes, **spec):
        self.calls.append(("forecast", csv_bytes, spec))
        if self.error:
            raise self.error
        return dict(self.forecast_result)

    async def execute(self, code, files, timeout_s):
        self.calls.append(("execute", code, files, timeout_s))
        if self.error:
            raise self.error
        return dict(self.execute_result)


@pytest.fixture
def wired(monkeypatch):
    """Owner has a GCS credential; datasets resolve to CSV without touching GCS."""
    fetches = []

    async def fake_fetch(session_factory, owner_account_id, ref):
        fetches.append((owner_account_id, ref.gcs_path))
        return CSV

    monkeypatch.setattr("src.agent_runtime.tools.datasets.resolve_default_credential",
                        lambda db, owner, kind: SimpleNamespace(id=9))
    monkeypatch.setattr(time_series_forecast, "fetch_dataset", fake_fetch)
    monkeypatch.setattr(code_execution, "fetch_dataset", fake_fetch)
    return fetches


def _kwargs(runner, **extra):
    return {"runner_client": runner, "session_factory": lambda: MagicMock(),
            "agent_owner_account_id": 7, **extra}


# ── Registration / entitlement ──────────────────────────────────────────────

def test_registered_and_explicitly_ungated():
    for tool_type in ("timeSeriesForecast", "codeExecution"):
        assert callable(ToolRegistry.get_builder(tool_type))
        assert tool_type in TOOL_MODULES and TOOL_MODULES[tool_type] is None


# ── Runner client resolution ────────────────────────────────────────────────

def test_no_runner_configured_means_no_tool(monkeypatch):
    monkeypatch.delenv("RUNNER_URL", raising=False)
    with pytest.raises(ValueError, match="RUNNER_URL"):
        resolve_runner_client({})


def test_runner_client_from_env(monkeypatch):
    monkeypatch.setenv("RUNNER_URL", "http://runner:8081/")
    monkeypatch.setenv("RUNNER_AUTH_MODE", "none")
    client = RunnerClient.from_env()
    assert client.base_url == "http://runner:8081"
    assert client.auth_mode == "none"


# ── timeSeriesForecast ──────────────────────────────────────────────────────

async def test_forecast_tool_shape_and_call(wired):
    runner = FakeRunner()
    tool = await create_time_series_forecast_tool(tool_config=FORECAST_CFG, account_id=1,
                                                  db=MagicMock(), **_kwargs(runner))
    assert tool.name == "time_series_forecast"
    assert "import_value" in tool.description and "duty_spend" in tool.description
    assert set(tool.args_schema.model_fields) == {"model", "rate"}

    out = await tool.coroutine(model="random_forest", rate=0.1)
    kind, csv_bytes, spec = runner.calls[0]
    assert (kind, csv_bytes) == ("forecast", CSV)
    assert spec == {"date_column": "month", "target_column": "import_value", "frequency": "monthly",
                    "rate_column": "duty_rate", "model": "random_forest", "rate_override": 0.1}
    assert out["dataset"] == "duty_spend.csv"
    assert out["rate"]["output_name"] == "duty_spend"
    assert wired == [(7, "datasets/duty_spend.csv")], "dataset read from the OWNER's bucket"


async def test_forecast_without_rate_offers_no_rate_argument(wired):
    cfg = {k: v for k, v in FORECAST_CFG.items() if k != "rate"}
    tool = await create_time_series_forecast_tool(tool_config=cfg, account_id=1, db=MagicMock(),
                                                  **_kwargs(FakeRunner()))
    assert set(tool.args_schema.model_fields) == {"model"}


async def test_forecast_custom_name_and_description(wired):
    cfg = {**FORECAST_CFG, "name": "forecast_duty_spend", "description": "Predict next month."}
    tool = await create_time_series_forecast_tool(tool_config=cfg, account_id=1, db=MagicMock(),
                                                  **_kwargs(FakeRunner()))
    assert (tool.name, tool.description) == ("forecast_duty_spend", "Predict next month.")


async def test_forecast_runner_refusal_is_a_result_not_an_exception(wired):
    runner = FakeRunner(error=RunnerError("Column 'x' not found", status=422))
    tool = await create_time_series_forecast_tool(tool_config=FORECAST_CFG, account_id=1,
                                                  db=MagicMock(), **_kwargs(runner))
    assert await tool.coroutine() == {"error": "Column 'x' not found"}


async def test_forecast_requires_owner_gcs_credential(monkeypatch):
    monkeypatch.setattr("src.agent_runtime.tools.datasets.resolve_default_credential",
                        lambda db, owner, kind: None)
    from src.agent_runtime.tools.exceptions import CredentialError
    with pytest.raises(CredentialError):
        await create_time_series_forecast_tool(tool_config=FORECAST_CFG, account_id=1,
                                               db=MagicMock(), **_kwargs(FakeRunner()))


# ── codeExecution ───────────────────────────────────────────────────────────

async def test_code_execution_ships_files_and_caches_the_dataset(wired):
    runner = FakeRunner()
    tool = await create_code_execution_tool(tool_config=EXEC_CFG, account_id=1, db=MagicMock(),
                                            **_kwargs(runner))
    assert tool.name == "run_python"
    assert "duty_spend.csv" in tool.description
    assert list(tool.args_schema.model_fields) == ["code"]

    out = await tool.coroutine(code="print('hi')")
    await tool.coroutine(code="print('again')")
    assert out["stdout"] == "hi\n"
    _, code, files, timeout_s = runner.calls[0]
    assert (code, files, timeout_s) == ("print('hi')", {"duty_spend.csv": CSV}, 30)
    assert len(wired) == 1, "same dataset is not re-downloaded within a turn"


async def test_code_execution_without_datasets_needs_no_gcs(monkeypatch):
    monkeypatch.setattr("src.agent_runtime.tools.datasets.resolve_default_credential",
                        lambda db, owner, kind: None)
    tool = await create_code_execution_tool(tool_config={"type": "codeExecution"}, account_id=1,
                                            db=MagicMock(), **_kwargs(FakeRunner()))
    assert "working directory contains" not in tool.description


async def test_code_execution_rejects_duplicate_filenames(wired):
    cfg = {"type": "codeExecution", "datasets": [
        {"gcsPath": "a/duty_spend.csv"}, {"gcsPath": "b/duty_spend.csv"}]}
    with pytest.raises(ValueError, match="unique"):
        await create_code_execution_tool(tool_config=cfg, account_id=1, db=MagicMock(),
                                         **_kwargs(FakeRunner()))


# ── Schema ──────────────────────────────────────────────────────────────────

def _config(tools: list) -> dict:
    return {"schema": "agent_config", "version": 4,
            "data": {"systemPrompt": "You are helpful.", "tools": tools}}


def test_schema_accepts_runner_backed_tools():
    validate_against_schema(_config([FORECAST_CFG, EXEC_CFG]), "agent_config", 4)


@pytest.mark.parametrize("bad", [
    {"type": "timeSeriesForecast", "dateColumn": "m", "targetColumn": "t"},          # no dataset
    {**FORECAST_CFG, "name": "Forecast Duty"},                                        # bad tool name
    {**FORECAST_CFG, "rate": {"column": "duty_rate"}},                                # rate needs outputName
    {"type": "codeExecution", "timeoutSeconds": 600},                                 # over the cap
    {"type": "codeExecution", "datasets": [{"gcsPath": "x.csv", "filename": "../x"}]},  # path in filename
])
def test_schema_rejects_bad_runner_tool_configs(bad):
    with pytest.raises(ValidationError):
        validate_against_schema(_config([bad]), "agent_config", 4)

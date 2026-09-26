"""timeSeriesForecast — next-month forecast over a CSV, trained in the runner.

The API side is deliberately thin: read the config, fetch the dataset bytes,
post them to the runner, hand the answer back. Training (lightgbm / sklearn)
happens in cmdlabs-runner so those libraries and that CPU never land in the
API process. See runner/runner/forecast.py for the model.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field, create_model
from sqlalchemy.orm import Session

from src.agent_runtime.runner_client import RunnerError, resolve_runner_client

from .datasets import fetch_dataset, parse_dataset_ref, require_gcs_credential
from .sessions import resolve_session_factory

logger = logging.getLogger(__name__)

DEFAULT_NAME = "time_series_forecast"
ModelName = Literal["lightgbm", "random_forest"]


def _args_schema(rate_cfg: dict[str, Any] | None) -> type[BaseModel]:
    """The `rate` argument only exists when the config declares a rate
    column; otherwise the model is not offered a knob that does nothing."""
    fields: dict[str, Any] = {
        "model": (ModelName, Field(default="lightgbm", description="Which learner to train.")),
    }
    if rate_cfg:
        fields["rate"] = (
            float | None,
            Field(default=None, ge=0,
                  description=f"Optional override for {rate_cfg['column']} to test a scenario, "
                              f"e.g. 0.10 for 10%. Omit to use the latest actual rate."),
        )
    return create_model("TimeSeriesForecastInput", **fields)


def _default_description(cfg: dict[str, Any], filename: str) -> str:
    text = (
        f"Train a model on {filename} and forecast next month's {cfg['targetColumn']}. "
        "Returns a point estimate with a low/high range and the model's out-of-sample error."
    )
    rate = cfg.get("rate")
    if rate:
        text += (
            f" Pass `rate` to model a change in {rate['column']}; the result then includes "
            f"{rate['outputName']} = forecast × rate."
        )
    return text


async def create_time_series_forecast_tool(
    tool_config: dict[str, Any],
    account_id: int,
    db: Session,
    auth_token: str | None = None,
    **kwargs,
) -> StructuredTool:
    for key in ("dateColumn", "targetColumn", "dataset"):
        if not tool_config.get(key):
            raise ValueError(f"timeSeriesForecast: '{key}' is required")
    dataset = parse_dataset_ref(tool_config["dataset"])
    rate_cfg = tool_config.get("rate") or None

    owner_account_id = kwargs.get("agent_owner_account_id", account_id)
    require_gcs_credential(db, owner_account_id)
    runner = resolve_runner_client(kwargs)
    session_factory = resolve_session_factory(kwargs)

    spec = {
        "date_column": tool_config["dateColumn"],
        "target_column": tool_config["targetColumn"],
        "frequency": tool_config.get("frequency", "monthly"),
        "rate_column": rate_cfg["column"] if rate_cfg else None,
    }

    async def _forecast(model: str = "lightgbm", rate: float | None = None) -> dict:
        try:
            csv_bytes = await fetch_dataset(session_factory, owner_account_id, dataset)
        except Exception as exc:
            logger.error("[FORECAST] dataset %s unavailable: %s", dataset.gcs_path, exc)
            return {"error": f"Could not read dataset {dataset.filename}: {exc}"}
        try:
            result = await runner.forecast(csv_bytes, model=model, rate_override=rate, **spec)
        except RunnerError as exc:
            return {"error": exc.detail}
        result["dataset"] = dataset.filename
        if rate_cfg and "rate" in result:
            result["rate"]["output_name"] = rate_cfg["outputName"]
        return result

    return StructuredTool.from_function(
        coroutine=_forecast,
        name=tool_config.get("name") or DEFAULT_NAME,
        description=tool_config.get("description") or _default_description(tool_config, dataset.filename),
        args_schema=_args_schema(rate_cfg),
    )

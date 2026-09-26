"""cmdlabs-runner — the API's isolated compute service.

Two endpoints, both synchronous HTTP, both called only by cmdlabs-api (Cloud
Run IAM; this service is not public):

  POST /execute   run model-written Python with the given files in cwd
  POST /forecast  train on a CSV and predict the next month

Nothing here touches a database, a credential or the network. The heavy work
runs in a worker thread so the health probe keeps answering while a request
trains a model.
"""

from __future__ import annotations

import base64
import binascii
import io
import logging
from typing import Literal

import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from runner import forecast as forecasting
from runner import sandbox

logger = logging.getLogger("runner")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="cmdlabs-runner", docs_url=None, redoc_url=None)


# ── Health ──────────────────────────────────────────────────────────────────

@app.get("/")
def health():
    return {"status": "OK!"}


# ── /execute ────────────────────────────────────────────────────────────────

class ExecuteRequest(BaseModel):
    code: str = Field(min_length=1, max_length=100_000)
    files: dict[str, str] = Field(default_factory=dict, description="name -> base64 content")
    timeout_s: int = Field(default=60, ge=1, le=120)


class ExecuteResponse(BaseModel):
    stdout: str
    stderr: str
    returncode: int
    timed_out: bool


@app.post("/execute", response_model=ExecuteResponse)
async def execute(req: ExecuteRequest):
    try:
        result = await run_in_threadpool(sandbox.execute, req.code, req.files, req.timeout_s)
    except (ValueError, binascii.Error) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ExecuteResponse(**result.__dict__)


# ── /forecast ───────────────────────────────────────────────────────────────

class ForecastRequest(BaseModel):
    csv_b64: str = Field(description="The dataset, base64-encoded CSV")
    date_column: str
    target_column: str
    frequency: Literal["monthly"] = "monthly"
    model: Literal["lightgbm", "random_forest"] = "lightgbm"
    rate_column: str | None = None
    rate_override: float | None = Field(default=None, ge=0)


def _run_forecast(req: ForecastRequest) -> dict:
    try:
        raw = base64.b64decode(req.csv_b64, validate=True)
        df = pd.read_csv(io.BytesIO(raw))
    except (binascii.Error, ValueError, pd.errors.ParserError) as exc:
        raise forecasting.ForecastError(f"Could not read the dataset as CSV: {exc}") from exc
    return forecasting.forecast(
        df,
        date_column=req.date_column,
        target_column=req.target_column,
        model_name=req.model,
        rate_column=req.rate_column,
        rate_override=req.rate_override,
    )


@app.post("/forecast")
async def forecast(req: ForecastRequest):
    try:
        return await run_in_threadpool(_run_forecast, req)
    except forecasting.ForecastError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

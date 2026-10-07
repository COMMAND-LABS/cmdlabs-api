"""
Monthly data files for the forecast and code-execution tools: upload a CSV,
list what is there. Mounted at /api/datasets, gated by the `agents` module
(modules_registry): uploading data is part of building an agent, and the
attach route below edits an agent the caller owns.

A CSV lands at datasets/<name>.csv in the UPLOADER's own GCS bucket, which is
exactly where the timeSeriesForecast / codeExecution tools read an agent
owner's datasets from (agent_runtime/tools/datasets.py). So an agent's owner
uploads here and points the forecast tool's `dataset.gcsPath` at the result;
the response carries a ready-to-paste tool config for that.

Monthly data: one row per month, a date column and at least one numeric
column. The runner needs MIN_ROWS rows to train (runner/runner/forecast.py).

So nobody has to copy JSON or type a storage path, two more routes close the
loop: GET /columns reads back a stored file's columns (for the forecast
ability form's dropdowns), and POST /attach writes the forecast tool straight
into an agent the caller owns.
"""
import asyncio
import copy
import csv
import datetime as dt
import io
import logging
import re

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile, status
from google.api_core.exceptions import NotFound
from jsonschema import ValidationError as JsonSchemaValidationError
from pydantic import BaseModel, Field

from src.deps import auth_dependency, db_dependency, org_dependency
from src.rate_limit import limiter
from src.routers.agents._shared import owned_agent_or_404
from src.schemas import validate_against_schema
from src.services import account_gcs_service
from src.services.account_gcs_service import AccountGcsCredentialMissing

logger = logging.getLogger(__name__)

router = APIRouter()

PREFIX = "datasets/"
DEFAULT_TOOL_NAME = "forecast_duty_spend"
MAX_BYTES = 10 * 1024 * 1024
MIN_ROWS = 32      # runner/runner/forecast.py: max(LAGS) + 20
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\.csv$")


class DatasetError(ValueError):
    """A problem with an uploaded CSV, in a message written here for the uploader."""


def _parse_date(value: str) -> dt.date | None:
    value = value.strip()
    for fmt in ("%Y-%m-%d", "%Y-%m", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return dt.datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _is_number(value: str) -> bool:
    try:
        float(value.replace(",", "").replace("$", "").strip())
        return True
    except ValueError:
        return False


def inspect_csv(data: bytes) -> dict:
    """Columns, row count, date range, and the forecast tool config this file
    supports. Raises DatasetError with a message a person can act on."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise DatasetError("The file is not UTF-8 text.")
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        raise DatasetError("The CSV has a header but no rows.")
    columns = [c for c in (rows[0].keys()) if c]

    def share(col, test):
        vals = [r.get(col) or "" for r in rows]
        return sum(1 for v in vals if v and test(v)) / len(vals)

    date_cols = [c for c in columns if share(c, lambda v: _parse_date(v) is not None) >= 0.95]
    if not date_cols:
        raise DatasetError("No date column found (expected values like 2025-01-01 or 2025-01).")
    date_col = date_cols[0]
    numeric = [c for c in columns if c != date_col and share(c, _is_number) >= 0.95]
    if not numeric:
        raise DatasetError("No numeric column found to forecast.")
    if len(rows) < MIN_ROWS:
        raise DatasetError(f"The forecast needs at least {MIN_ROWS} monthly rows; this file has {len(rows)}.")

    dates = sorted(d for d in (_parse_date(r[date_col] or "") for r in rows) if d)
    rate_col = next((c for c in numeric if "rate" in c.lower()), None)
    # Forecast the value the rate applies to (import_value), not the rate, and
    # not a column that is already value x rate (duty_spend).
    non_rate = [c for c in numeric if c != rate_col] or numeric
    target = next((c for c in non_rate if "spend" not in c.lower()), non_rate[0])
    return {
        "columns": columns,
        "rows": len(rows),
        "date_column": date_col,
        "numeric_columns": numeric,
        "first_period": dates[0].isoformat(),
        "last_period": dates[-1].isoformat(),
        "suggested": {"target_column": target, "rate_column": rate_col},
    }


def tool_config(gcs_path: str, info: dict) -> dict:
    cfg = {
        "type": "timeSeriesForecast",
        "name": DEFAULT_TOOL_NAME,
        "dataset": {"gcsPath": gcs_path},
        "dateColumn": info["date_column"],
        "targetColumn": info["suggested"]["target_column"],
        # Duty data is import value and duty in dollars (see the template).
        "currency": "USD",
    }
    if info["suggested"]["rate_column"]:
        cfg["rate"] = {"column": info["suggested"]["rate_column"], "outputName": "duty_spend"}
    return cfg


def _gcs_error(e: AccountGcsCredentialMissing):
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


def _inspect_or_400(data: bytes) -> dict:
    try:
        return inspect_csv(data)
    except DatasetError as e:
        raise HTTPException(status_code=400, detail=str(e))


def _checked_path(path: str) -> str:
    """A stored dataset path, datasets/<name>.csv, or 400. Never lets a caller
    reach outside datasets/ in their bucket."""
    path = (path or "").strip()
    name = path[len(PREFIX):] if path.startswith(PREFIX) else ""
    if not name or ".." in path or not _NAME.match(name):
        raise HTTPException(status_code=400, detail=(
            "Choose a file under datasets/ with a .csv name made of letters, "
            "digits, '.', '_' or '-'."))
    return path


async def _stored_info(db, account_id: int, path: str) -> dict:
    """Download a stored dataset from the caller's own bucket and inspect it."""
    try:
        data = await asyncio.to_thread(
            account_gcs_service.download_bytes, db, account_id,
            gcs_file_path=path, max_bytes=MAX_BYTES)
    except AccountGcsCredentialMissing as e:
        raise _gcs_error(e)
    except NotFound:
        raise HTTPException(status_code=404, detail=f"{path} was not found in your storage.")
    except ValueError:
        raise HTTPException(status_code=400, detail="File exceeds the 10 MB limit")
    return _inspect_or_400(data)


@router.get("")
@limiter.limit("30/minute")
async def list_datasets(db: db_dependency, auth: auth_dependency, org: org_dependency,
                        request: Request):
    try:
        objects = account_gcs_service.list_objects(db, org.account_id, prefix=PREFIX)
    except AccountGcsCredentialMissing as e:
        raise _gcs_error(e)
    return [o for o in objects if o["path"].lower().endswith(".csv")]


@router.post("", status_code=status.HTTP_201_CREATED)
@limiter.limit("20/minute")
async def upload_dataset(
    db: db_dependency, auth: auth_dependency, org: org_dependency, request: Request,
    file: UploadFile = File(...),
    replace: bool = Form(False),
):
    name = (file.filename or "").strip()
    if not _NAME.match(name):
        raise HTTPException(status_code=400, detail=(
            "Use a .csv file name made of letters, digits, '.', '_' or '-'."))
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="File is empty")
    if len(data) > MAX_BYTES:
        raise HTTPException(status_code=400, detail="File exceeds the 10 MB limit")
    info = _inspect_or_400(data)

    path = PREFIX + name
    try:
        if not replace and account_gcs_service.object_exists(db, org.account_id, gcs_file_path=path):
            raise HTTPException(status_code=409, detail=(
                f"{path} already exists. Tick 'Replace' to overwrite it; agents "
                "reading it will use the new data from their next forecast."))
        ref = account_gcs_service.upload_bytes(db, org.account_id, file_bytes=data,
                                               gcs_file_path=path, content_type="text/csv")
    except AccountGcsCredentialMissing as e:
        raise _gcs_error(e)

    return {"gcs_bucket": ref["gcs_bucket"], "gcs_file_path": path, **info,
            "tool_config": tool_config(path, info)}


@router.get("/columns")
@limiter.limit("30/minute")
async def dataset_columns(db: db_dependency, auth: auth_dependency, org: org_dependency,
                          request: Request, path: str = Query(...)):
    """Columns and suggestions for a file already uploaded, for the forecast
    ability form. Same checks as an upload, read from the caller's bucket."""
    path = _checked_path(path)
    info = await _stored_info(db, org.account_id, path)
    return {"path": path, **info}


class AttachRequest(BaseModel):
    path: str
    agent_id: int
    name: str | None = Field(default=None, max_length=64)


@router.post("/attach")
@limiter.limit("10/minute")
async def attach_dataset(body: AttachRequest, db: db_dependency, auth: auth_dependency,
                         org: org_dependency, request: Request):
    """Give an agent the caller OWNS a Forecast ability over a stored file.

    Replaces the agent's forecast tool of the same name (so re-attaching a new
    month's file is idempotent), else appends one. The file is read from the
    caller's bucket, which is the owner's bucket the tool reads at run time.
    """
    path = _checked_path(body.path)
    agent = owned_agent_or_404(db, body.agent_id, org)

    config = agent.config
    if (not isinstance(config, dict) or config.get("version") != 4
            or not isinstance(config.get("data"), dict)):
        raise HTTPException(status_code=400, detail=(
            "This agent's settings are in an older format. Open the agent and "
            "save it once, then try again."))

    info = await _stored_info(db, org.account_id, path)
    tool = tool_config(path, info)
    if body.name and body.name.strip():
        tool["name"] = body.name.strip()

    new_config = copy.deepcopy(config)
    tools = new_config["data"].get("tools")
    if not isinstance(tools, list):
        tools = []
    replaced = False
    for i, t in enumerate(tools):
        if (isinstance(t, dict) and t.get("type") == "timeSeriesForecast"
                and t.get("name") == tool["name"]):
            tools[i] = tool
            replaced = True
            break
    if not replaced:
        tools.append(tool)
    new_config["data"]["tools"] = tools

    try:
        validate_against_schema(new_config, "agent_config", 4)
    except JsonSchemaValidationError:
        raise HTTPException(status_code=400, detail=(
            "The agent's settings would not be valid with this ability "
            "(check the ability name: lowercase letters, digits and '_')."))
    except FileNotFoundError:
        logger.warning("[DATASETS] agent_config v4 schema file not found")

    agent.config = new_config
    db.commit()
    return {"agent_id": agent.id, "agent_name": agent.name, "tool": tool, "replaced": replaced}

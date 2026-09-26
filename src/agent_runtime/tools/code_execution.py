"""codeExecution — run model-written Python in cmdlabs-runner.

Nothing executes here. The tool fetches the configured datasets, ships them
with the code to the runner, and returns what the code printed. The runner's
container is the boundary (no credentials, no network, one request per
instance); see runner/runner/sandbox.py and runner/service.yaml.

Datasets are fetched once per tool instance — a turn builds its tools once,
so repeated calls within a turn do not re-download.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.agent_runtime.runner_client import RunnerError, resolve_runner_client

from .datasets import DatasetRef, fetch_dataset, parse_dataset_ref, require_gcs_credential
from .sessions import resolve_session_factory

logger = logging.getLogger(__name__)

DEFAULT_NAME = "run_python"
DEFAULT_TIMEOUT_S = 60


class CodeExecutionInput(BaseModel):
    code: str = Field(description="Python source to run. Print what you want to see.")


def _default_description(datasets: list[DatasetRef]) -> str:
    text = (
        "Run Python code in an isolated sandbox. pandas, numpy, scikit-learn and lightgbm "
        "are installed. There is no network access. Print what you want to see; only "
        "stdout/stderr come back."
    )
    if datasets:
        names = ", ".join(d.filename for d in datasets)
        text += f" The working directory contains: {names}."
    return text


async def create_code_execution_tool(
    tool_config: dict[str, Any],
    account_id: int,
    db: Session,
    auth_token: str | None = None,
    **kwargs,
) -> StructuredTool:
    datasets = [parse_dataset_ref(d) for d in tool_config.get("datasets") or []]
    if len({d.filename for d in datasets}) != len(datasets):
        raise ValueError("codeExecution: dataset filenames must be unique")
    timeout_s = int(tool_config.get("timeoutSeconds") or DEFAULT_TIMEOUT_S)

    owner_account_id = kwargs.get("agent_owner_account_id", account_id)
    if datasets:
        require_gcs_credential(db, owner_account_id)
    runner = resolve_runner_client(kwargs)
    session_factory = resolve_session_factory(kwargs)

    cache: dict[str, bytes] = {}

    async def _files() -> dict[str, bytes]:
        for ref in datasets:
            if ref.filename not in cache:
                cache[ref.filename] = await fetch_dataset(session_factory, owner_account_id, ref)
        return dict(cache)

    async def _run(code: str) -> dict:
        try:
            files = await _files()
        except Exception as exc:
            logger.error("[CODE EXECUTION] dataset unavailable: %s", exc)
            return {"error": f"Could not read a dataset: {exc}"}
        try:
            return await runner.execute(code, files, timeout_s)
        except RunnerError as exc:
            return {"error": exc.detail}

    return StructuredTool.from_function(
        coroutine=_run,
        name=tool_config.get("name") or DEFAULT_NAME,
        description=tool_config.get("description") or _default_description(datasets),
        args_schema=CodeExecutionInput,
    )

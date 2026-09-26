"""HTTP client for cmdlabs-runner, the isolated compute service.

Two agent tools (codeExecution, timeSeriesForecast) do their real work over
there; this is the only place in the API that knows how to reach it. Keeping
that knowledge in one class is what makes the runner swappable — a managed
sandbox vendor would be a second implementation of these two methods.

Configuration (environment):
  RUNNER_URL        base URL of the runner service. Unset = tools that need
                    it are not built (logged at build time), the agent still
                    runs with its other tools.
  RUNNER_AUTH_MODE  "iam" (default): send a Google ID token for RUNNER_URL,
                    minted from this service's identity — on Cloud Run that is
                    the API's service account, which holds run.invoker on the
                    runner. "none": no auth header, for a local runner.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Above the largest per-call budget the runner accepts (120s) plus its own
# startup slack; a request that outlives this is a runner problem, not a
# slow tool.
_HTTP_TIMEOUT_S = 150.0


class RunnerError(Exception):
    """The runner refused or failed a request. `detail` is safe to show the
    model — it is the runner's own explanation (bad column, timeout...)."""

    def __init__(self, detail: str, status: int | None = None):
        self.detail = detail
        self.status = status
        super().__init__(detail)


class RunnerClient:
    def __init__(self, base_url: str, *, auth_mode: str = "iam"):
        self.base_url = base_url.rstrip("/")
        self.auth_mode = auth_mode

    @classmethod
    def from_env(cls) -> RunnerClient | None:
        url = os.getenv("RUNNER_URL", "").strip()
        if not url:
            return None
        return cls(url, auth_mode=os.getenv("RUNNER_AUTH_MODE", "iam").strip().lower())

    # ── Public operations ───────────────────────────────────────────────────

    async def execute(self, code: str, files: dict[str, bytes], timeout_s: int) -> dict[str, Any]:
        """Run code with `files` in its working directory. Returns the
        runner's {stdout, stderr, returncode, timed_out}."""
        payload = {
            "code": code,
            "files": {name: base64.b64encode(data).decode() for name, data in files.items()},
            "timeout_s": timeout_s,
        }
        return await self._post("/execute", payload)

    async def forecast(self, csv_bytes: bytes, **spec: Any) -> dict[str, Any]:
        """Forecast the next period. `spec` is the runner's ForecastRequest
        minus csv_b64: date_column, target_column, model, rate_column,
        rate_override, frequency."""
        payload = {"csv_b64": base64.b64encode(csv_bytes).decode(), **spec}
        return await self._post("/forecast", payload)

    # ── Transport ───────────────────────────────────────────────────────────

    async def _headers(self) -> dict[str, str]:
        if self.auth_mode == "none":
            return {}
        if self.auth_mode != "iam":
            raise RunnerError(f"Unknown RUNNER_AUTH_MODE {self.auth_mode!r}")
        # google-auth's fetch is blocking (metadata server round-trip, cached
        # thereafter by the library); keep it off the event loop.
        token = await asyncio.to_thread(_fetch_id_token, self.base_url)
        return {"Authorization": f"Bearer {token}"}

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        headers = await self._headers()
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S) as client:
                resp = await client.post(f"{self.base_url}{path}", json=payload, headers=headers)
        except httpx.HTTPError as exc:
            logger.error("[RUNNER] %s unreachable: %s", path, exc)
            raise RunnerError("The compute service could not be reached.") from exc

        if resp.status_code == 422:
            # The runner's own validation message — meant for the model.
            raise RunnerError(_detail(resp), status=422)
        if resp.status_code >= 400:
            logger.error("[RUNNER] %s -> %s: %s", path, resp.status_code, resp.text[:500])
            raise RunnerError(f"The compute service returned an error ({resp.status_code}).",
                              status=resp.status_code)
        return resp.json()


def _detail(resp: httpx.Response) -> str:
    try:
        detail = resp.json().get("detail")
    except ValueError:
        detail = None
    return detail if isinstance(detail, str) else resp.text[:500]


def _fetch_id_token(audience: str) -> str:
    from google.auth.transport.requests import Request
    from google.oauth2 import id_token

    return id_token.fetch_id_token(Request(), audience)


def resolve_runner_client(kwargs: dict[str, Any]) -> RunnerClient:
    """Injected client for tests, else the environment's. Raises ValueError
    when none is configured so the factory skips the tool with a clear log."""
    client = kwargs.get("runner_client") or RunnerClient.from_env()
    if client is None:
        raise ValueError("RUNNER_URL is not configured; this tool needs the runner service.")
    return client

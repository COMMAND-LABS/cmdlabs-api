# cmdlabs-runner

The API's isolated compute service. Runs model-written Python and trains
forecast models so neither ever happens inside `cmdlabs-api`.

See `READMEs/runner/README.md` at the repo root for the deploy and one-time
GCP setup. This directory is self-contained: it does not import from `src/`.

```
POST /execute   {code, files: {name: b64}, timeout_s}  -> {stdout, stderr, returncode, timed_out}
POST /forecast  {csv_b64, date_column, target_column, model, rate_column?, rate_override?}
```

Local:

```
cd runner
uv venv && uv pip install -e '.[dev]'
uv run pytest
uv run uvicorn runner.app:app --port 8081
```

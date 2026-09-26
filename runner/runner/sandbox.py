"""Run model-written Python in a child process, one request at a time.

The real security boundary is the Cloud Run container this service runs in:
no roles on its service account, no secrets in its environment, no route to
the internet, and one request per instance. What this module adds is the
per-request hygiene inside that boundary:

- a fresh temp directory as the working directory, deleted afterwards, so one
  request's files never meet the next request's code;
- a scrubbed environment (no inherited variables at all beyond PATH/HOME);
- its own session, so a timeout kills the whole process group, not just the
  parent — a `subprocess.Popen` the code spawned dies with it;
- resource limits on CPU time, file size and open files.

Output is truncated to the last MAX_OUTPUT characters of each stream; the
model asked for what it printed, not a transcript.
"""

from __future__ import annotations

import base64
import os
import resource
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass

MAX_OUTPUT = 4000
MAX_FILE_BYTES = 64 * 1024 * 1024


@dataclass
class ExecutionResult:
    stdout: str
    stderr: str
    returncode: int
    timed_out: bool


def _limits(timeout_s: int):
    """Applied in the child before exec. Kept tiny: anything that raises here
    surfaces as a failed spawn rather than a hung request."""
    def apply():
        cpu = timeout_s + 5
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_FILE_BYTES, MAX_FILE_BYTES))
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    return apply


def _safe_name(name: str) -> str:
    """Files land flat in the working directory; a path component is a bug or
    an attempt to write outside it, and either way it is rejected."""
    base = os.path.basename(name)
    if not base or base != name or base in (".", ".."):
        raise ValueError(f"Invalid file name: {name!r}")
    return base


def execute(code: str, files: dict[str, str] | None = None, timeout_s: int = 60) -> ExecutionResult:
    """Run `code` with the runner's interpreter. `files` maps name -> base64
    content, placed in the working directory first."""
    with tempfile.TemporaryDirectory(prefix="run-") as workdir:
        for name, b64 in (files or {}).items():
            with open(os.path.join(workdir, _safe_name(name)), "wb") as f:
                f.write(base64.b64decode(b64))

        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": workdir,
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "MPLBACKEND": "Agg",
            # Quiet the threaded numeric libraries down to the CPUs we have.
            "OMP_NUM_THREADS": str(os.cpu_count() or 1),
        }

        proc = subprocess.Popen(
            [sys.executable, "-I", "-c", code],  # -I: isolated mode, no user site / env hooks
            cwd=workdir,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            preexec_fn=_limits(timeout_s),
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout_s)
            timed_out = False
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            stdout, stderr = proc.communicate()
            stderr = (stderr or "") + f"\nTimed out after {timeout_s}s"
            timed_out = True

        return ExecutionResult(
            stdout=(stdout or "")[-MAX_OUTPUT:],
            stderr=(stderr or "")[-MAX_OUTPUT:],
            returncode=proc.returncode if proc.returncode is not None else -1,
            timed_out=timed_out,
        )

"""Per-request hygiene of the sandbox: files, environment, timeouts, output."""

import base64
import os

import pytest

from runner.sandbox import MAX_OUTPUT, execute


def test_prints_come_back():
    out = execute("print('hello'); import sys; print('warn', file=sys.stderr)")
    assert out.returncode == 0
    assert out.stdout.strip() == "hello"
    assert out.stderr.strip() == "warn"
    assert out.timed_out is False


def test_files_are_placed_in_the_working_directory_and_cleaned_up():
    files = {"data.csv": base64.b64encode(b"a,b\n1,2\n").decode()}
    out = execute("import os; print(open('data.csv').read().strip()); print(os.getcwd())", files=files)
    assert out.stdout.splitlines()[:2] == ["a,b", "1,2"]
    workdir = out.stdout.splitlines()[-1]
    assert not os.path.exists(workdir)


def test_file_names_cannot_escape_the_working_directory():
    with pytest.raises(ValueError):
        execute("print(1)", files={"../evil.py": base64.b64encode(b"x").decode()})


def test_environment_is_scrubbed():
    os.environ["RUNNER_TEST_SECRET"] = "leak"
    try:
        out = execute("import os; print(os.environ.get('RUNNER_TEST_SECRET', 'absent'))")
    finally:
        del os.environ["RUNNER_TEST_SECRET"]
    assert out.stdout.strip() == "absent"


def test_timeout_kills_the_process_group():
    out = execute(
        "import subprocess, time, sys\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "time.sleep(30)",
        timeout_s=1,
    )
    assert out.timed_out is True
    assert "Timed out" in out.stderr


def test_output_is_truncated_to_the_tail():
    out = execute("print('x' * 10000)")
    assert len(out.stdout) <= MAX_OUTPUT + 1


def test_libraries_are_importable():
    out = execute("import pandas, sklearn, lightgbm; print('ok')")
    assert out.stdout.strip() == "ok", out.stderr

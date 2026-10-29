"""The Windows leg of CI's test job reports the Windows path tests' skips
(ENG-93 item 11).

`tests/test_sqlite_uri.py` opens a store through a UNC path
(``\\\\localhost\\C$\\...``), a verbatim path (``\\\\?\\C:\\...``) and a
device path (``\\\\.\\C:\\...``). Those opens run only on Windows, and the
UNC one skips where the runner's admin share does not answer. The suite's
``-q`` run counts a skip and drops its reason, so a leg where every UNC
case skipped read as green as one where each ran. Pinned here: the test
job keeps a Windows leg, and after its suite a step limited to Windows
runs the file holding those tests with ``-rs`` (each skip and its reason
in the log), with nothing deselected, and runs when the suite failed too.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

import yaml

_ROOT = Path(__file__).resolve().parents[1]
CI = _ROOT / ".github" / "workflows" / "ci.yml"
#: The tests the step is there to report on, by the file that holds them.
WINDOWS_PATH_TESTS = (
    "def test_a_store_on_a_unc_path_opens_at_every_site(",
    "def test_a_store_named_by_a_device_path_opens_at_every_site(",
)
#: Options that would run less than the named files.
NARROWING = ("-k", "-m", "--deselect", "--lf", "--last-failed", "-x", "--exitfirst")


def _test_job() -> dict[str, Any]:
    workflow = yaml.safe_load(CI.read_text(encoding="utf-8"))
    job = workflow["jobs"]["test"]
    assert isinstance(job, dict)
    return job


def _windows_path_step(steps: list[dict[str, Any]]) -> tuple[int, dict[str, Any]]:
    found = [
        (index, step)
        for index, step in enumerate(steps)
        if "runner.os == 'Windows'" in str(step.get("if", ""))
        and "tests/test_sqlite_uri.py" in str(step.get("run", ""))
    ]
    assert len(found) == 1, [step.get("name") for _, step in found]
    return found[0]


def _pytest_args(run: str) -> list[str]:
    words = shlex.split(run)
    assert words[:3] == ["uv", "run", "pytest"], words
    return words[3:]


def test_the_test_job_keeps_a_windows_leg() -> None:
    include = _test_job()["strategy"]["matrix"]["include"]
    assert any(str(leg["os"]).startswith("windows") for leg in include), include


def test_a_windows_step_runs_the_path_tests_with_their_skip_reasons() -> None:
    _, step = _windows_path_step(_test_job()["steps"])
    args = _pytest_args(step["run"])
    reports = [arg[2:] for arg in args if arg.startswith("-r") and len(arg) > 2]
    assert any(set(chars) & set("saA") for chars in reports), args
    targets = [arg for arg in args if not arg.startswith("-") and arg.endswith(".py")]
    assert targets, args
    held = "".join((_ROOT / target).read_text(encoding="utf-8") for target in targets)
    for test in WINDOWS_PATH_TESTS:
        assert test in held, (test, targets)
    narrowed = [arg for arg in args if arg.split("=", 1)[0] in NARROWING]
    assert not narrowed, narrowed


def test_the_windows_step_runs_after_the_suite_even_when_it_failed() -> None:
    steps = _test_job()["steps"]
    index, step = _windows_path_step(steps)
    suite = [i for i, s in enumerate(steps) if s.get("name") == "Test"]
    assert suite and suite[0] < index, (suite, index)
    condition = str(step["if"])
    assert "!cancelled()" in condition or "always()" in condition, condition

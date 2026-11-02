"""tools/ci/previous_release.py: the published release CI's cross-release job
runs against this checkout, and the version the checkout is built as beside
it (ENG-107). The choice is a pure function of PyPI's JSON for the project,
read here from documents shaped like it."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from .test_shim_versions import next_candidate as shim_next_candidate

REPO = Path(__file__).resolve().parents[1]


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        name, REPO / "tools" / "ci" / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


previous_release = _load("previous_release")
cross_release_builds = _load("cross_release_builds")


def _file(kind: str, uploaded: str, *, yanked: bool = False) -> dict[str, Any]:
    return {
        "packagetype": kind,
        "upload_time_iso_8601": uploaded,
        "yanked": yanked,
    }


def _released(uploaded: str = "2026-10-31T20:38:13.747704Z") -> list[dict[str, Any]]:
    return [_file("bdist_wheel", uploaded), _file("sdist", uploaded)]


def _pypi(*versions: str, **files: list[dict[str, Any]]) -> dict[str, Any]:
    """PyPI's JSON for the project: each of `versions` with a wheel and an
    sdist, and each keyword (a version with its dots and letters spelled
    as written, `v9_0_0rc3` for 9.0.0rc3) with the files given."""
    releases: dict[str, Any] = {version: _released() for version in versions}
    for key, given in files.items():
        releases[key.removeprefix("v").replace("_", ".")] = given
    return {"info": {"version": "8.0.0"}, "releases": releases}


def _chosen(document: dict[str, Any], current: str, *skip: str) -> str | None:
    found = previous_release.previous_release(document, current=current, skip=skip)
    return None if found is None else found.version


ALL = ("7.19.2", "8.0.0", "9.0.0rc1", "9.0.0rc9", "9.0.0rc10", "9.0.0rc14", "9.0.0rc15")


def test_the_newest_release_at_or_below_the_checkout_in_its_major() -> None:
    document = _pypi(*ALL)
    assert _chosen(document, "9.0.0rc15") == "9.0.0rc15"
    assert _chosen(document, "9.0.0rc16") == "9.0.0rc15"
    # rc10 sorts after rc9, as PEP 440 orders them, not as text does.
    assert _chosen(_pypi("9.0.0rc9", "9.0.0rc10"), "9.0.0rc11") == "9.0.0rc10"
    assert _chosen(document, "9.0.0rc12") == "9.0.0rc10"
    # The final 9.0.0 sorts after every candidate of it.
    assert _chosen(document, "9.0.0") == "9.0.0rc15"
    assert _chosen(document, "8.0.1") == "8.0.0"


def test_a_release_above_the_checkout_is_passed_over() -> None:
    assert _chosen(_pypi(*ALL, "9.0.0rc16", "9.0.0"), "9.0.0rc15") == "9.0.0rc15"


def test_a_version_the_head_tags_name_is_passed_over() -> None:
    document = _pypi(*ALL, "9.0.0rc16")
    assert _chosen(document, "9.0.0rc16", "9.0.0rc16") == "9.0.0rc15"
    # A tag that never reached PyPI changes nothing.
    assert _chosen(_pypi(*ALL), "9.0.0rc16", "9.0.0rc16") == "9.0.0rc15"


def test_no_release_in_the_checkouts_major_is_none() -> None:
    assert _chosen(_pypi(*ALL), "10.0.0rc1") is None
    assert _chosen(_pypi(*ALL), "9.0.0rc1", "9.0.0rc1") is None
    assert previous_release.output_lines("10.0.0rc1", None) == []


def test_a_release_without_a_live_wheel_is_passed_over() -> None:
    uploaded = "2026-10-30T00:20:43Z"
    document = _pypi(
        *ALL,
        v9_0_0rc16=[_file("sdist", uploaded)],
        v9_0_0rc17=[_file("bdist_wheel", uploaded, yanked=True)],
        v9_0_0rc18=[],
    )
    assert _chosen(document, "9.0.0rc18") == "9.0.0rc15"


def test_a_version_the_key_cannot_place_is_passed_over() -> None:
    document = _pypi("9.0.0rc15", "9.0.0.post1", "9.0.0+local", "not-a-version")
    assert _chosen(document, "9.0.0") == "9.0.0rc15"


def test_a_checkout_version_the_key_cannot_place_is_refused() -> None:
    with pytest.raises(ValueError, match="cannot place"):
        _chosen(_pypi(*ALL), "9.0.0.post1")


def test_the_upload_time_is_the_newest_live_wheels() -> None:
    document = _pypi(
        v9_0_0rc15=[
            _file("bdist_wheel", "2026-10-31T20:38:13.747704Z"),
            _file("bdist_wheel", "2026-10-31T21:00:00Z", yanked=True),
            _file("sdist", "2026-10-31T22:00:00Z"),
        ]
    )
    found = previous_release.previous_release(document, current="9.0.0rc15")
    assert found is not None
    assert found.uploaded == datetime(
        2026, 10, 31, 20, 38, 13, 747704, tzinfo=timezone.utc
    )
    assert previous_release.exclude_newer(found) == "2026-10-31T20:39:14Z"


def test_the_checkout_builds_as_the_next_candidate_beside_its_own_version() -> None:
    document = _pypi(*ALL)
    lines = previous_release.output_lines(
        "9.0.0rc15", previous_release.previous_release(document, current="9.0.0rc15")
    )
    assert lines == [
        "version=9.0.0rc15",
        "uploaded=2026-10-31T20:38:13Z",
        "exclude_newer=2026-10-31T20:39:14Z",
        "build=9.0.0rc16",
    ]
    found = previous_release.previous_release(document, current="9.0.0rc16")
    assert previous_release.build_version("9.0.0rc16", found) == "9.0.0rc16"


@pytest.mark.parametrize(
    "version",
    ["9.0.0rc15", "9.0.0", "8.0.0", "9.1.0.dev3", "9.0.0rc2.dev1", "10.0.0rc9"],
)
def test_the_next_candidate_is_the_one_the_shim_tests_use(version: str) -> None:
    assert previous_release.next_candidate(version) == shim_next_candidate(version)
    key = previous_release.version_key()
    assert key(previous_release.next_candidate(version)) > key(version)


def test_main_reads_a_document_and_prints_the_choice(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "pypi.json"
    path.write_text(json.dumps(_pypi(*ALL, "9.0.0rc16")), encoding="utf-8")
    argv = ["--pypi-json", str(path), "--version", "9.0.0rc16", "--skip", "9.0.0rc16"]
    assert previous_release.main(argv) == 0
    assert capsys.readouterr().out.splitlines() == [
        "version=9.0.0rc15",
        "uploaded=2026-10-31T20:38:13Z",
        "exclude_newer=2026-10-31T20:39:14Z",
        "build=9.0.0rc16",
    ]
    assert previous_release.main(["--pypi-json", str(path), "--version", "10.0.0"]) == 0
    assert capsys.readouterr().out == ""
    assert previous_release.main(["--pypi-json", str(path), "--version", "x"]) == 1


def test_the_cross_release_job_names_the_installs_the_test_reads() -> None:
    """The job's test step sets both variables tests/test_cross_release.py
    reads, under the names the builds script prints, so a rename cannot
    leave the test skipped on a green job."""
    from . import test_cross_release

    names = {test_cross_release.PREVIOUS_ENV, test_cross_release.NEXT_ENV}
    assert names == {cross_release_builds.PREVIOUS_ENV, cross_release_builds.NEXT_ENV}
    workflow = yaml.safe_load((REPO / ".github" / "workflows" / "ci.yml").read_text())
    steps = workflow["jobs"]["cross-release"]["steps"]
    runs = [
        step for step in steps if "tests/test_cross_release.py" in str(step.get("run"))
    ]
    assert len(runs) == 1, runs
    assert names <= set(runs[0].get("env") or {}), runs[0]

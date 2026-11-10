"""POSIX backslashes survive stored views and read-only folder lookup."""

from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from bettermemory import _location_probe, _paths
from bettermemory import location, location_git, membership_records
from bettermemory.cli import project_cmd
from bettermemory.membership import LIVE, REPLACED, View

from .conftest import shielded_child_env


@pytest.mark.parametrize("path", ["/work/trail\\", "/work/back\\\\", "/work/\\"])
def test_a_posix_view_keeps_its_final_backslash(path: str) -> None:
    assert _paths.norm(path) == path
    assert _location_probe.view_path(path) == os.path.realpath(path)


@pytest.mark.skipif(sys.platform == "win32", reason="backslash separates on Windows")
def test_local_remote_keys_do_not_conflate_backslash_names(tmp_path: Path) -> None:
    path = tmp_path / "trail\\"
    sibling = tmp_path / "trail"
    path.mkdir()
    sibling.mkdir()
    expected = "local:" + str(path)
    assert location.remote_key(str(path)) == expected
    assert location.remote_key(path.as_uri()) == expected
    assert location.remote_key(str(sibling)) != expected


@pytest.mark.parametrize("path", ["/work/trail\\", "/work/\\", "C:\\work\\app\\"])
def test_project_labels_keep_the_recorded_component(path: str) -> None:
    expected = {"/work/trail\\": "trail\\", "/work/\\": "\\", "C:\\work\\app\\": "app"}[
        path
    ]
    checkout = SimpleNamespace(newest_view=SimpleNamespace(path=path))
    assert membership_records._basename(path) == expected
    assert project_cmd._folder_name(cast(Any, checkout), path) == expected


@pytest.mark.skipif(sys.platform == "win32", reason="backslash separates on Windows")
def test_a_folder_manifest_keeps_its_literal_name(tmp_path: Path) -> None:
    root = tmp_path / "trail\\"
    root.mkdir()
    name, _ = location_git._manifest(str(root))
    assert name == "trail\\"


@pytest.mark.parametrize(
    ("path", "root", "expected"),
    [
        ("/work/trail\\/src/x.py", "/work/trail\\", True),
        ("/work/trail\\/src/x.py", "/work/trail", False),
        ("/work/app\\sibling", "/work/app", False),
        ("/work/app/src/x.py", "/work/app", True),
        ("C:\\work\\app\\src\\x.py", "C:\\work\\app\\", True),
        ("C:/work/app/src/x.py", "C:/work/app/", True),
        ("/work/app", "/", True),
        ("C:\\work\\app", "C:\\", True),
    ],
)
def test_containment_reads_the_separators_of_the_recorded_root(
    path: str, root: str, expected: bool
) -> None:
    assert _paths.inside(path, root) is expected


def _run(
    argv: list[str], cwd: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=60
    )


def _bm(cwd: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return _run([sys.executable, "-m", "bettermemory", *args], cwd, env)


def _question(
    tmp_path: Path, name: str, *, legacy: bool, sibling_repo: bool = False
) -> tuple[Path, str, dict[str, str]]:
    env = shielded_child_env()
    env.update(
        GIT_AUTHOR_NAME="Fixture",
        GIT_AUTHOR_EMAIL="fixture@example.invalid",
        GIT_COMMITTER_NAME="Fixture",
        GIT_COMMITTER_EMAIL="fixture@example.invalid",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
    )
    for key, folder in (
        ("HOME", "home"),
        ("BETTERMEMORY_DIR", "store"),
        ("BETTERMEMORY_KEYS_DIR", "keys"),
        ("BETTERMEMORY_STATE_DIR", "state"),
    ):
        target = tmp_path / folder
        target.mkdir()
        env[key] = str(target)
    env.pop("XDG_CONFIG_HOME", None)
    source = tmp_path / "source"
    source.mkdir()
    for args in (
        ("init", "-q"),
        ("commit", "-q", "--allow-empty", "-m", "one"),
        ("commit", "-q", "--allow-empty", "-m", "two"),
    ):
        done = _run(["git", *args], source, env)
        assert done.returncode == 0, done.stderr
    done = _bm(source, env, "project", "new")
    assert done.returncode == 0, done.stderr
    clone = tmp_path / name
    made = _run(["git", "clone", "-q", str(source), str(clone)], tmp_path, env)
    assert made.returncode == 0, made.stderr
    sibling = tmp_path / name.rstrip("\\")
    sibling.mkdir(exist_ok=True)
    assert not os.path.samefile(clone, sibling)
    if sibling_repo:
        other = _run(["git", "init", "-q"], sibling, env)
        assert other.returncode == 0, other.stderr
    if legacy:
        code = (
            "import sys; import bettermemory.membership as mb; "
            "mb._norm=lambda p: p.rstrip('/\\\\') or p; "
            "from bettermemory._entry import main; "
            "sys.argv=['bettermemory',*sys.argv[1:]]; main()"
        )
        _run([sys.executable, "-c", code, "project", "join", "NOPE"], clone, env)
    else:
        _bm(clone, env, "project", "join", "NOPE")
    pending = _bm(tmp_path, env, "project", "pending")
    assert pending.returncode == 0, pending.stderr
    found = re.search(r"^(Q\d+) .*first_sight", pending.stdout, re.M)
    assert found, pending.stdout
    return clone, found.group(1), env


@pytest.mark.skipif(sys.platform == "win32", reason="backslash separates on Windows")
@pytest.mark.parametrize("name", ["trail\\", "back\\\\", "\\"])
@pytest.mark.parametrize("legacy", [False, True])
def test_lookup_keeps_new_and_legacy_views_distinct_from_their_sibling(
    tmp_path: Path, name: str, legacy: bool
) -> None:
    clone, qid, env = _question(tmp_path, name, legacy=legacy)
    database = Path(env["BETTERMEMORY_DIR"]) / "memory.sqlite"
    before = database.read_bytes()
    looked = _bm(tmp_path, env, "project", "folder", qid)
    assert looked.returncode == 0, looked.stderr
    document = json.loads(looked.stdout)
    assert base64.b64decode(
        document["path_bytes_base64"], validate=True
    ) == os.fsencode(clone)
    assert document["path"] == str(clone)
    assert database.read_bytes() == before, "the folder lookup remains read-only"
    if legacy:
        # Normal use in the recovered checkout records its exact view;
        # no rewrite of the earlier signed row or manual repair command.
        _bm(clone, env, "project", "join", "NOPE")
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
            (latest,) = db.execute(
                "SELECT path FROM checkout_views ORDER BY seen_order DESC LIMIT 1"
            ).fetchone()
        assert latest == str(clone)


@pytest.mark.skipif(sys.platform == "win32", reason="backslash separates on Windows")
def test_a_legacy_view_never_selects_a_sibling_after_its_checkout_is_gone(
    tmp_path: Path,
) -> None:
    import shutil

    clone, qid, env = _question(tmp_path, "trail\\", legacy=True)
    shutil.rmtree(clone)
    looked = _bm(tmp_path, env, "project", "folder", qid)
    assert looked.returncode == 2
    assert looked.stdout == ""


@pytest.mark.skipif(sys.platform == "win32", reason="backslash separates on Windows")
def test_legacy_recovery_rejects_a_different_repository_at_the_stripped_path(
    tmp_path: Path,
) -> None:
    clone, qid, env = _question(tmp_path, "trail\\", legacy=True, sibling_repo=True)
    looked = _bm(tmp_path, env, "project", "folder", qid)
    assert looked.returncode == 0, looked.stderr
    document = json.loads(looked.stdout)
    assert base64.b64decode(
        document["path_bytes_base64"], validate=True
    ) == os.fsencode(clone)


@pytest.mark.skipif(sys.platform == "win32", reason="backslash separates on Windows")
@pytest.mark.parametrize("fault", ["ambiguous", "unanswered", "no identity"])
def test_legacy_recovery_requires_one_proven_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fault: str,
) -> None:
    old = str(tmp_path / "trail")
    candidates = [str(tmp_path / "trail\\"), str(tmp_path / "trail\\\\")]
    for path in candidates:
        Path(path).mkdir()
    view = View("K001", "machine", old, "first", "last", 1)
    model = SimpleNamespace(
        view_state=lambda _checkout, seen: LIVE if seen.path in candidates else REPLACED
    )
    checkout = SimpleNamespace(
        id="K001", object_key=None if fault == "no identity" else object()
    )
    monkeypatch.setattr(project_cmd, "_backslash_folders", lambda _path: candidates)
    if fault == "unanswered":
        monkeypatch.setattr(location, "bounded_read", lambda *_: location.UNANSWERED)
    with pytest.raises(SystemExit) as exc:
        project_cmd._checkout_folder(cast(Any, model), cast(Any, checkout), view)
    assert exc.value.code == 2
    out = capsys.readouterr()
    assert out.out == "" and "does not identify one local checkout" in out.err

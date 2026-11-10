"""A membership command takes its directory from data, never shell text.

The read-only lookup keeps line breaks and non-UTF-8 filesystem names.
Real POSIX shells and PowerShell carry its JSON to a tool which sets a
child's working directory without evaluating any part of the name.
"""

from __future__ import annotations

import base64
import errno
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import replace
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from bettermemory import location, membership_lines, project_file
from bettermemory.membership_records import (
    DUPLICATE_CHECKOUT,
    FILE_CONTRADICTION,
    FIRST_SIGHT,
    IMPORT,
    LEGACY,
    REMOTE_DIVERGED,
    SUBPROJECT_CONTRADICTION,
)

from .conftest import shielded_child_env
from .test_membership_cli import Scene, bm, scene

__all__ = ["scene"]

_KINDS = (
    FIRST_SIGHT,
    FILE_CONTRADICTION,
    REMOTE_DIVERGED,
    SUBPROJECT_CONTRADICTION,
    DUPLICATE_CHECKOUT,
    LEGACY,
    IMPORT,
)
_NAMES = (
    "$(touch PWNED)",
    "$(New-Item PWNED)",
    "`touch PWNED`",
    "both'and\"quotes",
    "embedded\nline\n",
    "carriage\rreturn",
    "wildcards[1]*?",
    "smart\u2019quote\u201c",
    os.fsdecode(b"bytes-\xff") if sys.platform != "win32" else "bytes-\udcff",
)


def _question(scene: Scene, kind: str, folder: str | None = None) -> str:
    app = scene.repo("app")
    res = scene.see(app)
    return scene.model.open_proposal(
        checkout=res.checkout,
        kind=kind,
        candidates=[str(res.project)],
        evidence={"dir": folder} if folder is not None else {},
        reason=kind,
        line='bettermemory: "old quoted path": init --new (in "old path")',
    ).id


def _document(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    out = capsys.readouterr()
    assert not out.err
    assert len(out.out.splitlines()) == 1 and out.out.isascii()
    result = json.loads(out.out)
    assert isinstance(result, dict)
    return result


def _path(document: dict[str, Any]) -> str:
    raw = base64.b64decode(document["path_bytes_base64"], validate=True)
    return os.fsdecode(raw)


@pytest.mark.parametrize("name", _NAMES)
@pytest.mark.parametrize("kind", _KINDS)
def test_every_question_kind_keeps_hostile_folders_out_of_shell_text(
    name: str, kind: str
) -> None:
    folder = "/work/" + name
    line = membership_lines.line_restated(
        "Q001", kind, kind, [("app", "P001")], at=folder, folder=folder
    )
    assert len(line.splitlines()) == 1
    assert "cd " not in line and "Set-Location" not in line
    assert "the user runs" not in line
    if "bettermemory project init" in line:
        assert "display only:" in line
        assert "bettermemory project folder Q001" in line
        assert "working directory, never paste the displayed name into a shell" in line
    assert 'id="Q001"' in line


@pytest.mark.parametrize("ref", [None, "Q001;touch PWNED", "$(touch PWNED)", "-x"])
def test_a_missing_or_malformed_reference_is_not_offered_as_a_shell_word(
    ref: str | None,
) -> None:
    line = membership_lines.run_in("/work/quotes'\"\n", ref)
    assert "bettermemory project folder" not in line
    assert "use its stored path" in line
    assert len(line.splitlines()) == 1


@pytest.mark.parametrize("kind", _KINDS)
def test_question_lookup_reads_the_target_checkout_instead_of_the_callers_folder(
    scene: Scene,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    kind: str,
) -> None:
    part = scene.root / "part"
    qid = _question(scene, kind, str(part))
    expected = part if kind == SUBPROJECT_CONTRADICTION else scene.root / "app"
    monkeypatch.chdir(scene.root)

    def resolved(*args: object, **kwargs: object) -> None:
        raise AssertionError("reading a stored path resolved the caller")

    monkeypatch.setattr(location, "resolve_here", resolved)
    before = scene.tables()
    rows = list(scene.store.membership_log())
    bm(monkeypatch, "project", "folder", qid)

    document = _document(capsys)
    assert document["path"] == str(expected)
    assert _path(document) == str(expected)
    assert scene.tables() == before
    assert list(scene.store.membership_log()) == rows


@pytest.mark.parametrize(
    "fault",
    [
        "answered",
        "gone",
        "no_checkout",
        "other_machine",
        "retired",
        "no_part",
        "relative",
    ],
)
def test_question_lookup_refuses_unusable_targets_without_stdout_or_changes(
    scene: Scene,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fault: str,
) -> None:
    qid = _question(scene, SUBPROJECT_CONTRADICTION, str(scene.root / "part"))
    model = scene.model
    pr = model.proposal(qid)
    assert pr.checkout is not None
    checkout = model.checkout(pr.checkout)
    if fault == "answered":
        model._close(pr, "new", "test")
    elif fault == "gone":
        qid = "Q999999"
    elif fault == "no_checkout":
        model._put_proposal(replace(pr, checkout=None))
    elif fault == "other_machine":
        monkeypatch.setattr(
            location, "process_machine", lambda: replace(scene.identity, id="elsewhere")
        )
    elif fault == "retired":
        model._put_checkout(replace(checkout, retired=True))
    elif fault == "no_part":
        model._put_proposal(replace(pr, evidence={}))
    elif fault == "relative":
        model._put_proposal(replace(pr, evidence={"dir": "relative"}))
    before = scene.tables()
    with pytest.raises(SystemExit) as exc:
        bm(monkeypatch, "project", "folder", qid)
    assert exc.value.code == 2
    out = capsys.readouterr()
    assert out.out == "" and len(out.err.splitlines()) == 1
    assert scene.tables() == before


def test_older_quoted_part_question_is_shown_with_the_new_directory_instructions(
    scene: Scene,
) -> None:
    folder = str(scene.root / "$(touch PWNED)'\n")
    qid = _question(scene, SUBPROJECT_CONTRADICTION, folder)
    old = scene.model.proposal(qid).line
    shown = next(pr.line for pr in scene.model.pending() if pr.id == qid)
    assert "restated from what it stores" in shown
    assert f"bettermemory project folder {qid}" in shown
    assert "never paste the displayed name into a shell" in shown
    assert scene.model.proposal(qid).line == old


def test_an_old_quoted_first_sight_with_a_file_command_gets_a_folder_lookup(
    scene: Scene, monkeypatch: pytest.MonkeyPatch
) -> None:
    qid = _question(scene, FIRST_SIGHT)
    model = scene.model
    pr = model.proposal(qid)
    old = 'bettermemory: "app"; separate: bettermemory project init --new (a fresh project file)'
    model._put_proposal(replace(pr, line=old))
    monkeypatch.setattr(
        "bettermemory.membership_queries.filed_elsewhere", lambda _m, _pr: True
    )
    shown = next(pr.line for pr in model.pending() if pr.id == qid)
    assert f"bettermemory project folder {qid}" in shown
    assert "never paste the displayed name into a shell" in shown
    assert model.proposal(qid).line == old


def test_a_question_lookup_leaves_a_closed_rollback_store_unchanged(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    qid = _question(scene, FIRST_SIGHT)
    store_path = scene.store.path
    scene.store.close()
    with closing(sqlite3.connect(store_path)) as db:
        assert db.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
    before = {
        str(p.relative_to(scene.root)): p.read_bytes()
        for p in scene.root.rglob("*")
        if p.is_file()
    }
    bm(monkeypatch, "project", "folder", qid)
    assert _path(_document(capsys)) == str(scene.root / "app")
    after = {
        str(p.relative_to(scene.root)): p.read_bytes()
        for p in scene.root.rglob("*")
        if p.is_file()
    }
    assert after == before


def test_a_file_lookup_needs_no_store_and_uses_the_nearest_matching_ancestor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    outer = tmp_path / "outer"
    inner = outer / "inner"
    child = inner / "child"
    child.mkdir(parents=True)
    written = project_file.write(outer, name="outer")
    shutil.copyfile(written.path, inner / project_file.PROJECT_FILE_NAME)
    monkeypatch.chdir(child)
    missing_store = tmp_path / "absent-store"
    monkeypatch.setenv("BETTERMEMORY_DIR", str(missing_store))
    bm(monkeypatch, "project", "folder", written.id)
    assert _path(_document(capsys)) == str(inner)
    assert not missing_store.exists()


def test_a_missing_question_lookup_creates_no_first_run_store_or_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "absent"
    monkeypatch.setenv("BETTERMEMORY_DIR", str(missing))
    with pytest.raises(SystemExit):
        bm(monkeypatch, "project", "folder", "Q001")
    assert capsys.readouterr().out == ""
    assert not missing.exists()


@pytest.mark.parametrize("name", _NAMES)
@pytest.mark.parametrize("shell", ["sh", "bash", "zsh", "pwsh"])
def test_real_shells_pass_folder_json_to_a_native_working_directory_without_expansion(
    tmp_path: Path, name: str, shell: str
) -> None:
    executable = shutil.which(shell)
    if executable is None:
        pytest.skip(f"{shell} is not installed")
    if sys.platform == "win32" and shell != "pwsh":
        pytest.skip("POSIX shells run on the Unix legs; Windows exercises PowerShell")
    if sys.platform == "win32" and any(c in name for c in '<>:"/\\|?*\n\r\udcff'):
        pytest.skip("the name is not a Windows directory name")
    target = tmp_path / name
    try:
        target.mkdir()
    except OSError as exc:
        if exc.errno not in (errno.EINVAL, errno.EILSEQ, errno.ENOTSUP):
            raise
        pytest.skip("the filesystem does not support this directory name")
    written = project_file.write(target, name="fixture")
    emitter = tmp_path / "emit.py"
    emitter.write_text(
        "import base64,os,sys\n"
        "from bettermemory.server import main\n"
        "os.chdir(os.fsdecode(base64.b64decode(os.environ['ENG234_TARGET'])))\n"
        "sys.argv=['bettermemory','project','folder',os.environ['ENG234_REF']]\n"
        "main()\n"
    )
    consumer = tmp_path / "consume.py"
    consumer.write_text(
        "import base64,json,os,subprocess,sys\n"
        "doc=json.load(sys.stdin)\n"
        "path=os.fsdecode(base64.b64decode(doc['path_bytes_base64'],validate=True))\n"
        "subprocess.run([sys.executable,'-c',"
        "'import os; print(os.fsencode(os.getcwd()).hex())'],cwd=path,check=True)\n"
    )
    env = shielded_child_env()
    env.update(
        ENG234_PYTHON=sys.executable,
        ENG234_REF=written.id,
        ENG234_EMITTER=str(emitter),
        ENG234_TARGET=base64.b64encode(os.fsencode(target)).decode("ascii"),
        ENG234_CONSUMER=str(consumer),
        POWERSHELL_TELEMETRY_OPTOUT="1",
    )
    if shell == "pwsh":
        script = (
            "$ErrorActionPreference='Stop'; "
            "$doc = & $env:ENG234_PYTHON $env:ENG234_EMITTER; "
            "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }; "
            "$doc | & $env:ENG234_PYTHON $env:ENG234_CONSUMER; exit $LASTEXITCODE"
        )
        command = [
            executable,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
        ]
    else:
        command = [
            executable,
            "-c",
            '"$ENG234_PYTHON" "$ENG234_EMITTER" | "$ENG234_PYTHON" "$ENG234_CONSUMER"',
        ]
    result = subprocess.run(
        command, env=env, cwd=tmp_path, capture_output=True, timeout=30
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert result.stdout.strip() == os.fsencode(target).hex().encode("ascii")
    assert not (tmp_path / "PWNED").exists()
    assert not (target / "PWNED").exists()


def test_folder_json_keeps_a_non_utf8_path_as_exact_filesystem_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    if sys.platform == "win32":
        pytest.skip("Windows paths are Unicode")
    folder = os.fsdecode(os.fsencode(tmp_path) + b"/bytes-\xff\n")
    monkeypatch.setattr(
        "bettermemory.cli.project_cmd._file_folder", lambda _ref: folder
    )
    bm(monkeypatch, "project", "folder", "bmp_fixture")
    document = _document(capsys)
    assert document["path"] is None
    assert os.fsencode(_path(document)) == os.fsencode(folder)


def test_a_forged_file_reference_stays_a_quoted_error_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        bm(monkeypatch, "project", "folder", 'bmp_"; touch PWNED\n')
    out = capsys.readouterr()
    assert out.out == "" and len(out.err.splitlines()) == 1
    assert not re.search(r"^touch", out.err, re.M)

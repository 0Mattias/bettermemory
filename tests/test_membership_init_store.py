"""`bettermemory project init` after it wrote the project file: every step
that reads or writes the store from there on, failing as the store fails,
ends in one line, never a traceback (ENG-207's review of round 10).

The file is written before the folder's first sight, the read of the
checkout that sight made, the read of the boundaries a rebind keeps
outside, the declaration and the report of it; `tests/test_membership_cli.py`
fails the first sight and the declaration, and these fail the rest. Each
line names the step, the error by its class (SQLite's apart from a file's,
so an error of a file the step read is not called the store's), the file
written and whether the declaration stands.
"""

from __future__ import annotations

import errno
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from bettermemory import project_file
from bettermemory import membership as mb
from bettermemory.cli import project_cmd
from bettermemory.history import quoted
from bettermemory.membership import GLOBAL
from bettermemory.store import Store

from .test_membership_cli import URL, Scene, _coded, bm, refused, scene, typed

__all__ = ["scene"]

_IO_READ = "sqlite3.OperationalError: disk I/O error"


def _io_read() -> Exception:
    return _coded(sqlite3.OperationalError, "disk I/O error", sqlite3.SQLITE_IOERR_READ)


def _failing(*args: Any, **kwargs: Any) -> Any:
    """A store read that fails as SQLite fails a read."""
    raise _io_read()


def _after_write(monkeypatch: pytest.MonkeyPatch, owner: Any, name: str) -> None:
    """`owner.name` fails (`_failing`) from the moment init has written the
    project file."""
    real = project_file.write

    def write(*args: Any, **kwargs: Any) -> project_file.ProjectFile:
        done = real(*args, **kwargs)
        monkeypatch.setattr(owner, name, _failing)
        return done

    monkeypatch.setattr(project_file, "write", write)


def _plain_folder(scene: Scene, monkeypatch: pytest.MonkeyPatch) -> Path:
    notes = scene.root / "notes"
    notes.mkdir()
    assert scene.see(notes).project == GLOBAL
    typed(monkeypatch, "notes")
    return notes


def test_the_read_of_the_checkout_the_first_sight_made(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The reviewer's case: the read of the checkout the folder's first
    sight made, between that sight's commit and the declaration, failing
    with an I/O error ended in a traceback, the file written."""
    notes = _plain_folder(scene, monkeypatch)
    real = project_cmd.caller
    calls: list[int] = []

    def caller(ctx: Any) -> Any:
        out = real(ctx)
        calls.append(1)
        if len(calls) == 2:
            monkeypatch.setattr(mb.Membership, "checkout", _failing)
        return out

    monkeypatch.setattr(project_cmd, "caller", caller)
    err = refused(monkeypatch, capsys, "project", "init")
    path = notes / project_file.PROJECT_FILE_NAME
    assert err == (
        f"bettermemory: the read of the checkout it made failed ({_IO_READ}); "
        f"wrote {quoted(str(path))} ({_file_id(path)}) and declared nothing: "
        "the next session decides"
    )


def _file_id(path: Path) -> str:
    found = project_file.parse(path)
    assert isinstance(found, project_file.ProjectFile)
    return found.id


def test_the_read_of_the_boundaries_a_rebind_keeps_outside(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A rebind reads the chain of the project it keeps, and the
    boundaries on the caller's chain outside it, once the file is
    rewritten."""
    app = scene.repo("app", remote=URL)
    scene.see(app)
    typed(monkeypatch, "app")
    bm(monkeypatch, "project", "init")
    capsys.readouterr()
    _after_write(monkeypatch, mb.Membership, "chain_of")
    typed(monkeypatch, "app")
    err = refused(monkeypatch, capsys, "project", "init", "--rebind")
    assert err.startswith(
        f"bettermemory: the read of the boundaries above it failed ({_IO_READ}); wrote "
    )
    assert err.endswith("declared nothing: the next session decides")


def test_the_report_of_the_declaration_says_it_stands(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The report reads the declaration's rows back once it committed: a
    read failing there says the file is written and declared, and the
    declaration is in the store."""
    notes = _plain_folder(scene, monkeypatch)
    real, log = mb.Membership.declare, Store.membership_log

    def declare(self: mb.Membership, decl: Any) -> Any:
        done = real(self, decl)
        monkeypatch.setattr(Store, "membership_log", _failing)
        return done

    monkeypatch.setattr(mb.Membership, "declare", declare)
    err = refused(monkeypatch, capsys, "project", "init")
    path = notes / project_file.PROJECT_FILE_NAME
    assert err == (
        f"bettermemory: the report of what it declared failed ({_IO_READ}); "
        f"wrote {quoted(str(path))} ({_file_id(path)}) and declared it: "
        "`bettermemory project list` shows it"
    )
    monkeypatch.setattr(Store, "membership_log", log)
    assert scene.model.file_binding(_file_id(path)) is not None


def test_a_file_s_error_in_the_first_sight_is_not_called_the_store_s(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An OSError the folder's first sight raises (its capture reading the
    folder, say) is named as the OS names it, not as the store failing."""
    notes = _plain_folder(scene, monkeypatch)
    real = project_cmd.caller
    calls: list[int] = []
    gone = OSError(errno.EIO, "Input/output error", str(notes))

    def caller(ctx: Any) -> Any:
        calls.append(1)
        if len(calls) == 2:
            raise gone
        return real(ctx)

    monkeypatch.setattr(project_cmd, "caller", caller)
    err = refused(monkeypatch, capsys, "project", "init")
    assert err.startswith(
        "bettermemory: the folder's first sight failed "
        f"(OSError: [Errno 5] Input/output error: {str(notes)!r}); wrote "
    )
    assert "store" not in err

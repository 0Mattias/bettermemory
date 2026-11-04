"""A share that does not answer is no absence, at any reader (ENG-217).

CPython raises a Windows share or drive that does not answer
(ERROR_BAD_NETPATH 53, ERROR_BAD_NET_NAME 67, ERROR_INVALID_DRIVE 15) as a
FileNotFoundError, as it raises a path that names nothing. ENG-217 told the
two apart by the Windows code in `gitfile_rule._entry_names` and origin's
worktree liveness only. The rule's other readers (`_names_here`,
`_names_folder`, `_same_folder`, a ``commondir`` and git's missing last
component), the hook probe's liveness, the migration's probe and the file
readers of membership, location and origin, and the claim oracle's
occupancy, still took any FileNotFoundError (or ENOENT) for an absence: a linked worktree's pointer back on a
share that was down read as a worktree moved away, and a ``.git`` file
naming that worktree's git directory was followed from any folder.

Every one of them now reads the error through one classifier
(`_location_probe.path_gone`, and `path_missing` for the readers that take
ENOENT and ENOTDIR alone as "no such file"), and each is held here on
every platform with an OSError carrying the Windows code, of the class
CPython raises for it.
"""

from __future__ import annotations

import errno
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from bettermemory import (
    _location_probe,
    checkout_token,
    claims,
    gitconfig,
    githead,
    gitfile_rule,
    membership_migrate,
    origin,
    origin_gitfiles,
    project_file,
)

from .test_location_gitdir_pin import _WINDOWS_GONE, _WINDOWS_UNKNOWN, _windows_error
from .test_membership_security import _git, checkout, neutral_git

#: Windows codes CPython raises as FileNotFoundError or NotADirectoryError:
#: the path names nothing (each reader's "no such file"), or a share or
#: drive does not answer (each reader's "cannot tell").
MISSING = {"ERROR_FILE_NOT_FOUND": 2, "ERROR_PATH_NOT_FOUND": 3, "ERROR_DIRECTORY": 267}
DOWN = {"ERROR_BAD_NETPATH": 53, "ERROR_BAD_NET_NAME": 67, "ERROR_INVALID_DRIVE": 15}
_CODES = {**_WINDOWS_GONE, **_WINDOWS_UNKNOWN}


def _error(name: str) -> OSError:
    return _windows_error(*_CODES[name])


def _fail_at(
    monkeypatch: pytest.MonkeyPatch, call: str, target: str, error: OSError
) -> None:
    """Make ``os.<call>`` raise `error` for the path `target` alone."""
    real = getattr(os, call)

    def failing(path: Any, *args: Any, **kwargs: Any) -> Any:
        if os.fsdecode(os.fspath(path)) == target:
            raise error
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, call, failing)


def _answer(read: Callable[[], Any]) -> Any:
    try:
        return read()
    except gitfile_rule._Unsettled:
        return "unsettled"
    except OSError:
        return "raises"


Reader = Callable[[Path, pytest.MonkeyPatch, OSError], Any]


def _names_here(tmp: Path, mp: pytest.MonkeyPatch, error: OSError) -> Any:
    target = tmp / "tree" / ".git"
    _fail_at(mp, "stat", str(target), error)
    return gitfile_rule._names_here(str(target), str(tmp), str(tmp))


def _names_folder(tmp: Path, mp: pytest.MonkeyPatch, error: OSError) -> Any:
    target = tmp / "tree" / ".git"
    _fail_at(mp, "lstat", str(target), error)
    return gitfile_rule._names_folder(str(target), str(tmp), str(tmp / "tree"))


def _same_folder(tmp: Path, mp: pytest.MonkeyPatch, error: OSError) -> Any:
    target = tmp / "tree"
    _fail_at(mp, "stat", str(target), error)
    return gitfile_rule._same_folder(str(target), str(tmp))


def _commondir(tmp: Path, mp: pytest.MonkeyPatch, error: OSError) -> Any:
    _fail_at(mp, "open", str(tmp / "commondir"), error)
    found = _answer(lambda: gitfile_rule._git_common(str(tmp)))
    return "its own" if found == (str(tmp), False) else found


def _missing_last(tmp: Path, mp: pytest.MonkeyPatch, error: OSError) -> Any:
    target = str(tmp / "common")
    real = os.path.realpath

    def realpath(path: Any, *args: Any, **kwargs: Any) -> Any:
        if os.fspath(path) == target:
            raise error
        return real(path, *args, **kwargs)

    mp.setattr(os.path, "realpath", realpath)
    found = _answer(lambda: gitfile_rule._real_to_last(target))
    return found if found == "unsettled" else "followed"


def _probe(tmp: Path, mp: pytest.MonkeyPatch, error: OSError) -> Any:
    _fail_at(mp, "stat", str(tmp / "cwd"), error)
    return _location_probe._path_state(str(tmp / "cwd"))[0]


def _migration(tmp: Path, mp: pytest.MonkeyPatch, error: OSError) -> Any:
    _fail_at(mp, "stat", str(tmp / "view"), error)
    run = cast(Any, None)  # the probe reads no state before its stat answers
    return membership_migrate._Run._read_probe(
        run, str(tmp / "view"), at_root=False
    ).state


def _format(tmp: Path, mp: pytest.MonkeyPatch, error: OSError) -> Any:
    _fail_at(mp, "open", str(tmp / "config"), error)
    read = gitconfig.read_git_config(str(tmp / "config"))
    return "no config" if read.why is None and not read.entries else "refused"


def _file_stamp(tmp: Path, mp: pytest.MonkeyPatch, error: OSError) -> Any:
    _fail_at(mp, "stat", str(tmp / "config"), error)
    found = gitconfig.file_stamp(str(tmp / "config"))
    return "unstated" if found == gitconfig._UNSTATED else found


def _config_bytes(tmp: Path, mp: pytest.MonkeyPatch, error: OSError) -> Any:
    """A config file that names nothing is skipped, as git skips it; one
    git cannot open for another reason is one git dies at (ENG-207's
    round 6: ``access_or_die``)."""
    _fail_at(mp, "open", str(tmp / "config"), error)
    try:
        found = gitconfig._config_bytes(str(tmp / "config"))
    except gitconfig._Dies:
        return "git dies"
    assert found is None
    return "skipped"


def _githead_stamp(tmp: Path, mp: pytest.MonkeyPatch, error: OSError) -> Any:
    _fail_at(mp, "stat", str(tmp / "HEAD"), error)
    return _answer(lambda: githead.stamp(str(tmp / "HEAD")))


def _token(tmp: Path, mp: pytest.MonkeyPatch, error: OSError) -> Any:
    _fail_at(mp, "open", str(tmp / "bettermemory"), error)
    found = checkout_token._read_text(tmp / "bettermemory")
    assert isinstance(found, checkout_token.TokenRead)
    return found.state


def _project(tmp: Path, mp: pytest.MonkeyPatch, error: OSError) -> Any:
    _fail_at(mp, "stat", str(tmp / "bettermemory.toml"), error)
    found = project_file._look(tmp / "bettermemory.toml")
    return None if found is None else type(found).__name__


def _origin_config(tmp: Path, mp: pytest.MonkeyPatch, error: OSError) -> Any:
    _fail_at(mp, "stat", str(tmp / "config"), error)
    found = origin_gitfiles._config_facts(tmp / "config")
    return "unread" if found is None else "no config"


def _claim(tmp: Path, mp: pytest.MonkeyPatch, error: OSError) -> Any:
    _fail_at(mp, "lstat", str(tmp / "cited.py"), error)
    return claims._occupancy(tmp / "cited.py")


def _worktree_root(tmp: Path, mp: pytest.MonkeyPatch, error: OSError) -> Any:
    _fail_at(mp, "stat", str(tmp / "tree"), error)
    return "gone" if origin._worktree_root_is_gone(str(tmp / "tree")) else "cannot tell"


#: Each reader, with its answer where the path names nothing and where a
#: share does not answer.
READERS: dict[str, tuple[Reader, Any, Any]] = {
    "names_here": (_names_here, None, False),
    "names_folder": (_names_folder, None, False),
    "same_folder": (_same_folder, None, False),
    "commondir": (_commondir, "its own", "unsettled"),
    "missing_last": (_missing_last, "followed", "unsettled"),
    "probe": (_probe, "gone", "unreachable"),
    "migration": (_migration, membership_migrate.GONE, membership_migrate.UNREACHABLE),
    "format": (_format, "no config", "refused"),
    "file_stamp": (_file_stamp, None, "unstated"),
    "config_bytes": (_config_bytes, "skipped", "git dies"),
    "githead_stamp": (_githead_stamp, None, "raises"),
    "token": (_token, "absent", "unreadable"),
    "project_file": (_project, None, "ProjectFileError"),
    "origin_config": (_origin_config, "no config", "unread"),
    "claim": (_claim, False, None),
    "worktree_root": (_worktree_root, "gone", "cannot tell"),
}


@pytest.mark.parametrize("reader", list(READERS))
@pytest.mark.parametrize("code", [*MISSING, *DOWN])
def test_a_share_that_does_not_answer_is_no_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reader: str, code: str
) -> None:
    """Each reader takes a path that names nothing as it always has, and a
    share or drive that does not answer as a read that failed, by the
    Windows code alone, whatever exception class CPython raised for it."""
    read, missing, down = READERS[reader]
    error = _error(code)
    assert isinstance(error, (FileNotFoundError, NotADirectoryError))
    found = read(tmp_path, monkeypatch, error)
    if code == "ERROR_DIRECTORY" and reader == "missing_last":
        # git's realpath allows only ENOENT for its last component.
        assert found == "unsettled"
        return
    assert found == (missing if code in MISSING else down), (reader, code)


@pytest.mark.parametrize("reader", list(READERS))
def test_a_posix_absence_reads_as_it_did(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reader: str
) -> None:
    """With no Windows code, ENOENT is each reader's absence, as before."""
    read, missing, _down = READERS[reader]
    found = read(tmp_path, monkeypatch, FileNotFoundError(errno.ENOENT, "gone"))
    assert found == missing, reader


@pytest.mark.parametrize("code", [*_WINDOWS_GONE, *_WINDOWS_UNKNOWN])
def test_one_classifier_reads_every_code(code: str) -> None:
    """`path_gone` and `path_missing` are one reading: a code is gone or
    not by itself, and missing only where it is gone and raised as no such
    file or folder. Origin's worktree liveness and the rule read it."""
    error = _error(code)
    gone = code in _WINDOWS_GONE
    assert _location_probe.path_gone(error) is gone
    assert _location_probe.path_missing(error) is (
        gone and isinstance(error, (FileNotFoundError, NotADirectoryError))
    )


@pytest.fixture(name="linked")
def _linked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A checkout with a linked worktree, and a folder elsewhere whose
    ``.git`` file names that worktree's git directory (a plant)."""
    neutral_git(tmp_path, monkeypatch)
    main = checkout(tmp_path / "work" / "app")
    _git(main, "worktree", "add", "-q", str(tmp_path / "work" / "wt"))
    plant = tmp_path / "elsewhere" / "plant"
    plant.mkdir(parents=True)
    (plant / ".git").write_text(f"gitdir: {main / '.git' / 'worktrees' / 'wt'}\n")
    return tmp_path / "work" / "wt", plant


@pytest.mark.parametrize("code", [*MISSING, *DOWN])
def test_a_plant_naming_a_worktree_on_a_share_that_is_down_is_refused(
    linked: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    """The worktree's git directory names its ``.git`` back. Where that
    ``.git`` is gone the worktree was moved or removed, and the git
    directory is followed from the folder that names it (ENG-158). Where it
    sits on a share that does not answer, nothing says it moved: the plant
    is refused, as it is while the share answers."""
    wt, plant = linked
    target = os.path.realpath(wt.parent / "app" / ".git" / "worktrees" / "wt")
    assert gitfile_rule.gitfile_refusal(str(plant), str(plant / ".git"), target)
    _fail_at(monkeypatch, "lstat", os.path.normpath(str(wt / ".git")), _error(code))
    why = gitfile_rule.gitfile_refusal(str(plant), str(plant / ".git"), target)
    if code in MISSING:
        assert why is None
    else:
        assert why is not None and "worktree at" in why, why


#: errnos with no Windows code: the four the OS answers "nothing there"
#: with (`_location_probe.GONE_ERRNOS`), and two it does not.
POSIX = {
    "ENOENT": errno.ENOENT,
    "ENOTDIR": errno.ENOTDIR,
    "ELOOP": errno.ELOOP,
    "ENAMETOOLONG": errno.ENAMETOOLONG,
    "EACCES": errno.EACCES,
    "EIO": errno.EIO,
}


@pytest.mark.parametrize("code", [*_CODES, *POSIX])
def test_the_migration_s_probe_reads_a_path_as_the_location_probe_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    """The migration's probe and the location probe ask the one question,
    whether anything is at a path, and take one answer from each error
    (`path_gone`). The migration read only "no such file" as gone
    (`path_missing`), so a symbolic link cycle and a name too long for this
    system, gone to the location probe, were unreachable to it. The state
    is a label there: only LIVE changes what the migration does."""
    error = _error(code) if code in _CODES else OSError(POSIX[code], code)
    probe = _probe(tmp_path, monkeypatch, error)
    migration = _migration(tmp_path, monkeypatch, error)
    expected = {
        "gone": membership_migrate.GONE,
        "unreachable": membership_migrate.UNREACHABLE,
    }[probe]
    assert migration == expected, (code, probe, migration)


@pytest.mark.skipif(os.name == "nt", reason="a symbolic link needs a privilege")
@pytest.mark.parametrize("shape", ["link_cycle", "name_too_long"])
def test_a_link_cycle_and_a_name_too_long_are_gone_to_both_probes(
    tmp_path: Path, shape: str
) -> None:
    """The OS's own ELOOP and ENAMETOOLONG: nothing is there, to the
    location probe and to the migration's."""
    if shape == "link_cycle":
        path = tmp_path / "cycle"
        os.symlink(path.name, path)
    else:
        path = tmp_path / ("n" * 300)
    assert _location_probe._path_state(str(path))[0] == "gone"
    run = cast(Any, None)  # the probe reads no state before its stat answers
    found = membership_migrate._Run._read_probe(run, str(path), at_root=False)
    assert found.state == membership_migrate.GONE

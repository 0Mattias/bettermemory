"""A path from outside the capture meets the views in one spelling
(ENG-152, ENG-155).

The capture records a view at the path's real spelling (``realpath``: each
symbolic link and Windows junction on it resolved). A declaration's folder
(`bettermemory project init`'s cwd) and the key of a write made where
nothing could be read (decision B6) were kept as the caller spelled them:
a declaration made through a symlinked cwd or a junction read as standing
nowhere in its checkout and was refused, and a write through a symlinked
cwd was keyed at a path no resolution looks at, so it was never settled.
`_location_probe.view_path` is the one spelling both are now read in.

The POSIX tests build real repositories and symbolic links under
``tmp_path``; the Windows-shaped test reads Windows spellings on any host
through ``ntpath`` with a junction table.
"""

from __future__ import annotations

import errno
import json
import ntpath
import os
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from bettermemory import _location_probe, location, membership_records, project_file
from bettermemory.config import Config, StorageConfig
from bettermemory.location import INDETERMINATE, capture, probe, resolve_caller
from bettermemory.membership import UNRESOLVED, Checkout, Declaration, Membership, View
from bettermemory.store import Store

from .test_membership_security import checkout, neutral_git

_git_here = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


@pytest.fixture
def lab(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Store, Config]]:
    """A store under `tmp_path` and its config, git set neutral."""
    neutral_git(tmp_path, monkeypatch)
    made = Store(tmp_path / "store")
    yield made, Config(storage=StorageConfig(directory=str(tmp_path / "store")))
    made.close()


def _link(link: Path, target: Path) -> Path:
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this platform makes no symbolic link here")
    return link


def _view_path(path: str) -> str:
    """`location.view_path`, read at the call so the module loads where it
    is not defined (the fail-before run)."""
    return str(location.view_path(path))


@_git_here
def test_a_declaration_through_a_symlinked_cwd_resolves_to_the_recorded_view(
    tmp_path: Path, lab: tuple[Store, Config]
) -> None:
    """The review's scenario (ENG-152): `project init --new` below a
    repository's root, from a cwd spelled through a symbolic link, is
    below that checkout's root (not refused as standing nowhere), makes
    the folder a part of the checkout's project, keeps the folder in the
    view's spelling, and the folder resolves to the part."""
    store, config = lab
    mono = checkout(tmp_path / "real" / "mono")
    api = mono / "packages" / "api"
    api.mkdir(parents=True)
    link = _link(tmp_path / "link", tmp_path / "real")
    through = link / "mono" / "packages" / "api"
    known = resolve_caller(store, config, str(mono)).resolution
    assert known.checkout is not None and known.project is not None
    model = Membership(store, location.process_machine().id)
    k = model.checkout(known.checkout)

    assert model.below_root(k, str(link / "mono")) is False
    assert model.below_root(k, str(link / "mono") + os.sep) is False
    assert model.below_root(k, str(through)) is True

    written = project_file.write(through, name="api")
    model.declare(
        Declaration(
            "init_new",
            "os:tester",
            checkout=k.id,
            target=known.project,
            file_id=written.id,
            name="api",
            directory=str(through),
        )
    )
    part = model.file_binding(written.id)
    assert part is not None and part != known.project
    assert model.project(part).parent == known.project
    assert model.checkout(k.id).project == known.project
    bound = [
        json.loads(r.payload) for r in store.membership_log(["project_file_bound"])
    ]
    assert [b.get("dir") for b in bound if b.get("file_id") == written.id] == [
        os.path.realpath(api)
    ]
    location.forget()
    res = resolve_caller(store, config, str(through)).resolution
    assert (res.project, res.checkout) == (part, known.checkout)


@_git_here
def test_a_write_through_a_symlinked_cwd_is_settled_when_the_checkout_is_resolved(
    tmp_path: Path, lab: tuple[Store, Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The review's scenario (ENG-155, D2): a write made while nothing could
    be read at a cwd spelled through a symbolic link (its stat refused) is
    keyed at the view's spelling, as a container client's probe keys it,
    and the same session's next resolution there settles it: the
    unresolved checkout is replaced by the checkout and the record is
    homed in its project."""
    store, config = lab
    app = checkout(tmp_path / "real" / "app")
    link = _link(tmp_path / "link", tmp_path / "real")
    through = link / "app"
    real_stat = os.stat

    def refused(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if isinstance(path, (str, os.PathLike)) and os.fspath(path) == str(through):
            raise PermissionError(errno.EACCES, "Permission denied", str(through))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", refused)
    location.forget()
    unread = resolve_caller(store, config, str(through), session="s-link")
    assert unread.location.state == INDETERMINATE
    record = store.write(
        content="written while the folder could not be read",
        scopes=["tools"],
        home=unread.home(),
    )
    probed = probe(str(through))
    monkeypatch.setattr(os, "stat", real_stat)
    location.forget()

    res = resolve_caller(store, config, str(through), session="s-link").resolution

    assert res.checkout is not None and res.project is not None
    model = Membership(store, location.process_machine().id)
    unresolved = [k for k in model.checkouts() if k.kind == UNRESOLVED]
    assert [(k.views[0].path, k.replaced_by) for k in unresolved] == [
        (os.path.realpath(app), res.checkout)
    ]
    home = model.home(record.id)
    assert home is not None and model.canonical(home.project) == res.project
    assert probed["cwd"] == os.path.realpath(app)


class _WindowsPaths:
    """``ntpath`` with junctions: `realpath` follows the table as Windows
    follows a junction (case folded on the way, the target's own spelling
    returned), on any host."""

    def __init__(self, junctions: dict[str, str]) -> None:
        self.junctions = junctions

    def __getattr__(self, name: str) -> Any:
        return getattr(ntpath, name)

    def realpath(self, path: str) -> str:
        p = ntpath.normpath(path)
        for link, target in self.junctions.items():
            here, key = ntpath.normcase(p), ntpath.normcase(link)
            if here == key or here.startswith(key + "\\"):
                return target + p[len(link) :]
        return p


def _windows_checkout(machine: str, path: str) -> Checkout:
    view = View("K1", machine, path, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", 1)
    return Checkout(
        id="K1",
        project="P1",
        machine=machine,
        kind="primary",
        object_key=None,
        common_object_key=None,
        git_dir=path + "\\.git",
        token=None,
        token_status="n/a",
        stamps=(),
        last_head=None,
        branch=None,
        remotes=(),
        manifest=None,
        legacy=False,
        retired=False,
        registered="2026-01-01T00:00:00Z",
        views=(view,),
    )


def test_windows_spellings_through_a_junction_meet_the_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows-shaped (ENG-152, ENG-155): a cwd spelled through a junction,
    in another case, with forward slashes or a trailing separator, is read
    in the view's spelling: below the checkout's root where the folder is,
    at the root where the junction names it, and a location nothing could
    be read at is keyed at the junction's target."""
    paths = _WindowsPaths({"C:\\Users\\Dev\\link": "D:\\work\\app"})
    monkeypatch.setattr(_location_probe, "_VIEW_PATHS", paths)
    monkeypatch.setattr(membership_records, "_DIR_PATHS", ntpath)
    store = Store(tmp_path / "store")
    try:
        model = Membership(store, "M-windows")
        k = _windows_checkout("M-windows", "D:\\work\\app")

        assert _view_path("c:\\users\\DEV\\link\\pkg\\") == "D:\\work\\app\\pkg"
        assert model.below_root(k, "C:\\Users\\Dev\\Link") is False
        assert model.below_root(k, "c:/users/dev/link/packages/api/") is True
        assert model.below_root(k, "d:/WORK/app/packages") is True
        assert model.below_root(k, "C:\\Users\\Dev\\other") is None
        unread = capture(
            "C:\\Users\\Dev\\link\\app\\src", write_token=False, use_cache=False
        )
        assert (unread.state, unread.cwd) == (INDETERMINATE, "D:\\work\\app\\app\\src")
    finally:
        store.close()

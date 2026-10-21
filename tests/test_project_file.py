"""Tests for project_file.py: `bettermemory.toml`, its parse, the walk from
the cwd up, and `project init`'s write (design 2.1, decisions D12, D13 and
D17).

The walk's shapes are the round 2 experiments' (E23 folder projects, E34
tenants, E35 a monorepo), built under ``tmp_path``; repositories are made
with git under a neutral configuration. The walk goes on to the filesystem
root, so assertions about what lies above a fixture look only at files under
``tmp_path``. Tests that name a design section 7 row carry it (I29, I31, X2,
X4, X5).
"""

from __future__ import annotations

import errno
import os
import re
import shutil
import subprocess
import sys
import types
from pathlib import Path
from typing import Any, cast

import pytest

from bettermemory import project_file
from bettermemory.project_file import (
    PROJECT_FILE_NAME,
    Above,
    ProjectFile,
    ProjectFileError,
    above_start,
    find,
    mint_id,
    parse,
    render,
    write,
)

from .conftest import shielded_child_env

_ID = "bmp_0XNK4RY1ZCMBN31P1J2EN2STQP"
_OTHER_ID = "bmp_7ZZ0000000000000000000000Z"


@pytest.fixture(autouse=True)
def neutral_git(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No user or system git configuration, no inherited repository
    variables, a fixed identity: the lab's environment."""
    empty = tmp_path_factory.mktemp("gitconfig") / "empty"
    empty.write_text("")
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_CEILING_DIRECTORIES",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Fixture")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "fixture@example.invalid")


@pytest.fixture
def base(tmp_path: Path) -> Path:
    return tmp_path.resolve()


def _repo(path: Path) -> Path:
    """A git repository at `path`; returns its root as git names it."""
    path.mkdir(parents=True, exist_ok=True)
    if shutil.which("git") is None:
        (path / ".git").mkdir()
        return path
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    top = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return Path(top)


def _file(
    directory: Path, project_id: str, name: str, *, boundary: bool = False
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    body = f'[project]\nid = "{project_id}"\nname = "{name}"\n'
    if boundary:
        body += "boundary = true\n"
    path = directory / PROJECT_FILE_NAME
    path.write_text(body, encoding="utf-8")
    return path


def _ids(files: tuple[ProjectFile, ...], base: Path) -> list[str]:
    """The names of the files under `base`, in walk order."""
    return [f.name for f in files if f.path.is_relative_to(base)]


def _new_id() -> str:
    return mint_id()


# ---------------------------------------------------------------------------
# parse
# ---------------------------------------------------------------------------


def test_parse_a_well_formed_file(base: Path) -> None:
    path = _file(base, _ID, "mono")
    assert parse(path) == ProjectFile(id=_ID, name="mono", boundary=False, path=path)


def test_parse_a_boundary_file(base: Path) -> None:
    path = _file(base, _ID, "client-a", boundary=True)
    result = parse(path)
    assert isinstance(result, ProjectFile)
    assert result.boundary is True
    assert result.directory == base


def test_parse_without_a_name_takes_the_directory_name(base: Path) -> None:
    path = base / "vault" / PROJECT_FILE_NAME
    path.parent.mkdir()
    path.write_text(f'[project]\nid = "{_ID}"\n', encoding="utf-8")
    assert parse(path) == ProjectFile(id=_ID, name="vault", boundary=False, path=path)


def test_parse_an_empty_name_stays_empty(base: Path) -> None:
    path = base / "vault" / PROJECT_FILE_NAME
    path.parent.mkdir()
    path.write_text(f'[project]\nid = "{_ID}"\nname = ""\n', encoding="utf-8")
    assert parse(path) == ProjectFile(id=_ID, name="", boundary=False, path=path)


@pytest.mark.skipif(sys.platform == "win32", reason="no line breaks in Windows names")
def test_parse_without_a_name_drops_control_characters_from_the_directory_name(
    base: Path,
) -> None:
    path = base / "two\nlines" / PROJECT_FILE_NAME
    path.parent.mkdir()
    path.write_text(f'[project]\nid = "{_ID}"\n', encoding="utf-8")
    result = parse(path)
    assert isinstance(result, ProjectFile) and result.name == "twolines"


def test_parse_leaves_keys_it_does_not_know(base: Path) -> None:
    path = base / PROJECT_FILE_NAME
    path.write_text(
        f'[project]\nid = "{_ID}"\nname = "x"\nlater = 1\n\n[tool]\nother = true\n',
        encoding="utf-8",
    )
    result = parse(path)
    assert isinstance(result, ProjectFile) and result.id == _ID


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (b"[project\nid = 1\n", "toml"),
        (b"\xff\xfe[project]\n", "toml"),
        (b"", "no_project_table"),
        (b"[other]\nx = 1\n", "no_project_table"),
        (b"project = 3\n", "wrong_type"),
        (b'[project]\nname = "a"\n', "id_missing"),
        (b"[project]\nid = 7\n", "wrong_type"),
        (b'[project]\nid = "bmp_SHORT"\n', "id_malformed"),
        (b'[project]\nid = "bmp_0xnk4ry1zcmbn31p1j2en2stqp"\n', "id_malformed"),
        (b'[project]\nid = "bmp_IXNK4RY1ZCMBN31P1J2EN2STQP"\n', "id_malformed"),
        (b'[project]\nid = "bmq_0XNK4RY1ZCMBN31P1J2EN2STQP"\n', "id_malformed"),
        (b'[project]\nid = " bmp_0XNK4RY1ZCMBN31P1J2EN2STQP"\n', "id_malformed"),
        (f'[project]\nid = "{_ID}"\nname = 3\n'.encode(), "wrong_type"),
        (f'[project]\nid = "{_ID}"\nboundary = "true"\n'.encode(), "wrong_type"),
        (f'[project]\nid = "{_ID}"\nboundary = 1\n'.encode(), "wrong_type"),
        (
            f'[project]\nid = "{_ID}"\nname = "a\\nbettermemory: b"\n'.encode(),
            "name_malformed",
        ),
        (f'[project]\nid = "{_ID}"\nname = "a\\u2028b"\n'.encode(), "name_malformed"),
    ],
    ids=[
        "bad-toml",
        "not-utf8",
        "empty",
        "no-project-table",
        "project-not-a-table",
        "id-missing",
        "id-not-a-string",
        "id-short",
        "id-lower-case",
        "id-non-crockford",
        "id-wrong-prefix",
        "id-leading-space",
        "name-not-a-string",
        "boundary-a-string",
        "boundary-an-int",
        "name-line-break",
        "name-line-separator",
    ],
)
def test_parse_reports_each_malformed_shape(
    base: Path, body: bytes, reason: str
) -> None:
    path = base / PROJECT_FILE_NAME
    path.write_bytes(body)
    result = parse(path)
    assert isinstance(result, ProjectFileError)
    assert result.reason == reason
    assert result.path == path and result.directory == base
    assert result.detail and str(path) in result.line and result.detail in result.line


def test_parse_quotes_only_the_start_of_a_long_bad_id(base: Path) -> None:
    path = base / PROJECT_FILE_NAME
    path.write_text(f'[project]\nid = "{"Z" * 500}"\n', encoding="utf-8")
    result = parse(path)
    assert isinstance(result, ProjectFileError) and result.reason == "id_malformed"
    assert "Z" * 41 not in result.detail


def test_parse_a_missing_file_is_unreadable_not_raised(base: Path) -> None:
    result = parse(base / PROJECT_FILE_NAME)
    assert isinstance(result, ProjectFileError) and result.reason == "unreadable"


def test_parse_a_directory_is_not_a_file(base: Path) -> None:
    (base / PROJECT_FILE_NAME).mkdir()
    result = parse(base / PROJECT_FILE_NAME)
    assert isinstance(result, ProjectFileError) and result.reason == "not_a_file"


def test_parse_an_oversized_file_is_not_read_further(base: Path) -> None:
    path = base / PROJECT_FILE_NAME
    path.write_text(f'[project]\nid = "{_ID}"\n' + "# pad\n" * 20000, encoding="utf-8")
    result = parse(path)
    assert isinstance(result, ProjectFileError) and result.reason == "too_large"


@pytest.fixture
def local_base(local_tmp_path: Path) -> Path:
    """`base` on local disk, for a file an SMB mount cannot host: a FIFO
    (EOPNOTSUPP) or a mode chmod sets (ignored without the unix extensions)."""
    return local_tmp_path.resolve()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs here")
def test_parse_a_fifo_does_not_block(local_base: Path) -> None:
    base = local_base
    make_fifo = getattr(os, "mkfifo")
    make_fifo(base / PROJECT_FILE_NAME)
    walk = find(base, None)
    assert [e.reason for e in walk.errors if e.path.is_relative_to(base)] == [
        "not_a_file"
    ]


@pytest.mark.skipif(
    sys.platform == "win32" or getattr(os, "geteuid", lambda: -1)() == 0,
    reason="POSIX permission bits, not enforced for root",
)
def test_an_unreadable_file_is_reported_on_the_walk(local_base: Path) -> None:
    base = local_base
    path = _file(base / "proj", _ID, "proj")
    path.chmod(0)
    try:
        walk = find(base / "proj", None)
    finally:
        path.chmod(0o644)
    assert [(e.path, e.reason) for e in walk.errors if e.path.is_relative_to(base)] == [
        (path, "unreadable")
    ]
    assert walk.folder is None or not walk.folder.path.is_relative_to(base)


def test_an_evicted_file_is_reported_and_never_read(
    base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _file(base, _ID, "synced")
    opened: list[object] = []
    real_open = os.open

    def recording_open(target: Any, *args: Any, **kwargs: Any) -> int:
        opened.append(target)
        return real_open(target, *args, **kwargs)

    monkeypatch.setattr(project_file, "_evicted", lambda st: True)
    monkeypatch.setattr(os, "open", recording_open)
    result = parse(path)
    assert isinstance(result, ProjectFileError) and result.reason == "evicted"
    assert path not in opened and str(path) not in opened


@pytest.mark.parametrize(
    ("flags", "attributes", "evicted"),
    [
        (0, 0, False),
        (0x40000000, 0, sys.platform == "darwin"),
        (0, 0x00400000, True),
        (0, 0x00040000, True),
        (0, 0x00000020, False),
    ],
    ids=[
        "resident",
        "sf-dataless",
        "recall-on-data-access",
        "recall-on-open",
        "archive-bit",
    ],
)
def test_eviction_is_read_from_the_stat_flags(
    flags: int, attributes: int, evicted: bool
) -> None:
    st = types.SimpleNamespace(st_flags=flags, st_file_attributes=attributes)
    assert project_file._evicted(cast(os.stat_result, st)) is evicted


# ---------------------------------------------------------------------------
# find: repositories
# ---------------------------------------------------------------------------


def test_root_file_binds_the_repository(base: Path) -> None:
    root = _repo(base / "work" / "app")
    bound = _file(root, _ID, "app")
    (root / "src" / "deep").mkdir(parents=True)
    walk = find(root / "src" / "deep", root)
    assert walk.root is not None and walk.root.path == bound
    assert walk.nested == ()
    assert walk.nearest == walk.root
    assert walk.placement(walk.root) == "root"
    assert _ids(walk.enclosing, base) == []
    assert walk.above.start == root.parent


def test_x5_monorepo_nested_files_nearest_first_never_a_sibling(base: Path) -> None:
    """E35 (a), (b): a package's session has its own file and the root's,
    never a sibling's; at the root only the root's."""
    mono = _repo(base / "work" / "mono")
    _file(mono, _ID, "mono")
    _file(mono / "packages" / "api", _new_id(), "api")
    _file(mono / "packages" / "api" / "v2", _new_id(), "api-v2")
    _file(mono / "packages" / "web", _new_id(), "web")
    (mono / "packages" / "api" / "v2" / "src").mkdir()

    walk = find(mono / "packages" / "api" / "v2" / "src", mono)
    assert [f.name for f in walk.nested] == ["api-v2", "api"]
    assert walk.root is not None and walk.root.name == "mono"
    assert walk.nearest is not None and walk.nearest.name == "api-v2"
    assert "web" not in [f.name for f in walk.files]
    assert [walk.placement(f) for f in walk.files if f.path.is_relative_to(base)] == [
        "nested",
        "nested",
        "root",
    ]

    at_root = find(mono, mono)
    assert at_root.nested == ()
    assert at_root.nearest is not None and at_root.nearest.name == "mono"


def test_x4_workspace_above_the_root_encloses_and_names_no_session(base: Path) -> None:
    """E34 (f), E23 (h): a client's folder file above a repository is its
    workspace, a boundary when it says so; the repository's own session
    project is not named by it."""
    client = base / "clients" / "a"
    workspace = _file(client, _ID, "client-a", boundary=True)
    app = _repo(client / "app")
    walk = find(app, app)
    assert walk.root is None and walk.nested == () and walk.nearest is None
    assert _ids(walk.enclosing, base) == ["client-a"]
    assert walk.enclosing[0].path == workspace
    assert walk.placement(walk.enclosing[0]) == "enclosing"
    assert [f.name for f in walk.boundaries if f.path.is_relative_to(base)] == [
        "client-a"
    ]
    assert walk.above.start == client


def test_x2_boundaries_are_marked_at_every_level(base: Path) -> None:
    client = base / "clients" / "b"
    _file(client, _ID, "client-b", boundary=True)
    app = _repo(client / "app")
    _file(app, _OTHER_ID, "client-b-app")
    walk = find(app, app)
    assert walk.root is not None and walk.root.boundary is False
    assert [f.name for f in walk.boundaries if f.path.is_relative_to(base)] == [
        "client-b"
    ]


def test_the_walk_above_the_root_is_cached_apart(base: Path) -> None:
    root = _repo(base / "ws" / "repo")
    _file(root, _ID, "repo")
    (root / "pkg").mkdir()
    first = find(root, root)
    assert above_start(root, root) == first.above.start == root.parent

    # A file appears above the root: a cached walk passed back does not see
    # it, a fresh one does.
    _file(base / "ws", _OTHER_ID, "workspace")
    cached = find(root / "pkg", root, above=first.above)
    assert _ids(cached.enclosing, base) == []
    assert _ids(find(root, root).enclosing, base) == ["workspace"]

    # Inside the repository everything is read on every call.
    _file(root / "pkg", _new_id(), "pkg")
    again = find(root / "pkg", root, above=first.above)
    assert [f.name for f in again.nested] == ["pkg"]

    with pytest.raises(ValueError, match="cached walk starts at"):
        find(base, None, above=first.above)


def test_malformed_files_stay_on_the_walk_and_never_raise(base: Path) -> None:
    root = _repo(base / "mono")
    _file(root, _ID, "mono")
    bad = root / "packages" / "tools" / PROJECT_FILE_NAME
    bad.parent.mkdir(parents=True)
    bad.write_text("[project]\nid = 3\n", encoding="utf-8")
    walk = find(bad.parent, root)
    assert walk.nested == ()
    assert walk.nearest is not None and walk.nearest.name == "mono"
    assert [(e.path, e.reason) for e in walk.errors if e.path.is_relative_to(base)] == [
        (bad, "wrong_type")
    ]
    assert walk.items[0] == walk.errors[0]


def test_a_cwd_outside_the_work_tree_walks_from_the_root(base: Path) -> None:
    """GIT_DIR with GIT_WORK_TREE elsewhere: the repository's own files
    decide, not the files around the cwd."""
    root = _repo(base / "tree")
    _file(root, _ID, "tree")
    elsewhere = base / "elsewhere"
    _file(elsewhere, _OTHER_ID, "elsewhere")
    walk = find(elsewhere, root)
    assert walk.root is not None and walk.root.name == "tree"
    assert "elsewhere" not in [f.name for f in walk.files]


def test_a_repository_at_the_filesystem_root_has_nothing_above(base: Path) -> None:
    anchor = Path(base.anchor)
    assert above_start(anchor, anchor) is None
    walk = find(anchor, anchor)
    assert walk.above == Above(start=None, items=())
    assert len(walk.inner) <= 1


def test_placement_of_a_file_not_on_the_walk_raises(base: Path) -> None:
    root = _repo(base / "a")
    stranger = ProjectFile(
        id=_ID, name="x", boundary=False, path=base / "b" / PROJECT_FILE_NAME
    )
    with pytest.raises(ValueError, match="not on this walk"):
        find(root, root).placement(stranger)


# ---------------------------------------------------------------------------
# find: plain folders (E23)
# ---------------------------------------------------------------------------


def test_i29_a_plain_folder_without_a_file_names_nothing(base: Path) -> None:
    folder = base / "Documents" / "notes"
    folder.mkdir(parents=True)
    walk = find(folder, None)
    assert _ids(walk.files, base) == []
    assert walk.folder is None or not walk.folder.path.is_relative_to(base)
    assert walk.inner == ()


def test_a_plain_folder_with_a_file_is_a_folder_project_from_any_subfolder(
    base: Path,
) -> None:
    """E23 (b): the nearest file at the cwd or an ancestor."""
    vault = base / "Documents" / "vault"
    path = _file(vault, _ID, "vault")
    (vault / "Daily").mkdir()
    walk = find(vault / "Daily", None)
    assert walk.folder is not None and walk.folder.path == path
    assert walk.nearest == walk.folder
    assert walk.placement(walk.folder) == "folder"
    assert walk.root is None and walk.nested == ()
    assert walk.above.start == vault / "Daily"
    assert above_start(vault / "Daily", None) == vault / "Daily"


def test_i31_nested_folder_projects(base: Path) -> None:
    """E23 (g): the inner folder's session is the inner project, the outer
    one encloses it."""
    outer = base / "Writing"
    _file(outer, _ID, "writing")
    _file(outer / "novel", _OTHER_ID, "novel")
    walk = find(outer / "novel", None)
    assert walk.folder is not None and walk.folder.name == "novel"
    assert _ids(walk.enclosing, base) == ["writing"]
    assert walk.placement(walk.enclosing[0]) == "enclosing"
    outer_walk = find(outer, None)
    assert outer_walk.folder is not None and outer_walk.folder.name == "writing"
    assert _ids(outer_walk.enclosing, base) == []


def test_a_repository_inside_a_folder_project(base: Path) -> None:
    """E23 (h): the folder's file encloses the repository; from the folder
    itself it is the folder project."""
    work = base / "Work"
    _file(work, _ID, "work")
    service = _repo(work / "service")
    in_repo = find(service, service)
    assert in_repo.nearest is None
    assert _ids(in_repo.enclosing, base) == ["work"]
    in_folder = find(work, None)
    assert in_folder.folder is not None and in_folder.folder.name == "work"


# ---------------------------------------------------------------------------
# write, mint_id
# ---------------------------------------------------------------------------


def test_mint_id_format() -> None:
    ids = {mint_id() for _ in range(200)}
    assert len(ids) == 200
    pattern = re.compile(r"bmp_[0-9ABCDEFGHJKMNPQRSTVWXYZ]{26}")
    assert all(pattern.fullmatch(i) for i in ids)


def test_write_mints_an_id_and_parses_back(base: Path) -> None:
    written = write(base, name="mono")
    assert written.path == base / PROJECT_FILE_NAME
    assert parse(written.path) == written
    assert written.boundary is False
    text = written.path.read_text(encoding="utf-8")
    assert text.startswith("# bettermemory project file")
    assert "boundary" not in text


def test_write_a_boundary(base: Path) -> None:
    written = write(base, name="client-a", boundary=True)
    assert parse(written.path) == written and written.boundary is True
    assert "boundary = true\n" in written.path.read_text(encoding="utf-8")


def test_write_refuses_to_overwrite(base: Path) -> None:
    first = write(base, name="one")
    before = first.path.read_bytes()
    with pytest.raises(FileExistsError):
        write(base, name="two")
    assert first.path.read_bytes() == before


def test_write_rebind_replaces_with_a_new_id(base: Path) -> None:
    first = write(base, name="one")
    second = write(base, name="one", rebind=True)
    assert second.id != first.id
    assert parse(second.path) == second
    assert sorted(p.name for p in base.iterdir()) == [PROJECT_FILE_NAME]


@pytest.mark.parametrize(
    "name",
    ['quote " and backslash \\', "Ångström 日本語", "tab-free name", ""],
)
def test_write_names_round_trip(base: Path, name: str) -> None:
    written = write(base, name=name)
    assert parse(written.path) == written
    assert written.name == name


@pytest.mark.parametrize(
    "name", ["two\nlines", "carriage\rreturn", "nul\x00", "sep\u2029"]
)
def test_write_refuses_control_characters_in_a_name(base: Path, name: str) -> None:
    with pytest.raises(ValueError, match="control characters"):
        write(base, name=name)
    assert not (base / PROJECT_FILE_NAME).exists()


def test_write_never_makes_the_directory(base: Path) -> None:
    with pytest.raises(FileNotFoundError):
        write(base / "missing", name="x")
    assert not (base / "missing").exists()


def test_write_cut_short_leaves_no_file(
    base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def disk_full(fd: int, data: Any) -> int:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "write", disk_full)
    with pytest.raises(OSError, match="No space left"):
        write(base, name="x")
    assert not (base / PROJECT_FILE_NAME).exists()


def test_render_matches_what_write_puts_down(base: Path) -> None:
    written = write(base, name="x", boundary=True)
    assert written.path.read_bytes() == render(written)


def test_the_capture_modules_import_nothing_beyond_the_standard_library() -> None:
    """The hook client reads project files and writes the token in its own
    namespace (design 8.1), on the standard library alone."""
    probe = (
        "import sys, bettermemory.project_file, bettermemory.checkout_token; "
        "print(' '.join(sorted(m for m in sys.modules "
        "if m.startswith(('bettermemory', 'platformdirs', 'pydantic', 'mcp')))))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
        env=shielded_child_env(),
    )
    assert result.stdout.split() == [
        "bettermemory",
        "bettermemory._fsutil",
        "bettermemory.checkout_token",
        "bettermemory.project_file",
    ]

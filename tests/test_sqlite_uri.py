"""`_fsutil.sqlite_file_uri`: the ``file:`` URI every SQLite open that
carries a query parameter names its file by.

`Path.as_uri` wrote a Windows UNC path's server as the URI's authority
(``file://server/share/...``), and SQLite takes only an empty or
``localhost`` authority, so the key ring binding check, the backup's
read-only reads, the daemon's store-moved check and the posture audit's
meta read all failed on a store on a network share. The helper writes
the empty authority SQLite takes (``file:////server/share/...``) and is
byte for byte `Path.as_uri` everywhere `Path.as_uri` was right: POSIX
paths and Windows drive paths.

The URI cases run on every host on pure paths. A real open through a
directory whose name holds every character SQLite's URI parser reads as
syntax runs where such a name can exist (POSIX), the UNC open runs on
Windows through this host's admin share when that share answers, and
the device-path (``\\\\?\\C:\\...``) open runs on every Windows host.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from types import ModuleType

import pytest

from bettermemory import backup, posture
from bettermemory import daemon as daemon_module
from bettermemory import store as store_module
from bettermemory._fsutil import sqlite_file_uri
from bettermemory.store import CREATED_AFTER_ORPHANS, STORE_FILENAME, Store

# A path `Path.as_uri` already wrote as SQLite reads it, and the URI both
# write for it.
AS_URI_WAS_RIGHT: list[tuple[PurePath, str]] = [
    (
        PureWindowsPath(r"C:\Users\fred\memory.sqlite"),
        "file:///C:/Users/fred/memory.sqlite",
    ),
    (PureWindowsPath(r"c:\x\memory.sqlite"), "file:///c:/x/memory.sqlite"),
    (PureWindowsPath("C:/x/memory.sqlite"), "file:///C:/x/memory.sqlite"),
    (PureWindowsPath("C:\\"), "file:///C:/"),
    (
        PureWindowsPath(r"C:\Documents and Settings\fred\memory.sqlite"),
        "file:///C:/Documents%20and%20Settings/fred/memory.sqlite",
    ),
    (PureWindowsPath(r"D:\a#b\memory.sqlite"), "file:///D:/a%23b/memory.sqlite"),
    (PureWindowsPath(r"D:\a?b\memory.sqlite"), "file:///D:/a%3Fb/memory.sqlite"),
    (PureWindowsPath(r"D:\a%41\memory.sqlite"), "file:///D:/a%2541/memory.sqlite"),
    (
        PureWindowsPath("D:\\J\u00f6rg\\\u65e5\u672c\\memory.sqlite"),
        "file:///D:/J%C3%B6rg/%E6%97%A5%E6%9C%AC/memory.sqlite",
    ),
    (
        PureWindowsPath(r"D:\a&b=c;d+e$f\x:stream"),
        "file:///D:/a%26b%3Dc%3Bd%2Be%24f/x%3Astream",
    ),
    (PurePosixPath("/home/fred/memory.sqlite"), "file:///home/fred/memory.sqlite"),
    (
        PurePosixPath("/home/a b/#?%41/\u00e9/memory.sqlite"),
        "file:///home/a%20b/%23%3F%2541/%C3%A9/memory.sqlite",
    ),
    (PurePosixPath("/a\\b/memory.sqlite"), "file:///a%5Cb/memory.sqlite"),
    (PurePosixPath("//host/x"), "file:////host/x"),
    (PurePosixPath("/"), "file:///"),
]

# A Windows path `Path.as_uri` wrote with something other than an empty
# authority, and the URI the helper writes for it.
AS_URI_WAS_WRONG: list[tuple[PureWindowsPath, str]] = [
    (
        PureWindowsPath(r"\\server\share\dir\memory.sqlite"),
        "file:////server/share/dir/memory.sqlite",
    ),
    (
        PureWindowsPath("//server/share/dir/memory.sqlite"),
        "file:////server/share/dir/memory.sqlite",
    ),
    (PureWindowsPath(r"\\server\share"), "file:////server/share/"),
    (
        PureWindowsPath("\\\\nas\\team share\\a#b?c%41\\J\u00f6rg\\memory.sqlite"),
        "file:////nas/team%20share/a%23b%3Fc%2541/J%C3%B6rg/memory.sqlite",
    ),
    (
        PureWindowsPath(r"\\localhost\C$\Users\fred\memory.sqlite"),
        "file:////localhost/C%24/Users/fred/memory.sqlite",
    ),
    (
        PureWindowsPath(r"\\?\C:\dir\memory.sqlite"),
        "file:////%3F/C%3A/dir/memory.sqlite",
    ),
    (
        PureWindowsPath(r"\\?\UNC\server\share\memory.sqlite"),
        "file:////%3F/UNC/server/share/memory.sqlite",
    ),
    (PureWindowsPath(r"\\.\C:\dir\memory.sqlite"), "file:////./C%3A/dir/memory.sqlite"),
]


def _as_uri(path: PurePath) -> str:
    """What the openers wrote before: `PurePath.as_uri`, which Python 3.14
    deprecates on a pure path (a concrete one keeps it)."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return path.as_uri()


@pytest.mark.parametrize(("path", "uri"), AS_URI_WAS_RIGHT, ids=str)
def test_a_path_as_uri_wrote_right_keeps_its_uri_byte_for_byte(
    path: PurePath, uri: str
) -> None:
    """POSIX paths and Windows drive paths: the drive as the first segment
    under an empty authority, everything but ``A-Za-z0-9_.-~/`` percent-
    encoded, ``?``, ``#`` and ``%`` among it. The same bytes `as_uri` wrote,
    so no opener's URI changes on these paths."""
    assert sqlite_file_uri(path) == uri
    assert _as_uri(path) == uri


@pytest.mark.parametrize(("path", "uri"), AS_URI_WAS_WRONG, ids=str)
def test_a_unc_path_takes_the_empty_authority(path: PureWindowsPath, uri: str) -> None:
    """A UNC path (and a verbatim or device one) puts the whole path, with
    its leading ``//``, under an empty authority: ``file:////server/...``.
    `as_uri` wrote the same path with the authority dropped to two
    slashes, its server read as the URI's host."""
    assert sqlite_file_uri(path) == uri
    assert _as_uri(path) == "file:" + uri[len("file://") :]


def test_a_concrete_path_takes_its_own_flavour(tmp_path: Path) -> None:
    """The openers pass a concrete `Path`: a `WindowsPath` takes the
    Windows forms, a `PosixPath` the POSIX one, the same URI `as_uri`
    writes for this host's temporary directory."""
    path = tmp_path.resolve() / "a b#c?d%41" / STORE_FILENAME
    assert sqlite_file_uri(path) == path.as_uri()


@pytest.mark.parametrize(
    "path",
    [
        PurePosixPath("memory.sqlite"),
        PurePosixPath("store/memory.sqlite"),
        PureWindowsPath(r"store\memory.sqlite"),
        PureWindowsPath(r"C:memory.sqlite"),
        PureWindowsPath(r"\store\memory.sqlite"),
    ],
    ids=str,
)
def test_a_relative_path_is_refused(path: PurePath) -> None:
    """An authority, even an empty one, names an absolute path; a relative
    one (a Windows path without its drive or its root among them) has no
    URI, as `as_uri` has none for it."""
    with pytest.raises(ValueError, match="relative"):
        sqlite_file_uri(path)


def test_sqlite_refuses_the_server_authority_as_uri_wrote() -> None:
    """The defect, on SQLite itself and on any host: the server `as_uri`
    made the authority fails the URI's parse, before any file is
    looked for. (`localhost` is the authority SQLite takes, and drops.)
    A SQLite built with SQLITE_ALLOW_URI_AUTHORITY hands a server on as a
    UNC name instead, so there is no defect to show on it."""
    probe = sqlite3.connect(":memory:")
    try:
        options = {str(row[0]) for row in probe.execute("PRAGMA compile_options")}
    finally:
        probe.close()
    if "ALLOW_URI_AUTHORITY" in options:
        pytest.skip("this SQLite passes a URI's authority on as a UNC name")
    path = PureWindowsPath(r"\\server\share\dir\memory.sqlite")
    with pytest.raises(sqlite3.OperationalError, match="invalid uri authority: server"):
        sqlite3.connect(f"{_as_uri(path)}?mode=ro", uri=True)


def test_sqlite_reads_the_empty_authority_and_keeps_the_leading_slashes(
    tmp_path: Path,
) -> None:
    """SQLite takes ``file:////...`` as an empty authority and hands its
    VFS the path with both leading slashes, the UNC name Windows reads.
    Shown on POSIX, where a path's leading ``//`` names the root: the
    store opens through the four-slash URI, and a server's name there
    is looked for as a file, not refused by the parse."""
    if sys.platform == "win32":
        pytest.skip("a //server path on Windows is a network lookup")
    path = tmp_path.resolve() / STORE_FILENAME
    _write_marker(path, "through four slashes")
    uri = sqlite_file_uri(PurePosixPath("/" + str(path)))
    assert uri.startswith("file:////")
    assert _read_marker(f"{uri}?mode=ro") == "through four slashes"
    unc = sqlite_file_uri(PureWindowsPath(r"\\no-such-server\share\memory.sqlite"))
    with pytest.raises(sqlite3.OperationalError) as refused:
        sqlite3.connect(f"{unc}?mode=ro", uri=True)
    assert "invalid uri authority" not in str(refused.value)
    assert "unable to open" in str(refused.value)


def test_a_directory_named_with_uri_syntax_opens_its_own_file(tmp_path: Path) -> None:
    """Spaces, ``#``, ``?`` and ``%`` in a directory's name, and a
    non-ASCII letter: every one is written ``%HH`` and SQLite decodes it
    back, so the read reaches the file and no other. Written plainly the
    ``#`` would end the URI, the ``?`` would start its query and ``%41``
    would read as ``A``."""
    if sys.platform == "win32":
        pytest.skip("'?' cannot be in a Windows file name")
    directory = tmp_path.resolve() / "a b#c?d%41e \u00e9"
    directory.mkdir()
    (tmp_path / "a bAe \u00e9").mkdir()
    path = directory / STORE_FILENAME
    _write_marker(path, "the awkward directory")
    for query in ("mode=ro", "mode=ro&immutable=1"):
        assert (
            _read_marker(f"{sqlite_file_uri(path)}?{query}") == "the awkward directory"
        )
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
        ["a b#c?d%41e \u00e9", "a bAe \u00e9"]
    )
    assert list((tmp_path / "a bAe \u00e9").iterdir()) == []


def _write_marker(path: Path, value: str) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO meta VALUES ('store_id', ?)", (value,))
        conn.commit()
    finally:
        conn.close()


def _read_marker(uri: str) -> str:
    conn = sqlite3.connect(uri, uri=True)
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'store_id'").fetchone()
    finally:
        conn.close()
    return str(row[0])


def _created_store(directory: Path) -> tuple[Path, str]:
    with Store.create(directory) as store:
        store_id = store.store_id
    return directory / STORE_FILENAME, store_id


def _orphan_marker(path: Path) -> list[str] | None:
    conn = sqlite3.connect(str(path))
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = ?", (CREATED_AFTER_ORPHANS,)
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else list(json.loads(row[0]))


_ORPHAN_NAME = f"{STORE_FILENAME}.orphaned-20000101T000000Z.sqlite"
_LONG_AGO = datetime(2000, 1, 1, tzinfo=timezone.utc)


def _run_site(site: str, path: Path, store_id: str) -> None:
    """Each opener that names its file by URI, read (or, for the daemon's
    store-moved check, written) through `path`, its answer checked."""
    if site == "store":
        assert store_module._store_id_at(path) == store_id
    elif site == "posture":
        assert posture.read_meta(path)["store_id"] == store_id
    elif site == "backup":
        assert backup._current_store_id(path) == store_id
        assert backup._log_end(path) is not None
    else:
        daemon_module._tell_successor(
            path,
            path.with_name(_ORPHAN_NAME),
            store_id="the store the daemon had open",
            created_after=_LONG_AGO,
            own_files={},
        )


SITES: dict[str, ModuleType] = {
    "store": store_module,
    "posture": posture,
    "backup": backup,
    "daemon": daemon_module,
}


def _read_then_run_site(site: str, named: Path, path: Path, store_id: str) -> None:
    """Read the store's id through the helper's URI for `named` resolved,
    then run `site` through `named`; `path` is the store's own name, the
    daemon leg's marker read back through it.

    The direct read is ``immutable=1``. A ``mode=ro`` read of a WAL-mode
    store leaves its ``-wal`` and ``-shm`` behind, and the daemon's
    store-moved check leaves alone a store with sidecars it cannot place
    (`daemon._tell_successor`), so the daemon leg would find nothing
    written, whatever the URI."""
    resolved = named.resolve()
    assert _read_marker(f"{sqlite_file_uri(resolved)}?mode=ro&immutable=1") == store_id
    _run_site(site, named, store_id)
    if site == "daemon":
        assert _orphan_marker(path) == [_ORPHAN_NAME]


@pytest.mark.parametrize("site", sorted(SITES))
def test_every_query_opener_names_its_file_through_the_helper(
    site: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The key ring binding check (`store._store_id_at`), the posture
    audit's meta read, the backup's read-only reads and the daemon's
    store-moved check each build their URI with `sqlite_file_uri`, for
    the file they were given. Each runs after a read through the helper,
    as the Windows tests below run it."""
    path, store_id = _created_store((tmp_path / "store").resolve())
    seen: list[PurePath] = []

    def recording(target: PurePath) -> str:
        seen.append(target)
        return sqlite_file_uri(target)

    monkeypatch.setattr(SITES[site], "sqlite_file_uri", recording)
    _read_then_run_site(site, path, path, store_id)
    assert seen and set(seen) == {path}


def _admin_share_path(path: Path) -> Path:
    """`path`, on a local drive, spelled through this host's admin share:
    ``C:\\x`` is ``\\\\localhost\\C$\\x``. Skips when the share does not
    answer (no Server service, or an account the share refuses)."""
    drive = PureWindowsPath(path).drive
    if len(drive) != 2 or drive[1] != ":":
        pytest.skip(f"{path} is not on a drive letter")
    unc = Path(f"\\\\localhost\\{drive[0]}$" + str(path)[len(drive) :])
    try:
        os.stat(unc)
    except OSError as exc:
        pytest.skip(f"the admin share does not answer here: {exc}")
    return unc


@pytest.mark.skipif(sys.platform != "win32", reason="a Windows UNC path")
@pytest.mark.parametrize("site", sorted(SITES))
def test_a_store_on_a_unc_path_opens_at_every_site(site: str, tmp_path: Path) -> None:
    """A store reached through ``\\\\localhost\\<drive>$\\...``, which
    resolves to that UNC path, opens at every site. Before the helper each
    one failed: `as_uri` wrote ``file://localhost/C%24/...``, whose
    ``localhost`` SQLite drops, and looked for ``/C$/...`` on the current
    drive."""
    path, store_id = _created_store((tmp_path / "share store").resolve())
    unc = _admin_share_path(path)
    resolved = unc.resolve()
    assert PureWindowsPath(resolved).drive.lower() == PureWindowsPath(unc).drive.lower()
    with pytest.raises(sqlite3.Error):
        _read_marker(f"{resolved.as_uri()}?mode=ro&immutable=1")
    _read_then_run_site(site, unc, path, store_id)


@pytest.mark.skipif(sys.platform != "win32", reason="a Windows device path")
@pytest.mark.parametrize("site", sorted(SITES))
def test_a_store_named_by_a_device_path_opens_at_every_site(
    site: str, tmp_path: Path
) -> None:
    """A store named ``\\\\?\\C:\\...`` (a store directory configured that
    way, or a name past MAX_PATH that `Path.resolve` leaves prefixed) keeps
    its prefix through `Path.resolve`, and the helper writes it as
    ``file:////%3F/C%3A/...``: SQLite hands its VFS ``//?/C:/...``, which
    Windows reads as the device path it names. It opens at every site, as
    the device form ``\\\\.\\C:\\...`` (``file:////./C%3A/...``) opens.
    Python 3.11 to 3.13's `as_uri` wrote ``file://%3F/...`` for it, which
    SQLite refuses; 3.14's drops the prefix."""
    path, store_id = _created_store((tmp_path / "device store").resolve())
    verbatim = Path("\\\\?\\" + str(path))
    resolved = verbatim.resolve()
    assert str(resolved).startswith("\\\\?\\"), resolved
    assert sqlite_file_uri(resolved).startswith("file:////%3F/")
    device = PureWindowsPath("\\\\.\\" + str(path))
    uri = sqlite_file_uri(device)
    assert uri.startswith("file:////./")
    assert _read_marker(f"{uri}?mode=ro&immutable=1") == store_id
    _read_then_run_site(site, verbatim, path, store_id)

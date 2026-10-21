"""`location.remote_key` for a ``file://`` URL (design 2.2, R).

A clone or a submodule of a local repository records the source as a file
URL or as a path, and the model finds its lineage by keying both the same
way: ``local:<absolute path>``, the path the checkout's view is recorded
under. The URL is read the way git reads it, through
`urllib.request.url2pathname` on Windows. The first half runs on the host
platform; the second keys Windows URLs on any host, with the Windows path
functions (``ntpath``) and URL reader (``nturl2path``) swapped in.
"""

from __future__ import annotations

import ntpath
import os
import warnings
from pathlib import Path
from types import SimpleNamespace

import pytest

from bettermemory import location
from bettermemory.location import remote_key

# ---------------------------------------------------------------------------
# On this platform
# ---------------------------------------------------------------------------


def test_a_file_url_keys_as_the_path_it_names(tmp_path: Path) -> None:
    """``Path.as_uri()`` is how the suite clones a local repository; its key
    is the plain path's (``file:///D:/a/repo`` on Windows, where stripping
    the scheme left ``/D:/a/repo``)."""
    repo = tmp_path / "libs" / "engine"
    repo.mkdir(parents=True)
    key = remote_key(repo.as_uri())
    assert key == remote_key(str(repo))
    assert key == "local:" + os.path.realpath(repo)


def test_a_percent_encoded_file_url_keys_as_the_decoded_path(tmp_path: Path) -> None:
    repo = tmp_path / "two words" / "r\u00e9po"
    repo.mkdir(parents=True)
    url = repo.as_uri()
    assert "%20" in url and "%C3%A9" in url, "premise: as_uri encodes them"
    assert remote_key(url) == remote_key(str(repo)) == "local:" + os.path.realpath(repo)


def test_a_localhost_file_url_keys_as_the_path(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    url = "file://localhost" + repo.as_uri()[len("file://") :]
    assert remote_key(url) == remote_key(str(repo))


def test_a_file_url_through_a_symbolic_link_keys_as_the_resolved_path(
    tmp_path: Path,
) -> None:
    real = tmp_path / "real" / "repo"
    real.mkdir(parents=True)
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(real.parent, target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links need a privilege this account lacks")
    assert remote_key((alias / "repo").as_uri()) == remote_key(str(real))
    assert remote_key((alias / "repo").as_uri()) == "local:" + os.path.realpath(real)


@pytest.mark.skipif(os.name == "nt", reason="POSIX spellings")
def test_posix_keys_of_an_absolute_file_url_are_unchanged(tmp_path: Path) -> None:
    """The key of ``file:///<absolute path>`` is the scheme stripped and the
    rest resolved, as it was; only a URL with an authority or an escape
    reads differently, as git reads it: the authority dropped, the escape
    decoded, a ``?`` or ``#`` kept in the path."""
    base = os.path.realpath(tmp_path)
    for rest in ("/repo", "/repo/", "/a/b/../c", "/repo.git", "/x?y#z"):
        expected = os.path.realpath(base + rest).rstrip("/")
        assert remote_key("file://" + base + rest) == "local:" + expected
        assert remote_key("FILE://" + base + rest) == "local:" + expected
    assert remote_key(f"file://server{base}/repo") == f"local:{base}/repo"
    assert remote_key("file://", base) == "local:" + base


def test_a_relative_path_remote_is_taken_from_the_top_level(tmp_path: Path) -> None:
    top = tmp_path / "work" / "repo"
    top.mkdir(parents=True)
    assert remote_key("../engine", str(top)) == "local:" + os.path.realpath(
        tmp_path / "work" / "engine"
    )


# ---------------------------------------------------------------------------
# Windows URLs, read on any host
# ---------------------------------------------------------------------------


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """`remote_key` with Windows' path functions and file URL reader. The
    resolution is ``ntpath.abspath``, which is what ``ntpath.realpath`` is
    off Windows, so the keys below are the same on every host and a UNC
    path is never looked up on the network."""
    with warnings.catch_warnings():
        # Deprecated since Python 3.14 in favour of urllib.request, whose
        # Windows branch runs on Windows only.
        warnings.simplefilter("ignore", DeprecationWarning)
        nturl2path = pytest.importorskip("nturl2path")
    paths = SimpleNamespace(
        sep=ntpath.sep, isabs=ntpath.isabs, join=ntpath.join, realpath=ntpath.abspath
    )
    monkeypatch.setattr(location, "_PATH", paths)
    monkeypatch.setattr(location, "_url_path", nturl2path.url2pathname)


@pytest.mark.parametrize(
    ("url", "key"),
    [
        ("file:///D:/a/repo", "local:D:\\a\\repo"),
        ("file:///D:/a/repo/", "local:D:\\a\\repo"),
        ("FILE:///D:/a/repo", "local:D:\\a\\repo"),
        ("file://localhost/D:/a/repo", "local:D:\\a\\repo"),
        # git's own spelling of a drive (`file://$(pwd)` in connect.c).
        ("file://D:/a/repo", "local:D:\\a\\repo"),
        ("file:///D:/two%20words/r%C3%A9po", "local:D:\\two words\\r\u00e9po"),
        ("file:///D:/a/c#/repo", "local:D:\\a\\c#\\repo"),
        ("file://server/share/repo", "local:\\\\server\\share\\repo"),
        ("file:////server/share/repo", "local:\\\\server\\share\\repo"),
    ],
)
@pytest.mark.usefixtures("windows")
def test_a_windows_file_url_keys_as_the_windows_path(url: str, key: str) -> None:
    assert remote_key(url) == key


@pytest.mark.usefixtures("windows")
def test_a_windows_file_url_and_its_plain_path_share_the_key() -> None:
    """The lineage a local clone or a submodule keeps: its source written as
    a URL, as a path with forward slashes, or as a native path, one key."""
    keys = {
        remote_key("file:///D:/a/libs/engine"),
        remote_key("file://localhost/D:/a/libs/engine"),
        remote_key("D:/a/libs/engine"),
        remote_key("D:\\a\\libs\\engine"),
        remote_key("D:\\a\\libs\\engine\\"),
    }
    assert keys == {"local:D:\\a\\libs\\engine"}


@pytest.mark.usefixtures("windows")
def test_a_windows_relative_remote_is_taken_from_the_top_level() -> None:
    assert remote_key("..\\engine", "D:\\a\\game") == "local:D:\\a\\engine"
    assert remote_key("../engine", "D:\\a\\game") == "local:D:\\a\\engine"


@pytest.mark.usefixtures("windows")
def test_hosted_remotes_read_the_same_with_windows_paths() -> None:
    assert remote_key("git@github.com:Acme/Tool.git") == "github.com/acme/tool"
    assert remote_key("https://github.com/acme/tool") == "github.com/acme/tool"

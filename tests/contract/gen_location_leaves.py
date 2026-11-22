"""Fixtures: the capture's filesystem leaves of `location`
(``location_leaves.jsonl``, ``location_leaves_python.jsonl``,
``location_leaves_posix.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/location/leaves.rs``.

``location_leaves`` (not gated) pins `_statx_birth` over the answers of a
scripted ``statx`` (``sys.platform`` set for the call), `_birth` over
stat results that are plain namespaces (the attributes `getattr` finds
are the case's, so no Python release or platform decides), and
`_file_url_path` off Windows (`_PATH` set to `posixpath`), which is
`urllib.parse.unquote` of what follows the authority.

``location_leaves_python`` (gated on the Python release) pins
`_file_url_path` with `_PATH` set to `ntpath` and `_url_path` to
`nturl2path.url2pathname`, and that function alone (each backslash of an
answer spelled ``<b>``): its text changed in
3.13 patch releases and again in 3.14. Every text answer is written
through `text_field`: 3.13.18 answers some inputs with a lone surrogate,
which a plain JSON string cannot carry to a strict reader.

``location_leaves_posix`` (POSIX only: links on disk) pins
`remote_key`'s local keys through `realpath`, `kept_apart`, `_real_git`,
`record_working_tree` (with `gitdir_pin.recorded` and `put` scripted) and
`_stamp`'s three shapes, each case in a fresh scratch folder ``@``. A
tree is a list of ``["d", rel]``, ``["f", rel, text]`` and
``["l", rel, target]``.
"""

from __future__ import annotations

import ctypes
import functools
import json
import os
import posixpath
import sys
import tempfile
import types
import warnings
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from bettermemory import gitdir_pin, location, location_git

from ._common import jsonl, text_field


@contextmanager
def _patched(obj: Any, **values: Any) -> Iterator[None]:
    old = {k: getattr(obj, k) for k in values}
    try:
        for k, v in values.items():
            setattr(obj, k, v)
        yield
    finally:
        for k, v in old.items():
            setattr(obj, k, v)


def _raised(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - the class is the answer
        return {"raise": type(exc).__name__}


# ---------------------------------------------------------------------------
# statx and birth
# ---------------------------------------------------------------------------

_STATX_CASES: list[tuple[str, list[int] | None]] = [
    ("linux", None),
    ("linux", [0, 0x800, 1_700_000_000, 123_456_789]),
    ("linux", [0, 0x7FF, 1_700_000_000, 5]),
    ("linux", [0, 0xFFF, -1, 999_999_999]),
    ("linux", [0, 0x800, -(2**63), 0]),
    ("linux", [0, 0x800, 2**63 - 1, 2**32 - 1]),
    ("linux", [0, 0xFFFF_FFFF, 0, 0]),
    ("linux", [-1, 0x800, 7, 7]),
    ("linux", [1, 0x800, 7, 7]),
    ("darwin", [0, 0x800, 7, 7]),
    ("win32", [0, 0x800, 7, 7]),
]


def _raw(mask: int, seconds: int, nanos: int) -> bytes:
    raw = bytearray(256)
    raw[0:4] = mask.to_bytes(4, "little")
    raw[80:88] = seconds.to_bytes(8, "little", signed=True)
    raw[88:92] = nanos.to_bytes(4, "little")
    return bytes(raw)


def _statx_row(platform: str, answer: list[int] | None) -> dict[str, Any]:
    if answer is None:
        fn = None
    else:
        rc, mask, seconds, nanos = answer
        raw = _raw(mask, seconds, nanos)

        def fn(_at: int, _path: bytes, _flags: int, _mask: int, buf: Any) -> int:
            ctypes.memmove(buf, raw, len(raw))
            return rc

    with _patched(sys, platform=platform), _patched(location, _STATX=[fn]):
        got = location._statx_birth("/x")
    return {
        "k": "statx",
        "p": platform,
        "a": answer,
        "r": None if got is None else str(got),
    }


def _attr(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return {"b": value}
    if isinstance(value, int):
        return {"i": str(value)}
    return {"f": repr(value)}


_BIRTH_ATTRS: list[Any] = [
    None,
    0,
    1_700_000_000_123_456_789,
    -5,
    True,
    False,
    1.5,
    -1.5,
    1e30,
    -1e300,
    float("inf"),
    float("-inf"),
    float("nan"),
    1_700_000_000.123_456_7,
]


def _birth_rows() -> Iterator[dict[str, Any]]:
    cases: list[tuple[bool, str, Any, Any, Any, Any]] = []
    for ns in _BIRTH_ATTRS:
        cases.append((True, "darwin", ns, None, None, None))
    for s in _BIRTH_ATTRS:
        cases.append((True, "darwin", None, s, None, None))
        cases.append((True, "darwin", 2.5, s, None, None))
    for platform in ("linux", "win32", "darwin", "freebsd14"):
        for x in (None, 42, -42):
            cases.append((True, platform, None, None, 77, x))
        cases.append((False, platform, 5, 5, 5, 5))
    cases.append((True, "linux", 9, 9, 9, 9))
    cases.append((True, "win32", None, 3, 9, 9))
    for report, platform, ns, s, ctime, x in cases:
        st = types.SimpleNamespace()
        if ns is not None:
            st.st_birthtime_ns = ns
        if s is not None:
            st.st_birthtime = s
        st.st_ctime_ns = ctime

        def statx(_path: str, x: Any = x) -> Any:
            return x

        with (
            _patched(sys, platform=platform),
            _patched(location, REPORT_BIRTH=report, _statx_birth=statx),
        ):
            got = _raised(
                functools.partial(location._birth, "/x", cast(os.stat_result, st))
            )
        if not isinstance(got, dict) and got is not None:
            got = str(got)
        yield {
            "k": "birth",
            "rep": report,
            "p": platform,
            "ns": _attr(ns),
            "s": _attr(s),
            "c": ctime,
            "x": x,
            "r": got,
        }


# ---------------------------------------------------------------------------
# File URLs
# ---------------------------------------------------------------------------

_URLS = [
    "file://",
    "file:///",
    "file:///srv/a/repo",
    "file://localhost/srv/a/repo",
    "file://host/x/y",
    "file://host",
    "FILE:///A/B",
    "file:///a%20b",
    "file:///a%2fb%2F",
    "file:///a%zzb%2",
    "file:///a%",
    "file:///%C3%A9",
    "file:///%C3",
    "file:///%ED%A0%80",
    "file:///%F0%9F%98%80x",
    "file:///%E2%82",
    "file:///caf\u00e9%C3%A9",
    "file:///%C3\u00e9%A9",
    "file:///a?b#c",
    "file:///a%3Fb%23c",
    "file:///D:/x",
    "file://D:/x",
    "file://d:",
    "file://D|/x",
    "file:///D|/x",
    "file://LOCALHOST/C:/x",
    "file://localhost",
    "file://localhost/",
    "file:////server/share/x",
    "file://server/share/a%20b",
    "file:///C:",
    "file:///C:/",
    "file:///C:/a:b",
    "file:///a:b",
    "file:///1:/x",
    "file:///C:/a?b#c",
    "file:///C:/%41",
    "file://%41:/x",
    "file://\u00e9/x",
    "file:///\U0001f600/%F0%9F%98%80",
    "file:///\u0080%41",
    "file://a:b/x",
    "file://ab:/x",
    "file://///x",
    "file://localhost////x",
]


def _bs(value: Any) -> Any:
    """A Windows answer with each backslash spelled ``<b>``: the host-fact
    guard refuses a UNC prefix's two backslashes in any golden file."""
    return value.replace("\\", "<b>") if isinstance(value, str) else value


def _answer(value: Any) -> dict[str, Any]:
    """A text answer through `text_field` (key ``r``, or ``r_cps`` when it
    holds a surrogate: `nturl2path.url2pathname` of 3.13.18 answers an
    undecodable byte as one where 3.13.13 answers U+FFFD), anything else
    under ``r`` as it is."""
    return text_field("r", value) if isinstance(value, str) else {"r": value}


def _file_rows(nt: bool) -> Iterator[dict[str, Any]]:
    import ntpath

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        import nturl2path
    for url in _URLS:
        if nt:
            with _patched(location, _PATH=ntpath, _url_path=nturl2path.url2pathname):
                got = _raised(functools.partial(location._file_url_path, url))
            yield {"k": "filent", "u": url, **_answer(_bs(got))}
            path = url[len("file:") :]
            yield {
                "k": "nt",
                "u": path,
                **_answer(
                    _bs(_raised(functools.partial(nturl2path.url2pathname, path)))
                ),
            }
        else:
            with _patched(location, _PATH=posixpath):
                got = _raised(functools.partial(location._file_url_path, url))
            yield {"k": "file", "u": url, **_answer(got)}


def _file(name: str, rows: list[dict[str, Any]]) -> dict[str, bytes]:
    return {f"{name}.jsonl": jsonl([{"fixture": name, "cases": len(rows)}, *rows])}


def generate() -> dict[str, bytes]:
    rows = [_statx_row(p, a) for p, a in _STATX_CASES]
    rows += list(_birth_rows())
    rows += list(_file_rows(False))
    return _file("location_leaves", rows)


def generate_python() -> dict[str, bytes]:
    return _file("location_leaves_python", list(_file_rows(True)))


# ---------------------------------------------------------------------------
# On disk (POSIX)
# ---------------------------------------------------------------------------

_SEP_CONFIG = "[core]\n\trepositoryformatversion = 0\n\tbare = false\n"
_WT_CONFIG = _SEP_CONFIG + "\tworktree = /elsewhere\n"
_SEP = [
    ["d", "sep.git"],
    ["f", "sep.git/config", _SEP_CONFIG],
    ["f", "sep.git/HEAD", "ref: refs/heads/main\n"],
]


def _build(root: str, tree: list[list[str]]) -> None:
    for entry in tree:
        at = os.path.join(root, entry[1])
        if entry[0] == "d":
            os.makedirs(at, exist_ok=True)
        elif entry[0] == "f":
            os.makedirs(os.path.dirname(at), exist_ok=True)
            Path(at).write_bytes(entry[2].replace("@", root).encode())
        else:
            os.makedirs(os.path.dirname(at), exist_ok=True)
            os.symlink(entry[2].replace("@", root), at)


def _at(text: str | None, root: str) -> str | None:
    return None if text is None else text.replace("@", root)


def _back(value: Any, root: str) -> Any:
    if isinstance(value, str):
        return value.replace(root, "@")
    if isinstance(value, list | tuple):
        return [_back(v, root) for v in value]
    return value


@contextmanager
def _scratch() -> Iterator[str]:
    with tempfile.TemporaryDirectory() as tmp:
        yield os.path.realpath(tmp)


_LOCAL_TREE = [
    ["d", "a/repo"],
    ["l", "link", "a/repo"],
    ["l", "abs", "@/a"],
    ["l", "loop", "loop"],
    ["l", "dangling", "nowhere/x"],
]
_LOCAL = [
    ("@/a/repo", None),
    ("@/link", None),
    ("@/link/../x", None),
    ("@/abs/repo/", None),
    ("@//a//./repo", None),
    ("@/loop", None),
    ("@/dangling", None),
    ("link", "@"),
    ("./link/sub", "@"),
    ("../link", "@/a"),
    ("abs/repo", "@/"),
    ("@/link", "@/a"),
    ("@/a/repo", "@/"),
    ("file://@/link", None),
    ("file://host@/link", None),
    ("file://@/a%2Frepo", None),
    ("file:///../..@/link", None),
    ("file://link", "@"),
    ("  @/link  ", None),
    (".", "@/link"),
    ("\\x", "@"),
    ("C:/x", "@"),
    ("C:x", "@"),
    ("git@host:owner/repo.git", "@"),
    ("https://Host/Owner/Repo.git", "@"),
]

_KEPT: list[tuple[list[list[str]], list[Any]]] = [
    # [tree], [dotgit, git_dir, common_dir, root, kind]
    (
        _SEP + [["d", "w"], ["f", "w/.git", "gitdir: @/sep.git\n"]],
        ["@/w/.git", "@/sep.git", "@/sep.git", "@/w", "primary"],
    ),
    (
        _SEP + [["d", "w"], ["l", "w/.git", "@/sep.git"]],
        ["@/w/.git", "@/sep.git", "@/sep.git", "@/w", "primary"],
    ),
    (
        _SEP + [["d", "w/.git"]],
        ["@/w/.git", "@/sep.git", "@/sep.git", "@/w", "primary"],
    ),
    (_SEP + [["d", "w"]], ["@/w/.git", "@/sep.git", "@/sep.git", "@/w", "primary"]),
    (
        _SEP + [["d", "w"], ["f", "w/.git", "x"]],
        ["@/sep.git", "@/sep.git", "@/sep.git", "@/w", "primary"],
    ),
    (
        _SEP + [["d", "w"], ["f", "w/.git", "x"]],
        [None, "@/sep.git", "@/sep.git", "@/w", "primary"],
    ),
    (
        _SEP + [["d", "w"], ["f", "w/.git", "x"]],
        ["@/w/.git", "@/sep.git", "@/sep.git", None, "primary"],
    ),
    (
        _SEP + [["d", "w"], ["f", "w/.git", "x"]],
        ["@/w/.git", "@/sep.git", "@/sep.git", "@/w", "linked"],
    ),
    (
        _SEP + [["d", "w"], ["f", "w/.git", "x"]],
        ["@/w/.git", "@/sep.git", "@/sep.git", "@/w", "bare"],
    ),
    (
        [
            ["d", "sep.git"],
            ["f", "sep.git/config", _WT_CONFIG],
            ["d", "w"],
            ["f", "w/.git", "x"],
        ],
        ["@/w/.git", "@/sep.git", "@/sep.git", "@/w", "primary"],
    ),
    (
        [["d", "sep.git"], ["d", "w"], ["f", "w/.git", "x"]],
        ["@/w/.git", "@/sep.git", "@/sep.git", "@/w", "primary"],
    ),
    (
        _SEP
        + [
            ["d", "real"],
            ["l", "w", "real"],
            ["l", "real/.git", "../sep.git"],
            ["l", "g", "sep.git"],
        ],
        ["@/w/.git", "@/g", "@/g/.", "@/w", "primary"],
    ),
    (
        [
            ["d", "c/.git/worktrees/w"],
            ["l", "g", "c/.git"],
            ["d", "w"],
            ["f", "w/.git", "gitdir: @/c/.git/worktrees/w\n"],
        ],
        ["@/w/.git", "@/g/worktrees/w", "@/c/.git", "@/w", "linked"],
    ),
]

_PIN = "pin"


def _pin(spec: list[str] | None, root: str) -> Any:
    if spec is None:
        return None
    return gitdir_pin.Pin(
        file=Path(root, _PIN),
        git_dir=spec[0].replace("@", root),
        tree=spec[1].replace("@", root),
    )


#: [tree], first record, put's answer, second record; the git directory
#: is ``@/sep.git`` and the folder ``@/w``.
_RECORD: list[tuple[list[list[str]], Any, bool, Any]] = [
    (_SEP + [["d", "w"]], None, True, None),
    (_SEP + [["d", "w"]], None, False, None),
    (
        _SEP + [["d", "w"], ["d", "o"], ["f", "o/.git", "gitdir: @/sep.git\n"]],
        None,
        False,
        ["@/sep.git", "@/o"],
    ),
    (_SEP + [["d", "w"]], ["@/sep.git", "@/w"], True, None),
    (_SEP + [["d", "w"], ["l", "v", "w"]], ["@/sep.git", "@/v"], True, None),
    (
        _SEP + [["d", "w"], ["d", "o"], ["f", "o/.git", "gitdir: @/sep.git\n"]],
        ["@/sep.git", "@/o"],
        True,
        None,
    ),
    (
        _SEP + [["d", "w"], ["d", "o"], ["f", "o/.git", "gitdir: @/other.git\n"]],
        ["@/sep.git", "@/o"],
        True,
        None,
    ),
    (_SEP + [["d", "w"]], ["@/sep.git", "@/gone"], True, None),
    (_SEP + [["d", "w"]], ["@/sep.git", "@/gone"], False, None),
    (
        _SEP + [["d", "w"], ["d", "o"], ["d", "o/.git"]],
        ["@/sep.git", "@/o"],
        True,
        None,
    ),
]


def _posix_rows() -> Iterator[dict[str, Any]]:
    for url, base in _LOCAL:
        with _scratch() as root:
            _build(root, _LOCAL_TREE)
            got = _raised(
                functools.partial(location.remote_key, _at(url, root), _at(base, root))
            )
            yield {"k": "local", "u": url, "b": base, "r": _back(got, root)}
    for tree, fields in _KEPT:
        with _scratch() as root:
            _build(root, tree)
            dotgit, git_dir, common, top, kind = [_at(f, root) for f in fields]
            assert git_dir is not None and common is not None and kind is not None
            found = location_git._Git(
                dotgit=dotgit, git_dir=git_dir, common_dir=common, root=top, kind=kind
            )
            kept = _raised(functools.partial(location.kept_apart, found))
            real = _raised(functools.partial(location._real_git, found))
            if not isinstance(real, dict):
                real = _back(
                    [real.dotgit, real.git_dir, real.common_dir, real.root, real.kind],
                    root,
                )
            yield {"k": "kept", "t": tree, "g": fields, "r": kept, "real": real}
    for tree, first, put, second in _RECORD:
        with _scratch() as root:
            _build(root, tree)
            answers = [_pin(first, root), _pin(second, root)]
            asked: list[Any] = []

            def recorded(_git_dir: str, answers: list[Any] = answers) -> Any:
                index = len(asked)
                asked.append(1)
                return answers[min(index, 1)]

            def wrote(
                _git_dir: str, _tree: str, _previous: Any = None, put: bool = put
            ) -> bool:
                return put

            fake = types.SimpleNamespace(recorded=recorded, put=wrote)
            with _patched(location, gitdir_pin=fake):
                got = _raised(
                    functools.partial(
                        location.record_working_tree, f"{root}/sep.git", f"{root}/w"
                    )
                )
            yield {
                "k": "record",
                "t": tree,
                "first": first,
                "put": put,
                "second": second,
                "r": got,
                "n": len(asked),
            }
    for path in ["f", "nothing", "nothing/below", "f/below", "f\0x", "l", "dangling"]:
        with _scratch() as root:
            _build(
                root, [["f", "f", "x"], ["l", "l", "f"], ["l", "dangling", "nowhere"]]
            )
            got = location._stamp(os.path.join(root, path))
            shape = (
                None
                if got is None
                else ("unread" if got == (-1, -1, -1, -1, -1) else "stat")
            )
            yield {"k": "stamp", "path": path, "r": shape}


def generate_posix() -> dict[str, bytes]:
    return _file("location_leaves_posix", list(_posix_rows()))


if __name__ == "__main__":
    for name, data in {**generate(), **generate_python(), **generate_posix()}.items():
        print(name, len(data), json.loads(data.splitlines()[0]))

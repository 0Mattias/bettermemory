"""Fixtures: `_location_probe`'s `view_path` and `probe` over real trees
(``location_probe.jsonl``, ``location_probe_python.jsonl``,
``location_probe_errno.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/location_probe.rs``. This pins it to
`view_path`, `probe` and, through them, `_path_state` and `_hostname`.

Each case is a tree written in a fresh directory of the scratch world, the
process working there (``chdir``), as `gen_fs_realpath` writes it (``f``,
``d``, ``l``, ``m``). The first line is a header (the count); then one line
per case, its tree and ``q``, one answer per query:

- ``p``: the path, spelled as `gen_fs_realpath` spells one (``=`` and the
  text, or hex), ``<root>`` standing for the case's directory;
- ``e``: where present, `os.stat` raises the OSError CPython's own
  `os.stat` raises for this errno name instead of asking the OS:
  ``OSError(errno, strerror, path)``, CPython picking the subclass by the
  errno;
- ``n``: the same by the errno's number, any number: 0, where CPython's
  message is ``Error`` (`PyErr_SetFromErrnoWithFilenameObject`) and
  `_path_state` reads ``str(exc)``, a number the platform names no errno
  for, or one past its list;
- ``g``: where present (0), `os.getcwd` raises FileNotFoundError;
- ``h``: the host's name `socket.gethostname` answers, spelled as ``p``,
  or null where it raises OSError (default ``probe-host``);
- ``v``: `view_path(p)`, spelled as ``p`` with the case's directory
  written ``<scratch>``;
- ``o``: `probe(p)`, its ``cwd`` (and ``hostname``) spelled as ``v``,
  its ``cwd_object`` written ``"stat"`` (the generator checks it is the
  device and inode `os.stat` gives the resolved cwd, which vary by run).

Gates. The files are POSIX only: the trees hold links and a mode-000
folder, and the answers are ``posixpath``'s. In ``location_probe.jsonl``
every errno whose `os.strerror` text differs between macOS and Linux is
left out (ENXIO, ENOTCONN, ESTALE, ETIMEDOUT, EILSEQ); its ``n`` rows are
errno 0, whose text is the path's ``repr`` alone. The ``_python`` file
holds the trees with a link loop, whose `realpath` 3.11 walks otherwise
than 3.13 (`gen_fs_realpath`), and the paths holding a NUL, whose
ValueError text 3.11 words otherwise than 3.12 and later. ``location_probe_errno.jsonl`` raises every
number in `ERRNO_NUMBERS`: the numbers and the C library's text for each
are the platform's, so the file is gated by platform (`generate.py`'s
``PLATFORM_GATED``). `refuse_root` stops the generator where a mode-000
folder is searchable.
"""

from __future__ import annotations

import errno
import os
import shutil
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest import mock

from bettermemory import _location_probe

from . import _sandbox
from ._common import jsonl
from .gen_fs_realpath import NAME_MAX, _b, refuse_root

HOST = "probe-host"


class Case:
    def __init__(
        self,
        queries: list[dict[str, Any]],
        *,
        files: dict[str, str] | None = None,
        dirs: tuple[str, ...] = (),
        links: dict[str, str] | None = None,
        modes: dict[str, int] | None = None,
        loop: bool = False,
        numbers: bool = False,
    ) -> None:
        self.queries = queries
        self.files = files or {}
        self.dirs = dirs
        self.links = links or {}
        self.modes = modes or {}
        self.loop = loop
        self.numbers = numbers

    def record(self) -> dict[str, Any]:
        out: dict[str, Any] = {"f": dict(self.files)}
        for key, value in (
            ("d", list(self.dirs)),
            ("l", dict(self.links)),
            ("m", {k: f"{v:03o}" for k, v in self.modes.items()}),
        ):
            if value:
                out[key] = value
        return out


def Q(p: str, **kw: Any) -> dict[str, Any]:
    return {"p": p, **kw}


#: Errnos whose `os.strerror` text macOS and Linux share.
INJECTED = (
    "ENOENT",
    "ENOTDIR",
    "ELOOP",
    "ENAMETOOLONG",
    "EACCES",
    "EPERM",
    "EIO",
    "EINVAL",
    "EINTR",
    "EBADF",
    "EISDIR",
    "EEXIST",
    "EHOSTDOWN",
)

#: The errno numbers ``location_probe_errno.jsonl`` raises: every number
#: from 0 through 133, the last one Linux defines (EHWPOISON; macOS's last
#: is 106 or 107), a negative one, and two far past any list.
#: `generate_errno` wants the run to go one past the last errno the
#: platform's `errno` module names, so it stops where the module names
#: 133 (CPython 3.14 on Linux).
ERRNO_NUMBERS = (-1, *range(134), 1000, 2**31 - 1)


def _cases() -> Iterator[Case]:
    yield Case(
        [
            Q("<root>"),
            Q("<root>/"),
            Q("<root>//"),
            Q("<root>/d"),
            Q("<root>/d/"),
            Q("<root>/./d/e/.."),
            Q("<root>/f"),
            Q("<root>/f/"),
            Q("<root>/f/x"),
            Q("<root>/missing"),
            Q("<root>/missing/"),
            Q("<root>/missing/a/../b//"),
            Q(""),
            Q("."),
            Q("d"),
            Q("d/e/"),
            Q("missing"),
            Q("missing/"),
            Q("f/x"),
            Q("/"),
            Q("//"),
            Q("///"),
            Q("<root>/\udcff\udcfe"),
            Q("<root>/café"),
        ],
        files={"f": "x"},
        dirs=("d", "d/e"),
    )
    # Links: to a folder, to a file, dangling, through `..`.
    yield Case(
        [
            Q("<root>/ld"),
            Q("<root>/ld/"),
            Q("<root>/ld/e"),
            Q("<root>/lf"),
            Q("<root>/lf/"),
            Q("<root>/dangling"),
            Q("<root>/dangling/"),
            Q("<root>/up/d"),
            Q("ld"),
            Q("ld/e/.."),
        ],
        files={"f": "x"},
        dirs=("d", "d/e", "sub"),
        links={"ld": "d", "lf": "f", "dangling": "nothing/here", "up": "sub/.."},
    )
    # A folder that cannot be searched: unreachable, never gone.
    yield Case(
        [
            Q("<root>/locked"),
            Q("<root>/locked/x"),
            Q("<root>/locked/x/"),
            Q("<root>/ll/x"),
        ],
        dirs=("locked",),
        links={"ll": "locked"},
        modes={"locked": 0o000},
    )
    # A name at the length limit and one past it.
    long = "n" * NAME_MAX
    yield Case(
        [Q(f"<root>/{long}"), Q(f"<root>/{long}n"), Q(f"<root>/{long}n/")],
        dirs=(long,),
    )
    # The host's name: empty, raising, past ASCII, not UTF-8.
    yield Case(
        [
            Q("<root>", h=""),
            Q("<root>", h=None),
            Q("<root>", h="hôst"),
            Q("<root>", h="h\udcff"),
            Q("<root>/missing", h=None),
        ],
    )
    # `os.getcwd` raising: realpath and abspath of a relative path fall
    # through to the path as given; an absolute one is resolved.
    yield Case(
        [
            Q("d", g=0),
            Q("d/", g=0),
            Q("missing//", g=0),
            Q("ld", g=0),
            Q("<root>/ld/", g=0),
            Q("", g=0),
        ],
        dirs=("d",),
        links={"ld": "d"},
    )
    # Each errno `os.stat` may raise: gone or unreachable, with its text.
    yield Case(
        [Q("<root>/d", e=name) for name in INJECTED]
        + [Q("d/", e=name) for name in ("ENOENT", "EACCES")],
        dirs=("d",),
    )
    # Errno 0 leaves `os.strerror` unasked: the detail is `str(exc)`, the
    # path's `repr` in it (relative paths: no host path in the text; no
    # backslash, whose escaped pair the host-fact guard reads as UNC).
    yield Case(
        [
            Q(p, n=0)
            for p in ("d", "it's", "a'b\"c", "café\udcff", "t\tb", "\u200b\x7f", "")
        ],
        dirs=("d",),
    )
    # Every errno number, by the platform's numbering and texts.
    yield Case([Q("d", n=code) for code in ERRNO_NUMBERS], dirs=("d",), numbers=True)
    # Loops: ELOOP is gone (`_python`: 3.11's realpath walks a loop
    # otherwise).
    yield Case(
        [Q("<root>/s"), Q("<root>/s/"), Q("<root>/s/x"), Q("s")],
        links={"s": "s"},
        loop=True,
    )
    yield Case(
        [Q("<root>/a"), Q("<root>/b/x/"), Q("<root>/into")],
        links={"a": "b", "b": "a", "into": "a"},
        loop=True,
    )
    # A NUL in the path: `os.stat` raises ValueError, whose text is the
    # release's ("embedded null byte" before 3.12, "embedded null
    # character in path" from 3.12), so `_python` holds them.
    yield Case(
        [
            Q("<root>/a\x00b"),
            Q("<root>/a\x00b/"),
            Q("a\x00b//"),
            Q("\x00"),
            Q("a\x00b/", g=0),
        ],
        loop=True,
    )


def _spell(text: str, real: str) -> str:
    return _b(text.replace(real, "<scratch>").encode("utf-8", "surrogateescape"))


def _raiser(code: int) -> Any:
    """`os.stat` raising as CPython's own does for errno `code`: the C
    library's text, or ``Error`` where the errno is 0, and the path."""

    def stat(path: Any, *_a: Any, **_k: Any) -> Any:
        raise OSError(code, os.strerror(code) if code else "Error", path)

    return stat


def _hostname(spec: Any) -> Any:
    def gethostname() -> str:
        if spec is None:
            raise OSError(errno.EINVAL, "no name")
        return str(spec)

    return gethostname


def _query(q: dict[str, Any], real: str) -> dict[str, Any]:
    path = q["p"].replace("<root>", real)
    out: dict[str, Any] = {"p": _b(q["p"].encode("utf-8", "surrogateescape"))}
    patches: list[Any] = [
        mock.patch.object(socket, "gethostname", _hostname(q.get("h", HOST)))
    ]
    if "e" in q:
        out["e"] = q["e"]
        patches.append(mock.patch.object(os, "stat", _raiser(getattr(errno, q["e"]))))
    if "n" in q:
        out["n"] = q["n"]
        patches.append(mock.patch.object(os, "stat", _raiser(q["n"])))
    if "g" in q:
        out["g"] = 0

        def getcwd() -> str:
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT))

        patches.append(mock.patch.object(os, "getcwd", getcwd))
    if "h" in q:
        h = q["h"]
        out["h"] = None if h is None else _b(h.encode("utf-8", "surrogateescape"))
    for p in patches:
        p.start()
    try:
        view = _location_probe.view_path(path)
        answer = _location_probe.probe(path)
    finally:
        for p in reversed(patches):
            p.stop()
    out["v"] = _spell(view, real)
    o = dict(answer)
    o["cwd"] = _spell(o["cwd"], real)
    if "cwd_object" in o:
        st = os.stat(answer["cwd"])
        if o["cwd_object"] != [str(st.st_dev), str(st.st_ino), None]:
            raise RuntimeError(f"cwd_object is not the stat of {answer['cwd']!r}")
        o["cwd_object"] = "stat"
    if o.get("hostname") is not None:
        o["hostname"] = _b(o["hostname"].encode("utf-8", "surrogateescape"))
    out["o"] = o
    return out


def _answer(case: Case, root: Path) -> list[dict[str, Any]]:
    for d in case.dirs:
        (root / d).mkdir(parents=True, exist_ok=True)
    for rel, text in case.files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    saved = os.getcwd()
    os.chdir(root)
    try:
        real = os.getcwd()
        for rel, target in case.links.items():
            os.symlink(target.replace("<root>", real), Path(real) / rel)
        for rel, mode in case.modes.items():
            os.chmod(Path(real) / rel, mode)
        try:
            return [_query(q, real) for q in case.queries]
        finally:
            for rel in case.modes:
                os.chmod(Path(real) / rel, 0o700)
    finally:
        os.chdir(saved)


def _lines(loop: bool, numbers: bool = False) -> list[dict[str, Any]]:
    refuse_root()
    lines: list[dict[str, Any]] = []
    with _sandbox.world() as world:
        base = world.root / "location-probe"
        chosen = (c for c in _cases() if c.loop == loop and c.numbers == numbers)
        for i, case in enumerate(chosen):
            root = base / str(i)
            root.mkdir(parents=True)
            answers = _answer(case, root)
            shutil.rmtree(root)
            lines.append({**case.record(), "q": answers})
    return lines


def generate() -> dict[str, bytes]:
    lines = _lines(loop=False)
    header = {"fixture": "location_probe", "cases": len(lines)}
    return {"location_probe.jsonl": jsonl([header, *lines])}


def generate_python() -> dict[str, bytes]:
    lines = _lines(loop=True)
    header = {"fixture": "location_probe_python", "cases": len(lines)}
    return {"location_probe_python.jsonl": jsonl([header, *lines])}


def generate_errno() -> dict[str, bytes]:
    # The run of numbers goes past the last errno this platform names.
    last = max(errno.errorcode)
    if last + 1 not in ERRNO_NUMBERS:
        raise RuntimeError(
            f"ERRNO_NUMBERS must run past this platform's last errno, {last}"
            f" ({errno.errorcode[last]}), and lacks {last + 1}: extend its"
            f" range to range({last + 2}) and regenerate"
            " location_probe_errno.jsonl"
        )
    lines = _lines(loop=False, numbers=True)
    header = {"fixture": "location_probe_errno", "cases": len(lines)}
    return {"location_probe_errno.jsonl": jsonl([header, *lines])}

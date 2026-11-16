"""Fixture: one git config file read from real files
(``gitconfig_file.jsonl``).

The native build reads a config file through a file-system trait of its
own (``native/crates/bettermemory-core/src/fsdata.rs``); this pins it to
the reference's two readers of one file, `gitconfig._config_bytes` (with
`_accessible`) and `gitconfig.read_git_config`, and to the three OS calls
the trait answers for them: what ``os.open`` then ``os.fstat`` say of a
path, ``os.access(path, os.R_OK)``, and ``os.lstat``'s kind.

Each case is a tree as data written as real files in a fresh directory of
the scratch world (`_sandbox.world`), then one path in it read. The first
line is a header (the count); then one line per case:

- ``"f"``: files, path (relative to the case's directory) to bytes:
  ``=`` and the text where it is printable ASCII without a backslash, else
  hex, or ``[[piece, count], ...]``, the pieces repeated and joined (a
  recipe for the large files); ``"d"``: directories; ``"l"``: symbolic
  links, path to target; ``"p"``: FIFOs; ``"x"``: paths set to mode 000
  (after everything is made); ``"t"``: the path read, relative;
- ``"r"``: the answers. ``rg``: `read_git_config(path)`, the entries as
  ``[[name, value], ...]`` (bytes as above, a value null for a key with no
  ``=``) or ``{"why": text}``; ``rn``: the same with
  ``names={core.repositoryformatversion, extensions.}``, left out where
  it equals ``rg``; ``cb``: `_config_bytes(path)`, ``null`` (skipped),
  ``{"b": bytes or {"n": length, "sha256": hex}, "id": whether an
  identity was taken}``, ``{"dies": text}`` or ``{"over": path}``;
  ``cg``: the same with ``gentle=True``, left out where it equals ``cb``;
  ``fk``: the kind ``os.fstat`` gives after the open (``reg``, ``dir``,
  ``fifo``, ``sock``, ``other``), or the open's exception name; ``ac``:
  ``os.access(path, R_OK)``; ``ls``: the kind ``os.lstat`` gives
  (``link`` too) or its exception name.

Recorded texts name the case's directory as ``<scratch>``; no other path
of the machine is recorded but ``/dev/null``, a link's target.

Gate: POSIX only (`generate.POSIX_ONLY`). The trees hold symbolic links,
FIFOs, mode-000 files and directories and paths through a file, which
Windows answers otherwise (a directory's open fails there, which is why
`_accessible` exists). Nothing depends on case folding, Unicode
normalization (every name is ASCII or NFC UTF-8 and read as written), a
name only Linux stores (no non-UTF-8 name: macOS refuses one), the
process running as root (none of CI's jobs does), or the Python release:
the readers use ``os.open``, ``os.fstat``, ``os.read`` and ``os.access``,
which 3.11 to 3.14 answer alike. No git runs, and no environment variable
is read, so the user's git configuration cannot reach a byte.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from bettermemory import gitconfig

from . import _sandbox
from ._common import jsonl

_Content = bytes | list[tuple[bytes, int]]

LIMIT = 1 << 21
NAMES = frozenset({b"core.repositoryformatversion", b"extensions."})
PLAIN = b"[core]\n\trepositoryformatversion = 1\n[extensions]\n\tobjectformat = sha256\n[user]\n\tname = x\n"


def _b(data: bytes) -> str:
    if all(0x20 <= b < 0x7F and b != 0x5C for b in data):
        return "=" + data.decode("ascii")
    return data.hex()


def _content(data: _Content) -> Any:
    if isinstance(data, bytes):
        return _b(data)
    return [[_b(piece), count] for piece, count in data]


def _expand(data: _Content) -> bytes:
    if isinstance(data, bytes):
        return data
    return b"".join(piece * count for piece, count in data)


def _out(data: bytes) -> Any:
    if len(data) > 256:
        return {"n": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    return _b(data)


class Case:
    def __init__(
        self,
        target: str,
        files: Mapping[str, _Content] | None = None,
        *,
        dirs: tuple[str, ...] = (),
        links: dict[str, str] | None = None,
        fifos: tuple[str, ...] = (),
        locked: tuple[str, ...] = (),
        modes: dict[str, int] | None = None,
        hard: dict[str, str] | None = None,
        same: str | None = None,
        empty: bool = False,
    ) -> None:
        self.target = target
        self.files = dict(files or {})
        self.dirs = dirs
        self.links = links or {}
        self.fifos = fifos
        self.modes = {rel: 0 for rel in locked} | (modes or {})
        self.hard = hard or {}
        self.same = same
        self.empty = empty
        # macOS stops a path at 32 links and Linux at 40: a chain that ends
        # between the two would read apart. A case holds fewer than 30
        # links, so a chain that ends is shorter, and one that loops fails
        # at either bound alike.
        assert len(self.links) < 30, target

    def record(self) -> dict[str, Any]:
        out: dict[str, Any] = {"f": {k: _content(v) for k, v in self.files.items()}}
        if self.dirs:
            out["d"] = list(self.dirs)
        if self.links:
            out["l"] = dict(self.links)
        if self.fifos:
            out["p"] = list(self.fifos)
        if self.hard:
            out["h"] = dict(self.hard)
        if self.modes:
            out["m"] = {k: f"{v:03o}" for k, v in self.modes.items()}
        if self.same is not None:
            out["s"] = self.same
        if self.empty:
            out["e"] = 1
        out["t"] = self.target
        return out


def _kind(mode: int) -> str:
    if stat.S_ISLNK(mode):
        return "link"
    if stat.S_ISREG(mode):
        return "reg"
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISSOCK(mode):
        return "sock"
    return "other"


def _entries(read: gitconfig.GitConfigRead, label: Any) -> Any:
    if read.why is not None:
        return {"why": label(read.why)}
    return [[_b(n), None if v is None else _b(v)] for n, v in read.entries]


def _answer(case: Case, root: Path) -> dict[str, Any]:
    for d in case.dirs:
        (root / d).mkdir(parents=True, exist_ok=True)
    for rel, data in case.files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_expand(data))
    for rel, existing in case.hard.items():
        os.link(root / existing, root / rel)
    for rel, target in case.links.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, path)
    for rel in case.fifos:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(path)  # type: ignore[attr-defined,unused-ignore]
    by_depth = sorted(case.modes, key=lambda rel: rel.count("/"))
    for rel in reversed(by_depth):
        os.chmod(root / rel, case.modes[rel])
    prefix = str(root)

    def label(text: str) -> str:
        return text.replace(prefix, "<scratch>")

    name = "" if case.empty else prefix + "/" + case.target
    try:
        out: dict[str, Any] = {"rg": _entries(gitconfig.read_git_config(name), label)}
        named = _entries(gitconfig.read_git_config(name, NAMES), label)
        if named != out["rg"]:
            out["rn"] = named

        def identity_of(at: str) -> tuple[int, int] | None:
            identity: list[tuple[int, int]] = []
            try:
                gitconfig._config_bytes(at, identity)
            except (gitconfig._Dies, gitconfig._Overflow):
                return None
            return identity[0] if identity else None

        if case.same is not None:
            mine = identity_of(name)
            other = identity_of(prefix + "/" + case.same)
            out["si"] = None if mine is None or other is None else mine == other

        def config_bytes(gentle: bool) -> Any:
            identity: list[tuple[int, int]] = []
            try:
                data = gitconfig._config_bytes(name, identity, gentle=gentle)
            except gitconfig._Dies as exc:
                return {"dies": label(exc.why)}
            except gitconfig._Overflow as exc:
                return {"over": label(str(exc))}
            if data is None:
                return None
            return {"b": _out(data), "id": bool(identity)}

        out["cb"] = config_bytes(False)
        gentle = config_bytes(True)
        if gentle != out["cb"]:
            out["cg"] = gentle
        try:
            fd = os.open(name, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        except (OSError, ValueError) as exc:
            out["fk"] = type(exc).__name__
        else:
            try:
                out["fk"] = _kind(os.fstat(fd).st_mode)
            finally:
                os.close(fd)
        out["ac"] = gitconfig._accessible(name)
        try:
            out["ls"] = _kind(os.lstat(name).st_mode)
        except (OSError, ValueError) as exc:
            out["ls"] = type(exc).__name__
    finally:
        for rel in by_depth:
            os.chmod(root / rel, 0o700)
    return out


def _cases() -> Iterator[Case]:
    # Regular files: plain, empty, every name kept, NULs, refused lines.
    yield Case("c", {"c": PLAIN})
    yield Case("c", {"c": b""})
    yield Case("c", {"c": b"[extensions]\n\tWorkTreeConfig\n[Core]\nBare=false\n"})
    yield Case("c", {"c": b'[a]\n\tb = x\x00y\n[core "sub"]\n\tk = v\n'})
    yield Case("c", {"c": b"[a\n"})
    yield Case("c", {"c": b'[core]\n\trepositoryformatversion = "1\n'})
    yield Case("c", {"c": b"\xef\xbb\xbf[core]\n\tbare\n"})
    yield Case("c", {"c": b"[core]\r\n\tbare = true\r\n"})
    yield Case("c", {"c": b"x = 1\n"})
    # Names: a space, NFC UTF-8, a newline, a backslash, a quote.
    for name in ("a b", "été", "a\nb", "a\\b", 'a"b', "-x", ".c"):
        yield Case(name, {name: PLAIN})
    # Missing: a file, a directory of the path, a trailing slash.
    yield Case("missing")
    yield Case("no/such/file")
    yield Case("c/", {"c": PLAIN})
    yield Case("c/x", {"c": PLAIN})
    yield Case("d/c/x", {"d/c": PLAIN})
    yield Case("c/.", {"c": PLAIN})
    yield Case("d/../c", {"c": PLAIN}, dirs=("d",))
    yield Case("d/../c", {"c": PLAIN})
    # Directories.
    yield Case("d", dirs=("d",))
    yield Case("d/", dirs=("d",))
    yield Case(".", dirs=())
    # Links: to a file, a directory, nothing, itself, a loop, a chain, the
    # null device, through a link to a directory.
    yield Case("l", {"c": PLAIN}, links={"l": "c"})
    yield Case("l", dirs=("d",), links={"l": "d"})
    yield Case("l/", dirs=("d",), links={"l": "d"})
    yield Case("l", links={"l": "missing"})
    yield Case("l", links={"l": "l"})
    yield Case("a", links={"a": "b", "b": "a"})
    yield Case("a", {"c": PLAIN}, links={"a": "b", "b": "c"})
    yield Case("l", links={"l": "/dev/null"})
    yield Case("l/c", {"d/c": PLAIN}, links={"l": "d"})
    yield Case("l/x", {"c": PLAIN}, links={"l": "c"})
    yield Case("l", {"d/c": PLAIN}, links={"l": "d/../d/c"})
    # FIFOs, through a link too.
    yield Case("p", fifos=("p",))
    yield Case("l", fifos=("p",), links={"l": "p"})
    yield Case("p/", fifos=("p",))
    # Mode 000: a file, a directory, a file inside one.
    yield Case("c", {"c": PLAIN}, locked=("c",))
    yield Case("d", dirs=("d",), locked=("d",))
    yield Case("d/c", {"d/c": PLAIN}, locked=("d",))
    yield Case("d/missing", dirs=("d",), locked=("d",))
    yield Case("l", {"c": PLAIN}, links={"l": "c"}, locked=("c",))
    # Modes 100 to 700: a file, a directory, a file in a directory.
    for mode in range(0o100, 0o1000, 0o100):
        yield Case("c", {"c": PLAIN}, modes={"c": mode})
        yield Case("d", dirs=("d",), modes={"d": mode})
        yield Case("d/c", {"d/c": PLAIN}, modes={"d": mode})
    # `..` and `.` through a directory that cannot be searched.
    yield Case("d/../c", {"c": PLAIN}, dirs=("d",), locked=("d",))
    yield Case("d/../..", dirs=("d",), locked=("d",))
    yield Case("d/../b/", dirs=("d", "b"), locked=("d",))
    yield Case("d/.", dirs=("d",), locked=("d",))
    yield Case("l/../c", {"c": PLAIN, "e/x": b""}, links={"l": "e"}, locked=("e",))
    # A trailing separator needs no search permission on the directory
    # it names; `/.` does.
    yield Case("d/", dirs=("d",), locked=("d",))
    yield Case("d/e/", dirs=("d/e",), modes={"d/e": 0o200})
    yield Case("d/e/.", dirs=("d/e",), modes={"d/e": 0o200})
    yield Case("l/", dirs=("d",), links={"l": "d"}, locked=("d",))
    # The empty path.
    yield Case("", empty=True)
    # A link's target ending in a separator.
    yield Case("l", links={"l": "/dev/null/"})
    yield Case("l", {"c": PLAIN}, links={"l": "c/"})
    yield Case("l", dirs=("d",), links={"l": "d/"})
    # The zero device, without end.
    yield Case("l", links={"l": "/dev/zero"})
    # Hard links: one identity under two names; a copy has its own.
    yield Case("h", {"c": PLAIN}, hard={"h": "c"}, same="c")
    yield Case("h", {"c": PLAIN}, hard={"h": "c"}, modes={"c": 0}, same="c")
    yield Case("l", {"c": PLAIN}, links={"l": "c"}, same="c")
    yield Case("e", {"c": PLAIN, "e": PLAIN}, same="c")
    # Sizes: the bound minus one, the bound, one past, well past.
    head = b"[core]\n\tbare = true\n#"
    for size in (LIMIT - 1, LIMIT, LIMIT + 1, LIMIT + 4096):
        yield Case("c", {"c": [(head, 1), (b"x", size - len(head))]})
    yield Case("c", {"c": [(b"[a]\n", 1), (b"x = 1\n", (LIMIT + 6) // 6)]})
    yield Case("l", {"c": [(head, 1), (b"x", LIMIT + 1 - len(head))]}, links={"l": "c"})


def generate() -> dict[str, bytes]:
    cases = list(_cases())
    lines: list[dict[str, Any]] = []
    with _sandbox.world() as world:
        base = world.root / "gitconfig-file"
        for i, case in enumerate(cases):
            root = base / str(i)
            root.mkdir(parents=True)
            answer = _answer(case, root)
            shutil.rmtree(root)
            lines.append({**case.record(), "r": answer})
    header = {"fixture": "gitconfig_file", "cases": len(cases)}
    return {"gitconfig_file.jsonl": jsonl([header, *lines])}

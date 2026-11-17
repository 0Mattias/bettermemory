"""Fixture: the answers about the git on PATH recorded in the state
directory, and whether one stands (``gitsystem_record.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/gitsystem/record.rs``; this pins it
to `gitsystem`'s `_key` (with `_developer`), `_entries`, `_recorded`,
`_recorded_version`, `_current`, `_kept` and `_record`.

Each case is a tree under a scratch root (``"f"`` files, each a `_content`
recipe, ``"d"`` folders, ``"l"`` symbolic links and their targets, ``"m"``
modes; every other file 644, every folder 755): ``bin/git`` stands for
the git binary and ``state`` for the state directory, the record
``state/git-config-sources-v5.json``. In a file's bytes ``@ROOT@`` is
the root's path and ``@STAMP0@`` the JSON of ``bin/git``'s stamp once the
tree is built. Then ``"s"`` the steps, each answered in ``"r"``:

- ``["recorded", key]``: `_recorded`, ``a`` the answer (null for none);
- ``["current", answer, now, stamp]``: `_current`, ``c``;
- ``["kept", key, now, [answer or null, ...]]``: `_kept`, ``a``;
- ``["record", answer]``: `_record`; ``w`` the record after it (its
  bytes, or the kind of what is there: ``"dir"``, ``"link"``, null for
  nothing), ``md`` its mode, ``sd`` the state directory's, ``ls`` the
  state directory's names, sorted;
- ``["key", path, macos, developer]``: `_key`, ``k``, with PATH and
  DEVELOPER_DIR the bytes given (null: unset, decoded as `os.fsdecode`
  does) and `XCODE_SELECT_LINK` the root's ``xcode``;
- ``["change", kind, path, data]`` as in ``location_stamped.jsonl``;
  ``["put", path, content]`` writes a file.

Where the reference raises out of the step, ``x`` names the exception (an
OverflowError of `int()` or `float()`, a RecursionError of `json.loads`).

An answer is ``{"k", "g", "st", "a", "ok", "o", "sy", "lg", "rs", "px",
"cd", "hc", "v", "t"}``: the key, git, stamp, asked, ok, own, system,
legacy, resets, prefix, compat_differ, hasconfig, version and tried.
Text is `_b` of its ``surrogatepass`` bytes, a leading ``@/`` the root;
a time is its ``repr``; a stamp five decimal texts, or ``"<git>"`` (in)
for ``bin/git``'s now (``"<git~i>"``: with its ``i``-th part one more), and ``"<git:i>"`` (out) for the ``i``-th stamp of
``bin/git`` seen (0 once the tree is built, the next at each ``"<git>"``
that differs). In a written record the root is ``@ROOT@`` and each stamp
seen ``@STAMPi@``; one longer than 2,048 bytes is ``{"n", "sha256"}``.

Records nested more than `DEEP` levels are in
``gitsystem_record_python.jsonl``, Python-gated: whether ``json.loads``
raises RecursionError is the interpreter's.

POSIX only (`generate.POSIX_ONLY`): modes, links, stamps by inode and
change time, the host's ``os.path``. Not run as root
(`gen_fs_realpath.refuse_root`). No FIFO stands for the record: the
reference opens it blocking and waits for a writer (the native read opens
it without blocking and reads nothing: ``gitsystem_record_robustness``).
"""

from __future__ import annotations

import hashlib
import math
import json
import os
import shutil
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest import mock

from bettermemory import gitconfig, gitsystem

from . import _sandbox
from ._common import jsonl
from .gen_fs_realpath import refuse_root
from .gen_gitconfig_file import _b, _Content, _content, _expand
from .gen_location_stamped import _change

K = "k" * 32
K2 = "m" * 32
GIT = "@/bin/git"
#: The longest written record shown whole.
SHOWN = 512


def _t(text: str) -> Any:
    """Text as the fixture spells it: `_sp` of its ``surrogatepass``
    bytes."""
    return _sp(text.encode("utf-8", "surrogatepass"))


def _sp(data: bytes) -> Any:
    """Bytes as `_b` spells them, or, where a byte runs 32 times or more,
    as `_content`'s recipe of pieces and counts."""
    runs: list[tuple[bytes, int]] = []
    plain = b""
    i = 0
    while i < len(data):
        j = i
        while j < len(data) and data[j] == data[i]:
            j += 1
        if j - i >= 32:
            if plain:
                runs.append((plain, 1))
                plain = b""
            runs.append((data[i : i + 1], j - i))
        else:
            plain += data[i:j]
        i = j
    if not runs:
        return _b(data)
    if plain:
        runs.append((plain, 1))
    return _content(runs)


def _answer_spec(**over: Any) -> dict[str, Any]:
    """A whole answer git gave for `K`, asked at 1000.0 (`SPEC`), with
    `over`: the fixture holds `over` alone."""
    return dict(over)


#: The answer a step's answer changes (the header's ``"answer"``).
SPEC: dict[str, Any] = {
    "k": K,
    "g": _t(GIT),
    "st": "<git>",
    "a": "1000.0",
    "ok": True,
    "o": [_t("/etc/a")],
    "sy": _t("/etc/gitconfig"),
    "lg": True,
    "rs": True,
    "px": _t("/usr"),
    "cd": False,
    "hc": True,
    "v": [2, 54, 0],
    "t": None,
}


#: A whole entry as a record holds it, for `K`.
BASE: dict[str, Any] = {
    "path": K,
    "git": "@ROOT@/bin/git",
    "stamp": "@STAMP0@",
    "asked": 1000.0,
    "ok": True,
    "own": ["/etc/a"],
    "system": "/etc/gitconfig",
    "legacy": True,
    "resets": True,
    "prefix": "/usr",
    "compat_differ": False,
    "hasconfig": True,
    "version": [2, 54, 0],
    "tried": None,
}


def _entry_text(entry: dict[str, Any]) -> str:
    text = json.dumps(entry)
    return text.replace('"@STAMP0@"', "@STAMP0@")


def _record_text(*entries: Any, fmt: Any = 5) -> bytes:
    """A record of `entries` (dicts, or raw JSON text)."""
    parts = [e if isinstance(e, str) else _entry_text(e) for e in entries]
    return (
        '{"format": %s, "entries": [%s]}\n' % (json.dumps(fmt), ", ".join(parts))
    ).encode("ascii")


def _many(entry: str, n: int, *, last: bool = True) -> _Content:
    """A record of `entry` `n` times, then `BASE`'s where `last`."""
    head = b'{"format": 5, "entries": ['
    if not last:
        return [
            (head, 1),
            (entry.encode() + b", ", n - 1),
            (entry.encode() + b"]}\n", 1),
        ]
    return [
        (head, 1),
        (entry.encode() + b", ", n),
        (_entry_text(BASE).encode() + b"]}\n", 1),
    ]


def _with(**over: Any) -> dict[str, Any]:
    entry = dict(BASE)
    for name, value in over.items():
        if value is _GONE:
            entry.pop(name, None)
        else:
            entry[name] = value
    return entry


_GONE = object()
RECORD = "state/" + gitsystem.RECORD
#: The encodings a record is put in (``"e"``: the name and whether a BOM
#: leads), each of its ASCII text: `json.detect_encoding` reads them.
ENCODINGS = {
    "u16le": "utf-16-le",
    "u16be": "utf-16-be",
    "u32le": "utf-32-le",
    "u32be": "utf-32-be",
}
BOMS = {
    "u16le": b"\xff\xfe",
    "u16be": b"\xfe\xff",
    "u32le": b"\xff\xfe\x00\x00",
    "u32be": b"\x00\x00\xfe\xff",
}
#: The record of `BASE` alone (the header's ``"base"``).
WHOLE = _record_text(BASE)
#: An answer out as `SPEC` reads back (the header's ``"out"``): an answer
#: is written as the fields that differ from it.
OUT: dict[str, Any] = {
    "k": K,
    "g": _t(GIT),
    "st": "<git:0>",
    "a": "1000.0",
    "ok": True,
    "o": [_t("/etc/a")],
    "sy": _t("/etc/gitconfig"),
    "lg": True,
    "rs": True,
    "px": _t("/usr"),
    "cd": False,
    "hc": True,
    "v": [2, 54, 0],
    "t": None,
}


class Case:
    def __init__(
        self,
        steps: list[list[Any]],
        record: _Content | None = None,
        *,
        files: dict[str, _Content] | None = None,
        dirs: tuple[str, ...] = (),
        links: dict[str, str] | None = None,
        modes: dict[str, int] | None = None,
        enc: tuple[str, bool] | None = None,
    ) -> None:
        self.enc = enc
        self.files: dict[str, _Content] = {"bin/git": b"git\n"}
        if record is not None:
            self.files[RECORD] = record
        self.files.update(files or {})
        self.dirs = ("state", *dirs)
        self.links = dict(links or {})
        self.modes = dict(modes or {})
        self.steps = steps

    def record(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        files = {
            k: _spelled(v) if isinstance(v, bytes) else _content(v)
            for k, v in self.files.items()
            if (k, v) != ("bin/git", b"git\n")
        }
        if files:
            out["f"] = files
        if self.dirs[1:]:
            out["d"] = list(self.dirs[1:])
        if self.enc:
            out["e"] = list(self.enc)
        if self.links:
            out["l"] = self.links
        if self.modes:
            out["m"] = {k: f"{v:03o}" for k, v in self.modes.items()}
        out["s"] = self.steps
        return out


def _stamp_text(stamp: Any) -> str:
    return json.dumps(list(stamp))


def _shown(data: bytes) -> Any:
    """Written bytes: `_spelled`, or past `SHOWN` bytes their length and
    SHA-256."""
    if len(data) > SHOWN:
        return {"n": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    return _spelled(data)


def _spelled(data: bytes) -> Any:
    """Bytes as `_b` spells them, or as ``{"d": [i, k, mid]}``: the
    header's ``"base"`` (`WHOLE`) with all but its first ``i`` and last
    ``k`` bytes replaced by ``mid`` (`_b`), where that is shorter."""
    i = 0
    while i < min(len(data), len(WHOLE)) and data[i] == WHOLE[i]:
        i += 1
    k = 0
    while k < min(len(data), len(WHOLE)) - i and data[-1 - k] == WHOLE[-1 - k]:
        k += 1
    diff = {"d": [i, k, _sp(data[i : len(data) - k])]}
    plain = _sp(data)
    return diff if len(json.dumps(diff)) < len(json.dumps(plain)) else plain


def _run(case: Case, root: Path) -> list[Any]:
    root = Path(os.path.realpath(root))
    prefix = str(root)
    for rel in case.dirs:
        (root / rel).mkdir(parents=True, exist_ok=True)
    for rel, data in case.files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(b"")
    for rel, target in case.links.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target.replace("@/", prefix + "/", 1), root / rel)
    for path in sorted(root.rglob("*"), key=lambda p: -len(p.parts)):
        if path.is_symlink():
            continue
        os.chmod(path, 0o755 if path.is_dir() else 0o644)
    git_path = prefix + "/bin/git"
    (root / "bin/git").write_bytes(_expand(case.files["bin/git"]))
    for rel, mode in case.modes.items():
        if rel == "bin/git":
            os.chmod(root / rel, mode)
    seen: list[Any] = [gitconfig.file_stamp(git_path)]

    def fill(data: bytes, stamp: Any) -> bytes:
        return data.replace(b"@ROOT@", prefix.encode()).replace(
            b"@STAMP0@", _stamp_text(stamp).encode()
        )

    for rel, data in case.files.items():
        if rel != "bin/git":
            filled = fill(_expand(data), seen[0])
            if rel == RECORD and case.enc:
                name, bom = case.enc
                filled = (BOMS[name] if bom else b"") + filled.decode("ascii").encode(
                    ENCODINGS[name]
                )
            (root / rel).write_bytes(filled)
    for rel, mode in case.modes.items():
        if rel != "bin/git":
            os.chmod(root / rel, mode)

    def txt(spelled: Any) -> str | None:
        raw = txt_bytes(spelled)
        if raw is None:
            return None
        text = raw.decode("utf-8", "surrogatepass")
        return prefix + text[1:] if text.startswith("@/") else text

    def lab(text: str | None) -> str | None:
        if text is None:
            return None
        if text.startswith(prefix + "/"):
            text = "@" + text[len(prefix) :]
        return _t(text)

    def git_now() -> Any:
        stamp = gitconfig.file_stamp(git_path)
        if stamp not in seen:
            seen.append(stamp)
        return stamp

    def answer_in(over: dict[str, Any]) -> gitsystem._Answer:
        spec = {**SPEC, **over}
        st = spec["st"]
        if st == "<git>":
            stamp = git_now()
        elif isinstance(st, str) and st.startswith("<git~"):
            # `bin/git`'s stamp now with one part moved by one.
            moved = int(st[5])
            stamp = tuple(x + (j == moved) for j, x in enumerate(git_now() or ()))
        else:
            stamp = None if st is None else tuple(int(x) for x in st)
        return gitsystem._Answer(
            key=spec["k"],
            git=txt(spec["g"]),
            stamp=stamp,
            asked=float(spec["a"]),
            ok=spec["ok"],
            sources=gitsystem.Sources(
                tuple(txt(o) or "" for o in spec["o"]),
                txt(spec["sy"]),
                spec["lg"],
                spec["rs"],
                txt(spec["px"]),
                spec["cd"],
                spec["hc"],
                None if spec["v"] is None else tuple(spec["v"]),
            ),
            tried=None if spec["t"] is None else float(spec["t"]),
        )

    def answer_out(a: gitsystem._Answer | None) -> Any:
        if a is None:
            return None
        if a.stamp is None:
            st: Any = None
        elif a.stamp in seen:
            st = f"<git:{seen.index(a.stamp)}>"
        else:
            st = [str(x) for x in a.stamp]
        s = a.sources
        full = {
            "k": a.key,
            "g": lab(a.git),
            "st": st,
            "a": repr(a.asked),
            "ok": a.ok,
            "o": [lab(o) for o in s.own],
            "sy": lab(s.system),
            "lg": s.legacy,
            "rs": s.resets,
            "px": lab(s.prefix),
            "cd": s.compat_differ,
            "hc": s.hasconfig,
            "v": None if s.version is None else list(s.version),
            "t": None if a.tried is None else repr(a.tried),
        }
        return {k: v for k, v in full.items() if v != OUT.get(k, _GONE)}

    def written() -> dict[str, Any]:
        path = root / RECORD
        out: dict[str, Any] = {}
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            out["w"] = None
        else:
            if stat.S_ISLNK(st.st_mode):
                out["w"] = "link"
            elif stat.S_ISDIR(st.st_mode):
                out["w"] = "dir"
            else:
                data = path.read_bytes().replace(prefix.encode(), b"@ROOT@")
                for i, stamp in enumerate(seen):
                    if stamp is None:
                        continue
                    data = data.replace(
                        _stamp_text(stamp).encode(), f"@STAMP{i}@".encode()
                    )
                out["w"] = _shown(data)
            out["md"] = f"{stat.S_IMODE(st.st_mode):03o}"
        state = root / "state"
        out["sd"] = f"{stat.S_IMODE(os.lstat(state).st_mode):03o}"
        try:
            out["ls"] = sorted(os.listdir(state))
        except OSError:
            out["ls"] = None
        return out

    out: list[Any] = []
    changes = 0
    saved = os.getcwd()
    os.chdir(root)
    try:
        with mock.patch.object(gitsystem, "_record_path", lambda: root / RECORD):
            for step in case.steps:
                kind = step[0]
                got: dict[str, Any] = {}
                try:
                    if kind == "recorded":
                        got["a"] = answer_out(gitsystem._recorded(step[1]))
                    elif kind == "current":
                        got["c"] = gitsystem._current(
                            answer_in(step[1]), float(step[2]), stamp=step[3]
                        )
                    elif kind == "kept":
                        seen_answers = [
                            None if s is None else answer_in(s) for s in step[3]
                        ]
                        got["a"] = answer_out(
                            gitsystem._kept(step[1], float(step[2]), *seen_answers)
                        )
                    elif kind == "record":
                        gitsystem._record(answer_in(step[1]))
                        got |= written()
                    elif kind == "key":
                        got["k"] = _key(root, step)
                    elif kind == "change":
                        _change(root, step[1], step[2], txt_bytes(step[3]), changes)
                        changes += 1
                    elif kind == "put":
                        (root / step[1]).write_bytes(
                            fill(_expand(_unspell(step[2])), seen[0])
                        )
                    else:
                        raise ValueError(kind)
                except (OverflowError, RecursionError) as exc:
                    got = {"x": type(exc).__name__}
                out.append(got)
    finally:
        os.chdir(saved)
        for path in root.rglob("*"):
            if not path.is_symlink():
                try:
                    os.chmod(path, 0o700)
                except OSError:
                    pass
    return out


def txt_bytes(spelled: Any) -> bytes | None:
    if spelled is None:
        return None
    if isinstance(spelled, list):
        return _expand([(txt_bytes(p) or b"", n) for p, n in spelled])
    return spelled[1:].encode() if spelled.startswith("=") else bytes.fromhex(spelled)


def _unspell(content: Any) -> _Content:
    if isinstance(content, dict):
        i, k, mid = content["d"]
        return WHOLE[:i] + (txt_bytes(mid) or b"") + WHOLE[len(WHOLE) - k :]
    if isinstance(content, str):
        return txt_bytes(content) or b""
    return [(txt_bytes(p) or b"", n) for p, n in content]


def _key(root: Path, step: list[Any]) -> str:
    _, path, macos, developer = step
    env = dict(os.environ)
    env.pop("PATH", None)
    env.pop("DEVELOPER_DIR", None)
    if path is not None:
        env["PATH"] = os.fsdecode(txt_bytes(path) or b"")
    if developer is not None:
        env["DEVELOPER_DIR"] = os.fsdecode(txt_bytes(developer) or b"")
    with (
        mock.patch.dict(os.environ, env, clear=True),
        mock.patch.object(gitsystem, "_MACOS", macos),
        mock.patch.object(gitsystem, "XCODE_SELECT_LINK", str(root / "xcode")),
    ):
        return gitsystem._key(os.environ.get("PATH"))


def _cases() -> Iterator[Case]:
    rec = ["recorded", K]
    whole = _record_text(BASE)
    # The record's file in every shape: none, empty, not JSON, the wrong
    # JSON type at the top, truncated, another encoding.
    yield Case([rec])
    for data in (
        b"",
        b"   ",
        b"null",
        b"[]",
        b"5",
        b'"x"',
        b"{}",
        b"hello",
        b"\x00\xff\xfe",
        b"{",
        whole[:-10],
        whole[: len(whole) // 2],
        whole + b"x",
        b"\xef\xbb\xbf" + whole,
        whole.replace(b"/etc/a", b"/etc/\xff"),
        whole.replace(b"/etc/a", b"/etc/\xed\xa0\x80"),
    ):
        yield Case([rec], data)
    for enc in ENCODINGS:
        for bom in (True, False):
            yield Case([rec], whole, enc=(enc, bom))
    # The format and entries.
    for fmt in (4, 6, "5", 5.0, 5.5, True, None, [5]):
        yield Case([rec], _record_text(BASE, fmt=fmt))
    yield Case([rec], whole.replace(b'"format": 5, ', b""))
    for entries in (b"null", b"{}", b'"x"', b"5", b"[]", b"[[]]"):
        yield Case([rec], b'{"format": 5, "entries": ' + entries + b"}")
    yield Case([rec], _record_text("5", "null", '"x"', "[]", _entry_text(BASE)))
    # The eighth entry is read, the ninth not, a non-dict counted.
    other = _with(path=K2)
    yield Case([rec, ["recorded", K2]], _many(_entry_text(other), 7))
    yield Case([rec], _many(_entry_text(other), 8))
    yield Case([rec], _many("5", 7))
    yield Case([rec], _many("5", 8))
    # The first entry for the key decides, a bad one too.
    yield Case([rec], _record_text(_with(own=5), BASE))
    yield Case([rec], _record_text(BASE, _with(asked=1.0)))
    # Duplicate keys: the last stands.
    dup = _entry_text(BASE)[:-1] + ', "asked": 7.0, "git": 5}'
    yield Case([rec], _record_text(dup))
    dup2 = _entry_text(_with(git=5))[:-1] + ', "git": "@ROOT@/bin/git"}'
    yield Case([rec], _record_text(dup2))
    yield Case([rec], whole.replace(b'{"format": 5', b'{"entries": 5, "format": 5'))
    # An entry for another git, and for another key.
    yield Case([rec], _record_text(_with(path=K2)))
    yield Case([rec], _record_text(_with(path=5)))
    yield Case([rec], _record_text(_with(path=_GONE)))
    yield Case([rec], _record_text(_with(git="@ROOT@/bin/other")))
    # Every field, missing, null and of every wrong type.
    shapes: list[Any] = [_GONE, None, 0, 1.5, "s", [], {}, True, False]
    for name in BASE:
        if name == "path":
            continue
        for shape in shapes:
            yield Case([rec], _record_text(_with(**{name: shape})))
    # Text bounds: empty, 4,096 and 4,097 code points, a NUL, surrogates.
    for name in ("git", "system"):
        for value in (
            "",
            "/" + "a" * 4095,
            "/" + "a" * 4096,
            "/a\u0000b",
            "/\ud800",
            "/\udc80",
            "/é",
        ):
            yield Case([rec], _record_text(_with(**{name: value})))
    for own in (
        [],
        ["/e"] * 8,
        ["/e"] * 9,
        ["/a", ""],
        ["/a", 5],
        ["/a", None],
        ["/a", "/a"],
        ["/" + "a" * 4096],
        ["/a\u0000"],
        ["/\ud800", "/\U0001f600"],
    ):
        yield Case([rec], _record_text(_with(own=own)))
    for prefix in ("", "/p\u0000", "/\ud800", "%(prefix)", "a" * 5000):
        yield Case([rec], _record_text(_with(prefix=prefix)))
    # The stamp: lengths, and each part as `int()` takes or refuses it.
    for stamp_text in (
        "[1, 2, 3, 4]",
        "[1, 2, 3, 4, 5, 6]",
        "[1, 2, 3, 4, 5]",
        "[true, false, 3, 4, 5]",
        "[1.9, -1.9, 3, 4, 5]",
        '["12", " 13 ", "1_4", "\\u0661\\u0662", "+5"]',
        '["x", 2, 3, 4, 5]',
        '["1__2", 2, 3, 4, 5]',
        '["1" , 2, 3, 4, "' + "9" * 4301 + '"]',
        "[null, 2, 3, 4, 5]",
        "[[], 2, 3, 4, 5]",
        "[NaN, 2, 3, 4, 5]",
        "[Infinity, 2, 3, 4, 5]",
        "[1, 2, 3, 4, -Infinity]",
        '["x", 2, 3, 4, Infinity]',
        '[Infinity, 2, 3, 4, "x"]',
        "[1e300, 2, 3, 4, 5]",
        "[" + "9" * 40 + ", 2, 3, 4, 5]",
        "[-1, -1, -1, -1, -1]",
        "{}",
        '"12345"',
    ):
        entry = _entry_text(BASE).replace("@STAMP0@", stamp_text)
        yield Case([rec], _record_text(entry))
    # `asked` and `tried` as `float()` takes or refuses them.
    for value_text in (
        "1000",
        "-0.0",
        '"1000"',
        '" 1e3 "',
        '"\\u000b5"',
        '"\\u001c5"',
        '"\\u20005\\u3000"',
        '"\\u0661"',
        '"1_000.5"',
        '"nan"',
        '"-Infinity"',
        '"0x10"',
        '"\\ud800"',
        "true",
        "NaN",
        "Infinity",
        "1e400",
        "1" + "0" * 400,
        "1" + "0" * 300,
        "179769313486231580793728971405303415079934132710037826936173778980444968292764750946649017977587207096330286416692887910946555547851940402630657488671505820681908902000708383676273854845817711531764475730270069855571366959622842914819860834936475292719074168444365510704342711559699508093042880177904174497791",
        "179769313486231580793728971405303415079934132710037826936173778980444968292764750946649017977587207096330286416692887910946555547851940402630657488671505820681908902000708383676273854845817711531764475730270069855571366959622842914819860834936475292719074168444365510704342711559699508093042880177904174497792",
        "0.1",
        "1e-320",
    ):
        for name in ("asked", "tried"):
            entry = _entry_text(BASE).replace(
                f'"{name}": ' + json.dumps(BASE[name]), f'"{name}": {value_text}'
            )
            yield Case([rec], _record_text(entry))
    # `asked` raises before `tried` is read; a missing `asked` hides both.
    yield Case(
        [rec],
        _record_text(
            _entry_text(BASE)
            .replace('"asked": 1000.0', '"asked": 1e999')
            .replace('"tried": null', '"tried": "x"')
        ),
    )
    yield Case(
        [rec],
        _record_text(
            _entry_text(_with(asked=_GONE)).replace('"tried": null', '"tried": 1e999')
        ),
    )
    # `ok` is True alone, and needs git and a stamp.
    for ok in (1, "true", None, False):
        yield Case([rec], _record_text(_with(ok=ok)))
    yield Case([rec], _record_text(_with(git=None)))
    yield Case([rec], _record_text(_with(stamp=None)))
    # The version in every shape `_recorded_version` takes or refuses.
    for version in (
        [0, 0, 0],
        [65535, 65535, 65535],
        [65536, 0, 0],
        [-1, 0, 0],
        [2, 54],
        [2, 54, 0, 1],
        [True, 54, 0],
        [2.0, 54, 0],
        [2, "54", 0],
        [2, None, 0],
        "2.54.0",
        {"a": 1},
        [10**30, 0, 0],
        [[2], 54, 0],
    ):
        yield Case([rec], _record_text(_with(version=version)))
    yield Case(
        [rec], _record_text(_entry_text(BASE).replace("[2, 54, 0]", "[2, 054, 0]"))
    )
    # Bounds: 65,536 bytes read, 65,537 not; 10,000 entries; nesting.
    # No placeholder in these two: the bytes on disk are the bytes here,
    # whatever the root's path.
    plain = _record_text(_with(git="/usr/bin/git", stamp=[1, 2, 3, 4, 5]))
    pad = 65536 - len(plain)
    yield Case([rec], [(plain[:-2], 1), (b" ", pad), (b"}\n", 1)])
    yield Case([rec], [(plain[:-2], 1), (b" ", pad + 1), (b"}\n", 1)])
    yield Case([rec], [(whole[:-3], 1), (b", {}", 10_000), (b"]}\n", 1)])
    yield Case(
        [rec],
        [
            (b'{"format": 5, "entries": [', 1),
            (b"{}, ", 9_999),
            (_entry_text(BASE).encode() + b"]}\n", 1),
        ],
    )
    nested = _entry_text(BASE)[:-1] + ', "deep": ' + "[" * 100 + "]" * 100 + "}"
    yield Case([rec], _record_text(nested))
    yield Case(
        [rec],
        [
            (_record_text(BASE)[:-3], 1),
            (b", [", 1),
            (b"[", 20_000),
            (b"]", 20_001),
            (b"]}\n", 1),
        ],
    )
    yield Case([rec], [(b"[", 32_000), (b"]", 32_000)])
    yield Case([rec], [(b" ", 70_000), (whole, 1)])
    # What stands where the record should: a link, a directory, mode 000.
    yield Case([rec], links={RECORD: "@/elsewhere"}, files={"elsewhere": whole})
    yield Case([rec], links={RECORD: "@/nothing"})
    yield Case([rec], links={RECORD: "@/state"})
    yield Case([rec], dirs=(RECORD,))
    yield Case([rec], whole, modes={RECORD: 0o000})
    yield Case([rec], whole, modes={RECORD: 0o200})
    yield Case([rec], whole, modes={"state": 0o000})
    # The clock against every window: a whole answer for REASK_SECONDS, a
    # partial one, the fallback and a kept one for RETRY_SECONDS.
    partials: list[dict[str, Any]] = [
        {},
        {"lg": None},
        {"px": None},
        {"cd": None},
        {"hc": None},
        {"v": None},
        {"ok": False},
        {"rs": None, "sy": None, "o": []},
        {"t": "1000.0"},
    ]
    for over in partials:
        steps: list[list[Any]] = []
        for now in (
            "1000.0",
            "999.999",
            "1059.999",
            "1060.0",
            "4599.999",
            "4600.0",
            "nan",
            "inf",
            "-inf",
        ):
            steps.append(["current", _answer_spec(**over), now, True])
        yield Case(steps)
    for asked in ("nan", "inf", "-inf", "-0.0", "1e308"):
        yield Case(
            [
                ["current", _answer_spec(a=asked), now, True]
                for now in ("1000.0", "inf", "nan", "1e308", "-0.0")
            ]
        )
    # The lower edges: `now` equal to `asked` (or `tried`), one float step
    # below and 1e-7 below; the clock gone back is never reused.
    below = repr(math.nextafter(1000.0, -math.inf))
    for over in ({}, {"t": "1000.0"}, {"ok": False}):
        yield Case(
            [
                ["current", _answer_spec(**over), now, s]
                for now in ("1000.0", below, "999.9999999")
                for s in (True, False)
            ]
        )
    yield Case(
        [["kept", K, now, []] for now in ("1000.0", below, "999.9999999")],
        _record_text(_with(tried=1000.0)),
    )
    for tried in ("nan", "inf", "-0.0", "0.0"):
        yield Case(
            [
                ["current", _answer_spec(t=tried), now, False]
                for now in ("0.0", "59.0", "60.0", "inf", "-1.0")
            ]
        )
    # The stamp: as stated, another, none; a git gone; relative; text the
    # OS cannot be given; `stamp` off.
    stamps: list[dict[str, Any]] = [
        {},
        {"st": ["1", "2", "3", "4", "5"]},
        {"st": None},
        {"g": _t("@/nogit"), "st": None},
        {"g": _t("@/nogit"), "st": "<git>"},
        {"g": _t("bin/git")},
        {"g": _t("bin/../bin/git")},
        {"g": None, "st": None},
        {"g": _t("/\ud800"), "st": ["-1"] * 5},
        {"g": _t("/\ud800"), "st": None},
        {"g": _t("@/\udcff"), "st": None},
        {"g": _t("@/bin"), "st": ["1", "2", "3", "4", "5"]},
        *({"st": f"<git~{i}>"} for i in range(5)),
    ]
    for over in stamps:
        yield Case(
            [["current", _answer_spec(**over), "1000.0", s] for s in (True, False)]
        )
    yield Case(
        [["current", _answer_spec(), "1000.0", True]],
        files={"bin/git": b"git\n"},
        modes={"bin/git": 0o000},
    )
    # `_kept`: the record's answer that stands; else the newest given.
    # Recorded for another binary: it does not stand, and `_kept` chooses.
    stale = _with(asked=1.0, stamp=[1, 2, 3, 4, 5])
    old = _answer_spec(a="500.0")
    new = _answer_spec(a="900.0")
    for record, seen_answers in (
        (whole, []),
        (whole, [new]),
        (_record_text(stale), []),
        (_record_text(stale), [old, new]),
        (_record_text(stale), [new, old]),
        (_record_text(stale), [None, _answer_spec(a="1.0", ok=False)]),
        (_record_text(_with(ok=False)), [None]),
        (_record_text(_with(ok=False, asked=900.0)), [old]),
        (
            _record_text(stale),
            [_answer_spec(a="1.0"), _answer_spec(a="1.0", g=_t("/other"))],
        ),
        (_record_text(stale), [_answer_spec(a="nan"), old]),
        (_record_text(_with(asked=float("nan"))), [old]),
        (_record_text(_with(tried=1100.0)), []),
        (_record_text(_with(stamp=[1, 2, 3, 4, 5])), [old]),
        (None, []),
        (None, [old]),
        (b"hello", [new]),
        (_record_text(_with(asked=1e999)), [new]),
    ):
        yield Case([["kept", K, "1000.0", seen_answers]], record)
    # `_record`: written, read back, the git changed each way, read again.
    for kind, change in (
        ("write", _b(b"git 2\n")),
        ("write", _b(b"git\n")),
        ("mtime", None),
        ("rename", _b(b"git\n")),
        ("mode", "=600"),
        ("remove", None),
        ("dir", None),
        ("fifo", None),
    ):
        yield Case(
            [
                ["record", _answer_spec()],
                rec,
                ["current", _answer_spec(), "1000.0", True],
                ["kept", K, "1000.0", []],
                ["change", kind, "bin/git", change],
                rec,
                ["current", _answer_spec(st="<git>"), "1000.0", True],
                ["kept", K, "1001.0", []],
                ["record", _answer_spec(a="1001.0")],
                rec,
            ]
        )
    fallback = _answer_spec(
        g=None,
        st=None,
        ok=False,
        o=[],
        lg=None,
        rs=None,
        px=None,
        cd=None,
        hc=None,
        v=None,
    )
    # The fallback over an answer git gave is not written; over the
    # fallback, or a stale bad entry, it is.
    yield Case([["record", fallback], rec], whole)
    yield Case([["record", fallback], rec], _record_text(_with(ok=False)))
    yield Case([["record", fallback], rec], _record_text(_with(own=5)))
    yield Case([["record", fallback], rec])
    yield Case([["record", _answer_spec(t="1001.0")], rec], whole)
    yield Case(
        [["record", fallback]],
        _record_text(_entry_text(BASE).replace("@STAMP0@", "[Infinity, 2, 3, 4, 5]")),
    )
    # Two keys, newest first; nine keys, eight kept.
    yield Case(
        [
            ["record", _answer_spec()],
            ["record", _answer_spec(k=K2, a="2000.0")],
            rec,
            ["recorded", K2],
            ["record", _answer_spec(a="3000.0")],
        ]
    )
    yield Case(
        [["record", _answer_spec(k=f"{i:032x}")] for i in range(10)]
        + [["recorded", f"{0:032x}"], ["recorded", f"{9:032x}"]]
    )
    # Others written back as they were read: odd values, duplicate keys,
    # non-ASCII, big numbers; past the eighth not.
    odd = (
        '{"path": "'
        + K2
        + '", "x": NaN, "y": -Infinity, "z": 1e400, "n": '
        + "1" * 300
        + ', "u": "\\u00e9\\ud800\\u0041", "f": 0.1, "e": 1E5,'
        + ' "d": {"a": 1, "a": 2, "b": [true, null]}}'
    )
    yield Case([["record", _answer_spec()]], _record_text(odd, "5", "[]"))
    yield Case([["record", _answer_spec()]], _many(odd, 9, last=False))
    yield Case([["record", _answer_spec()]], _record_text(BASE, odd, fmt=5.0))
    yield Case([["record", _answer_spec()]], _record_text(odd, fmt=4))
    # Text the answer carries, written as `json.dumps` escapes it.
    yield Case(
        [
            [
                "record",
                _answer_spec(
                    g=_t('/gé\ud800"\\\n'),
                    o=[_t("/\U0001f600"), _t("/b")],
                    sy=None,
                    px=_t(""),
                    a="1e-07",
                    t="-0.0",
                    v=[65535, 0, 1],
                ),
            ],
            rec,
        ]
    )
    yield Case([["record", _answer_spec(a="nan", t="inf")], rec])
    yield Case(
        [["record", _answer_spec(st=["1" + "0" * 40, "-1", "0", "0", "0"])], rec]
    )
    # Where the state directory or the record is not a plain file.
    yield Case([["record", _answer_spec()], rec], dirs=(RECORD,))
    yield Case(
        [["record", _answer_spec()], rec],
        links={RECORD: "@/elsewhere"},
        files={"elsewhere": b"x"},
    )
    yield Case([["record", _answer_spec()], rec], links={RECORD: "@/nothing"})
    yield Case([["record", _answer_spec()], rec], whole, modes={RECORD: 0o000})
    yield Case([["record", _answer_spec()], rec], modes={"state": 0o755})
    yield Case([["record", _answer_spec()], rec], modes={"state": 0o500})
    yield Case([["record", _answer_spec()], rec], modes={"state": 0o711})
    # A hostile record replaced whole.
    yield Case([["record", _answer_spec()], rec], [(b"\xff", 70_000)])
    yield Case([["record", _answer_spec()], rec], [(b"[", 32_000), (b"]", 32_000)])
    # A record put between reads.
    yield Case(
        [
            rec,
            ["put", RECORD, _spelled(whole)],
            rec,
            ["put", RECORD, _spelled(_record_text(_with(asked=2000.0)))],
            rec,
            ["current", _answer_spec(a="2000.0"), "5599.0", True],
            ["current", _answer_spec(a="2000.0"), "5600.0", True],
        ]
    )
    # The key: PATH unset, empty, given; text with bytes no UTF-8 holds;
    # the developer directory on macOS and not.
    for path in (
        None,
        "=",
        "=/usr/bin:/bin",
        "2f75f6ff",
        "2fe282",
        "2feda080",
        "2fc3a9",
    ):
        for macos, dev in (
            (False, None),
            (True, None),
            (True, "=/Applications/X.app"),
            (True, "2fff"),
        ):
            yield Case([["key", path, macos, dev]])
    yield Case(
        [
            ["key", "=/bin", True, None],
            ["key", "=/bin", True, "=/d"],
            ["key", "=/bin", False, "=/d"],
        ],
        links={"xcode": "/Library/Developer/CommandLineTools"},
    )
    yield Case([["key", "=/bin", True, None]], links={"xcode": "/x\u00e9"})
    yield Case([["key", "=/bin", True, None]], files={"xcode": b"x"})


#: A record nested deeper than this goes in ``gitsystem_record_python.jsonl``
#: (`generate_python`): whether `json.loads` raises RecursionError on it is
#: the interpreter's, and even depends on how deep in the C stack the call
#: is made, so such a case is Python-gated by its input. The deep cases sit
#: thousands of levels from 9,999 (CPython 3.13's limit) on either side.
DEEP = 1000


def _depth(data: bytes) -> int:
    """The deepest nesting of brackets in `data`, outside strings."""
    depth = most = 0
    in_str = esc = False
    for byte in data:
        if in_str:
            if esc:
                esc = False
            elif byte == 0x5C:
                esc = True
            elif byte == 0x22:
                in_str = False
        elif byte == 0x22:
            in_str = True
        elif byte in (0x5B, 0x7B):
            depth += 1
            most = max(most, depth)
        elif byte in (0x5D, 0x7D):
            depth -= 1
    return most


def _deep(case: Case) -> bool:
    return any(_depth(_expand(v)) > DEEP for v in case.files.values())


def _file(python: bool) -> bytes:
    refuse_root()
    cases = [c for c in _cases() if _deep(c) == python]
    lines: list[dict[str, Any]] = []
    with _sandbox.world() as world:
        base = world.root / "gitsystem-record"
        for i, case in enumerate(cases):
            root = base / str(i)
            root.mkdir(parents=True)
            os.chmod(root, 0o755)
            answer = _run(case, root)
            shutil.rmtree(root)
            lines.append({**case.record(), "r": answer})
    header = {
        "fixture": "gitsystem_record_python" if python else "gitsystem_record",
        "cases": len(cases),
        "base": _b(WHOLE),
        "answer": SPEC,
        "out": OUT,
    }
    return jsonl([header, *lines])


def generate() -> dict[str, bytes]:
    return {"gitsystem_record.jsonl": _file(False)}


def generate_python() -> dict[str, bytes]:
    return {"gitsystem_record_python.jsonl": _file(True)}

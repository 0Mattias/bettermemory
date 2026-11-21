"""Fixtures: `attestation_paths.py` whole (``attestation_paths.jsonl``,
``attestation_paths_posix.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/attestation_paths.rs``.

``attestation_paths.jsonl`` runs every function over a scripted host: the
module's ``os`` is replaced by one whose ``name`` is the row's (``nt``),
whose ``path`` is ``posixpath`` or ``ntpath`` with a scripted
``expanduser``, and whose ``lstat`` answers by the path's last character.
`ntpath` and `posixpath` are the modules themselves, so the file is the
same on every host. The rules, which the native test repeats:

- ``expanduser(p)``: ``~`` alone or before a separator is the home
  (``/hm/u``, or ``C:\\Hm\\u`` under ``nt``) and the rest; ``~z``
  raises ValueError, ``~!`` raises KeyError; anything else is returned.
- ``lstat(p)``: a NUL raises ValueError; a last character ``a`` answers,
  ``b`` raises PermissionError, ``d`` NotADirectoryError, ``e`` OSError,
  anything else FileNotFoundError.

An answer is spelled by `spell`: ``N`` None, ``T``/``F``, ``S`` and the
text, ``L`` and the items joined by U+001D, ``!`` and the class raised.

A ``sweep`` row pins every function over seeded spellings by count and
SHA-256 of the answers in order (`_sweep` says how they are made).

``attestation_paths_posix.jsonl`` runs the module with the real ``os`` over
a scratch tree (a file, a folder, a dangling link, a mode-000 folder);
``@`` stands for the tree. POSIX only: links and modes.
"""

from __future__ import annotations

import contextlib
import hashlib
import ntpath
import os
import posixpath
import shutil
import tempfile
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest import mock

from bettermemory import attestation_paths as ap

from ._common import jsonl

MODES = ("auto", "literal", "reference")
ROOTS: tuple[str | None, ...] = (
    None,
    "/r",
    "r/s",
    "C:\\r",
    "c:/R/",
    "\\\\srv\\sh\\r",
    "/",
    "",
)
PIECES = (
    "a", "b", "d", "e", "x", ".", "..", "/", "\\", ":", "C:", "c:/", "~",
    "~z", "~!", "$HOME/", "${HOME}/", " ", "*", "<", '"', "|", "?", "\0",
    "/path/to/", "<path>", "\u00e9", "\n", "//", "foo", "@", "://", "-",
    "\t", "src/", "x.py", "~/",
)  # fmt: skip
NAMED_ROOTS: tuple[str | None, ...] = (None, "/r", "C:\\r")
SWEEP_SEED = 20261121
SWEEP_COUNT = 1200


def _expanduser(nt: bool):
    home = "C:\\Hm\\u" if nt else "/hm/u"

    def expanduser(p: str) -> str:
        if p.startswith("~") and (len(p) == 1 or p[1] in "/\\"):
            return home + p[1:]
        if p.startswith("~z"):
            raise ValueError("scripted")
        if p.startswith("~!"):
            raise KeyError("scripted")
        return p

    return expanduser


def _lstat(p: str) -> None:
    if "\0" in p:
        raise ValueError("embedded null character")
    last = p[-1:]
    if last == "a":
        return
    if last == "b":
        raise PermissionError(13, "scripted")
    if last == "d":
        raise NotADirectoryError(20, "scripted")
    if last == "e":
        raise OSError(62, "scripted")
    raise FileNotFoundError(2, "scripted")


@contextlib.contextmanager
def scripted(nt: bool) -> Iterator[None]:
    flavour = ntpath if nt else posixpath
    fake = types.SimpleNamespace(
        name="nt" if nt else "posix",
        path=types.SimpleNamespace(
            normpath=flavour.normpath, expanduser=_expanduser(nt)
        ),
        lstat=_lstat,
    )
    with mock.patch.object(ap, "os", fake):
        yield


def spell(value: Any) -> str:
    if value is None:
        return "N"
    if value is True:
        return "T"
    if value is False:
        return "F"
    if isinstance(value, str):
        return "S" + value
    if isinstance(value, dict):
        return "D" + "\x1d".join(spell(k) + "=" + spell(v) for k, v in value.items())
    return "L" + "\x1d".join(spell(v) for v in value)


def call(fn: Any, *args: Any, **kw: Any) -> str:
    try:
        return spell(fn(*args, **kw))
    except Exception as exc:  # noqa: BLE001
        return "!" + type(exc).__name__


def _per_input(s: str, roots: tuple[str | None, ...] = ROOTS) -> list[str]:
    """Every answer the sweep pins for one spelling under one host."""
    out = [call(ap.absolute_spelling, s), call(ap.literal_path, s)]
    out.append(call(ap.reference_path, s))
    for root in roots:
        out.append(call(ap.target_path, s, root))
        for mode in MODES:
            out.append(call(ap.interpret_path, s, mode=mode, root=root))
            out.append(call(ap.placeholder_spelling, s, mode=mode, root=root))
            if root is not None:
                out.append(call(ap.joined_path, root, s, mode=mode))
    return out


class Lcg:
    """A 64-bit linear congruential generator, its high 31 bits drawn."""

    def __init__(self, seed: int) -> None:
        self.x = seed

    def draw(self, n: int) -> int:
        self.x = (self.x * 6364136223846793005 + 1442695040888963407) % (1 << 64)
        return (self.x >> 33) % n


def sweep_inputs() -> list[str]:
    rng = Lcg(SWEEP_SEED)
    out = []
    for _ in range(SWEEP_COUNT):
        length = 1 + rng.draw(6)
        out.append("".join(PIECES[rng.draw(len(PIECES))] for _ in range(length)))
    return out


def _sweep(nt: bool) -> dict[str, Any]:
    digest = hashlib.sha256()
    count = 0
    inputs = sweep_inputs()
    with scripted(nt):
        for i, s in enumerate(inputs):
            for answer in _per_input(s):
                digest.update(answer.encode("utf-8", "surrogatepass") + b"\x1e")
                count += 1
            # compile_targets over a pair and a repeat, every root and mode.
            t = inputs[(i + 1) % len(inputs)]
            for root in ROOTS:
                for mode in MODES:
                    for absent in (False, True):
                        answer = call(
                            ap.compile_targets,
                            [s, t, s],
                            root=root,
                            mode=mode,
                            absent=absent,
                        )
                        digest.update(answer.encode("utf-8", "surrogatepass") + b"\x1e")
                        count += 1
    return {
        "k": "sweep",
        "nt": nt,
        "seed": SWEEP_SEED,
        "inputs": SWEEP_COUNT,
        "n": count,
        "sha": digest.hexdigest(),
    }


NAMED = (
    "", " ", "a", "/a", "~", "~/a", "~z/a", "~!", "~user/x", "$HOME/a",
    "${HOME}/a", "$HOME", "C:/a", "c:\\a", "\u00e9:/a", "1:/a", "C:a",
    "../a", "a/../../b", "a/..", "./a", "a/./b", "//srv/a",
    "a\\..\\..\\b", "/path/to/file", "~/path/to/x", "<path>", "path/to/file",
    "a.", "a)", "`a`", "a:12", "src/x.py:3", "src/x.py.", "x\0a", "a b",
    "http://x/a", "a<b", "a:b", "C:\\a:b", 
    "/__attested_reference__", "\t/a\n",
)  # fmt: skip
#: Long spellings and those with a lone surrogate, pinned by digest:
#: (head, filler, count, tail), each text as code points.
NAMED_LONG = (
    ("/a/", "x", 600, ""),
    ("", "x", 577, "."),
    ("", "x", 576, ""),
    ("/path/to/", "x", 568, ""),
    ("/path/to/", "x", 567, ""),
    ("", "x", 511, "/path/to/"),
    ("/", "x", 513, ""),
    ("\ud800", "a", 1, ""),
    ("~/\udc80", "a", 1, ""),
)


def _named_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for nt in (False, True):
        with scripted(nt):
            for s in NAMED:
                rows.append(
                    {
                        "k": "one",
                        "nt": nt,
                        "s": [ord(c) for c in s],
                        "o": _per_input(s, NAMED_ROOTS),
                    }
                )
            for head, fill, count, tail in NAMED_LONG:
                answers = _per_input(head + fill * count + tail, NAMED_ROOTS)
                text = "".join(a + "\x1e" for a in answers).encode(
                    "utf-8", "surrogatepass"
                )
                rows.append(
                    {
                        "k": "long",
                        "nt": nt,
                        "s": [
                            [ord(c) for c in head],
                            [ord(c) for c in fill],
                            count,
                            [ord(c) for c in tail],
                        ],
                        "n": len(answers),
                        "sha": hashlib.sha256(text).hexdigest(),
                    }
                )
    return rows


def _paths(kind: str, items: list[str]) -> Any:
    if kind == "L":
        return ap.LiteralPaths(items)
    if kind == "R":
        return ap.ReferencePaths(items)
    return tuple(items)


def _record(spec: dict[str, Any]) -> Any:
    def targets(v: Any) -> Any:
        return None if v is None else dict(v)

    return types.SimpleNamespace(
        verified_paths=spec["vp"],
        verified_absent_paths=spec["va"],
        verified_path_targets=targets(spec["vt"]),
        verified_absent_path_targets=targets(spec["vat"]),
    )


RECORDS: tuple[dict[str, Any], ...] = (
    {"vp": ["a", "/b", "x/../y"], "va": ["c"], "vt": None, "vat": None},
    {
        "vp": ["a", "~/q", " /b "],
        "va": ["c"],
        "vt": None,
        "vat": [["n", "/n"], ["m", None]],
    },
    {
        "vp": ["a"],
        "va": [],
        "vt": [["a", "/r/a"], ["b", None], ["c", "/r/cx"]],
        "vat": None,
    },
    {"vp": [], "va": ["z"], "vt": [["p", "/px"]], "vat": [["z", "/z"]]},
    {"vp": ["..", "", "/path/to/x"], "va": [], "vt": None, "vat": []},
    {"vp": ["\t"], "va": [], "vt": [], "vat": None},
)


def _record_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for spec in RECORDS:
        record = _record(spec)
        out: dict[str, Any] = {}
        for absent in (False, True):
            got = ap.record_paths(record, absent=absent)
            out[f"rp{int(absent)}"] = [ap.path_mode(got), list(got)]
        try:
            got = ap.commit_paths(record)
            out["cp"] = [ap.path_mode(got), list(got)]
        except Exception as exc:  # noqa: BLE001
            out["cp"] = "!" + type(exc).__name__
        calls: list[list[Any]] = []

        def probe(paths: Any) -> list[str]:
            calls.append([ap.path_mode(paths), list(paths)])  # noqa: B023
            return [p for p in paths if p.endswith("x")]

        out["uv"] = ap.unverifiable_inputs(record, probe)
        out["calls"] = calls
        rows.append({"k": "record", "rec": spec, "o": out})
    for kind in ("L", "R", "T"):
        rows.append({"k": "mode", "kind": kind, "o": ap.path_mode(_paths(kind, ["a"]))})
    for bad in ("Auto", "", "literal "):
        rows.append(
            {
                "k": "badmode",
                "mode": bad,
                "o": call(ap.compile_targets, ["a"], root=None, mode=bad),
            }
        )
    return rows


def _compile_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    lists: tuple[list[str], ...] = (
        ["a", "b", "a"],
        ["x/../a", "x/a", "~/a", "$HOME/a"],
        ["/path/to/file", "<path>", "path/to/a"],
        ["a.", "a)", "src/x.py:3", "../a"],
        ["~z", "~!/a"],
        ["C:\\r\\a", "c:/a"],
        [],
    )
    for nt in (False, True):
        with scripted(nt):
            for values in lists:
                for root in ("C:\\r",) if nt else ("/r", "C:\\r", None):
                    for mode in MODES:
                        for absent in (False, True):
                            rows.append(
                                {
                                    "k": "compile",
                                    "nt": nt,
                                    "v": values,
                                    "root": root,
                                    "mode": mode,
                                    "absent": absent,
                                    "o": call(
                                        ap.compile_targets,
                                        values,
                                        root=root,
                                        mode=mode,
                                        absent=absent,
                                    ),
                                }
                            )
    return rows


def generate() -> dict[str, bytes]:
    """``attestation_paths.jsonl``: not gated (the host is scripted)."""
    rows = [
        *_named_rows(),
        *_record_rows(),
        *_compile_rows(),
        _sweep(False),
        _sweep(True),
    ]
    return {
        "attestation_paths.jsonl": jsonl([{"k": "header", "cases": len(rows)}, *rows])
    }


POSIX_NAMES = (
    "file", "dir", "dir/", "gone", "dangling", "locked/inner", "file/under",
    "../x", "/path/to/file", "<path>", "loop",
)  # fmt: skip


def _tree(root: Path) -> None:
    (root / "file").write_bytes(b"x")
    (root / "dir").mkdir()
    (root / "dangling").symlink_to(root / "nowhere")
    (root / "loop").symlink_to(root / "loop")
    (root / "locked").mkdir()
    (root / "locked" / "inner").write_bytes(b"")
    (root / "locked").chmod(0)


def generate_posix() -> dict[str, bytes]:
    """``attestation_paths_posix.jsonl``: POSIX only (links, modes)."""
    rows: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(os.path.realpath(scratch)) / "R"
        root.mkdir()
        _tree(root)
        r = str(root)

        def at(answer: str) -> str:
            return answer.replace(r, "@")

        try:
            for name in POSIX_NAMES:
                for mode in MODES:
                    rows.append(
                        {
                            "k": "real",
                            "s": name,
                            "mode": mode,
                            "o": [
                                at(call(ap.interpret_path, name, mode=mode, root=r)),
                                at(call(ap.joined_path, r, name, mode=mode)),
                                at(
                                    call(
                                        ap.placeholder_spelling, name, mode=mode, root=r
                                    )
                                ),
                                at(call(ap.compile_targets, [name], root=r, mode=mode)),
                                at(call(ap.interpret_path, r + "/" + name, mode=mode)),
                            ],
                        }
                    )
        finally:
            (root / "locked").chmod(0o755)
            shutil.rmtree(root)
    return {
        "attestation_paths_posix.jsonl": jsonl(
            [{"k": "header", "cases": len(rows)}, *rows]
        )
    }

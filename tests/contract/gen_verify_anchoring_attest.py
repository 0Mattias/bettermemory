"""Fixtures: the attestation checks of `verify_anchoring`
(``verify_anchoring_attest.jsonl``, ``verify_anchoring_attest_unicode.jsonl``,
``verify_anchoring_attest_posix.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/verify_anchoring/attest.rs``. This
pins `_is_absolute_attestation`, `_join_under`, `unverifiable_attestations`,
`_unverifiable_at` and `_normalize_attestations` over the scripted host of
`gen_attestation_paths` (``nt`` the row's; `Path` is ``PurePosixPath`` or
``PureWindowsPath`` to match), and `_resolves_under` and
`_worktree_root_is_live` over a scratch tree.

The module's closures are replaced by rules the native test repeats:

- `_normalize_for_compare(p)`: ``p`` with each backslash a slash;
- `_path_exists(p)`: ``ok`` occurs in ``p``;
- `_absent_by_sparsity(p)`: ``sp`` occurs in ``p``;
- the keys of `_normalize_attestations`: ``""`` where ``zz`` occurs, else
  ``p`` without a last ``k``.

Each call of the first three is logged (``c``: its initial and argument).
The stub anchor (``a``: root, convicting, declared root) answers
``absolute(p)``: ``(None, "unanchored")`` where ``un`` occurs, ``("/H" + p,
"home_checkout")`` where ``hq`` occurs, else ``(p, "standin")``.

An answer is spelled as `gen_attestation_paths.spell`.

Left out because the answer follows the Python release: a relative part
whose first code point is a non-ASCII letter before a colon joined under
``PureWindowsPath`` (``ntpath.splitroot`` reads it as a drive on 3.13 and
not on 3.11 and 3.12), and resolution through a link loop
(``posixpath.realpath`` answers it differently on 3.14). The native kills
test states the 3.13.13 answer of the first.

``verify_anchoring_attest_unicode.jsonl`` pins `_is_absolute_attestation`
of ``c + ":/"`` for every code point ``c`` (surrogates included) by count
and SHA-256 of the answers in order, ``T`` or ``F`` each: `str.isalpha`
follows the Unicode release, so the file is gated on it.

``verify_anchoring_attest_posix.jsonl`` reads a scratch tree with a link
(``@`` stands for it); ``expanduser`` maps a leading ``~`` to ``@/home``.
POSIX only: links.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import posixpath
import tempfile
import types
from collections.abc import Iterator
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from unittest import mock

from bettermemory import verify_anchoring as va

from ._common import jsonl
from .gen_attestation_paths import _paths, call, scripted

ABS_INPUTS = (
    "", "/", "~", "~x", "$HOME/a", "${HOME}/a", "$HOME", "${HOME}", "$HOMEx/a",
    "C:/x", "C:\\x", "c:x", "C:", "1:/x", "_:/x", ":/x", "C|/x", "\u00e9:/x",
    "\u4e2d:\\x", "ab:/x", "//srv/sh", "rel/x", " /x", "./x",
)  # fmt: skip
JOIN_ROOTS = ("/r", "/r/", "//r", "", ".", "C:\\r", "c:/R/", "//srv/sh/r", "/")
JOIN_RELS = (
    "a.py", "src/a.py", "./a.py", "a/../b.py", "../x.py", "a/../../x.py", "/abs.py",
    "\\abs.py", "C:x.py", "c:/x.py", "a//b.py", "", "x/", "*.py",
    "/path/to/x", "path/to/x", "a\\b.py", "~/x.py",
)  # fmt: skip
UV_ITEMS = (
    "/ok/a", "/gone/a", "ok.py", "gone.py", "/path/to/file", "src/../../x",
    "$HOME/ok", "${HOME}/gone", "~/gone", "~/ok", "C:\\ok\\a", "C:/gone/x",
    "  /gone/b  ", " ", "", "sp/gone", "/sp/gone", "/var/log/*.log",
    "/hq/gone", "/un/gone", "/hq/ok", "a\\gone", "/healthz",
)  # fmt: skip
UV_ROOTS = (None, "/r", "/r/sub/", "C:\\r", "/r/ok")
ANCHORS = (
    (None, False, None),
    ("/r", True, "/r"),
    ("/r", False, "/r"),
    ("/r", False, None),
    ("/r/ok", True, None),
)
SWEEP_TAIL = ":/"


@contextlib.contextmanager
def _world(nt: bool, log: list[str]) -> Iterator[None]:
    def norm(p: str) -> str:
        log.append("n" + p)
        return p.replace("\\", "/")

    def exists(p: str) -> bool:
        log.append("e" + p)
        return "ok" in p

    def sparse(p: str) -> bool:
        log.append("s" + p)
        return "sp" in p

    with (
        scripted(nt),
        mock.patch.object(va, "Path", PureWindowsPath if nt else PurePosixPath),
        mock.patch.object(va, "_normalize_for_compare", norm),
        mock.patch.object(va, "_path_exists", exists),
        mock.patch.object(va, "_absent_by_sparsity", sparse),
    ):
        yield


def _anchor(spec: tuple[str | None, bool, str | None]) -> Any:
    root, convicting, declared = spec

    def absolute(p: str) -> tuple[str | None, str]:
        if "un" in p:
            return None, "unanchored"
        if "hq" in p:
            return "/H" + p, "home_checkout"
        return p, "standin"

    return types.SimpleNamespace(
        root=root, convicting=convicting, declared_root=declared, absolute=absolute
    )


def _keys(p: str) -> str:
    if "zz" in p:
        return ""
    return p[:-1] if p.endswith("k") else p


def _rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    rows.append(
        {
            "k": "abs",
            "s": list(ABS_INPUTS),
            "o": [call(va._is_absolute_attestation, s) for s in ABS_INPUTS],
        }
    )
    for nt in (False, True):
        log: list[str] = []
        with _world(nt, log):
            for root in JOIN_ROOTS:
                rows.append(
                    {
                        "k": "join",
                        "nt": nt,
                        "root": root,
                        "o": [call(va._join_under, root, rel) for rel in JOIN_RELS],
                    }
                )
    for nt in (False, True):
        for kind in ("P", "L", "R"):
            for uv_root in UV_ROOTS:
                log = []
                with _world(nt, log):
                    paths = _paths(kind, list(UV_ITEMS))
                    out = call(
                        va.unverifiable_attestations, paths, worktree_root=uv_root
                    )
                rows.append(
                    {
                        "k": "uv",
                        "nt": nt,
                        "kind": kind,
                        "root": uv_root,
                        "o": out,
                        "c": log,
                    }
                )
            for spec in ANCHORS:
                for stored in (False, True):
                    log = []
                    with _world(nt, log):
                        paths = _paths(kind, list(UV_ITEMS))
                        out = call(
                            va.unverifiable_attestations,
                            paths,
                            anchor=_anchor(spec),
                            stored=stored,
                        )
                    rows.append(
                        {
                            "k": "at",
                            "nt": nt,
                            "kind": kind,
                            "a": list(spec),
                            "stored": stored,
                            "o": out,
                            "c": log,
                        }
                    )
            na_items = [*UV_ITEMS, "/zz/a", "/a/k", "/a/kk", "~/k", "rel/k"]
            log = []
            with _world(nt, log):
                out = call(
                    lambda p: sorted(va._normalize_attestations(p, _keys)),
                    _paths(kind, na_items),
                )
            rows.append({"k": "na", "nt": nt, "kind": kind, "s": na_items, "o": out})
    return rows


def generate() -> dict[str, bytes]:
    """``verify_anchoring_attest.jsonl``: not gated (the host is scripted)."""
    rows = _rows()
    return {
        "verify_anchoring_attest.jsonl": jsonl(
            [{"k": "header", "cases": len(rows)}, *rows]
        )
    }


def generate_unicode() -> dict[str, bytes]:
    """``verify_anchoring_attest_unicode.jsonl``: gated on the Unicode
    release (`str.isalpha`)."""
    h = hashlib.sha256()
    n = 0
    for c in range(0x110000):
        h.update(b"T" if va._is_absolute_attestation(chr(c) + SWEEP_TAIL) else b"F")
        n += 1
    row = {"k": "sweep", "tail": SWEEP_TAIL, "n": n, "sha": h.hexdigest()}
    return {
        "verify_anchoring_attest_unicode.jsonl": jsonl(
            [{"k": "header", "cases": 1}, row]
        )
    }


RES_PATHS = (
    "@/w/a", "@/w", "@/wx", "@/link/a", "@/w/../x", "@/w/./a", "~/a", "rel",
    "@/w/\0", "/", "@//w/a",
)  # fmt: skip
RES_ROOTS = ("@/w", "@/link", "@/w/", "/", "@/home", "@/loop")
LIVE_ROOTS = ("@/w", "@/link", "@/file", "@/gone", "~", "~/x", "@/w/", "@/w\0", "")


def generate_posix() -> dict[str, bytes]:
    """``verify_anchoring_attest_posix.jsonl``: POSIX only (links)."""
    rows: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as scratch:
        base = Path(os.path.realpath(scratch)) / "R"
        base.mkdir()
        (base / "w").mkdir()
        (base / "home").mkdir()
        (base / "file").write_bytes(b"x")
        (base / "link").symlink_to(base / "w")
        (base / "loop").symlink_to(base / "loop")
        r = str(base)

        def expanduser(p: str) -> str:
            if p.startswith("~") and (len(p) == 1 or p[1] == "/"):
                return r + "/home" + p[1:]
            return p

        def at(s: str) -> str:
            return s.replace("@", r)

        with mock.patch.object(posixpath, "expanduser", expanduser):
            for p in RES_PATHS:
                rows.append(
                    {
                        "k": "res",
                        "s": p,
                        "o": [
                            call(va._resolves_under, at(p), Path(at(root)))
                            for root in RES_ROOTS
                        ],
                    }
                )
            rows.append(
                {
                    "k": "live",
                    "s": list(LIVE_ROOTS),
                    "o": [call(va._worktree_root_is_live, at(s)) for s in LIVE_ROOTS],
                }
            )
    return {
        "verify_anchoring_attest_posix.jsonl": jsonl(
            [{"k": "header", "cases": len(rows)}, *rows]
        )
    }

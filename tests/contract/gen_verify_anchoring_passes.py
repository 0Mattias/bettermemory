"""Fixtures: the attestation and citation read passes of `verify_anchoring`
(``verify_anchoring_passes.jsonl``, ``verify_anchoring_passes_unicode.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/verify_anchoring/passes.rs``. This
pins `_relative_attestation_keys`, `_attestation_read`,
`_read_attestations`, `_read_citations`, `_check_anchored_attestations`,
`_is_checkable_citation` and `_check_anchored_citations` over the scripted
host of `gen_attestation_paths` (``nt`` the row's; `Path` is
``PurePosixPath`` or ``PureWindowsPath`` to match, and `os.path` of this
module is that flavour's `dirname` with the scripted `expanduser`).

The module's effects are replaced by rules the native test repeats, each
call logged (``c``: its initial and argument):

- the claim key (not logged): ``""`` where ``zz`` occurs, else the path with
  ASCII capitals lowered;
- `_path_exists` (``e``): ``ok`` occurs in the path;
- `_absent_by_sparsity` (``s``): ``sp`` occurs in the path;
- `_resolves_under(p, root)` (``r``, the path): ``p`` starts with
  ``str(root)``;
- `os.path.isdir` (``d``): ``nod`` does not occur in the path.

The stub anchor (``a``: root, rule, convicting) answers ``absolute(p)``:
``(None, "unanchored")`` where ``un`` occurs, ``("/H" + p,
"home_checkout")`` where ``hq`` occurs, ``(p, "absolute")`` where ``ab``
occurs, else ``(p, "standin")``.

`_RELATIVE_CITATION_RE` is not ported yet: each citation row records the
spans of group 1 of its matches over the bounded body (``m``), which the
native test hands to the pass. Bodies are ASCII: the pattern's word class
follows the Unicode release.

A row's buckets (``o``) are the `_AnchoredRead` lists after the call, and
``x`` what the call raised, spelled as `gen_attestation_paths.call`.
Rows seed the seen set first through ``first`` (``pre``).

``verify_anchoring_passes_unicode.jsonl`` pins `_is_checkable_citation`
of ``"a/b." + c + "s"`` and ``"a" + c + "/b.py"`` for every code point
``c`` by count and SHA-256 of the answers in order (``T`` or ``F``):
`str.lower` follows the Unicode release, so the file is gated on it.
"""

from __future__ import annotations

import contextlib
import hashlib
import ntpath
import posixpath
import types
from collections.abc import Iterator
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any
from unittest import mock

from bettermemory import verify_anchoring as va
from bettermemory.attestation_paths import path_mode
from bettermemory.verify_candidates import _RELATIVE_CITATION_RE, _bounded_scan_text

from ._common import jsonl
from .gen_attestation_paths import _expanduser, _paths, call, scripted

VP = (
    "src/ok.py", "src/gone.py", "SRC/OK.py", "/r/ok/a.py", "/r/gone/b.py",
    "/hq/gone", "/un/x", "/ab/r/ok", "/ab/r/gone", "/ab/else/gone", "~/gone.py",
    "$HOME/ok", "C:\\r\\ok.py", "../up.py", "src/../../x.py", "//srv/x",
    "a//b.py", "", " ", "sp/gone.py", "nod/gone.py", "zz/x.py", "/path/to/x",
    "src/*.py", "~z/x", "/r/sp/q",
)  # fmt: skip
AP = (
    "src/gone.py", "src/ok.py", "absent.py", "/r/ok/a.py", "/hq/ok", "sp/x",
    "SRC/GONE.PY", "/ab/r/absent", "/un/y", "",
)  # fmt: skip
ANCHORS: tuple[tuple[str | None, str, bool], ...] = (
    (None, "unanchored", False),
    ("/r", "home_checkout", True),
    ("/r/", "standin", False),
    ("C:\\r", "checkout", True),
    ("/r/nod", "home_checkout", True),
    ("~/r", "checkout", True),
    ("~z/r", "checkout", True),
)
ROOTS: tuple[str | None, ...] = (None, "/r", "/r/", "C:\\r", "/r/nod", "~/r")
PRE = ((), ("/r/src/ok.py", "/r/src/sp.py"))
#: ``expanduser`` raises KeyError (not caught) on a leading ``~!``.
RAISING = ("src/ok.py", "~!/x", "src/gone.py")
BUDGET_BODY = " ".join(
    f"run.py www.x.com/a/b.md d/f{i}.py" if i % 2 else f"d/f{i}.py" for i in range(14)
)
BODIES = (
    "",
    "see src/ok.py and src/gone.py, also SRC/OK.py and nod/gone.py plus "
    "sp/a.py; run.py; www.x.com/a/b.md; .github/ci.yml; path/to/config.yaml; "
    "a/b.unknownext; docs/x.MD; a/../../x.py; zz/q.py src/ok.py",
    "x/a.py docs/sp.md nod/q.rs pkg.v2/x.py .hidden/a.toml",
    BUDGET_BODY,
)
CITATIONS = (
    "a.py", "a/b.py", "a/b.PY", "a/b.pyc", ".github/x.yml", "a.b/c.py",
    "x/.a.py", "a/b.", "a/b", "path/to/x.py", "path/x.py", "/a/b.py",
    "a/b.unknownext", "foo/bar.md", "etc/bar.py", "a/b.tar.gz", "a/.py",
    "www.x.com/a/b.md", "a/b.Md", "./a/b.py", "a/b.py.txt", "",
)  # fmt: skip
SWEEP = (("a/b.", "s"), ("a", "/b.py"))


def _lower(p: str) -> str:
    return "".join(chr(ord(c) + 32) if "A" <= c <= "Z" else c for c in p)


class _Reads:
    def __init__(self, log: list[str]) -> None:
        self.log = log

    def key(self, p: str) -> str:
        return "" if "zz" in p else _lower(p)

    def exists(self, p: str) -> bool:
        self.log.append("e" + p)
        return "ok" in p

    def sparse(self, p: str) -> bool:
        self.log.append("s" + p)
        return "sp" in p

    def under(self, p: str, root: Any) -> bool:
        self.log.append("r" + p)
        return p.startswith(str(root))

    def isdir(self, p: str) -> bool:
        self.log.append("d" + p)
        return "nod" not in p


@contextlib.contextmanager
def _world(nt: bool, reads: _Reads) -> Iterator[None]:
    flavour = ntpath if nt else posixpath
    fake_os = types.SimpleNamespace(
        path=types.SimpleNamespace(
            dirname=flavour.dirname, expanduser=_expanduser(nt), isdir=reads.isdir
        )
    )
    with (
        scripted(nt),
        mock.patch.object(va, "Path", PureWindowsPath if nt else PurePosixPath),
        mock.patch.object(va, "os", fake_os),
        mock.patch.object(va, "_path_exists", reads.exists),
        mock.patch.object(va, "_absent_by_sparsity", reads.sparse),
        mock.patch.object(va, "_resolves_under", reads.under),
    ):
        yield


def _anchor(spec: tuple[str | None, str, bool]) -> Any:
    root, rule, convicting = spec

    def absolute(p: str) -> tuple[str | None, str]:
        if "un" in p:
            return None, "unanchored"
        if "hq" in p:
            return "/H" + p, "home_checkout"
        if "ab" in p:
            return p, "absolute"
        return p, "standin"

    return types.SimpleNamespace(
        root=root, rule=rule, convicting=convicting, absolute=absolute
    )


def _out(reads: _Reads, pre: tuple[str, ...]) -> Any:
    out = va._AnchoredRead()
    setattr(out, "keys", reads.key)  # noqa: B010
    for p in pre:
        out.first(p)
    return out


def _buckets(out: Any) -> dict[str, list[str]]:
    return {
        "checked": out.checked,
        "missing": out.missing,
        "claim": out.claim_anchored,
        "verified": out.verified,
        "absent": out.expected_absent,
        "unconfirmed": out.unconfirmed,
        "reasons": out.reasons,
        "present_absent": out.present_absent,
    }


def _spans(body: str) -> list[list[int]]:
    text = _bounded_scan_text(body)
    return [list(m.span(1)) for m in _RELATIVE_CITATION_RE.finditer(text)]


def _rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    rows.append(
        {
            "k": "ic",
            "s": list(CITATIONS),
            "o": [call(va._is_checkable_citation, s) for s in CITATIONS],
        }
    )
    for nt in (False, True):
        for kind in ("P", "L", "R") if not nt else ("P", "L"):
            pres = PRE if kind == "P" and not nt else PRE[:1]
            for root in ROOTS[1:]:
                log: list[str] = []
                reads = _Reads(log)
                with _world(nt, reads):
                    keyset = call(
                        lambda r=root, k=kind: sorted(
                            va._relative_attestation_keys(
                                r, _paths(k, list(VP)), _paths(k, list(AP)), reads.key
                            )
                        )
                    )
                rows.append(
                    {
                        "k": "rk",
                        "nt": nt,
                        "kind": kind,
                        "root": root,
                        "o": keyset,
                        "c": log,
                    }
                )
            for spec in ANCHORS:
                if kind != "L":
                    log = []
                    reads = _Reads(log)
                    with _world(nt, reads):
                        anchor = _anchor(spec)
                        pure = PureWindowsPath if nt else PurePosixPath
                        at_root: Any = pure(spec[0]) if spec[0] is not None else None
                        mode = path_mode(_paths(kind, []))
                        o = [
                            call(
                                lambda s=s: (
                                    None
                                    if (
                                        f := va._attestation_read(
                                            anchor, at_root, s, mode=mode
                                        )
                                    )
                                    is None
                                    else [f.path, f.rule, f.convicting, f.readable]
                                )
                            )
                            for s in VP
                        ]
                    rows.append(
                        {
                            "k": "at",
                            "nt": nt,
                            "kind": kind,
                            "a": list(spec),
                            "o": o,
                            "c": log,
                        }
                    )
                for pre in pres:
                    log = []
                    reads = _Reads(log)
                    with _world(nt, reads):
                        out = _out(reads, pre)
                        x = call(
                            va._read_attestations,
                            _anchor(spec),
                            _paths(kind, list(VP)),
                            _paths(kind, list(AP)),
                            out,
                        )
                    rows.append(
                        {
                            "k": "ra",
                            "nt": nt,
                            "kind": kind,
                            "a": list(spec),
                            "pre": list(pre),
                            "x": x,
                            "o": _buckets(out),
                            "c": log,
                        }
                    )
            for root in ROOTS if kind == "P" else ROOTS[:2] + ROOTS[3:4]:
                for pre in pres:
                    log = []
                    reads = _Reads(log)
                    with _world(nt, reads):
                        out = _out(reads, pre)
                        x = call(
                            va._check_anchored_attestations,
                            root,
                            _paths(kind, list(VP)),
                            _paths(kind, list(AP)),
                            checked=out.checked,
                            missing=out.missing,
                            claim_anchored=out.claim_anchored,
                            verified=out.verified,
                            expected_absent=out.expected_absent,
                            seen=out.seen,
                            keys=reads.key,
                        )
                    rows.append(
                        {
                            "k": "ca",
                            "nt": nt,
                            "kind": kind,
                            "root": root,
                            "pre": list(pre),
                            "x": x,
                            "o": _buckets(out),
                            "c": log,
                        }
                    )
        for b, body in enumerate(BODIES):
            for spec in ANCHORS[1:] if b != 3 else ANCHORS[1:3]:
                for pre in PRE if b == 1 else PRE[:1]:
                    log = []
                    reads = _Reads(log)
                    with _world(nt, reads):
                        out = _out(reads, pre)
                        x = call(va._read_citations, _anchor(spec), body, out)
                    rows.append(
                        {
                            "k": "rc",
                            "nt": nt,
                            "a": list(spec),
                            "body": body,
                            "m": _spans(body),
                            "pre": list(pre),
                            "x": x,
                            "o": _buckets(out),
                            "c": log,
                        }
                    )
                    log = []
                    reads = _Reads(log)
                    with _world(nt, reads):
                        out = _out(reads, pre)
                        x = call(
                            va._check_anchored_citations,
                            (PureWindowsPath if nt else PurePosixPath)(spec[0] or ""),
                            body,
                            checked=out.checked,
                            missing=out.missing,
                            claim_anchored=out.claim_anchored,
                            seen=out.seen,
                            keys=reads.key,
                        )
                    rows.append(
                        {
                            "k": "cc",
                            "nt": nt,
                            "root": spec[0],
                            "body": body,
                            "m": _spans(body),
                            "pre": list(pre),
                            "x": x,
                            "o": _buckets(out),
                            "c": log,
                        }
                    )
    for nt in (False, True):
        log = []
        reads = _Reads(log)
        with _world(nt, reads):
            out = _out(reads, ())
            x = call(
                va._read_attestations,
                _anchor(ANCHORS[1]),
                list(RAISING),
                [],
                out,
            )
        rows.append(
            {
                "k": "ra",
                "nt": nt,
                "kind": "P",
                "a": list(ANCHORS[1]),
                "vp": list(RAISING),
                "pre": [],
                "x": x,
                "o": _buckets(out),
                "c": log,
            }
        )
        log = []
        reads = _Reads(log)
        with _world(nt, reads):
            out = _out(reads, ())
            x = call(
                va._check_anchored_attestations,
                "/r",
                [],
                list(RAISING),
                checked=out.checked,
                missing=out.missing,
                claim_anchored=out.claim_anchored,
                verified=out.verified,
                expected_absent=out.expected_absent,
                seen=out.seen,
                keys=reads.key,
            )
        rows.append(
            {
                "k": "ca",
                "nt": nt,
                "kind": "P",
                "root": "/r",
                "ap": list(RAISING),
                "pre": [],
                "x": x,
                "o": _buckets(out),
                "c": log,
            }
        )
    return rows


def generate() -> dict[str, bytes]:
    """``verify_anchoring_passes.jsonl``: not gated (the host is scripted,
    the bodies ASCII)."""
    rows = _rows()
    return {
        "verify_anchoring_passes.jsonl": jsonl(
            [{"k": "header", "cases": len(rows)}, *rows]
        )
    }


def generate_unicode() -> dict[str, bytes]:
    """``verify_anchoring_passes_unicode.jsonl``: gated on the Unicode
    release (`str.lower`)."""
    rows: list[dict[str, Any]] = []
    for head, tail in SWEEP:
        h = hashlib.sha256()
        n = 0
        for c in range(0x110000):
            h.update(b"T" if va._is_checkable_citation(head + chr(c) + tail) else b"F")
            n += 1
        rows.append(
            {"k": "sweep", "head": head, "tail": tail, "n": n, "sha": h.hexdigest()}
        )
    return {
        "verify_anchoring_passes_unicode.jsonl": jsonl(
            [{"k": "header", "cases": len(rows)}, *rows]
        )
    }

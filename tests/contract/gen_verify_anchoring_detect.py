"""Fixtures: `detect_path_drift` and `holds_commit` of `verify_anchoring`
(``verify_anchoring_detect.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/verify_anchoring/detect.rs``.

Drift rows (``k`` ``d``) run `detect_path_drift` over the scripted host of
`gen_attestation_paths` (``nt`` the row's) and the closure rules of
`gen_verify_anchoring_passes`, each call logged (``c``) with its initial:
`_path_exists` (``e``, also the one `path_drift_steps` imports),
`_absent_by_sparsity` (``s``), `_resolves_under` (``r``), `os.path.isdir`
(``d``), and two more rules this file adds:

- `_is_multi_segment_routelike` (``t``): the path starts with ``/api/``;
- `_worktree_root_is_live` (``l``): ``dead`` does not occur in the root.

`_ClaimKeys()` answers the claim key rule of the passes (``""`` where
``zz`` occurs, else ASCII capitals lowered). The stub anchors are those
of the passes. `_extract_candidates` is the reference's own, with
``HOME`` set to ``/h`` (no body names a user with ``~name``), and the
citations of `_RELATIVE_CITATION_RE` are recorded as the spans of group 1
over the bounded body (``m``), which the native test hands to the pass.
Rows with ``x`` read through `_extended`, the stub anchor with two more
translations: a path holding ``mv`` moves below ``/api/m`` (a route target
that is not the cited path), and a path holding a space moves below ``/S``
with ``ok`` cut to ``k`` (a spaced citation whose whole target is missing
while its prefix exists).
``o`` is the report as ``to_dict`` lists it (the anchored keys only under
an anchor) or what the call raised (``!`` and the class name).

Holds rows (``k`` ``h``) run `holds_commit` over small repositories
written to a scratch folder: loose and packed objects, SHA-1 and
SHA-256, built by hand (no git runs), recorded as their files (``w``:
path below the scratch root and the bytes as hex; a path ending in ``/``
is an empty folder). ``root`` is below the scratch root; ``sha`` the name
asked; ``o`` the answer or the class name of the raise.

The file is POSIX only: `_extract_candidates` spells paths through the
host's `Path`, and `holds_commit` locates through `os.path.realpath`.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import struct
import tempfile
import types
import zlib
from collections.abc import Iterator
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from unittest import mock

from bettermemory import path_drift_steps as pds
from bettermemory import verify_anchoring as va
from bettermemory.verify_candidates import _RELATIVE_CITATION_RE, _bounded_scan_text

from ._common import jsonl
from .gen_attestation_paths import _expanduser, _paths, scripted
from .gen_verify_anchoring_passes import ANCHORS, _anchor, _Reads

BODIES = (
    "",
    "plain prose with no paths at all",
    "see /r/ok/a.py and /r/gone/b.py, then `/r/gone/b.py` again; "
    "/api/v1/events/x and /hq/gone and /un/x and /ab/r/ok; /r/sp/q; "
    "~/gone.py; /R/OK/A.PY; /zz/q.py; src/ok.py src/gone.py nod/gone.py",
    "open `/r/ok dir/x.py` and `/r/no dir/y.py` and /r/ok/c.py",
    "relative only: src/ok.py, docs/gone.md, nod/x.py, sp/a.rs",
    "/r/a/b/c/d/ok/e.py /api/x/y /api/ok/z /r/gone/f.py /r/gone/f.py",
)
VP = (
    "/r/ok/a.py", "/r/gone/b.py", "src/gone.py", "src/ok.py", "/hq/gone",
    "/un/x", "~/gone.py", "/ab/r/gone", "../up.py", "/api/v1/events/x",
)  # fmt: skip
AP = ("/r/sp/q", "src/x.py", "/r/ok/a.py", "/r/gone/f.py", "/hq/ok", "sp/x")
RAISING = ("src/ok.py", "~!/x")
ROOTS: tuple[str | None, ...] = (None, "/r", "/r/dead", "~/r")


class _DReads(_Reads):
    def routelike(self, p: str) -> bool:
        self.log.append("t" + p)
        return p.startswith("/api/")

    def live(self, root: Any) -> bool:
        self.log.append("l" + str(root))
        return "dead" not in str(root)


def _extended(spec: tuple[str | None, str, bool]) -> Any:
    base = _anchor(spec)

    def absolute(p: str) -> tuple[str | None, str]:
        if "mv" in p:
            return "/api/m" + p, "home_checkout"
        if " " in p:
            return "/S" + p.replace("ok", "k"), "home_checkout"
        return base.absolute(p)

    return types.SimpleNamespace(
        root=base.root, rule=base.rule, convicting=base.convicting, absolute=absolute
    )


#: Anchored rows over `_extended`: (body, verified, absent, anchor). The
#: route target differs from the cited path; a bare spaced citation whose
#: prefix exists and is attested, and one whose prefix is missing; an
#: existing attested path under a convicting rule; an absolute target a
#: relative attestation defers to.
EXTENDED: tuple[tuple[str, tuple[str, ...], tuple[str, ...], Any], ...] = (
    ("call /mv/q and /mv/ok", (), (), ANCHORS[1]),
    ("call /mv/q and /mv/ok", (), (), ANCHORS[0]),
    ("see /hq/ok Dir/x.py and /hq/no Dir/y.py", ("/hq/ok",), (), ANCHORS[1]),
    ("see /hq/ok Dir/x.py and /hq/no Dir/y.py", (), ("/hq/ok",), ANCHORS[1]),
    ("see /hq/ok Dir/x.py", ("/H/hq/ok",), (), ANCHORS[1]),
    ("see /ab/r/ok", ("/ab/r/ok",), (), ANCHORS[1]),
    ("see /ab/r/ok", (), ("/ab/r/ok",), ANCHORS[1]),
    ("see /r/src/gone.py", ("src/gone.py",), (), ANCHORS[1]),
    ("see /r/src/gone.py", ("src/gone.py",), (), ANCHORS[0]),
    ("see /r/src/ok.py", (), ("src/ok.py",), ANCHORS[1]),
)


@contextlib.contextmanager
def _world(nt: bool, reads: _DReads) -> Iterator[None]:
    flavour = __import__("ntpath" if nt else "posixpath")
    fake_os = types.SimpleNamespace(
        path=types.SimpleNamespace(
            dirname=flavour.dirname, expanduser=_expanduser(nt), isdir=reads.isdir
        )
    )
    with (
        scripted(nt),
        mock.patch.dict(os.environ, {"HOME": "/h"}),
        mock.patch.object(va, "Path", PureWindowsPath if nt else PurePosixPath),
        mock.patch.object(va, "os", fake_os),
        mock.patch.object(va, "_ClaimKeys", lambda: reads.key),
        mock.patch.object(va, "_path_exists", reads.exists),
        mock.patch.object(pds, "_path_exists", reads.exists),
        mock.patch.object(va, "_is_multi_segment_routelike", reads.routelike),
        mock.patch.object(pds, "_is_multi_segment_routelike", reads.routelike),
        mock.patch.object(va, "_absent_by_sparsity", reads.sparse),
        mock.patch.object(va, "_resolves_under", reads.under),
        mock.patch.object(va, "_worktree_root_is_live", reads.live),
    ):
        yield


def _spans(body: str) -> list[list[int]]:
    text = _bounded_scan_text(body)
    return [list(m.span(1)) for m in _RELATIVE_CITATION_RE.finditer(text)]


def _report(r: Any) -> Any:
    d = r.to_dict()
    if "unconfirmed" in d:
        d["unconfirmed"] = [[c["path"], c["reason"]] for c in d["unconfirmed"]]
    return d


def _drift(
    nt: bool,
    body: str,
    vp: tuple[str, ...],
    ap: tuple[str, ...],
    kind: str,
    root: str | None,
    spec: tuple[str | None, str, bool] | None,
    extended: bool = False,
) -> dict[str, Any]:
    log: list[str] = []
    make = _extended if extended else _anchor
    reads = _DReads(log)
    with _world(nt, reads):
        try:
            o: Any = _report(
                va.detect_path_drift(
                    body,
                    verified_paths=_paths(kind, list(vp)),
                    absent_paths=_paths(kind, list(ap)),
                    worktree_root=root,
                    anchor=None if spec is None else make(spec),
                )
            )
        except Exception as exc:  # noqa: BLE001
            o = "!" + type(exc).__name__
    row: dict[str, Any] = {"k": "d", "nt": nt, "body": body, "kind": kind}
    if vp != VP:
        row["vp"] = list(vp)
    if ap != AP:
        row["ap"] = list(ap)
    row |= {"root": root, "a": None if spec is None else list(spec)}
    if extended:
        row["x"] = True
    row |= {"m": _spans(body), "o": o, "c": log}
    return row


def _drift_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for nt in (False, True):
        for b, body in enumerate(BODIES):
            for vp, ap in ((VP, AP), ((), ())):
                if b == 1 and not vp:
                    continue
                for root in ROOTS if not nt or b in (2, 4) else ROOTS[:2]:
                    rows.append(_drift(nt, body, vp, ap, "P", root, None))
                for spec in ANCHORS[:3] if not nt or b == 2 else ANCHORS[1:2]:
                    rows.append(_drift(nt, body, vp, ap, "P", None, spec))
        rows.append(_drift(nt, BODIES[2], VP, AP, "L", "/r", None))
        rows.append(_drift(nt, BODIES[2], VP, AP, "R", None, ANCHORS[1]))
        rows.append(_drift(nt, BODIES[3], RAISING, (), "P", "/r", None))
        rows.append(_drift(nt, BODIES[3], RAISING, (), "P", None, ANCHORS[1]))
        rows.append(_drift(nt, BODIES[3], (), RAISING, "P", None, ANCHORS[3]))
        for spec in ANCHORS[3:]:
            rows.append(_drift(nt, BODIES[2], VP, AP, "P", None, spec))
    for xbody, xvp, xap, xspec in EXTENDED:
        rows.append(_drift(False, xbody, xvp, xap, "P", None, xspec, extended=True))
    return rows


# Repositories written by hand.


def _obj(kind: bytes, data: bytes) -> bytes:
    return kind + b" " + str(len(data)).encode() + b"\0" + data


def _name(raw: bytes, algo: str) -> str:
    return hashlib.new(algo, raw).hexdigest()


def _deflate(data: bytes) -> bytes:
    """`data` as a zlib stream of one stored block (no compression), written
    here rather than by `zlib.compress`, whose bytes follow the compressor
    the interpreter links. Only `zlib.decompress` is asked, as a check."""
    if len(data) > 0xFFFF:
        raise AssertionError("one stored block holds at most 65535 bytes")
    n = len(data)
    out = (
        b"\x78\x01\x01"
        + struct.pack("<HH", n, n ^ 0xFFFF)
        + data
        + struct.pack(">I", zlib.adler32(data))
    )
    if zlib.decompress(out) != data:
        raise AssertionError("the stored stream gives other bytes")
    return out


def _loose(algo: str, raw: bytes) -> tuple[str, bytes]:
    n = _name(raw, algo)
    return f"objects/{n[:2]}/{n[2:]}", _deflate(raw)


def _pack(algo: str, raws: list[bytes]) -> dict[str, bytes]:
    """A version 2 pack and its index of whole objects (no deltas)."""
    hl = hashlib.new(algo).digest_size
    body = b"PACK" + struct.pack(">II", 2, len(raws))
    entries: list[tuple[bytes, int, int]] = []
    codes = {b"commit": 1, b"tree": 2, b"blob": 3, b"tag": 4}
    for raw in raws:
        kind, rest = raw.split(b" ", 1)
        data = rest.split(b"\0", 1)[1]
        off = len(body)
        size = len(data)
        head = bytearray()
        c = (codes[kind] << 4) | (size & 15)
        size >>= 4
        while size:
            head.append(c | 0x80)
            c = size & 0x7F
            size >>= 7
        head.append(c)
        piece = bytes(head) + _deflate(data)
        body += piece
        entries.append((hashlib.new(algo, raw).digest(), off, zlib.crc32(piece)))
    body += hashlib.new(algo, body).digest()
    entries.sort()
    fan = [0] * 256
    for name, _o, _c in entries:
        fan[name[0]] += 1
    acc = 0
    idx = b"\xfftOc" + struct.pack(">I", 2)
    for i in range(256):
        acc += fan[i]
        idx += struct.pack(">I", acc)
    idx += b"".join(e[0] for e in entries)
    idx += b"".join(struct.pack(">I", e[2]) for e in entries)
    idx += b"".join(struct.pack(">I", e[1]) for e in entries)
    idx += body[-hl:]
    idx += hashlib.new(algo, idx).digest()
    stem = "objects/pack/pack-" + body[-hl:].hex()
    return {stem + ".pack": body, stem + ".idx": idx}


def _repo(algo: str, packed: bool, tag: str) -> tuple[dict[str, bytes], str]:
    """The files of one repository below ``.git`` and its commit's name."""
    blob = _obj(b"blob", tag.encode())
    tree_entry = b"100644 f\0" + hashlib.new(algo, blob).digest()
    tree = _obj(b"tree", tree_entry)
    commit = _obj(
        b"commit",
        b"tree " + _name(tree, algo).encode() + b"\n"
        b"author a <a> 0 +0000\ncommitter a <a> 0 +0000\n\n" + tag.encode() + b"\n",
    )
    files: dict[str, bytes] = {"HEAD": b"ref: refs/heads/main\n"}
    config = b"[core]\n\trepositoryformatversion = 0\n"
    if algo == "sha256":
        config = (
            b"[core]\n\trepositoryformatversion = 1\n[extensions]\n"
            b"\tobjectformat = sha256\n"
        )
    files["config"] = config
    files["refs/heads/"] = b""
    if packed:
        files |= _pack(algo, [blob, tree, commit])
    else:
        for raw in (blob, tree, commit):
            p, z = _loose(algo, raw)
            files[p] = z
    return files, _name(commit, algo)


def _write(base: Path, world: dict[str, bytes]) -> None:
    for rel, data in world.items():
        p = base / rel
        if rel.endswith("/"):
            p.mkdir(parents=True, exist_ok=True)
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


def _holds_rows() -> list[dict[str, Any]]:
    world: dict[str, bytes] = {}
    names: dict[str, str] = {}
    for algo in ("sha1", "sha256"):
        for packed in (False, True):
            tag = f"{algo}-{'p' if packed else 'l'}"
            files, sha = _repo(algo, packed, tag)
            names[tag] = sha
            world |= {f"{tag}/.git/{k}": v for k, v in files.items()}
    # A linked worktree's `.git` file naming a common folder.
    world["link/.git"] = b"gitdir: ../sha1-l/.git/worktrees/w\n"
    world["sha1-l/.git/worktrees/w/HEAD"] = b"ref: refs/heads/main\n"
    world["sha1-l/.git/worktrees/w/commondir"] = b"../..\n"
    world["sha1-l/.git/worktrees/w/gitdir"] = b"/nowhere/.git\n"
    world["sub/dir/"] = b""
    world["plain/"] = b""
    world["broken/.git"] = b"not a gitdir line\n"
    world["odd/.git/HEAD"] = b"ref: refs/heads/main\n"
    world["odd/.git/config"] = (
        b"[core]\n\trepositoryformatversion = 1\n[extensions]\n\tobjectformat = md5\n"
    )
    world["odd/.git/objects/"] = b""
    world["file"] = b"x"
    # An alternates entry holding a NUL: `realpath` raises ValueError out
    # of `has`, which `holds_commit` answers False. (A release whose
    # `realpath` does not raise passes the entry over: False as well.)
    for tag, alt in (("nul", b"/x\0y\n"), ("nulrel", b"a\0b\n")):
        world[f"{tag}/.git/HEAD"] = b"ref: refs/heads/main\n"
        world[f"{tag}/.git/refs/heads/"] = b""
        world[f"{tag}/.git/objects/info/alternates"] = alt
    asks: list[tuple[str, str]] = []
    for tag, sha in names.items():
        other = names["sha1-l" if tag != "sha1-l" else "sha1-p"]
        asks += [(tag, sha), (tag, sha.upper()), (tag, sha[:12]), (tag, other)]
        asks += [(tag + "/", sha), (tag + "/.git", sha)]
    s1 = names["sha1-l"]
    asks += [
        ("link", s1),
        ("link", names["sha1-p"]),
        ("sha1-l/sub", s1),
        ("sub/dir", s1),
        ("plain", s1),
        ("broken", s1),
        ("odd", s1),
        ("file", s1),
        ("missing", s1),
        ("nul", s1),
        ("nulrel", s1),
        ("sha1-l", ""),
        ("sha1-l", "zz" * 20),
        ("sha1-l", "0" * 40),
        ("sha1-l", s1 + "00"),
        ("sha1-l", s1[:3]),
        ("sha1-l", "g" + s1[1:]),
        ("sha1-p", ""),
        ("sha1-p", "f" * 40),
        ("sha256-p", names["sha256-p"][:40]),
    ]
    world["sha1-l/sub/"] = b""
    rows: list[dict[str, Any]] = [
        {"k": "w", "w": {k: v.hex() for k, v in sorted(world.items())}}
    ]
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(os.path.realpath(tmp))
        _write(base, world)
        for root, sha in asks:
            try:
                o: Any = va.holds_commit(str(base / root) if root else str(base), sha)
            except Exception as exc:  # noqa: BLE001
                o = "!" + type(exc).__name__
            rows.append({"k": "h", "root": root, "sha": sha, "o": o})
    return rows


def generate() -> dict[str, bytes]:
    """``verify_anchoring_detect.jsonl``: POSIX only (the host's `Path` in
    the candidate scan, `realpath` in discovery)."""
    rows = _drift_rows() + _holds_rows()
    return {
        "verify_anchoring_detect.jsonl": jsonl(
            [{"k": "header", "cases": len(rows)}, *rows]
        )
    }

"""Fixture: a location capture's vocabulary, the data a caller's
directory is described by before it is resolved
(``capture_vocabulary.jsonl``).

Pins the native ``capture`` module to the pure part of the reference:
`location_git.Capture.to_token` and `Capture.from_token` (with
`FileSeen` and the helpers `_text` to `_manifest_from`), the hosted
branch of `location.remote_key` and its hosted-or-local decision,
`membership_records.ObjectKey.compare` and `on_any_device`,
`location.to_location` (the ``history`` reader left out) and
`_paths.norm`, `inside` and `basename`. Nothing here reads a file, the
clock or the environment.

Tokens are written as a base and a change: ``"b"`` indexes the header's
``bases`` (tokens of captures built through `Capture`), ``"s"`` sets
keys (a new key goes last) and ``"d"`` deletes them; ``"raw"`` is a
whole input that is no mapping. Recipes (`built`): ``{"$n": [n, leaf]}``
is `leaf` inside `n` lists, ``{"$r": [text, n]}`` is `text` repeated,
``{"$x": [item, n]}`` a list of `n` items, ``{"$i": [n, neg]}`` the int
of `n` nines (negated where `neg`), which JSON cannot carry past 4,300
digits. The answer ``"o"`` is null for a refusal (None),
``{"raise": class}`` where `from_token` raises, else the keys of
``to_token`` of the capture read whose value differs from the input's
(empty: the token came back as it went in). A string longer than 200
characters, or an int of more than 200 digits, in an answer is
``{"$len": n, "$sha": hex}`` of its UTF-8 (``surrogatepass``). The
header's ``order`` is `to_token`'s key order, which ``sort_keys`` would
otherwise hide.

``remote_key`` records ``"o"``: the key; null; or, where the reference
takes its local branch, ``["local", path, file_url]``: the path `_local`
received and whether `_file_url_path` ran first. Both are replaced while
generating (`_file_url_path` by the identity), so neither ``realpath``
nor the host's path rules run.

Two files. ``capture_vocabulary.jsonl`` is not gated: its answers are
the same under Python 3.11 to 3.14 and on every platform: type checks,
`str` methods on characters whose properties no Unicode release since
14.0 changed, `re` patterns, `int()` and `str()` of ints (with the
4,300-digit limit every release since 3.11 has), `str()` of floats
(`repr`'s shortest round trip) and of containers nested 50 to 400 deep,
far below every recursion limit (3.11's is about 993). Checked identical
under Python 3.11, 3.12 and 3.13.

``capture_vocabulary_python.jsonl``, gated on the Python release
(`PYTHON_GATED`), holds every token whose answer is RecursionError (a key
item nested 100,000 deep, alone or paired with another field). Whether
`str()` raises there is a property of the interpreter build and its
stack, not of the reference: CI showed CPython 3.14 on Linux returning
None for those tokens where 3.13, and 3.14 on macOS, raise (3.14 checks
the real C stack, and thread stack sizes differ by platform). No depth is
portable, so these are compared only under the release the files were
made with. The native port answers RecursionError at its own fixed depth
(`STR_DEPTH_TOP`) on every platform, tested natively.
"""

from __future__ import annotations

import dataclasses
import hashlib
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from bettermemory import _paths
from bettermemory import location as loc
from bettermemory.location_git import Capture, FileSeen
from bettermemory.membership_records import ObjectKey

from ._common import dumps_line

H40 = "0123456789abcdef0123456789abcdef01234567"
H64 = H40 + "89abcdef01234567890abcde"


def built(spec: Any) -> Any:
    """A fixture value with its recipes made (iterative for nesting)."""
    if isinstance(spec, dict):
        if set(spec) == {"$n"}:
            depth, leaf = spec["$n"]
            value: Any = built(leaf)
            for _ in range(depth):
                value = [value]
            return value
        if set(spec) == {"$r"}:
            text, count = spec["$r"]
            return text * count
        if set(spec) == {"$i"}:
            digits, negative = spec["$i"]
            number = 10**digits - 1
            return -number if negative else number
        if set(spec) == {"$x"}:
            item, count = spec["$x"]
            return [built(item) for _ in range(count)]
        return {k: built(v) for k, v in spec.items()}
    if isinstance(spec, list):
        return [built(v) for v in spec]
    return spec


def _short(value: Any) -> Any:
    """An answer as written: long texts and ints by length and digest,
    long runs of one item as ``$x``."""
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) >= 10**200:
        value = str(value)
    if isinstance(value, str) and len(value) > 200:
        data = value.encode("utf-8", "surrogatepass")
        return {"$len": len(value), "$sha": hashlib.sha256(data).hexdigest()}
    if isinstance(value, list):
        if len(value) > 8 and all(_same(value[0], v) for v in value):
            return {"$x": [_short(value[0]), len(value)]}
        return [_short(v) for v in value]
    if isinstance(value, dict):
        return {k: _short(v) for k, v in value.items()}
    return value


def _spec(value: Any) -> Any:
    """An input as written: long runs of one item as ``$x``."""
    if isinstance(value, list):
        if len(value) > 8 and all(_same(value[0], v) for v in value):
            return {"$x": [_spec(value[0]), len(value)]}
        return [_spec(v) for v in value]
    if isinstance(value, dict) and not ({"$n", "$r", "$x", "$i"} & set(value)):
        return {k: _spec(v) for k, v in value.items()}
    return value


def _same(a: Any, b: Any) -> bool:
    """Equal in type and value all the way down (``1`` is not ``True``,
    ``-0.0`` is not ``0.0``; a mapping's order aside, as the file's sorted
    keys lose it), without recursion: an input may be deep."""
    work = [(a, b)]
    while work:
        x, y = work.pop()
        if type(x) is not type(y):
            return False
        if isinstance(x, list):
            if len(x) != len(y):
                return False
            work.extend(zip(x, y, strict=True))
        elif isinstance(x, dict):
            if set(x) != set(y):
                return False
            work.extend((x[k], y[k]) for k in x)
        elif isinstance(x, float):
            if repr(x) != repr(y):
                return False
        elif x != y:
            return False
    return True


def _answer(token: Any) -> Any:
    try:
        cap = Capture.from_token(token)
    except Exception as e:  # noqa: BLE001 - the class is the answer
        return {"raise": type(e).__name__}
    if cap is None:
        return None
    return {
        k: _short(v)
        for k, v in cap.to_token().items()
        if not (isinstance(token, dict) and k in token and _same(token[k], v))
    }


def _key(d: str = "16777220", i: str = "1234", b: str | None = "1700000000000000000"):
    return (d, i, b)


def _captures() -> list[Capture]:
    full = Capture(
        cwd="/work/alpha/src",
        state="repo",
        root="/work/alpha",
        kind="primary",
        git_dir="/work/alpha/.git",
        common_dir="/work/alpha/.git",
        object_key=_key(),
        common_object_key=_key(i="1235", b=None),
        root_object_key=_key(i="1236"),
        cwd_key=_key(i="1237"),
        head=H40,
        branch="main",
        ref_tips=(H40, H64),
        remote_keys=("example.invalid/owner/alpha",),
        shallow=True,
        token="tok-1",
        token_written="tok-2",
        token_status="written",
        stamps=(("description", 5, 1700000000000000000, 1700000000000000001, 73),),
        ticks=True,
        files=(
            FileSeen(
                "id-1",
                "alpha",
                "/work/alpha",
                "/work/alpha/bettermemory.toml",
                True,
                _key(i="9"),
            ),
            FileSeen("id-2", "beta", "/work", "/work/bettermemory.toml", False, None),
        ),
        file_errors=("bad line",),
        file_tracked=False,
        evicted=True,
        detail="HEAD",
        manifest=("alpha", (("a.txt", 3), ("b/c.txt", 0))),
        hostname="host.example.invalid",
        alias_keys=("example.invalid/alias/alpha",),
        kept_apart=True,
        tree_recorded=True,
        config_sources=("/etc/gitconfig",),
    )
    win = Capture(
        cwd="C:\\work\\alpha",
        state="indeterminate",
        detail="C:\\work\\alpha\\.git\\HEAD",
        evicted=True,
    )
    folder = Capture(
        cwd="/work/beta",
        state="outside",
        kind="folder",
        root="/work/beta",
        head=H64.upper(),
        files=(
            FileSeen(
                "id-3",
                "beta",
                "/work/beta",
                "/work/beta/bettermemory.toml",
                False,
                None,
            ),
        ),
        manifest=("beta", ()),
    )
    return [Capture(cwd="/work/alpha", state="outside"), full, win, folder]


def _edges(full: Capture) -> Iterator[Capture]:
    """`full` changed one field at a time over its edges."""
    r = dataclasses.replace
    for state in ("repo", "outside", "indeterminate"):
        for kind in (None, "primary", "linked", "bare", "folder"):
            yield r(full, state=state, kind=kind)
    for name in (
        "root", "kind", "git_dir", "common_dir", "object_key", "common_object_key",
        "root_object_key", "cwd_key", "head", "branch", "token", "token_written",
        "ticks", "file_tracked", "manifest", "hostname",
    ):  # fmt: skip
        yield r(full, **{name: None})
    for name in (
        "ref_tips",
        "remote_keys",
        "alias_keys",
        "stamps",
        "files",
        "file_errors",
    ):
        yield r(full, **{name: ()})
    for b in (False,):
        yield r(full, shallow=b, evicted=b, ticks=b, file_tracked=True)
    yield r(full, kept_apart=False, tree_recorded=False, config_sources=())
    for text in (
        "",
        " ",
        "\u00e9",
        "\U0001f600",
        "\ud800",
        "a\x00b",
        "\\",
        "C:\\work\\alpha",
    ):
        yield r(full, cwd=text, branch=text, detail=text, token_status=text)
    yield r(full, head=H40.upper(), ref_tips=(H64.upper(), H40))
    yield r(full, head="abc", ref_tips=("x",))
    yield r(full, ref_tips=(H40,) * 64)
    yield r(full, ref_tips=(H40,) * 65)
    yield r(full, remote_keys=tuple(f"o/{n}" for n in range(256)))
    yield r(full, remote_keys=("k",) * 257)
    yield r(full, alias_keys=("k",) * 256)
    yield r(full, alias_keys=("k",) * 257)
    yield r(full, stamps=(("s", 0, 0, 0, 0),) * 8)
    yield r(full, stamps=(("s", 0, 0, 0, 0),) * 9)
    yield r(full, stamps=(("s", -1, 2**63, -(2**64), 10**40),))
    f = full.files[1]
    yield r(full, files=(f,) * 64)
    yield r(full, files=(f,) * 65)
    yield r(full, file_errors=("e",) * 64)
    yield r(full, file_errors=("e",) * 65)
    yield r(full, manifest=("m", (("p", 1),) * 4096))
    yield r(full, manifest=("m", (("p", 1),) * 4097))
    yield r(full, manifest=("", (("", -5), ("\ud800", 10**30))))
    yield r(full, object_key=("", "", ""), cwd_key=("a", "b", None))
    yield r(full, token_status="failed:no space", detail="config")
    yield r(full, token_status="skipped:read-only")


#: Plain wrong values every key is tried with.
_PLAIN: list[Any] = [None, True, False, 0, 7, -1, "", "x", [], ["x"], {}, {"a": "b"}]


def _hostile() -> Iterator[tuple[dict[str, Any], list[str]]]:
    """(changes, deletions) to a base token, plain values only."""
    keys = list(Capture(cwd="", state="repo").to_token())
    for key in keys:
        yield {}, [key]
        for value in _PLAIN:
            yield {key: value}, []
    yield {"zz": 1, "extra": {"a": [1]}}, []
    yield {"v": 2}, []
    yield {"cwd": {"$r": ["a", 10_000_000]}}, []
    yield {"detail": {"$r": ["\u00e9", 1_000_000]}}, []
    yield {"state": "REPO"}, []
    yield {"kind": "Folder"}, []
    yield {"head": H40.upper()}, []
    yield {"head": H40[:39]}, []
    yield {"head": H40 + "0"}, []
    yield {"head": H64}, []
    yield {"head": H40.replace("0", "\uff10")}, []
    yield {"head": "\u212a" + H40[1:]}, []
    yield {"ref_tips": [H40, "x"]}, []
    yield {"ref_tips": [H40] * 64}, []
    yield {"ref_tips": [H40] * 65}, []
    yield {"remotes": ["a"] * 257}, []
    yield {"aliases": [1]}, []
    yield {"file_errors": [None]}, []
    yield {"file_errors": ["e"] * 65}, []
    for k in (
        ["a", "b"],
        ["a", "b", "c", "d"],
        ["a", "b", None],
        ["a", "b", "c"],
        [5, 6, 7],
        "abc",
    ):
        for name in ("object", "common_object", "root_object", "cwd_object"):
            yield {name: k}, []
    for stamp in (
        ["s", 1, 2, 3, 4], ["s", 1, 2, 3], ["s", 1, 2, 3, 4, 5], [1, 1, 2, 3, 4],
        "s1234", "s123", "", {"0": "s"}, ["s", "1", "-2", " 3 ", "1_0"], ["s", "x", 1, 1, 1],
        ["s", 10**4000, 0, 0, 0], ["s", True, False, 0, 0], ["s", [], 0, 0, 0], None,
    ):  # fmt: skip
        yield {"stamps": [stamp]}, []
    yield {"stamps": [["s", 0, 0, 0, 0]] * 9}, []
    fs = {
        "id": "i",
        "name": "n",
        "dir": "/work",
        "path": "/work/f",
        "boundary": True,
        "object": None,
    }
    yield {"files": [fs]}, []
    for sub in fs:
        yield {"files": [{k: v for k, v in fs.items() if k != sub}]}, []
        for value in (None, 1, "x", ["a", "b", "c"], [], True):
            yield {"files": [dict(fs, **{sub: value})]}, []
    yield {"files": [dict(fs, extra=1)]}, []
    yield {"files": ["x"]}, []
    yield {"files": [["i", "n", "d", "p"]]}, []
    yield {"files": [fs] * 65}, []
    manifest: Any
    for manifest in (
        {"name": "m", "entries": [["p", 1]]}, {"name": "m"}, {"entries": []},
        {"name": 1, "entries": []}, {"name": "m", "entries": None},
        {"name": "m", "entries": [["p"]]}, {"name": "m", "entries": ["p1"]},
        {"name": "m", "entries": ["p"]}, {"name": "m", "entries": [[1, 1]]},
        {"name": "m", "entries": [["p", "x"]]}, {"name": "m", "entries": [["p", 1, 2]]},
        {"name": "m", "entries": [["p", 1]] * 4097}, {"name": "m", "entries": {}},
        ["m", []], "m",
    ):  # fmt: skip
        yield {"manifest": manifest}, []
    for name in ("ticks", "file_tracked", "shallow", "evicted"):
        for value in ("true", 1, 0, [0]):
            yield {name: value}, []
    yield {"token_status": None}, []
    yield {"detail": None}, []
    yield {"cwd": "", "state": "repo"}, []
    yield {"v": 1, "cwd": "\ud800\udc00"}, []
    yield {"files": [{"$n": [3, 0]}]}, []


#: The fields `from_token` reads, in its order, with a value each that it
#: refuses there; and the fields that can raise, with a value that does.
_REFUSED = {
    "cwd": 1, "state": "x", "root": 1, "kind": "x", "git_dir": 1, "common_dir": 1,
    "object": 1, "common_object": 1, "root_object": 1, "cwd_object": 1, "head": "x",
    "branch": 1, "ref_tips": 1, "remotes": 1, "aliases": 1, "token": 1,
    "token_written": 1, "token_status": 1, "stamps": 1, "ticks": 1, "files": 1,
    "file_errors": 1, "detail": 1, "manifest": 1, "hostname": 1,
}  # fmt: skip
_DEEP = {"$n": [100_000, 0]}
_FS = {"id": "i", "name": "n", "dir": "/work", "path": "/work/f"}
_RAISING: dict[str, Any] = {
    "object": [_DEEP, "1", None],
    "common_object": ["1", _DEEP, None],
    "root_object": ["1", "1", _DEEP],
    "cwd_object": [_DEEP, "1", None],
    "stamps": [["s", float("inf"), 0, 0, 0]],
    "files": [dict(_FS, object=[_DEEP, "1", None])],
    "manifest": {"name": "m", "entries": [["p", float("-inf")]]},
}


def _python_rules() -> Iterator[tuple[dict[str, Any], list[str]]]:
    """Python's own rules where `from_token` lets them through, and the
    order it reads its fields in."""
    for a, refused in _REFUSED.items():
        for b, raising in _RAISING.items():
            if a != b:
                yield {a: refused, b: raising}, []
    for a, first in _RAISING.items():
        for b in _RAISING:
            if a != b:
                second = {"stamps": [["s", 0, 0, 0, float("nan")]]}.get(b, _RAISING[b])
                yield {a: first, b: second}, []
    odd: list[Any] = [
        1.5, -0.0, 1e300, 1e-07, 1e16, float("inf"), float("nan"), True, None,
        [1, "a"], {"k": [None, False]}, [], {}, 10**30, -7,
    ]  # fmt: skip
    for value in odd:
        yield {"object": [value, "1", value]}, []
        yield {"cwd_object": ["1", value, None]}, []
    for value in (
        1.9, -1.9, 0.5, 1e300, float("inf"), float("-inf"), float("nan"), "\u0661\u0662",
        {"$r": ["1", 4300]}, {"$r": ["1", 4301]}, "\t7\n", "+5", "0x1", "1.0", "", 2.0**70,
        {"$i": [4301, False]}, {"$i": [5000, True]},
    ):  # fmt: skip
        yield {"stamps": [["s", value, 0, 0, 0]]}, []
        yield {"stamps": [["s", 0, 0, 0, value]]}, []
        yield {"manifest": {"name": "m", "entries": [["p", value]]}}, []
    for v in (1.0, True, float("nan"), 1.5, -0.0, 1, 0, False):
        yield {"v": v}, []
    for value in (0.0, -0.0, float("nan"), 2.5):
        yield {"shallow": value, "evicted": value, "file_tracked": value}, []
        yield {"ticks": value}, []
    # `str()` of an int past 4,300 digits raises ValueError (caught).
    for digits in (4300, 4301):
        for negative in (False, True):
            big = {"$i": [digits, negative]}
            yield {"object": [big, "1", None]}, []
            yield {"root_object": ["1", big, None]}, []
            yield {"cwd_object": ["1", "1", big]}, []
            yield {"files": [dict(_FS, object=["1", "1", [big]])]}, []
    big = {"$i": [4301, False]}
    yield {"object": [[big, _DEEP], "1", None]}, []
    yield {"object": [[_DEEP, big], "1", None]}, []
    for depth in (50, 200, 400, 100_000):
        leaf: Any
        for leaf in (0, [], {"a": 1}, "x"):
            yield {"object": [{"$n": [depth, leaf]}, "1", None]}, []
            yield {"root_object": ["1", "1", {"$n": [depth, leaf]}]}, []
        yield {"files": [dict(_FS, object=[{"$n": [depth, 0]}, "1", None])]}, []
        yield {"files": [_FS, dict(_FS, object=["1", "1", {"$n": [depth, []]}])]}, []
    yield {"manifest": {"name": "m", "entries": [["p", _DEEP]]}}, []


def _token_records(
    bases: list[dict[str, Any]],
    cases: Iterator[tuple[dict[str, Any], list[str]]],
    *,
    all_bases: bool = False,
    kind: str = "tok",
) -> list[dict[str, Any]]:
    records = []
    listed = list(cases)
    for b, base in enumerate(bases[:2]):
        for changes, deletions in listed:
            if b == 0 and changes and not all_bases:
                continue
            token = dict(base)
            for key in deletions:
                del token[key]
            for key, spec in changes.items():
                token[key] = built(spec)
            rec: dict[str, Any] = {"k": kind, "b": b, "o": _answer(token)}
            if changes:
                rec["s"] = _spec(changes)
            if deletions:
                rec["d"] = deletions
            records.append(rec)
    return records


def _diff(token: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in token.items() if not _same(base.get(k, object()), v)}


# --------------------------------------------------------------- remote_key


@contextmanager
def _no_host(seen: list[Any]) -> Iterator[None]:
    """`_local` and `_file_url_path` replaced: each call is recorded in
    `seen`, and neither reads the file system or the host's path rules."""
    saved = loc._local, loc._file_url_path

    def local(path: str, base: str | None) -> str:
        seen.append(path)
        return "<local>"

    def file_url_path(url: str) -> str:
        seen.append(None)
        return url

    loc._local = local
    loc._file_url_path = file_url_path
    try:
        yield
    finally:
        loc._local, loc._file_url_path = saved


def _urls() -> Iterator[str | None]:
    schemes = ["ssh", "git", "https", "HTTP", "git+ssh", "x-y.z+1"]
    users = ["", "git@", "user:pw@", "a@b@"]
    hosts = [
        "example.invalid", "EXAMPLE.Invalid", "sub.example.invalid", "192.0.2.1",
        "[::1]", "\u00c9xample.invalid", "\u212aexample.invalid", "a b.invalid",
    ]  # fmt: skip
    ports = ["", ":22", ":", ":\u0661\u0662"]
    paths = [
        "owner/alpha", "Owner/Alpha.GIT", "owner/alpha/", "/owner/alpha.git", "owner/alpha.git/",
        "owner/alpha.git.git", "a%20b/c", "", "~user/alpha", "a/b?x=1#f",
    ]  # fmt: skip
    n = 0
    for scheme in schemes:
        for user in users:
            for port in ports:
                for path in paths:
                    host = hosts[n % len(hosts)]
                    n += 1
                    yield f"{scheme}://{user}{host}{port}/{path}"
    scp_paths = [
        "owner/alpha.git", "Owner/Alpha", "/abs/alpha", "alpha", "a:b", "", "x/", ".git",
        "a\nb", "~/alpha", "\\x", "C:\\x",
    ]  # fmt: skip
    for user in ["", "git@", "a@b@", "u:p@"]:
        for host in ["example.invalid", "EXAMPLE.invalid", "h", "c", "C", "\u00e9"]:
            for path in scp_paths:
                yield f"{user}{host}:{path}"
    plain = [
        None, "", "   ", "\u3000\u00a0", "/work/alpha", "./alpha", "../alpha", ".git",
        "C:\\work\\alpha", "C:/work/alpha", "c:alpha", "\u00e9:alpha", "1:alpha",
        "\\srv\\share\\alpha", "file:///work/alpha", "FILE://host/x", "File://", "file:/x",
        "alpha", "ssh://", "ssh:///alpha", "ssh://h/", "ssh://h", "://h/x", "1ssh://h/x",
        "s\u0131sh://h/x", "\u017fsh://h/x", "\u0130://h/x", "ssh://h:1\u00b2/x",
        "ssh://h/a\nb", "ssh://h/.git", "ssh://h//", "ssh://h/a.git//", "h:/", "h:a/.GIT",
        "@h:x", "a@:x", "a/b:c", "\ta:b\n", "\x1ca:b\x1f", "\u2028a:b\u2029",
    ]  # fmt: skip
    yield from plain
    for u in ["ssh://example.invalid/o/a", "example.invalid:o/a"]:
        for pad in (" ", "\t", "\n", "\x0b", "\x85", "\u00a0", "\u2003", "\x1c"):
            yield pad + u + pad


def _remote_records() -> list[dict[str, Any]]:
    out = []
    seen: list[Any] = []
    with _no_host(seen):
        for url in dict.fromkeys(_urls()):
            seen.clear()
            key: Any = loc.remote_key(url)
            if key is not None and key.startswith("local:"):
                assert key == "local:<local>", key
                key = ["local", seen[-1], None in seen]
            out.append({"k": "rk", "u": url, "o": key})
    return out


# ------------------------------------------------------------- paths, keys

_PATHS = [
    "/", "//", "/work/alpha", "/work/alpha/", "/work/alpha//", "/work/al\\pha", "/work/alpha\\",
    "/work/alphabet", "/work", "C:\\", "C:", "C:\\/", "C:\\work\\alpha", "C:/work/alpha/",
    "C:\\work/alpha\\", "C:\\work\\alphabet", "c:\\work", "\\srv\\share\\alpha", "",
    "work/alpha", "work\\alpha\\", "1:\\x", "\u00e9:\\x", "C:x", "C:/",
]  # fmt: skip

_KEYS: list[tuple[str, str, str | None] | None] = [
    ("1", "2", "3"), ("1", "2", None), ("1", "2", "4"), ("9", "2", "3"), ("1", "5", "3"),
    ("9", "2", None), ("", "", ""), None,
]  # fmt: skip


def _path_records() -> list[dict[str, Any]]:
    out = []
    for p in _PATHS:
        bits = "".join("1" if _paths.inside(p, r) else "0" for r in _PATHS)
        out.append(
            {
                "k": "path",
                "p": p,
                "n": _paths.norm(p),
                "b": _paths.basename(p),
                "in": bits,
            }
        )
    return out


def _key_records() -> list[dict[str, Any]]:
    out = []
    for a in _KEYS:
        if a is None:
            continue
        ka = ObjectKey(*a)
        for b in _KEYS:
            kb = None if b is None else ObjectKey(*b)
            any_dev = None if b is None else ka.on_any_device(b[1], b[2])
            out.append(
                {
                    "k": "key",
                    "a": list(a),
                    "b": b and list(b),
                    "c": ka.compare(kb),
                    "y": any_dev,
                }
            )
    return out


def _location_json(cap: Capture, machine: str, session: str | None) -> dict[str, Any]:
    location = loc.to_location(cap, machine=machine, session=session)

    def key(k: ObjectKey | None) -> list[str | None] | None:
        return None if k is None else [k.device, k.inode, k.birth]

    out: dict[str, Any] = {}
    for f in dataclasses.fields(location):
        if f.name == "history":
            continue
        value = getattr(location, f.name)
        if isinstance(value, ObjectKey):
            value = key(value)
        elif isinstance(value, frozenset):
            value = sorted(value)
        elif f.name == "stamps":
            value = [list(s) for s in value]
        elif f.name == "files":
            value = [
                {
                    "id": r.id,
                    "name": r.name,
                    "dir": r.dir,
                    "path": r.path,
                    "boundary": r.boundary,
                    "object": key(r.object_key),
                }
                for r in value
            ]
        elif f.name == "manifest" and value is not None:
            value = value.to_json()
        elif isinstance(value, tuple):
            value = list(value)
        out[f.name] = value
    out["born"] = location.born
    return out


def _deep(record: dict[str, Any]) -> bool:
    """A case whose answer is the interpreter's recursion limit."""
    return record.get("o") == {"raise": "RecursionError"}


def generate() -> dict[str, bytes]:
    return _files()[0]


def generate_python() -> dict[str, bytes]:
    return _files()[1]


def _files() -> tuple[dict[str, bytes], dict[str, bytes]]:
    caps = _captures()
    bases = [c.to_token() for c in caps]
    records: list[dict[str, Any]] = []
    for c in list(_edges(caps[1])):
        token = c.to_token()
        back = Capture.from_token(token)
        rec = {
            "k": "cap",
            "b": 1,
            "s": _spec(_diff(token, bases[1])),
            "o": _answer(token),
        }
        if back is not None and rec["o"] == {}:
            assert back == dataclasses.replace(
                c, kept_apart=False, tree_recorded=False
            ), c
        records.append(rec)
    for b, base in enumerate(bases):
        assert Capture.from_token(base) is not None
        records.append({"k": "cap", "b": b, "o": _answer(base)})
    records += _token_records(bases, _hostile())
    records += _token_records(bases, _python_rules())
    for raw in (None, [], "x", 1, [["v", 1]], True):
        records.append({"k": "tok", "raw": raw, "o": _answer(raw)})
    for b, base in enumerate(bases):
        # The capture a token describes, as the daemon builds a location.
        cap = Capture.from_token(base)
        assert cap is not None
        for machine, session in (("m1", None), ("", "s1")):
            records.append(
                {
                    "k": "loc",
                    "b": b,
                    "m": machine,
                    "x": session,
                    "o": _location_json(cap, machine, session),
                }
            )
    records += _remote_records()
    records += _path_records()
    records += _key_records()
    deep = [r for r in records if _deep(r)]
    records = [r for r in records if not _deep(r)]
    counts: dict[str, int] = {}
    for r in records:
        counts[r["k"]] = counts.get(r["k"], 0) + 1
    header = {
        "fixture": "capture_vocabulary",
        "bases": bases,
        "order": list(bases[0]),
        "paths": _PATHS,
        "counts": counts,
    }
    lines = [dumps_line(header)] + [dumps_line(r) for r in records]
    gated = {
        "fixture": "capture_vocabulary_python",
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "bases": bases,
        "cases": len(deep),
    }
    deep_lines = [dumps_line(gated)] + [dumps_line(r) for r in deep]
    return (
        {"capture_vocabulary.jsonl": ("\n".join(lines) + "\n").encode("ascii")},
        {
            "capture_vocabulary_python.jsonl": ("\n".join(deep_lines) + "\n").encode(
                "ascii"
            )
        },
    )

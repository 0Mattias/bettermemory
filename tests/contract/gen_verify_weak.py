"""Fixtures: `verify`'s claim-level narrowing (``verify_weak.jsonl``, not
gated).

The native build reads it in
``native/crates/bettermemory-core/src/verify/weak.rs`` and
``src/verify/legs.rs``. This pins `_weak_tier_evaluation`, `_claim_legs`,
`_resolve_reachable`, `_resolve_with_claims` and `resolve_commit_drift`.

No git runs. Every read the narrowing makes through `verify`'s own
bindings (`resolve_repo_pathspecs`,
`commit_author_sha_pairs_touching_pathspecs`, `paths_in_history`,
`too_large`, `local_patch_window`, `commit_patch_stream`,
`literal_floor`, `spelled`, `_literal_pathspecs` and
`resolve_commit_drift_count`) is scripted per case, logged per call and
restored before the case returns. The patch streams are literal
``git log -p -U0`` texts; the claim index and the literal reader are the
reference's own.

One JSON object per line, keys sorted, ASCII. The first line is the
header ``{"k": "header", "count": n, "max": MAX_PATCH_STREAM_COMMITS,
"streams": {name: text}, "walk": WALK}``; the rows are squeezed as
`_squeeze` says, and a reader spells them out first.
Every other line has ``k`` (``"rcd"``, ``"weak"`` or ``"legs"``), the
claims ``c`` (each ``[kind, path, name, value]``), the script ``s``, the
calls ``calls`` and the answer ``o``:

- ``s``: ``rp`` what `resolve_repo_pathspecs` answers (absent: the
  anchors as given); ``pairs`` the answers of the author-date log in call
  order, each null or ``[[isoformat, sha], ...]``; ``hist`` what
  `paths_in_history` answers; ``big`` `too_large`; ``stream`` the patch
  stream or null; ``split`` null or the `PatchWindow` ``[stream, counted,
  touched, deleted]``; ``floor`` null or the `LiteralFloor` ``[paths,
  commits, listed]`` (absent: the empty floor); ``chosen`` the spellings
  `spelled` chooses (``{spec: [names]}``) and ``declined`` its flag;
  ``count`` what `resolve_commit_drift_count` answers.
- ``"rcd"``: ``a`` anchors, ``since`` an isoformat, ``u`` unfiltered,
  ``top`` the toplevel or null, ``w`` null or the walk ``[anchor,
  commits, [[path, [shas]], ...], listed]``; ``o`` null or ``[count,
  checked, drifted, basis]``.
- ``"weak"``: ``shas``, ``specs``, ``sp`` whether a `Spelled` is passed;
  ``o`` ``[drifted, sorted implicated or null]``.
- ``"legs"``: ``a`` anchors; ``o`` null or ``[ungoverned, governed]``.

A call is ``[name, toplevel, ...]``: ``rp`` the paths, ``pairs`` and
``hist`` the pathspecs, ``big``, ``stream`` and ``split`` the shas and
pathspecs, ``floor`` the shas, specs and pathspecs, ``spelled`` the specs,
the anchor and the stamp, ``count`` nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from bettermemory import verify
from bettermemory.claims import COMMIT_MARK as M
from bettermemory.claims import Claim
from bettermemory.origin import MAX_PATCH_STREAM_COMMITS
from bettermemory.origin_history import ReachableWalk
from bettermemory.origin_patch import PatchWindow
from bettermemory.verify_layout import LiteralFloor
from bettermemory.verify_spelling import Spelled

from ._common import jsonl

A, B, C, D, E = ("a" * 40, "b" * 40, "c" * 40, "d" * 40, "e" * 40)
CWD = "/w"
_UTC = timezone.utc
T0 = datetime(2026, 1, 1, 10, 0, 0, tzinfo=_UTC)


def _lines(*lines: str) -> str:
    return "\n".join(lines) + "\n"


def _diff(path: str, *body: str) -> list[str]:
    return [f"diff --git a/{path} b/{path}", f"--- a/{path}", f"+++ b/{path}", *body]


LIT = _lines(
    M + A,
    *_diff(
        "m.py",
        "@@ -1 +1 @@",
        "-TIMEOUT = 30",
        "+TIMEOUT = 45",
        "@@ -9,0 +10 @@",
        "+def handler(e):",
    ),
)
PATH = _lines(
    M + B, *_diff("p.txt", "@@ -1 +1 @@", "-old words here", "+new words here")
)
HDR = _lines(
    M + C,
    "diff --git a/dir/new.py b/dir/new.py",
    "new file mode 100644",
    "index 0000000..e69de29",
)
GONE = _lines(
    M + C,
    "diff --git a/m.py b/m.py",
    "deleted file mode 100644",
    "index e69de29..0000000",
)
MERGE = _lines(M + D)
BAD = "this is not a diff at all\n"
MISMATCH = _lines(
    M + A, *_diff("m.py", "@@ -1,3 +1,3 @@", "-TIMEOUT = 30", "+TIMEOUT = 45")
)
ALL = LIT + PATH + HDR + MERGE

L30 = ["literal", "m.py", "TIMEOUT", "30"]
L99 = ["literal", "m.py", "OTHER", "99"]
SYM = ["symbol", "m.py", "handler", ""]
QUIET = ["symbol", "m.py", "quiet", ""]
P = ["path", "p.txt", "", ""]
PUN = ["path", "q.txt", "", ""]
ABS = ["absent", "dir", "", ""]
ABS2 = ["absent", "gone", "", ""]
CONT = ["literal", "c.py", "X", "[1, 2]"]
CTRL = ["path", "a\nb", "", ""]
GATE = ["path", "../out", "", ""]
ROOTED = ["symbol", "/etc/x.py", "f", ""]

WALK = [
    A,
    [A, B, C, D, E],
    [
        ["dir/new.py", [C]],
        ["m.py", [A, C]],
        ["p.txt", [B]],
        ["u.py", [D]],
        ["z/old", [E]],
    ],
    True,
]


def _pairs(*shas: str, before: tuple[str, ...] = ()) -> list[list[str]]:
    out = [[(T0 - timedelta(days=1)).isoformat(), s] for s in before]
    return out + [
        [(T0 + timedelta(hours=i + 1)).isoformat(), s] for i, s in enumerate(shas)
    ]


@contextmanager
def _scripted(s: dict[str, Any], calls: list[list[Any]]) -> Iterator[None]:
    pairs = list(s.get("pairs", []))

    def rp(cwd: Any, paths: list[str], *, toplevel: Any = None) -> Any:
        calls.append(["rp", _top(toplevel), list(paths)])
        return (
            list(s["rp"])
            if "rp" in s and s["rp"] is not None
            else (None if "rp" in s else list(paths))
        )

    def sha_pairs(cwd: Any, specs: list[str], *, toplevel: Any = None) -> Any:
        calls.append(["pairs", _top(toplevel), list(specs)])
        got = pairs.pop(0)
        return (
            None
            if got is None
            else [(datetime.fromisoformat(t), sha) for t, sha in got]
        )

    def hist(cwd: Any, specs: list[str], *, toplevel: Any = None) -> Any:
        calls.append(["hist", _top(toplevel), list(specs)])
        return s.get("hist")

    def big(cwd: Any, shas: Any, specs: list[str], *, toplevel: Any = None) -> bool:
        calls.append(["big", _top(toplevel), list(shas), list(specs)])
        return bool(s.get("big", False))

    def stream(cwd: Any, shas: Any, specs: list[str], *, toplevel: Any = None) -> Any:
        calls.append(["stream", _top(toplevel), list(shas), list(specs)])
        return s.get("stream")

    def split(cwd: Any, shas: Any, specs: list[str], *, toplevel: Any = None) -> Any:
        calls.append(["split", _top(toplevel), list(shas), list(specs)])
        w = s.get("split")
        if w is None:
            return None
        return PatchWindow(w[0], frozenset(w[1]), frozenset(w[2]), frozenset(w[3]))

    def floor(
        cwd: Any,
        shas: Any,
        specs: Any,
        pathspecs: Any,
        claims: Any,
        spell: Any,
        *,
        toplevel: Any,
    ) -> Any:
        calls.append(
            ["floor", _top(toplevel), list(shas), list(specs), list(pathspecs)]
        )
        if "floor" not in s:
            return LiteralFloor()
        f = s["floor"]
        return None if f is None else LiteralFloor(*(frozenset(x) for x in f))

    def spelled(
        cwd: Any, specs: Any, *, toplevel: Any, anchor: Any, since: Any = None
    ) -> Spelled:
        stamp = None if since is None else since.isoformat()
        calls.append(["spelled", _top(toplevel), list(specs), anchor, stamp])
        chosen = {k: tuple(v) for k, v in s.get("chosen", {}).items()}
        return Spelled(chosen, bool(s.get("declined", False)))

    def count(**kwargs: Any) -> Any:
        calls.append(["count", _top(kwargs["toplevel"])])
        return s.get("count")

    def literal(names: Any) -> list[str]:
        return [":(literal)" + n for n in names]

    patches: dict[str, Callable[..., Any]] = {
        "resolve_repo_pathspecs": rp,
        "commit_author_sha_pairs_touching_pathspecs": sha_pairs,
        "paths_in_history": hist,
        "too_large": big,
        "commit_patch_stream": stream,
        "local_patch_window": split,
        "literal_floor": floor,
        "spelled": spelled,
        "resolve_commit_drift_count": count,
        "_literal_pathspecs": literal,
    }
    saved = {name: getattr(verify, name) for name in patches}
    try:
        for name, fn in patches.items():
            setattr(verify, name, fn)
        yield
    finally:
        for name, fn in saved.items():
            setattr(verify, name, fn)


def _top(toplevel: Any) -> Any:
    return None if toplevel is None else Path(toplevel).as_posix()


def _claims(raw: list[list[str]]) -> list[Claim]:
    return [Claim(*c) for c in raw]


def _err(e: Exception) -> str:
    return type(e).__name__


def _rcd(
    c: list[list[str]],
    s: dict[str, Any],
    *,
    a: tuple[str, ...] = (),
    w: Any = None,
    since: datetime = T0,
    u: int = 7,
    top: str | None = None,
) -> dict[str, Any]:
    calls: list[list[Any]] = []
    walk = None
    if w is not None:
        walk = ReachableWalk(
            w[0], "f" * 40, tuple(w[1]), {p: frozenset(x) for p, x in w[2]}, w[3]
        )
    with _scripted(s, calls):
        try:
            r = verify.resolve_commit_drift(
                cwd=Path(CWD),
                since=since,
                unfiltered=u,
                anchors=list(a),
                claims=_claims(c),
                toplevel=None if top is None else Path(top),
                walk=walk,
            )
            o: Any = (
                None
                if r is None
                else [r.count, r.claims_checked, list(r.claims_drifted), r.basis]
            )
        except Exception as e:  # the reference's raise is the answer
            o = _err(e)
    return {
        "k": "rcd",
        "c": c,
        "s": s,
        "a": list(a),
        "w": w,
        "since": since.isoformat(),
        "u": u,
        "top": top,
        "o": o,
        "calls": calls,
    }


def _weak(
    c: list[list[str]],
    s: dict[str, Any],
    shas: list[str],
    specs: list[str],
    sp: bool = True,
) -> dict[str, Any]:
    calls: list[list[Any]] = []
    spell = (
        Spelled({k: tuple(v) for k, v in s.get("chosen", {}).items()}) if sp else None
    )
    with _scripted(s, calls):
        drifted, implicated = verify._weak_tier_evaluation(
            Path(CWD), shas, specs, _claims(c), None, spell
        )
    o = [drifted, None if implicated is None else sorted(implicated)]
    return {
        "k": "weak",
        "c": c,
        "s": s,
        "shas": shas,
        "specs": specs,
        "sp": sp,
        "o": o,
        "calls": calls,
    }


def _legs(c: list[list[str]], s: dict[str, Any], a: tuple[str, ...]) -> dict[str, Any]:
    calls: list[list[Any]] = []
    with _scripted(s, calls):
        r = verify._claim_legs(Path(CWD), list(a), _claims(c), None)
    o = None if r is None else [list(r[0]), list(r[1])]
    return {"k": "legs", "c": c, "s": s, "a": list(a), "o": o, "calls": calls}


def _cases() -> list[dict[str, Any]]:
    many = _many(MAX_PATCH_STREAM_COMMITS + 1)
    out = [
        # The legs.
        _legs([], {}, ()),
        _legs([L30, P, CONT], {}, ("u.py", "m.py", "c.py", "u.py")),
        _legs([L30], {"rp": None}, ("u.py",)),
        _legs([CONT, ["path", "c.py", "", ""]], {"rp": ["c.py", "./c.py"]}, ("x",)),
        _legs([SYM, ["literal", "M.py", "X", "1"]], {"rp": []}, ("gone",)),
        # The weak tier directly.
        _weak([L30], {"stream": LIT}, many, ["m.py"]),
        _weak([L30], {"stream": LIT}, many[:-1], ["m.py"]),
        _weak([L30, QUIET], {"stream": LIT}, [A], ["m.py"], sp=False),
        _weak([L30], {"big": True, "stream": LIT}, [A], ["m.py"]),
        _weak([L30], {"stream": ""}, [A], ["m.py"]),
        _weak([L30], {"stream": " \n\t\n"}, [A], ["m.py"]),
        _weak([L30], {"stream": None}, [A], ["m.py"]),
        _weak(
            [L30],
            {"stream": None, "split": ["", [A], ["m.py"], ["m.py"]]},
            [A, B],
            ["m.py"],
        ),
        _weak(
            [L30, P],
            {"stream": None, "split": [PATH, [A], ["m.py"], []]},
            [A, B],
            ["m.py", "p.txt"],
        ),
        _weak([P], {"stream": None, "split": [PATH, [A], [], []]}, [A, B], ["p.txt"]),
        _weak(
            [PUN],
            {"stream": None, "split": ["", [A], ["q.txt"], []]},
            [A, B],
            ["q.txt"],
        ),
        _weak([L30], {"stream": MERGE}, [D], ["m.py"]),
        _weak([L30], {"stream": BAD}, [A], ["m.py"]),
        _weak([L30], {"stream": MISMATCH}, [A], ["m.py"]),
        _weak([L30, SYM, QUIET, L99], {"stream": LIT}, [A, B], ["m.py"]),
        _weak([L30], {"stream": LIT + MERGE}, [A, D, E], ["m.py"]),
        _weak([L99], {"stream": LIT + MERGE}, [A, D], ["m.py"]),
        _weak([L30], {"stream": LIT, "floor": None}, [A], ["m.py"]),
        _weak([L30], {"stream": LIT, "floor": [["m.py"], [E], []]}, [A, E], ["m.py"]),
        _weak(
            [L30], {"stream": LIT, "floor": [["m.py"], [E], ["m.py"]]}, [A, E], ["m.py"]
        ),
        _weak(
            [L99],
            {"stream": LIT, "floor": [["x.py"], [E, "f" * 40], []]},
            [A, E],
            ["m.py"],
        ),
        _weak([P, PUN], {"stream": PATH}, [B], ["p.txt", "q.txt"]),
        _weak([ABS], {"stream": HDR}, [C], ["dir"]),
        _weak([ABS, ABS2], {"stream": HDR + PATH}, [B, C], ["dir", "gone"]),
        _weak([["absent", "dir/", "", ""]], {"stream": HDR}, [C], ["dir/"]),
        _weak([["absent", "dir/new.py", "", ""]], {"stream": HDR}, [C], ["dir/new.py"]),
        _weak([P], {"stream": GONE}, [C], ["m.py"]),
        _weak([["path", "m.py", "", ""], SYM], {"stream": GONE}, [C], ["m.py"]),
        _weak([L30], {"stream": LIT}, [B], ["m.py"]),
        # Spellings: a twin held in the stream, the claim's own not.
        _weak(
            [["literal", "M.py", "TIMEOUT", "30"]],
            {"stream": LIT, "chosen": {"M.py": ["M.py", "m.py"]}},
            [A],
            ["M.py"],
        ),
        _weak(
            [["literal", "M.py", "TIMEOUT", "30"]],
            {"stream": LIT, "chosen": {"M.py": ["M.py", "n.py"]}},
            [A],
            ["M.py"],
        ),
        _weak(
            [["absent", "D", "", ""]],
            {"stream": HDR, "chosen": {"D": ["D", "dir"]}},
            [C],
            ["D"],
        ),
        _weak([], {"stream": LIT}, [A], ["m.py"]),
        # resolve_commit_drift on the reachable basis.
        _rcd([L30], {"stream": LIT}, w=WALK, a=("u.py",)),
        _rcd([L99], {"stream": LIT}, w=WALK),
        _rcd([L99], {"stream": LIT}, w=[A, [A], [["m.py", [A]]], True]),
        _rcd([], {"hist": True}, w=[A, [B], [["p.txt", [B]]], True], a=("m.py",)),
        _rcd([], {"hist": False}, w=[A, [B], [["p.txt", [B]]], True], a=("m.py",)),
        _rcd([], {"hist": None}, w=[A, [B], [["p.txt", [B]]], True], a=("m.py",)),
        _rcd([PUN], {"hist": True}, w=WALK),
        _rcd([L30], {"rp": None}, w=WALK, a=("u.py",)),
        _rcd([], {"rp": []}, w=WALK, a=("../x",)),
        _rcd([L30], {}, w=[A, [A, B], [], False]),
        _rcd([L30], {"declined": True}, w=WALK),
        _rcd([L30], {"stream": None}, w=WALK, top="/w"),
        _rcd([CONT], {"hist": True}, w=WALK),
        _rcd([CONT], {}, w=[A, [A], [["c.py", [A]]], True]),
        _rcd([CTRL, L30], {}, w=WALK),
        _rcd([GATE], {"hist": False}, w=WALK),
        _rcd([ROOTED, P], {"stream": PATH}, w=WALK),
        _rcd([ABS], {"stream": HDR}, w=WALK, a=("z",)),
        _rcd([L30], {"stream": LIT, "chosen": {"m.py": ["m.py", "M.py"]}}, w=WALK),
        # resolve_commit_drift on author dates.
        _rcd([], {"count": 3}),
        _rcd([], {"count": None}, a=("m.py",)),
        _rcd([L30], {"pairs": [_pairs(A, before=(B,))], "stream": LIT}),
        _rcd([L99], {"pairs": [_pairs(A)], "stream": LIT}),
        _rcd(
            [L30],
            {"pairs": [_pairs(D, before=(E,)), _pairs(A)], "stream": LIT},
            a=("u.py",),
        ),
        _rcd([L30], {"pairs": [[], []]}, a=("u.py",)),
        _rcd([L30], {"pairs": [[], _pairs(before=(A,))]}, a=("u.py",)),
        _rcd([L30], {"pairs": [None]}, a=("u.py",)),
        _rcd([L30], {"pairs": [_pairs(D), None]}, a=("u.py",)),
        _rcd([L30], {"pairs": [_pairs(A)], "stream": None}),
        _rcd([L30], {"pairs": [_pairs(A)], "big": True}),
        _rcd([L30], {"rp": None}, a=("u.py",)),
        _rcd([L30], {"declined": True}),
        _rcd([CONT], {"pairs": [_pairs(A)]}),
        _rcd([CONT], {"pairs": [[]]}),
        _rcd(
            [L30], {"pairs": [_pairs(A)], "stream": LIT}, since=T0.replace(tzinfo=None)
        ),
        _rcd(
            [L30],
            {"pairs": [[[T0.replace(tzinfo=None).isoformat(), A]]], "stream": LIT},
        ),
        _rcd(
            [L30],
            {
                "pairs": [
                    [
                        [T0.isoformat(), A],
                        [(T0 + timedelta(microseconds=1)).isoformat(), B],
                    ]
                ],
                "stream": LIT,
            },
        ),
        _rcd(
            [L30],
            {
                "pairs": [
                    [[T0.astimezone(timezone(timedelta(hours=2))).isoformat(), A]]
                ],
                "stream": LIT,
            },
        ),
        _rcd([CTRL], {}, u=11),
        _rcd([GATE, ROOTED], {"count": 5}),
        _rcd([L30, P, ABS], {"pairs": [_pairs(A, B, C)], "stream": ALL}, top="/w"),
        _rcd(
            [L30, P],
            {
                "pairs": [_pairs(A, B)],
                "stream": None,
                "split": [LIT, [B], ["p.txt"], []],
            },
        ),
    ]
    return out


def _many(n: int) -> list[str]:
    return [f"{i:040x}" for i in range(n)]


_NAMED = {
    "LIT": LIT,
    "PATH": PATH,
    "HDR": HDR,
    "GONE": GONE,
    "MERGE": MERGE,
    "BAD": BAD,
    "MISMATCH": MISMATCH,
    "ALL": ALL,
    "LITMERGE": LIT + MERGE,
    "HDRPATH": HDR + PATH,
}


def _squeeze(value: Any) -> Any:
    """The row spelled compactly: a list of the first n `_many` hashes, n
    over 8, as ``{"seq": n}``; a hash of one repeated letter as ``"#"``
    and the letter; a named stream as ``"$"`` and its name; the walk
    `WALK` as ``"WALK"``."""
    if isinstance(value, dict):
        return {k: _squeeze(v) for k, v in value.items()}
    if isinstance(value, list):
        if value == WALK:
            return "WALK"
        if len(value) > 8 and value == _many(len(value)):
            return {"seq": len(value)}
        return [_squeeze(v) for v in value]
    if isinstance(value, str):
        if len(value) == 40 and len(set(value)) == 1 and value[0] in "abcdef":
            return "#" + value[0]
        for name, text in _NAMED.items():
            if value == text:
                return "$" + name
    return value


def generate() -> dict[str, bytes]:
    """``verify_weak.jsonl``: not gated."""
    cases = [_squeeze(c) for c in _cases()]
    header = {
        "k": "header",
        "count": len(cases),
        "max": MAX_PATCH_STREAM_COMMITS,
        "streams": _NAMED,
        "walk": WALK,
    }
    return {"verify_weak.jsonl": jsonl([header, *cases])}

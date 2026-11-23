"""Fixtures: `verify`'s claim anchors and commit-drift count
(``verify_count.jsonl``, POSIX only: the anchors are deduplicated on
`_normalize_for_compare`, the host's `Path`, and the scan's Windows
flavour is not ported).

The native build reads it in
``native/crates/bettermemory-core/src/verify/anchors.rs`` and
``src/verify/count.rs``. This pins `commit_drift_anchor_paths`,
`_anchor_paths`, `_attested_anchor`, `resolve_commit_drift_count`,
`_quiescent_drift_applicable`, `_anchor_specs`, `applicable_unread`,
`compute_commit_drift` and `_narrow_commit_drift`.

No git runs. The reads `gen_verify_weak` scripts are scripted as it
scripts them (its `_scripted`), `resolve_commit_drift_count` excepted,
which runs as the reference's own; beside them
`commit_author_timestamps_touching_pathspecs` (script ``ts``: null or
isoformats), `repo_toplevel` (``rt``), `project_commit_anchors`
(``unmapped``), and `verify_basis`' `repo_toplevel_and_head` (``th``:
null or ``[toplevel, head]``), `commits_since_anchor` (``walk``: null or
a walk as `gen_verify_weak` spells one), `unwalked` and `given_up_since`
(``gu``: null or why a walk was given up) and `commit_author_timestamps`
(``dates``: null, an unread value, or isoformats); ``deadline`` makes
that last read raise `ReadDeadline`. Every patch is restored before the
case returns, and the body parse memo is left as it was found.

One JSON object per line, keys sorted, ASCII. The first line is the
header ``{"k": "header", "count": n, "walk": WALK}``. Every other line
has ``k``:

- ``"anchors"``: ``body``, ``vp`` the attestations, ``kind``
  (``literal``, ``reference`` or ``plain``); ``o`` the anchors.
- ``"count"``: as `gen_verify_weak`'s ``"rcd"`` row, ``o`` the count,
  null or the exception's class name.
- ``"quiet"``, ``"specs"`` and ``"unread"`` (with ``why``): ``a``, ``c``,
  ``top``; ``o`` the bool, the specs or null, or the unread value or null.
- ``"ccd"``: ``lva`` null or an isoformat, ``vp``, ``body``, ``claims``
  (stored claim strings), ``vh`` the verified head, ``root`` what the
  anchor's `commit_root` answers (absent: no anchor); ``o`` null,
  ``["unread", value]`` or ``[status, count, recommendation, checked,
  drifted, basis]``.

Every row has the script ``s`` and the calls ``calls``; a call of the
extra reads is ``["ts", toplevel, pathspecs]``, ``["rt"]``, ``["th"]``,
``["walk", anchor, toplevel, head]`` or ``["dates"]``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

from bettermemory import git_read_budget, verify, verify_basis, verify_candidates
from bettermemory.attestation_paths import LiteralPaths, ReferencePaths
from bettermemory.origin_history import HistoryUnread, ReachableWalk

from ._common import jsonl
from .gen_verify_weak import (
    LIT,
    T0,
    WALK,
    A,
    B,
    D,
    _claims,
    _scripted,
    _top,
)

CWD = "/w"
L30 = ["literal", "m.py", "TIMEOUT", "30"]
P = ["path", "p.txt", "", ""]
#: The home `~` expands to while the cases are made.
HOME = "/hm/u"

CTRL = ["path", "a\nb", "", ""]


def _walk(w: Any) -> ReachableWalk | None:
    if w is None:
        return None
    return ReachableWalk(
        w[0], "f" * 40, tuple(w[1]), {p: frozenset(x) for p, x in w[2]}, w[3]
    )


def _iso(xs: list[str]) -> list[datetime]:
    return [datetime.fromisoformat(t) for t in xs]


class _Projected:
    def __init__(self, paths: tuple[str, ...], unmapped: bool) -> None:
        self.paths = paths
        self.unmapped = unmapped


class _Anchor:
    def __init__(self, root: str | None) -> None:
        self.root = root

    def commit_root(self, commit: str | None) -> tuple[str | None, str]:
        return self.root, "home_checkout"


@contextmanager
def _patched(module: Any, patches: dict[str, Any]) -> Iterator[None]:
    saved = {name: getattr(module, name) for name in patches}
    try:
        for name, fn in patches.items():
            setattr(module, name, fn)
        yield
    finally:
        for name, fn in saved.items():
            setattr(module, name, fn)


@contextmanager
def _script(s: dict[str, Any], calls: list[list[Any]]) -> Iterator[None]:
    def ts(cwd: Any, specs: list[str], *, toplevel: Any = None) -> Any:
        calls.append(["ts", str(cwd), _top(toplevel), list(specs)])
        got = s.get("ts")
        return None if got is None else _iso(got)

    def rt(cwd: Any) -> Any:
        calls.append(["rt", str(cwd)])
        got = s.get("rt")
        return None if got is None else Path(got)

    def project(paths: tuple[str, ...], anchor: Any) -> _Projected:
        return _Projected(tuple(paths), bool(s.get("unmapped", False)))

    def th(cwd: Any) -> Any:
        calls.append(["th", str(cwd)])
        got = s.get("th")
        return None if got is None else (Path(got[0]), got[1])

    def walk(cwd: Any, anchor: str, *, toplevel: Any, head: str) -> Any:
        calls.append(["walk", str(cwd), anchor, _top(toplevel), head])
        return _walk(s.get("walk"))

    def given_up_since(before: int) -> Any:
        got = s.get("gu")
        return None if got is None else HistoryUnread(got)

    def dates(cwd: Any) -> Any:
        calls.append(["dates", str(cwd)])
        if s.get("deadline"):
            raise git_read_budget.ReadDeadline()
        got = s.get("dates")
        if got is None:
            return None
        if isinstance(got, str):
            return HistoryUnread(got)
        return _iso(got)

    real_count = verify.resolve_commit_drift_count
    with ExitStack() as stack:
        stack.enter_context(_scripted(s, calls))
        stack.enter_context(
            _patched(
                verify,
                {
                    "resolve_commit_drift_count": real_count,
                    "commit_author_timestamps_touching_pathspecs": ts,
                    "repo_toplevel": rt,
                    "project_commit_anchors": project,
                },
            )
        )
        stack.enter_context(
            _patched(
                verify_basis,
                {
                    "repo_toplevel_and_head": th,
                    "commits_since_anchor": walk,
                    "unwalked": lambda: 0,
                    "given_up_since": given_up_since,
                    "commit_author_timestamps": dates,
                },
            )
        )
        yield


def _err(e: Exception) -> str:
    return type(e).__name__


def _paths(vp: list[str], kind: str) -> Any:
    if kind == "literal":
        return LiteralPaths(vp)
    if kind == "reference":
        return ReferencePaths(vp)
    return tuple(vp)


def _anchors(
    body: str, vp: tuple[str, ...] | list[str] = (), kind: str = "plain"
) -> dict[str, Any]:
    memo = list(verify_candidates._ANCHORS.items())
    try:
        o: Any = list(verify.commit_drift_anchor_paths(body, _paths(list(vp), kind)))
    except Exception as e:  # the reference's raise is the answer
        o = _err(e)
    finally:
        verify_candidates._ANCHORS.clear()
        verify_candidates._ANCHORS.update(memo)
    return {"k": "anchors", "body": body, "vp": list(vp), "kind": kind, "o": o}


def _count(
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
    with _script(s, calls):
        try:
            o: Any = verify.resolve_commit_drift_count(
                cwd=Path(CWD),
                since=since,
                unfiltered=u,
                anchors=list(a),
                claims=_claims(c),
                toplevel=None if top is None else Path(top),
                walk=_walk(w),
            )
        except Exception as e:  # the reference's raise is the answer
            o = _err(e)
    return {
        "k": "count",
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


def _leaf(
    k: str,
    c: list[list[str]],
    s: dict[str, Any],
    a: tuple[str, ...] = (),
    top: str | None = None,
    why: str = "timeout",
) -> dict[str, Any]:
    calls: list[list[Any]] = []
    t = None if top is None else Path(top)
    with _script(s, calls):
        if k == "quiet":
            o: Any = verify._quiescent_drift_applicable(
                Path(CWD), list(a), _claims(c), toplevel=t
            )
        elif k == "specs":
            o = verify._anchor_specs(Path(CWD), list(a), _claims(c), toplevel=t)
        else:
            got = verify.applicable_unread(
                HistoryUnread(why), Path(CWD), list(a), _claims(c), toplevel=t
            )
            o = None if got is None else got.value
    row = {"k": k, "c": c, "s": s, "a": list(a), "top": top, "o": o, "calls": calls}
    if k == "unread":
        row["why"] = why
    return row


class _Calls(list[list[Any]]):
    """Raise the shared deadline at the scripted Git read."""

    def __init__(self, deadline_at: int | None) -> None:
        super().__init__()
        self.deadline_at = deadline_at

    def append(self, call: list[Any]) -> None:
        super().append(call)
        if len(self) == self.deadline_at:
            raise git_read_budget.ReadDeadline()


def _ccd(
    s: dict[str, Any],
    *,
    lva: datetime | None = T0,
    vp: tuple[str, ...] | list[str] = (),
    body: str = "",
    claims: tuple[str, ...] | list[str] = (),
    vh: str | None = None,
    root: Any = CWD,
    anchor: bool = True,
) -> dict[str, Any]:
    calls = _Calls(s.get("deadline_at"))
    memo = list(verify_candidates._ANCHORS.items())
    with _script(s, calls):
        try:
            got = verify.compute_commit_drift(
                lva,
                None,
                caller_origin=None,
                verified_paths=list(vp),
                body=body,
                claims=list(claims),
                verified_head=vh,
                anchor=_Anchor(root) if anchor else None,  # type: ignore[arg-type]
            )
            if got is None:
                o: Any = None
            elif isinstance(got, HistoryUnread):
                o = ["unread", got.value]
            else:
                o = [
                    got.status,
                    got.commits_since_verify,
                    got.recommendation,
                    got.claims_checked,
                    list(got.claims_drifted),
                    got.basis,
                ]
        except Exception as e:  # the reference's raise is the answer
            o = _err(e)
        finally:
            verify_candidates._ANCHORS.clear()
            verify_candidates._ANCHORS.update(memo)
    row: dict[str, Any] = {
        "k": "ccd",
        "s": s,
        "lva": None if lva is None else lva.isoformat(),
        "vp": list(vp),
        "body": body,
        "claims": list(claims),
        "vh": vh,
        "o": o,
        "calls": calls,
    }
    if anchor:
        row["root"] = root
    return row


def _ts(*hours: int) -> list[str]:
    return [(T0 + timedelta(hours=h)).isoformat() for h in hours]


MANY = " ".join(f"see src/f{i}.py:{i}" for i in range(30))
BODY = (
    "Cites `/etc/app.conf`, ~/proj/notes.md and src/x.py:12, docs/y.md, "
    "CHANGELOG.md, ../out.py, src/x.py again, a/b.c.d.py, v1.2.3 and "
    "https://example.com/z.html"
)
UNI = "Grüße: siehe src/ä.py und lib/mü.rs, dann src/ok.py — fin"
WALKED = [A, [A, B], [["m.py", [A]], ["p.txt", [B]]], True]


def _cases() -> list[dict[str, Any]]:
    stale = T0 - timedelta(days=2)
    return [
        # The anchors.
        _anchors(""),
        _anchors("", ["", "src/a.py", "$HOME/x/y.py", "../up.py"], "reference"),
        _anchors("", ["", "src/a.py", "$HOME/x/y.py", "../up.py"], "literal"),
        # A body path under the pinned home meets its `~` form in the
        # verified paths.
        _anchors("see /hm/u/x/y.py here", ["~/x/y.py"], "literal"),
        _anchors("", ["src/a.py:12", "  ", "/abs/p.py,"], "plain"),
        _anchors("", ["src/a.py:12", "/abs/p.py,"], "literal"),
        _anchors(BODY, ["src/x.py", "docs/y.md"], "reference"),
        _anchors(MANY),
        _anchors(MANY, [f"src/f{i}.py" for i in range(5)], "reference"),
        _anchors(UNI),
        _anchors("x.py y.pyc .hidden.rs a/.b.go foo.toolongext file.c"),
        _anchors("dir/sub/ dir/f.py/ q/r.py.bak s/t.py.x path\\win.py ~x.py -a.py"),
        _anchors("~/a.py ~/a.py /etc/hosts /etc/hosts"),
        # The count without claims.
        _count([], {}),
        _count([], {"rp": None}, a=("m.py",)),
        _count([], {"rp": []}, a=("../x",)),
        _count([], {"declined": True}, a=("m.py",)),
        _count([], {"ts": None}, a=("m.py",)),
        _count([], {"ts": []}, a=("m.py",)),
        _count([], {"ts": _ts(-2, -1, 0, 1, 2)}, a=("m.py", "p.txt")),
        _count([], {"ts": _ts(0)}, a=("m.py",), since=T0.replace(tzinfo=None)),
        _count(
            [],
            {"ts": [T0.replace(tzinfo=None).isoformat()]},
            a=("m.py",),
            top="/w",
        ),
        _count([], {"ts": _ts(1), "chosen": {"m.py": ["m.py", "M.py"]}}, a=("m.py",)),
        _count([CTRL], {}, u=9),
        _count([CTRL], {}, w=WALK),
        # Unreadable claims over an empty reachable walk count none.
        _count([CTRL], {}, w=["", [], [], True]),
        _count([L30], {"stream": LIT}, w=WALK, a=("u.py",)),
        _count([], {"hist": False}, w=WALKED, a=("q.py",)),
        _count([L30], {"pairs": [[[_ts(1)[0], A]]], "stream": LIT}),
        _count([L30], {"pairs": [[]]}),
        # The leaves.
        _leaf("specs", [], {}, ()),
        _leaf("specs", [L30, P, L30], {}, ("u.py", "m.py")),
        _leaf("specs", [], {"rp": None}, ("u.py",)),
        _leaf("specs", [CTRL, ["path", "../o", "", ""]], {"rp": []}, ("x",)),
        _leaf("quiet", [], {"rt": "/w"}, ("m.py",)),
        _leaf("quiet", [], {"rt": "/w", "hist": True}, ("m.py",)),
        _leaf("quiet", [], {"hist": False}, ("m.py",), top="/t"),
        _leaf("quiet", [], {"rp": None}, ("m.py",)),
        _leaf("quiet", [], {"rp": []}, ("m.py",)),
        _leaf("quiet", [P], {"rp": [], "hist": False}, ("m.py",)),
        _leaf("quiet", [], {"declined": True}, ("m.py",)),
        _leaf("unread", [], {"rt": "/w"}, ("m.py",)),
        _leaf("unread", [], {"rp": []}, ("m.py",), why="shallow"),
        _leaf("unread", [L30], {"rp": []}, ("m.py",), top="/t", why="error"),
        _leaf("unread", [], {"rp": None}, ("m.py",), why="over_budget"),
        _leaf("unread", [], {}, (), why="call_budget"),
        # compute_commit_drift.
        _ccd({}, lva=None, body="src/x.py"),
        _ccd({}, anchor=False, body="src/x.py"),
        _ccd({}, root=None, body="src/x.py"),
        _ccd({}, body="a preference with no path"),
        _ccd({"unmapped": True}, body="src/x.py"),
        _ccd({"dates": None}, body="src/x.py"),
        _ccd({"dates": None}, root="", body="src/x.py"),
        _ccd({"dates": "timeout"}, root="/w/./x//", body="src/x.py"),
        _ccd(
            {"th": ["/w", D], "walk": WALKED},
            root="//w//é/../",
            vh=A,
            body="m.py",
        ),
        _ccd({"dates": "timeout", "rp": []}, body="src/x.py"),
        # An unread returned as data still passes through the applicability check.
        _ccd({"dates": "call_budget", "rp": []}, body="src/x.py"),
        _ccd({"dates": "timeout", "rt": "/w"}, body="src/x.py"),
        _ccd({"dates": "shallow"}, claims=["path:p.txt"]),
        _ccd({"dates": _ts(1, 2), "rt": "/w", "ts": _ts(-1, 2)}, body="src/x.py"),
        _ccd({"dates": _ts(1, 2), "ts": []}, body="src/x.py"),
        _ccd({"dates": _ts(1), "ts": _ts(-1)}, body="src/x.py"),
        _ccd({"dates": _ts(-1), "hist": True}, body="src/x.py"),
        _ccd({"dates": _ts(-1), "hist": False}, body="src/x.py"),
        _ccd({"dates": _ts(-1), "rp": []}, vp=["/elsewhere/a.py"]),
        _ccd({"dates": _ts(-1), "rt": "/w"}, body="src/x.py", lva=stale),
        _ccd({"deadline": True}, body="src/x.py"),
        _ccd({"dates": _ts(-1)}, body="src/x.py", lva=T0.replace(tzinfo=None)),
        _ccd({"dates": [T0.replace(tzinfo=None).isoformat()]}, body="src/x.py"),
        _ccd(
            {"dates": _ts(1), "pairs": [[[_ts(1)[0], A]]], "stream": LIT},
            claims=["literal:m.py:TIMEOUT=30", "not a claim"],
        ),
        _ccd({"dates": _ts(1)}, claims=["path:a\x01b"], body="src/x.py"),
        _ccd(
            {"th": ["/w", D], "walk": WALKED, "stream": LIT},
            vh=A,
            claims=["literal:m.py:TIMEOUT=30"],
        ),
        _ccd({"th": ["/w", D], "walk": WALKED, "hist": True}, vh=A, body="m.py"),
        _ccd(
            {"th": ["/w", D], "walk": [A, [], [], True], "hist": True},
            vh=A,
            body="m.py",
        ),
        _ccd({"th": ["/w", D], "walk": None, "gu": "timeout"}, vh=A, body="m.py"),
        _ccd(
            {"th": ["/w", D], "walk": None, "gu": "error", "rp": []}, vh=A, body="m.py"
        ),
        _ccd(
            {"th": ["/w", D], "walk": None, "dates": _ts(1), "ts": _ts(1)},
            vh=A,
            body="m.py",
        ),
        _ccd({"th": None, "dates": _ts(1), "ts": _ts(1)}, vh=A, body="m.py"),
        _ccd({"dates": _ts(1), "ts": _ts(1)}, body=UNI, vp=["$HOME/p/q.py"]),
        _ccd({"dates": _ts(1), "ts": _ts(1), "rp": ["src/x.py"]}, body="src/x.py"),
    ]


def generate() -> dict[str, bytes]:
    """``verify_count.jsonl``: POSIX only."""
    # The compare form expands `~` through HOME; the Rust test fixes the
    # same home, so every runner writes the same bytes.
    with patch.dict(os.environ, {"HOME": HOME}):
        cases = _cases()
        # The scripted reads return pathlib.Path, as the origin functions do.
        cases.extend(
            (
                _ccd({"rt": "/w/./s/..", "dates": "timeout"}, body="m.py"),
                _ccd({"th": ["//w/./sub//", D], "walk": WALKED}, vh=A, body="m.py"),
            )
        )
        # Each offset is a Git read that can raise before the next one.
        scenarios: list[tuple[dict[str, Any], dict[str, Any]]] = [
            ({"th": None, "dates": "timeout"}, {"vh": A, "body": "m.py"}),
            (
                {"th": ["/w", D], "walk": None, "dates": _ts(1)},
                {"vh": A, "body": "m.py"},
            ),
            ({"dates": _ts(-1), "hist": True}, {"body": "m.py"}),
            ({"dates": _ts(1), "ts": _ts(1)}, {"body": "m.py"}),
            (
                {"dates": _ts(1), "pairs": [[[_ts(1)[0], A]]], "stream": LIT},
                {"claims": ["literal:m.py:TIMEOUT=30"]},
            ),
            (
                {"th": ["/w", D], "walk": WALKED, "hist": True},
                {"vh": A, "body": "m.py"},
            ),
            (
                {
                    "dates": _ts(1),
                    "pairs": [[[_ts(1)[0], A]]],
                    "stream": None,
                    "split": [LIT, [A], ["m.py"], []],
                },
                {"claims": ["literal:m.py:TIMEOUT=30"]},
            ),
        ]
        for script, record in scenarios:
            for offset in range(1, len(_ccd(script, **record)["calls"]) + 1):
                cases.append(_ccd({**script, "deadline_at": offset}, **record))
    header = {"k": "header", "count": len(cases), "walk": WALK}
    return {"verify_count.jsonl": jsonl([header, *cases])}

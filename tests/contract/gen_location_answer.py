"""Fixtures: `location.py`'s client answer and liveness reads
(``location_answer.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/location_answer.rs``. This pins
`answer`, `path_liveness`, `path_liveness_many` (and through both
`_liveness_read`), `Exchange.to_reply` and `_history_of`.

The effects are scripted. `RepositoryHistory` is a stub built from the
row's ``h`` (``objects`` it holds, ``commits`` as {sha: [fingerprint,
parents]}, ``reach`` as [relations, bounded], ``blobs`` as {sha: ids},
``independent``) that records every call; `checkout_token.remint` a stub
answering ``w`` that records its arguments; `view_state` a stub that
answers by path from ``vs``: ``echo`` (the default) answers the JSON of
``[[path, keyword arguments]]``, so the arguments a view is read with are
pinned, ``slow`` sleeps past `PROBE_BUDGET`, ``raise:<class>`` raises,
anything else is the state answered. No real path is read, so no row
follows the platform.

Rows, by ``f``:

- ``answer``: ``q`` and ``a`` (JSON text), the capture ``cap``, ``h``,
  ``vs``, ``w``; ``o`` is ``{"r": json.dumps(out)}`` or ``{"raise":
  class}``, ``calls`` the history's and the re-mint's calls in order.
- ``liveness``: the checkout ``k``, the view's path ``v``, ``vs``; ``o``
  is ``{"r": state}`` or ``{"raise": class}``.
- ``many``: ``pairs`` as [checkout, path], ``vs``; ``o`` the list.
- ``exchange``: ``q`` (JSON text); ``o`` ``{"r": text}`` or
  ``{"raise": class}``.
- ``history_of``: ``cap``; ``o`` the common directory the history is
  built from, or null.
"""

from __future__ import annotations

import json
import time
from typing import Any
from unittest import mock

from bettermemory import checkout_token, location
from bettermemory.location_git import Capture, Reach
from bettermemory.membership_records import Checkout, InitStamp, ObjectKey, View

from ._common import jsonl

H1 = "c1"
H2 = "c2"
H3 = "c3"
F40 = "a" * 40
H4 = "4" * 64
B1 = "b1"

_ERRORS: dict[str, type[Exception]] = {"OSError": OSError, "ValueError": ValueError}


class _History:
    def __init__(self, script: dict[str, Any], calls: list[Any]) -> None:
        self.s = script
        self.calls = calls

    def holds_objects(self, ids: Any) -> set[str]:
        self.calls.append(["holds_objects", list(ids)])
        return set(ids) & set(self.s.get("objects", []))

    def fingerprint(self, sha: str) -> str | None:
        self.calls.append(["fingerprint", sha])
        got = self.s.get("commits", {}).get(sha)
        return None if got is None else got[0]

    def parents(self, sha: str) -> tuple[str, ...]:
        self.calls.append(["parents", sha])
        got = self.s.get("commits", {}).get(sha)
        return () if got is None else tuple(got[1])

    def reachable_among(self, among: Any) -> Reach:
        self.calls.append(["reachable_among", list(among)])
        rel, bounded = self.s.get("reach", [{}, False])
        return Reach({k: frozenset(v) for k, v in rel.items()}, bounded=bounded)

    def blobs(self, sha: str) -> list[str]:
        self.calls.append(["blobs", sha])
        return list(self.s.get("blobs", {}).get(sha, []))

    def independent(self, of: Any) -> Any:
        self.calls.append(["independent", list(of)])
        return self.s.get("independent")


def _fake_view_state(vs: dict[str, str]) -> Any:
    def fake(
        path: str,
        kind: str,
        key: Any,
        token: Any,
        *,
        last_head: Any = None,
        stamps: Any = (),
        common_key: Any = None,
        superseded: bool = False,
    ) -> str:
        what = vs.get(path, "echo")
        if what == "slow":
            time.sleep(1.5)
            return "live"
        if what.startswith("raise:"):
            raise _ERRORS[what[6:]]("x")
        if what != "echo":
            return what
        kw = {
            "kind": kind,
            "key": key,
            "token": token,
            "last_head": last_head,
            "stamps": [list(s) for s in stamps],
            "common_key": common_key,
            "superseded": superseded,
        }
        return json.dumps([[path, kw]])

    return fake


def _capture(c: dict[str, Any]) -> Capture:
    key = c.get("key")
    return Capture(
        cwd="/r",
        state=c.get("state", "repo"),
        kind=c.get("kind"),
        git_dir=c.get("git_dir"),
        common_dir=c.get("common_dir"),
        head=c.get("head"),
        ref_tips=tuple(c.get("ref_tips", ())),
        evicted=c.get("evicted", False),
        object_key=None if key is None else tuple(key),
        token=c.get("token"),
        token_written=c.get("written"),
    )


def _okey(k: Any) -> ObjectKey | None:
    return None if k is None else ObjectKey(k[0], k[1], k[2])


def _checkout(k: dict[str, Any]) -> Checkout:
    return Checkout(
        id="c1",
        project="p1",
        machine="m1",
        kind=k.get("kind", "primary"),
        object_key=_okey(k.get("key")),
        common_object_key=_okey(k.get("common")),
        git_dir="/r/.git",
        token=k.get("token"),
        token_status=k.get("status", "present"),
        stamps=tuple(InitStamp(*s) for s in k.get("stamps", [])),
        last_head=k.get("last_head"),
        branch=None,
        remotes=(),
        manifest=None,
        legacy=False,
        retired=False,
        registered="2026-01-01T00:00:00+00:00",
    )


def _view(path: str) -> View:
    return View(
        checkout="c1", machine="m1", path=path, first_seen="x", last_seen="x", order=0
    )


_CATCH = (ValueError, TypeError, OSError, AttributeError, RecursionError)


def _answer(
    q: str,
    a: str,
    cap: dict[str, Any],
    h: dict[str, Any] | None = None,
    vs: dict[str, str] | None = None,
    w: list[Any] | None = None,
) -> dict[str, Any]:
    h = h or {}
    vs = vs or {}
    w = w or ["T", "written"]
    calls: list[Any] = []

    def factory(common: str) -> _History:
        calls.append(["history", common])
        return _History(h, calls)

    def remint(git_dir: Any, *, policy: bool, bare: bool, replacing: Any) -> Any:
        r = None if replacing is None else list(replacing)
        calls.append(["remint", git_dir, policy, bare, r])
        return type("R", (), {"token": w[0], "status": w[1]})()

    row: dict[str, Any] = {"f": "answer", "q": q, "a": a, "cap": cap}
    row.update({"h": h, "vs": vs, "w": w})
    with (
        mock.patch.object(location, "RepositoryHistory", factory),
        mock.patch.object(location, "view_state", _fake_view_state(vs)),
        mock.patch.object(checkout_token, "remint", remint),
    ):
        try:
            got = location.answer(json.loads(q), json.loads(a), _capture(cap))
            row["o"] = {"r": json.dumps(got)}
        except _CATCH as exc:
            row["o"] = {"raise": type(exc).__name__}
    row["calls"] = calls
    return row


def _liveness(k: dict[str, Any], v: str, vs: dict[str, str]) -> dict[str, Any]:
    row: dict[str, Any] = {"f": "liveness", "k": k, "v": v, "vs": vs}
    with mock.patch.object(location, "view_state", _fake_view_state(vs)):
        try:
            row["o"] = {"r": location.path_liveness(_checkout(k), _view(v))}
        except _CATCH as exc:
            row["o"] = {"raise": type(exc).__name__}
    return row


def _many(pairs: list[Any], vs: dict[str, str]) -> dict[str, Any]:
    with mock.patch.object(location, "view_state", _fake_view_state(vs)):
        got = location.path_liveness_many([(_checkout(k), _view(v)) for k, v in pairs])
    return {"f": "many", "pairs": pairs, "vs": vs, "o": got}


def _exchange(q: str) -> dict[str, Any]:
    try:
        got = location.Exchange(json.loads(q)).to_reply()
        return {"f": "exchange", "q": q, "o": {"r": json.dumps(got)}}
    except _CATCH as exc:
        return {"f": "exchange", "q": q, "o": {"raise": type(exc).__name__}}


def _history_of(cap: dict[str, Any]) -> dict[str, Any]:
    seen: list[str] = []

    def factory(common: str) -> str:
        seen.append(common)
        return common

    with mock.patch.object(location, "RepositoryHistory", factory):
        got = location._history_of(_capture(cap))
    return {"f": "history_of", "cap": cap, "o": got}


REPO: dict[str, Any] = {
    "common_dir": "/r/.git",
    "git_dir": "/r/.git",
    "head": H1,
    "kind": "primary",
}
HIST: dict[str, Any] = {
    "objects": [H1, B1],
    "commits": {H1: ["fp1", [H2]], H2: ["fp2", [H3]], H3: [None, []]},
    "reach": [{H1: [H2, H3], H2: [H3]}, False],
    "blobs": {H1: [B1], H2: []},
    "independent": [H2, H1],
}
KEY = ["7", "42", None]
K1 = {"kind": "primary", "key": KEY, "token": "T1", "last_head": H1}


def _answer_rows() -> list[dict[str, Any]]:
    j = json.dumps
    r: list[dict[str, Any]] = []
    # The plain rounds: a repository with history, none, evicted, outside.
    r.append(_answer("{}", "{}", REPO, HIST))
    r.append(_answer('{"exchange": "e1"}', "{}", REPO, HIST))
    r.append(_answer("{}", "{}", {**REPO, "head": None, "ref_tips": [H2, H3]}, HIST))
    r.append(_answer("{}", "{}", {**REPO, "head": ""}, HIST))
    r.append(_answer("{}", "{}", {**REPO, "evicted": True}, HIST))
    r.append(_answer("{}", "{}", {**REPO, "state": "outside"}, HIST))
    r.append(_answer("{}", "{}", {**REPO, "state": "indeterminate"}, HIST))
    r.append(_answer("{}", "{}", {**REPO, "common_dir": ""}, HIST))
    r.append(_answer("{}", "{}", {**REPO, "common_dir": None}, HIST))
    # Held: asked ids lowered and merged with earlier rounds.
    held_q = j({"held": [H1.upper(), B1, "c9", 5]})
    r.append(_answer(held_q, "{}", REPO, HIST))
    r.append(_answer(held_q, j({"asked": ["z"], "held": ["z"]}), REPO, HIST))
    r.append(_answer(held_q, "{}", {**REPO, "state": "outside"}, HIST))
    r.append(_answer(j({"held": "AB"}), "{}", REPO, HIST))
    r.append(_answer(j({"held": {"X": 1}}), "{}", REPO, HIST))
    r.append(_answer(j({"held": 5}), "{}", REPO, HIST))
    r.append(_answer(j({"held": [None, True, 1.5, "Ä"]}), "{}", REPO, HIST))
    r.append(_answer(j({"held": [[1]]}), "{}", REPO, HIST))
    r.append(_answer("{}", j({"asked": [1, "a"]}), REPO, HIST))
    r.append(_answer("{}", j({"asked": [3, 1, 2, 1.0, True]}), REPO, HIST))
    r.append(_answer("{}", j({"asked": [None]}), REPO, HIST))
    r.append(_answer("{}", j({"asked": [None, None]}), REPO, HIST))
    r.append(_answer("{}", j({"asked": [[1]]}), REPO, HIST))
    r.append(_answer("{}", j({"asked": "ba"}), REPO, HIST))
    r.append(_answer("{}", j({"asked": {"q": 1}, "held": 0}), REPO, HIST))
    r.append(_answer("{}", j({"held": 7}), REPO, HIST))
    r.append(_answer(j({"held": ["x"]}), j({"asked": [2]}), REPO, HIST))
    # Walk: kept, re-asked, commits added.
    prior = j({"walk": {H3: ["old", []], "zz": [None, []]}})
    r.append(_answer("{}", prior, REPO, HIST))
    r.append(_answer(j({"walk": True}), prior, REPO, HIST))
    r.append(_answer(j({"walk": 0}), prior, REPO, HIST))
    r.append(_answer(j({"commits": [H2.upper(), "nope"]}), "{}", REPO, HIST))
    r.append(_answer(j({"commits": [H2], "walk": 1}), prior, REPO, HIST))
    r.append(_answer(j({"commits": [H2]}), prior, REPO, HIST))
    r.append(_answer(j({"walk": True}), j({"walk": None}), REPO, HIST))
    r.append(_answer(j({"walk": True}), j({"walk": [["k", 1], "ab"]}), REPO, HIST))
    r.append(_answer(j({"walk": True}), j({"walk": [["k", 1, 2]]}), REPO, HIST))
    r.append(_answer(j({"walk": True}), j({"walk": [5]}), REPO, HIST))
    r.append(_answer(j({"walk": True}), j({"walk": [[[1], 2]]}), REPO, HIST))
    r.append(_answer(j({"walk": True}), j({"walk": 3}), REPO, HIST))
    r.append(_answer("{}", j({"walk": {}}), {**REPO, "state": "outside"}))
    r.append(_answer(j({"commits": 4}), "{}", REPO, HIST))
    # A walk that loops and one past its limit's start.
    loop = {"commits": {H1: ["a", [H2, H1]], H2: ["b", [H1]]}}
    r.append(_answer("{}", "{}", REPO, loop))
    # Ancestry: with history, without, bounded.
    anc = j({"ancestry": [H1.upper(), H2, H3]})
    r.append(_answer(anc, "{}", REPO, HIST))
    r.append(_answer(anc, "{}", REPO, {**HIST, "reach": [{H1: [H2]}, True]}))
    r.append(_answer(j({"ancestry": []}), "{}", REPO, HIST))
    noh = j({"ancestry": [F40.upper(), H4, H1, "abc", "g" * 40, "Ä" * 40, 7]})
    r.append(_answer(noh, "{}", {**REPO, "state": "outside"}))
    r.append(_answer(noh, "{}", {**REPO, "common_dir": ""}))
    r.append(_answer(j({"ancestry": "ab"}), "{}", {**REPO, "state": "outside"}))
    r.append(_answer(j({"ancestry": 2}), "{}", {**REPO, "state": "outside"}))
    # Blobs: HEAD and the asked ones, earlier rounds kept.
    blob_q = j({"blobs": [H2.upper(), "", None, 0, H3]})
    r.append(_answer(blob_q, j({"blobs": {"old": ["x"], H2: ["y"]}}), REPO, HIST))
    r.append(_answer(blob_q, "{}", {**REPO, "head": None}, HIST))
    r.append(_answer(j({"blobs": 1}), "{}", REPO, HIST))
    # Independent: with history, without, earlier rounds.
    ind = j({"independent": [H2, H1.upper(), H2]})
    r.append(_answer(ind, "{}", REPO, HIST))
    r.append(_answer(ind, "{}", REPO, {**HIST, "independent": None}))
    r.append(_answer(ind, "{}", {**REPO, "state": "outside"}))
    kept = j({"independent": [{"of": [H1, H2], "top": [H1]}, 5, {"of": [H3]}]})
    r.append(_answer(ind, kept, REPO, HIST))
    r.append(_answer("{}", kept, REPO, HIST))
    # Re-mint: asked, already answered, no git directory.
    r.append(_answer(j({"remint": True, "write_token": True}), "{}", REPO, HIST))
    r.append(
        _answer(
            j({"remint": 1}), j({"remint": {"token": None}}), REPO, HIST, w=["X", "e"]
        )
    )
    r.append(_answer(j({"remint": True}), "{}", {**REPO, "git_dir": None}, HIST))
    r.append(_answer(j({"remint": True}), "{}", {**REPO, "kind": "bare"}, HIST))
    r.append(_answer(j({"remint": 0}), "{}", REPO, HIST))
    r.append(_answer(j({"remint": True, "replacing": [5]}), "{}", REPO, HIST))
    # Views: echoed, states, unreachable, raising, merged with earlier rounds.
    views = {
        "views": [
            {"path": "/a", "kind": "linked", "object": KEY, "token": "T1"},
            {"path": "/b", "last_head": F40.upper(), "superseded": True},
            {"path": 5},
            {"path": "/c", "stamps": [["description", 1, 2, 3, 4]]},
            {"path": "/a", "common_object": ["1", "2", "3"]},
        ]
    }
    r.append(_answer(j(views), "{}", REPO, HIST))
    r.append(_answer(j(views), j({"views": {"/z": "gone", "/c": "x"}}), REPO, HIST))
    r.append(_answer(j(views), "{}", REPO, HIST, {"/b": "gone", "/c": "live"}))
    r.append(_answer(j(views), "{}", REPO, HIST, {"/c": "raise:OSError"}))
    r.append(
        _answer(
            j(views),
            "{}",
            REPO,
            HIST,
            {"/b": "raise:ValueError", "/c": "raise:OSError"},
        )
    )
    r.append(
        _answer(
            j({"views": [{"path": "/slow-a"}]}), "{}", REPO, HIST, {"/slow-a": "slow"}
        )
    )
    r.append(
        _answer(j({"views": [{"path": "/v", "object": [1, 2]}]}), "{}", REPO, HIST)
    )
    r.append(_answer(j({"views": 5}), "{}", REPO, HIST))
    r.append(_answer(j({"views": [{"path": "/v"}]}), j({"views": [["k"]]}), REPO, HIST))
    r.append(
        _answer(j({"views": [{"path": "/v"}]}), "{}", {**REPO, "state": "outside"})
    )
    # Key order and values carried through, NaN and nesting kept.
    carried = '{"zeta": NaN, "remint_ok": false, "exchange": "old", "views": {}}'
    r.append(_answer('{"exchange": [1, {"a": 2}]}', carried, REPO, HIST))
    r.append(_answer("{}", '{"walk": {"1": 2}, "asked": [], "held": null}', REPO, HIST))
    # Not mappings.
    r.append(_answer("{}", "[]", REPO, HIST))
    r.append(_answer("[]", "{}", REPO, HIST))
    r.append(_answer('"q"', "{}", REPO, HIST))
    return r


def _rows() -> list[dict[str, Any]]:
    rows = _answer_rows()
    k_full = {
        "kind": "linked",
        "key": ["1", "2", "3"],
        "common": ["4", "5", None],
        "token": "T2",
        "status": "superseded",
        "last_head": F40.upper(),
        "stamps": [["description", 1, 2, 3, 4], ["info/exclude", 5, 6, 7, 8]],
    }
    rows.append(_liveness(K1, "/l1", {}))
    rows.append(_liveness(k_full, "/l2", {}))
    rows.append(_liveness({"kind": "unresolved"}, "/l3", {}))
    rows.append(_liveness(K1, "/l4", {"/l4": "gone"}))
    rows.append(_liveness(K1, "/l5", {"/l5": "raise:OSError"}))
    rows.append(_liveness(K1, "/slow-l", {"/slow-l": "slow"}))
    rows.append(
        _many(
            [[K1, "/m1"], [k_full, "/m2"], [K1, "/m3"], [K1, "/m4"], [K1, "/slow-m"]],
            {"/m2": "replaced", "/m3": "raise:ValueError", "/slow-m": "slow"},
        )
    )
    rows.append(_many([], {}))
    for q in [
        '{"a": 1, "b": [2]}',
        "{}",
        "[]",
        '[["k", 1]]',
        '["ab"]',
        '["abc"]',
        "[5]",
        "null",
        "0",
        '""',
    ]:
        rows.append(_exchange(q))
    caps: list[dict[str, Any]] = [
        REPO,
        {**REPO, "evicted": True},
        {**REPO, "state": "outside"},
        {**REPO, "common_dir": ""},
        {**REPO, "common_dir": None},
    ]
    for cap in caps:
        rows.append(_history_of(cap))
    return rows


def generate() -> dict[str, bytes]:
    return {"location_answer.jsonl": jsonl(_rows())}

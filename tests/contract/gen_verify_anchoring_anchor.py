"""Fixtures: the drift report and the claim anchors
(``verify_anchoring_anchor.jsonl``, not gated).

The native build reads it in
``native/crates/bettermemory-core/src/verify_anchoring/report.rs`` and
``anchor.rs``. This pins `verify_anchoring`'s `PathDriftReport`
(`has_drift`, `has_claim_anchored_drift`, `should_report`, `to_dict`),
the vocabulary constants, `ClaimAnchor` (`convicting`, `declared_root`,
`absolute`, `commit_root`) and `Anchoring` (`prime`, `home`,
`caller_root`, `for_record`, `holds`, `translate`), over a stub
membership model that logs every call it answers.

One JSON object per line, keys sorted, ASCII. The first line is the header
``{"k": "header", "count": n, "constants": {...}}``. Every other line is a
case, ``f`` naming it:

- ``"report"``: ``r`` the report's fields; ``o`` is ``{"has_drift",
  "has_claim", "should", "dict"}``, ``dict`` a list of [key, value] pairs
  in insertion order or ``"ValueError"``;
- ``"anchor"``: a `ClaimAnchor` built from ``a`` (its fields; ``t`` a
  table path -> [target, rule] standing for `translate`, absent for
  None; ``h`` a list of held [root, sha] pairs standing for `holds`, or
  null for None, when `holds_commit` answers from ``hc``), then ``ops``;
  ``o`` lists the answers and ``log`` the calls of the stand-ins;
- ``"anchoring"``: an `Anchoring` over the world ``w`` and caller ``c``,
  driven by ``ops``; ``o`` the answers, ``log`` every call of the model,
  of `holds_commit`, `expanduser`, `inside`, `expected_home` and
  `relative_under`, in order.

The path rules are stand-ins pinned here, not the reference's own:
``inside(p, r)`` is ``p == r or p.startswith(r + "/")``,
``relative_under(p, r)`` the rest below ``r`` (``"."`` at ``r``) or None,
``expected_home(p, roots)`` any ``inside``; ``expanduser`` turns a
leading ``~`` into ``/h/u``. Nothing here depends on the Python
release, the Unicode data or the platform.
"""

from __future__ import annotations

import os.path
from typing import Any
from unittest import mock

from bettermemory import verify_anchoring as va

from ._common import jsonl

Log = list[list[Any]]


def _inside(p: str, r: str) -> bool:
    return p == r or p.startswith(r + "/")


class _Paths:
    def __init__(self, log: Log) -> None:
        self.log = log

    def inside(self, p: str, r: str) -> bool:
        self.log.append(["inside", p, r])
        return _inside(p, r)

    def relative_under(self, p: str, r: str | None) -> str | None:
        self.log.append(["relative_under", p, r])
        if r is None or not _inside(p, r):
            return None
        return p[len(r) + 1 :] or "."

    def expected_home(self, p: str, roots: tuple[str, ...]) -> bool:
        self.log.append(["expected_home", p, list(roots)])
        return any(_inside(p, r) for r in roots)

    def expanduser(self, p: str) -> str:
        self.log.append(["expanduser", p])
        return "/h/u" + p[1:] if p.startswith("~") else p


class _View:
    def __init__(self, v: list[str]) -> None:
        self.machine, self.path = v


class _Checkout:
    def __init__(self, cid: str, spec: dict[str, Any]) -> None:
        self.id = cid
        self.project = spec["project"]
        self.remotes = tuple(spec["remotes"])
        self.views = tuple(_View(v) for v in spec["views"])


class _Home:
    def __init__(self, h: list[Any]) -> None:
        self.project, self.checkout = h


class _Caller:
    def __init__(self, checkout: str | None) -> None:
        self.checkout = checkout


class _Model:
    """The membership model as `Anchoring` calls it, from a world table."""

    def __init__(self, w: dict[str, Any], log: Log) -> None:
        self.w = w
        self.log = log

    @property
    def machine(self) -> str:
        self.log.append(["machine"])
        return str(self.w["machine"])

    def homes(self, ids: list[str]) -> dict[str, _Home]:
        self.log.append(["homes", list(ids)])
        return {i: _Home(self.w["homes"][i]) for i in ids if i in self.w["homes"]}

    def home(self, rid: str) -> _Home | None:
        self.log.append(["home", rid])
        h = self.w["homes"].get(rid)
        return None if h is None else _Home(h)

    def checkout(self, cid: str) -> _Checkout:
        self.log.append(["checkout", cid])
        return _Checkout(cid, self.w["checkouts"][cid])

    def checkouts(self) -> list[_Checkout]:
        self.log.append(["checkouts"])
        return [_Checkout(k, v) for k, v in self.w["checkouts"].items()]

    def live_view(self, k: _Checkout) -> str | None:
        self.log.append(["live_view", k.id])
        return self.w["live"].get(k.id)

    def anchor(self, rid: str, caller: _Caller | None) -> tuple[str | None, str]:
        self.log.append(["anchor", rid, None if caller is None else caller.checkout])
        root, rule = self.w["anchors"][rid]
        return root, rule

    def canonical(self, project: str | None) -> str | None:
        self.log.append(["canonical", project])
        return self.w["canonical"].get(project, project)

    def translate(self, path: str, caller: _Caller | None) -> tuple[str, str]:
        self.log.append(
            ["translate", path, None if caller is None else caller.checkout]
        )
        target, rule = self.w["translate"].get(path, [path, "checkout"])
        return target, rule


def _held(pairs: list[list[str]], log: Log) -> Any:
    table = {(r, s) for r, s in pairs}

    def held(root: str, sha: str) -> bool:
        log.append(["holds_commit", root, sha])
        return (root, sha) in table

    return held


def _anchor_dict(a: va.ClaimAnchor | None) -> Any:
    if a is None:
        return None
    return {
        "root": a.root,
        "rule": a.rule,
        "caller_root": a.caller_root,
        "origin_root": a.origin_root,
        "recorded_roots": list(a.recorded_roots),
        "commit_standin": a.commit_standin,
    }


def _run(
    ops: list[list[Any]], target: Any, anchors: list[Any], paths: _Paths
) -> list[Any]:
    out: list[Any] = []
    for op in ops:
        name, args = op[0], op[1:]
        got: Any
        if name == "prime":
            got = target.prime(args[0])
        elif name == "home":
            h = target.home(args[0])
            got = None if h is None else [h.project, h.checkout]
        elif name == "caller_root":
            got = target.caller_root
        elif name == "for_record":
            a = target.for_record(args[0], origin_root=args[1])
            anchors.append(a)
            got = _anchor_dict(a)
        elif name == "holds":
            got = target.holds(args[0], args[1])
        elif name == "fill":
            got = [target.holds(args[0], f"s{i}") for i in range(args[1])].count(True)
        elif name == "translate":
            got = list(target.translate(args[0]))
        else:
            a = anchors[args[0]]
            if name == "convicting":
                got = a.convicting
            elif name == "declared_root":
                got = a.declared_root
            elif name == "absolute":
                got = list(a.absolute(args[1]))
            elif name == "commit_root":
                got = list(a.commit_root(args[1]))
            else:
                raise ValueError(name)
        out.append(got)
    return out


def _patched(paths: _Paths, held: Any) -> Any:
    return mock.patch.multiple(
        va,
        inside=paths.inside,
        relative_under=paths.relative_under,
        expected_home=paths.expected_home,
        holds_commit=held,
    )


def _anchor_case(
    a: dict[str, Any], hc: list[list[str]], ops: list[list[Any]]
) -> dict[str, Any]:
    log: Log = []
    paths = _Paths(log)
    t = a.get("t")

    def translate(p: str) -> tuple[str, str]:
        log.append(["translate", p])
        target, rule = t[p] if t is not None and p in t else [p, "absolute"]
        return target, rule

    h = a.get("h")
    anchor = va.ClaimAnchor(
        root=a.get("root"),
        rule=a["rule"],
        translate=translate if t is not None else None,
        caller_root=a.get("caller_root"),
        holds=None if h is None else _held(h, log),
        origin_root=a.get("origin_root"),
        recorded_roots=tuple(a.get("recorded_roots", ())),
        commit_standin=a.get("commit_standin", False),
    )
    with _patched(paths, _held(hc, log)):
        o = _run([[op[0], 0, *op[1:]] for op in ops], None, [anchor], paths)
    return {"f": "anchor", "a": a, "hc": hc, "ops": ops, "o": o, "log": log}


def _anchoring_case(
    w: dict[str, Any], c: list[Any] | None, hc: list[list[str]], ops: list[list[Any]]
) -> dict[str, Any]:
    log: Log = []
    paths = _Paths(log)
    caller = None if c is None else _Caller(c[0])
    with (
        _patched(paths, _held(hc, log)),
        mock.patch.object(os.path, "expanduser", paths.expanduser),
    ):
        anchoring = va.Anchoring(_Model(w, log), caller)  # type: ignore[arg-type]
        o = _run(ops, anchoring, [], paths)
    if any(op[0] == "fill" for op in ops):
        n = sum(1 for e in log if e[0] == "holds_commit")
        log = [e for e in log if e[0] != "holds_commit"] + [["holds_commit_count", n]]
    return {"f": "anchoring", "w": w, "c": c, "hc": hc, "ops": ops, "o": o, "log": log}


def _report_case(r: dict[str, list[str] | str]) -> dict[str, Any]:
    fields = {k: (v if k == "anchor" else tuple(v)) for k, v in r.items()}
    rep = va.PathDriftReport(**fields)  # type: ignore[arg-type]
    try:
        d: Any = [[k, v] for k, v in rep.to_dict().items()]
    except ValueError:
        d = "ValueError"
    o = {
        "has_drift": rep.has_drift,
        "has_claim": rep.has_claim_anchored_drift,
        "should": rep.should_report,
        "dict": d,
    }
    return {"f": "report", "r": r, "o": o}


def _reports() -> list[dict[str, Any]]:
    base = {"checked": ["/a", "/b"], "missing": []}
    rows: list[dict[str, Any]] = [
        {"checked": [], "missing": []},
        {**base, "missing": ["/b"]},
        {**base, "missing": ["/b"], "claim_anchored_missing": ["/b"]},
        {**base, "verified": ["/a"]},
        {**base, "expected_absent": ["/c"]},
        {**base, "dropped_as_route": ["/api/x/y"]},
        {**base, "present_but_attested_absent": ["/a"]},
        {**base, "unconfirmed": ["/z"], "unconfirmed_reasons": ["sparse_checkout"]},
        {**base, "anchor": "home_checkout"},
        {**base, "anchor": "", "present_but_attested_absent": ["/a"]},
        {
            **base,
            "anchor": "project_checkout",
            "unconfirmed": ["/z", "/y"],
            "unconfirmed_reasons": ["no_live_checkout", "unanchored"],
            "missing": ["/b"],
            "verified": ["/a"],
            "expected_absent": ["/e"],
            "dropped_as_route": ["/r/s"],
            "claim_anchored_missing": ["/b"],
        },
        {**base, "anchor": "checkout", "unconfirmed": ["/z"]},
        {**base, "anchor": "checkout", "unconfirmed_reasons": ["x"]},
        {**base, "unconfirmed": ["/z"]},
    ]
    return [_report_case(r) for r in rows]


def _anchors() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    simple = [["convicting"], ["declared_root"]]
    for root in (None, "/w"):
        for rule in (
            "home_checkout",
            "checkout",
            "absolute",
            "caller_checkout_same_project",
            "project_checkout",
            "no_live_checkout",
            "global_record",
            "unanchored",
        ):
            out.append(_anchor_case({"root": root, "rule": rule}, [], simple))
    t = {
        "/old/app/x": ["/w/x", "checkout"],
        "/gone/app/x": ["/gone/app/x", "unanchored"],
        "/rec/x": ["/rec/x", "absolute"],
        "/w/y": ["/w/y", "absolute"],
        "/orig/z": ["/orig/z", "absolute"],
        "/else/q": ["/q", "absolute"],
    }
    paths_ops = [["absolute", p] for p in [*t, "/free/p", "/rec", ""]]
    out.append(_anchor_case({"root": "/w", "rule": "absolute"}, [], paths_ops))
    for a in (
        {
            "root": "/w",
            "rule": "home_checkout",
            "t": t,
            "recorded_roots": ["/rec", "/w"],
        },
        {"root": None, "rule": "no_live_checkout", "t": t, "recorded_roots": ["/rec"]},
        {"root": "/w", "rule": "project_checkout", "t": t, "origin_root": "/orig"},
        {"root": "/w", "rule": "project_checkout", "t": t, "origin_root": ""},
        {
            "root": "/rec/x",
            "rule": "checkout",
            "t": t,
            "recorded_roots": ["/rec"],
            "origin_root": "/else",
        },
    ):
        out.append(_anchor_case(a, [], paths_ops))
    commits: list[Any] = [None, "", "c1", "c2"]
    held = [["/caller", "c1"], ["/w", "c1"], ["/w", "c2"], ["/w", ""], ["/caller", ""]]
    specs: list[dict[str, Any]] = [
        {"root": "/w", "rule": "home_checkout", "caller_root": "/caller"},
        {"root": None, "rule": "home_checkout", "caller_root": "/caller"},
        {"root": "/w", "rule": "project_checkout", "caller_root": "/caller"},
        {"root": "/w", "rule": "project_checkout", "caller_root": "/w"},
        {"root": "/w", "rule": "project_checkout", "commit_standin": True},
        {"root": "/w", "rule": "caller_checkout_same_project", "caller_root": "/x"},
        {"root": None, "rule": "no_live_checkout", "caller_root": "/caller"},
        {"root": None, "rule": "no_live_checkout", "commit_standin": True},
    ]
    for spec in specs:
        ops = [["commit_root", c] for c in commits]
        out.append(_anchor_case(spec, held, ops))
        out.append(_anchor_case({**spec, "h": [["/w", "c2"]]}, held, ops))
    return out


def _world() -> dict[str, Any]:
    return {
        "machine": "m1",
        "homes": {
            "r1": ["p", "k1"],
            "r2": ["p", "k1"],
            "r3": ["p", "k2"],
            "r4": ["p", None],
            "r5": ["q", "kx"],
            "r6": ["p2", "k3"],
        },
        "checkouts": {
            "k1": {
                "project": "p",
                "remotes": ["git@h:o/p", "https://h/o/p"],
                "views": [["m0", "/old/app"], ["m1", "/w1"]],
            },
            "k2": {"project": "p", "remotes": [], "views": [["m1", "/w2"]]},
            "k3": {
                "project": "p-old",
                "remotes": ["https://h/o/p"],
                "views": [["m2", "/w3"], ["m1", "/w3"]],
            },
            "k4": {
                "project": "p2",
                "remotes": ["https://h/o/p"],
                "views": [["m1", "/w4"]],
            },
            "k5": {
                "project": "p2",
                "remotes": ["https://h/o/p"],
                "views": [["m1", "/w5"]],
            },
        },
        "live": {"k1": "/w1", "k3": "/w3", "k4": "/w4", "k5": "/w5-moved"},
        "anchors": {
            "r1": ["/w1", "home_checkout"],
            "r2": ["/w9", "project_checkout"],
            "r3": [None, "no_live_checkout"],
            "r4": ["/w1", "caller_checkout_same_project"],
            "r5": [None, "global_record"],
            "r6": ["/w3", "project_checkout"],
        },
        "canonical": {"p-old": "p2"},
        "translate": {
            "/old/app/x": ["/w1/x", "checkout"],
            "/w2/y": ["/w2/y", "unanchored"],
        },
    }


def _anchorings() -> list[dict[str, Any]]:
    w = _world()
    out = []
    ops: list[list[Any]] = [
        ["prime", ["r1", "r1", "r9", "r3"]],
        ["prime", ["r1", "r3"]],
        ["prime", ["r2", "r9"]],
        ["home", "r1"],
        ["home", "r9"],
        ["home", "r4"],
        ["home", "r4"],
    ]
    out.append(_anchoring_case(w, None, [], ops))
    for c in (None, [None], ["k1"], ["kz"], ["k2"], [""]):
        out.append(_anchoring_case(w, c, [], [["caller_root"], ["caller_root"]]))
    rec_ops: list[list[Any]] = [
        ["for_record", "r1", None],
        ["for_record", "r2", None],
        ["for_record", "r2", "/o"],
        ["for_record", "r3", None],
        ["for_record", "r4", None],
        ["for_record", "r5", None],
        ["for_record", "r6", None],
        ["for_record", "r9", None],
        ["for_record", "r1", None],
        ["absolute", 0, "/old/app/x"],
        ["absolute", 2, "/old/app/x"],
        ["absolute", 0, "~/n"],
        ["absolute", 3, "/w2/y"],
        ["absolute", 1, "/w1/z"],
        ["commit_root", 1, "c1"],
        ["commit_root", 1, "c1"],
        ["commit_root", 6, None],
        ["commit_root", 6, "c2"],
        ["convicting", 4],
        ["declared_root", 4],
    ]
    held = [["/w1", "c1"], ["/w3", "c2"]]
    for c in (None, ["k1"], ["k3"]):
        out.append(_anchoring_case(w, c, held, rec_ops))
    tr_ops: list[list[Any]] = [
        ["translate", "/old/app/x"],
        ["translate", "/old/app/x"],
        ["translate", "/old/application"],
        ["translate", "~"],
        ["translate", "~/q"],
        ["translate", "/w2/y"],
        ["translate", "/w2"],
        ["translate", ""],
        ["translate", "/w3/a"],
    ]
    out.append(_anchoring_case(w, ["k1"], [], tr_ops))
    w_home = {
        **w,
        "checkouts": {"k1": {**w["checkouts"]["k1"], "views": [["m1", "/h/u/r"]]}},
    }
    out.append(
        _anchoring_case(w_home, None, [], [["translate", "~/r/x"], ["translate", "~x"]])
    )
    empty = {**w, "checkouts": {}}
    out.append(
        _anchoring_case(empty, None, [], [["translate", "/a"], ["translate", "/b"]])
    )
    out.append(
        _anchoring_case(
            w,
            None,
            [["/r", "s0"], ["/r", "s7"]],
            [
                ["holds", "/r", "s0"],
                ["holds", "/r", "s0"],
                ["holds", "/r", "s1"],
                ["holds", "/r", "s1"],
            ],
        )
    )
    cap = va._HELD_COMMIT_MEMO_CAP
    held_sweep = [["/r", "s0"], ["/r", "s4096"]]
    out.append(
        _anchoring_case(
            w,
            None,
            held_sweep,
            [["fill", "/r", cap], ["holds", "/r", "s0"], ["fill", "/r", cap]],
        )
    )
    out.append(
        _anchoring_case(
            w,
            None,
            held_sweep,
            [
                ["fill", "/r", cap],
                ["holds", "/r", "s0"],
                ["holds", "/r", f"s{cap}"],
                ["fill", "/r", 2],
            ],
        )
    )
    return out


def generate() -> dict[str, bytes]:
    """``verify_anchoring_anchor.jsonl``: not gated."""
    cases = _reports() + _anchors() + _anchorings()
    header = {
        "k": "header",
        "count": len(cases),
        "constants": {
            "CONVICTING_ANCHORS": sorted(va.CONVICTING_ANCHORS),
            "UNANCHORED": va.UNANCHORED,
            "SPARSE_CHECKOUT": va.SPARSE_CHECKOUT,
            "UNCONFIRMED_COMMIT": va.UNCONFIRMED_COMMIT,
            "GLOBAL_RECORD": va.GLOBAL_RECORD,
            "HELD_COMMIT_MEMO_CAP": va._HELD_COMMIT_MEMO_CAP,
        },
    }
    return {"verify_anchoring_anchor.jsonl": jsonl([header, *cases])}

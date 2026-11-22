"""Fixtures: the wire shapes of a search hit and of `memory_show`
(``response_render.jsonl``, not gated).

The native build reads it in
``native/crates/bettermemory-core/src/response_render.rs``. This pins it
to `_response`'s `ResponseBuilder.render_hit` and `render_show`, and
through them `_SHOW_DEFAULTS`, `_slashed`, `_path_states`,
`_compact_origin` and `_grouped_links`.

One JSON object per line, keys sorted, ASCII. The first line is the header
``{"k": "header", "count": n}``. Every other line is a case: ``f`` names
the function, ``a`` its arguments and ``o`` the answer with every dict
written ``{"d": [[key, value], ...]}`` in its order (so the order of
every dict is pinned), or
``{"raised": class}``. Every argument is read through ``json.loads`` of
its sorted text before the reference sees it, as the native test reads
it.

- ``hit``: ``render_hit(a)``.
- ``show``: ``render_show(a.state, Memory.model_validate(a.m), drift)``,
  ``drift`` a stub with ``a.d``'s fields (each absent one empty, ``anchor``
  None).
- ``ps``: ``_path_states(Memory.model_validate(a.m), drift)`` as
  ``[[[path, findings], ...], claims]``.
- ``co``: ``_compact_origin(a)``.
- ``gl``: ``_grouped_links(a.e, a.end)``.
- ``sl``: ``_slashed(a.p)``.

`claims.parse_claim` is a stub: ``path:X`` parses as kind ``path`` with
``rel_path`` X, ``absent:X`` as ``absent``, ``symbol:X`` as ``symbol``;
anything else raises ValueError. ``_NATIVE_SEP`` is the case's ``sep``
(null or a backslash; null by default), set for the call: the reference
reads it from ``os.sep``, so every row names it and nothing here follows
the host.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, Iterator

from bettermemory import _response, claims as claims_mod
from bettermemory._response import ResponseBuilder
from bettermemory.models import Memory

from ._common import jsonl

_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
_DRIFT_FIELDS = (
    "missing",
    "claim_anchored_missing",
    "verified",
    "expected_absent",
    "present_but_attested_absent",
    "unconfirmed",
    "unconfirmed_reasons",
    "dropped_as_route",
    "checked",
)


def _parse_claim(raw: str) -> Any:
    for kind in ("path", "absent", "symbol"):
        if raw.startswith(kind + ":"):
            return SimpleNamespace(kind=kind, rel_path=raw[len(kind) + 1 :])
    raise ValueError(raw)


@contextmanager
def _patched(sep: str | None) -> Iterator[None]:
    saved = (claims_mod.parse_claim, _response._NATIVE_SEP)
    claims_mod.parse_claim = _parse_claim
    _response._NATIVE_SEP = sep
    try:
        yield
    finally:
        claims_mod.parse_claim, _response._NATIVE_SEP = saved


def _drift(d: dict[str, Any]) -> Any:
    fields = {k: list(d.get(k, [])) for k in _DRIFT_FIELDS}
    return SimpleNamespace(**fields, anchor=d.get("anchor"))


def _memory(**f: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": _ID,
        "created": "2026-01-02T03:04:05Z",
        "updated": "2026-01-02T03:04:05Z",
        "scopes": ["global"],
        "confidence": "medium",
        "source": "explicit-statement",
        "body": "b",
    }
    out.update(f)
    return out


def _ordered(v: Any) -> Any:
    """`v` with each dict as its pairs in order."""
    if isinstance(v, dict):
        return {"d": [[k, _ordered(x)] for k, x in v.items()]}
    if isinstance(v, (list, tuple)):
        return [_ordered(x) for x in v]
    return v


def _run(fn: Any) -> Any:
    try:
        return _ordered(fn())
    except (TypeError, KeyError, AttributeError, IndexError, ValueError) as e:
        return {"raised": type(e).__name__}


def _case(f: str, a: Any, sep: str | None = None) -> dict[str, Any]:
    a = json.loads(json.dumps(a, sort_keys=True))
    b = ResponseBuilder.__new__(ResponseBuilder)
    # Validated outside the call: a ValidationError is a ValueError.
    mem = Memory.model_validate(a["m"]) if f in ("show", "ps") else None

    def call() -> Any:
        if f == "hit":
            return b.render_hit(a)
        if f == "show":
            assert mem is not None
            return b.render_show(a["state"], mem, _drift(a["d"]))
        if f == "ps":
            assert mem is not None
            paths, claims = _response._path_states(mem, _drift(a["d"]))
            return [list(map(list, paths.items())), claims]
        if f == "co":
            return _response._compact_origin(a)
        if f == "gl":
            return _response._grouped_links(a["e"], a["end"])
        return _response._slashed(a["p"])

    with _patched(sep):
        o = _run(call)
    case = {"f": f, "a": a, "o": o}
    if sep is not None:
        case["sep"] = sep
    return case


def _hits() -> list[Any]:
    base = {
        "id": "a",
        "snippet": "s",
        "relevance": 0.5,
        "staleness_verdict": "fresh",
        "provenance": "local",
    }
    var: list[dict[str, Any]] = [
        {},
        {"provenance": "synced"},
        {"provenance": None},
        {"provenance": 3},
        {"provenance": "unaccounted"},
        {"provenance": "unaccounted", "verification": {"status": "fresh"}},
        {"trust_unavailable": True},
        {"trust_unavailable": 0, "provenance": "synced"},
        {"_contradicted_by": ["x", "y"]},
        {"_contradicted_by": "xy"},
        # A dict here is left out: `_named` slices it, which raises
        # TypeError on 3.11 and KeyError after.
        {"_contradicted_by": []},
        {"_contradicted_by": None},
        {"_contradicted_by": 5},
        {"contradicts": [{"id": "c1"}, {"id": "c2", "x": 1}]},
        {"contradicts": [{"id": "c1"}], "_contradicted_by": ["z"]},
        {"contradicts": [{"no": 1}]},
        {"contradicts": ["s"]},
        {"contradicts": [None]},
        {"contradicts": 7},
        {"contradicts": 0},
        {"contradicts": "ab"},
        {"superseded_by": [{"id": "s1"}, {"id": "s2"}]},
        {"superseded_by": []},
        {"superseded_by": [[1]]},
        {"superseded_by": {"id": 1}},
        {"held": True},
        {"held": "yes"},
        {"held": 0},
        {"body": None},
        {"body": "full body"},
        {"use_token": "t"},
        {"use_token": None, "body": 1},
        {"staleness_verdict": "spot_check_recommended"},
        {
            "staleness_verdict": "stale",
            "path_drift": {"missing": ["a.py"], "claim_anchored_missing": ["a.py"]},
        },
        {"staleness_verdict": "stale", "commit_drift_count": 3},
        {
            "recent_negative_outcomes": [
                {
                    "count_in_window": 2,
                    "outcome": "ignored",
                    "most_recent_ts": "2026-01-02T03:04:05Z",
                }
            ]
        },
        {"verification": 5},
        {"verification": None},
        {"snippet": ["x"], "relevance": None, "id": 4},
    ]
    out = [{**base, **v} for v in var]
    for key in ("id", "snippet", "relevance", "staleness_verdict"):
        out.append({k: v for k, v in base.items() if k != key})
    return out


def _states() -> list[tuple[Any, Any, Any]]:
    st = {
        "id": "a",
        "staleness_verdict": "fresh",
        "use_token": "t",
        "scopes": ["global"],
        "body": "b",
        "created": "c",
        "updated": "c",
        "provenance": "local",
        "verification": {"status": "fresh", "age_days": 0},
    }
    m0 = _memory()
    d0: dict[str, Any] = {}
    out: list[tuple[Any, Any, Any]] = [(st, m0, d0)]
    var: list[dict[str, Any]] = [
        {"updated": "u"},
        {"updated": 1, "created": 1.0},
        {"updated": True, "created": 1},
        {"last_verified_at": "v"},
        {"last_verified_at": None},
        {"verification": {"status": None, "age_days": 3, "recommendation": "r"}},
        {"verification": {}},
        {"verification": None},
        {"verification": []},
        {"verification": "s"},
        {"confidence": "high", "source": "inferred", "category": "decision"},
        {"confidence": "medium", "source": "explicit-statement", "category": "fact"},
        {"confidence": None, "category": None},
        {"category": 1, "confidence": ["medium"]},
        {"provenance": "synced"},
        {"provenance": None},
        {"provenance": None, "trust_unavailable": True},
        {"provenance": None, "trust_unavailable": 0},
        {
            "trust_unavailable": True,
            "held": True,
            "held_hint": "h",
            "standing_question": "q",
        },
        {"held": 0, "held_hint": "", "standing_question": None},
        {"origin": {"cwd": "/w", "worktree_root": "/w", "host": None, "x": 1}},
        {"origin": {"cwd": "/w/a", "worktree_root": "/w"}},
        {"origin": {"cwd": "/w"}},
        {"origin": {"cwd": None, "worktree_root": None}},
        {"origin": {"cwd": "/w", "worktree_root": "/w"}},
        {
            "origin": {
                "cwd_bytes_base64": "AA==",
                "worktree_root_bytes_base64": "AA==",
                "cwd": "x",
            }
        },
        {"origin": "o"},
        {"actor": {"client": "c", "version": None}},
        {"actor": {"client": None}},
        {"actor": "a"},
        {"commit_drift": {"commits_since_verify": 2, "basis": "b", "extra": 1}},
        {
            "commit_drift": {
                "commits_since_verify": 2,
                "basis": "b",
                "claim_drift": {"drifted": ["x"]},
            },
            "staleness_verdict": "stale",
        },
        {"commit_drift": {"basis": "b"}},
        {"commit_drift": {"commits_since_verify": 0}},
        {"commit_drift": 3},
        {"commit_drift_unread": True, "commit_drift_unchecked": 1},
        {"commit_drift_unread": 0},
        {
            "links": [
                {"type": "supersedes", "target_id": "t1"},
                {"type": "relates", "target_id": "t2", "note": "n"},
                {"type": "supersedes", "target_id": "t3"},
                {"type": "relates", "target_id": "t4", "note": "n"},
                {"type": "relates", "target_id": "t5", "note": None},
            ]
        },
        {"reverse_links": [{"type": "supersedes", "source_id": "s1"}]},
        {"links": [], "reverse_links": None},
        {"links": [{"type": "x"}]},
        {"links": [{"target_id": "x"}]},
        {"links": ["s"]},
        {"reverse_links": [{"type": ["l"], "source_id": "x"}]},
        {"reverse_links": [{"type": "t", "note": {"a": 1}, "source_id": "x"}]},
        {
            "links": [
                {"type": 1, "target_id": "a"},
                {"type": True, "target_id": "b"},
                {"type": 1.0, "target_id": "c"},
            ]
        },
        {"links": 5},
        {
            "staleness_verdict": "stale",
            "path_drift": {"missing": ["a"], "claim_anchored_missing": []},
        },
    ]
    for v in var:
        out.append(({**st, **v}, m0, d0))
    for key in (
        "id",
        "use_token",
        "scopes",
        "body",
        "created",
        "updated",
        "verification",
        "staleness_verdict",
    ):
        out.append(({k: v for k, v in st.items() if k != key}, m0, d0))
    mems = [
        _memory(verified_commits=["c1", "c2"], verified_versions=["v1"]),
        _memory(verified_head="0123456789abcdef0123456789abcdef01234567"),
        _memory(corroborations=2, last_corroborated="2026-03-04T05:06:07.123456Z"),
        _memory(corroborations=1),
        _memory(corroborations=0, last_corroborated="2026-03-04T05:06:07Z"),
        _memory(claims=["symbol:f", "bare", "path:a.py", "absent:gone"]),
    ]
    for m in mems:
        out.append((st, m, d0))
    out.append(
        (st, _memory(verified_paths=["a.py"]), {"checked": ["/r/a.py"], "anchor": "at"})
    )
    out.append((st, m0, {"anchor": "at"}))
    out.append((st, m0, {"unconfirmed": ["x"]}))
    return out


def _drifts() -> list[tuple[Any, Any, str | None]]:
    full = {
        "missing": ["/r/m1", "/r/m2", "/r/m1"],
        "claim_anchored_missing": ["/r/m2", "/r/m3"],
        "verified": ["/r/v", "/r/m1"],
        "expected_absent": ["/r/e"],
        "present_but_attested_absent": ["/r/p"],
        "unconfirmed": ["/r/u", "/r/u"],
        "unconfirmed_reasons": ["too big", "timed out"],
        "dropped_as_route": ["/api/x"],
        "checked": ["/r/c", "/r/v", "/r/m1"],
    }
    m0 = _memory()
    rows: list[tuple[Any, Any, str | None]] = [
        (m0, {}, None),
        (m0, full, None),
        (m0, {"unconfirmed": ["a", "b"], "unconfirmed_reasons": ["r"]}, None),
        (m0, {"unconfirmed": ["a"], "unconfirmed_reasons": ["r", "s"]}, None),
        (m0, {"unconfirmed_reasons": ["r"], "missing": ["x"]}, None),
        (
            _memory(
                verified_paths=["a.py", "./b/c.py", "/abs/d", "../e", "zz", "a.py"],
                verified_absent_paths=["gone", "./b/c.py", "f"],
            ),
            {
                "checked": ["/r/a.py", "/r/b/c.py", "/r/ff", "/x/zz"],
                "missing": ["/r/gone"],
            },
            None,
        ),
        # Two read keys share the tail `/e`: the first in read order wins.
        (
            _memory(verified_paths=["e"], verified_path_targets={"e": "t"}),
            {"checked": ["/a/x/e", "/b/e"]},
            None,
        ),
        (
            _memory(verified_paths=["...//.a", "", ".", "/"]),
            {"checked": ["/r/a", "/r/.a", "/"]},
            None,
        ),
        (
            _memory(
                verified_path_targets={
                    "l": "t",
                    "same": "same",
                    "n": None,
                    "./r/x": "y",
                },
                verified_absent_path_targets={"q": "z"},
                verified_paths=["l"],
            ),
            {"checked": ["/r/x"]},
            None,
        ),
        (_memory(verified_path_targets={}), {}, None),
        (
            _memory(
                claims=[
                    "path:a.py",
                    "absent:b",
                    "symbol:a.py:f",
                    "nope",
                    "path:a.py",
                    "path:./c",
                    "absent:a.py",
                    "",
                ]
            ),
            {"checked": ["/r/a.py", "/r/c"]},
            None,
        ),
        # The separator: a backslash is one only where the platform says so.
        (
            _memory(verified_paths=["b\\c.py", "d/e"], claims=["path:x\\y"]),
            {"checked": ["C:\\r\\b\\c.py", "C:\\r\\d\\e", "C:\\r\\x\\y"]},
            "\\",
        ),
        (
            _memory(verified_paths=["b\\c.py", "d/e"], claims=["path:x\\y"]),
            {"checked": ["C:\\r\\b\\c.py", "C:\\r\\d\\e", "C:\\r\\x\\y"]},
            None,
        ),
        (
            _memory(verified_paths=[".\\b"]),
            {"checked": ["C:\\r\\b", "C:\\r\\.\\b"]},
            "\\",
        ),
        (
            _memory(verified_paths=["\u00e9/\u4e2d"]),
            {"checked": ["/r/\u00e9/\u4e2d"]},
            None,
        ),
    ]
    return rows


def _cases() -> list[dict[str, Any]]:
    cases = [_case("hit", r) for r in _hits()]
    cases.append(
        _case(
            "hit",
            {
                **_hits()[0],
                "_contradicted_by": ["c"],
                "verification": {"status": "fresh"},
            },
        )
    )
    for state, m, d in _states():
        cases.append(_case("show", {"state": state, "m": m, "d": d}))
    state = _states()[0][0]
    for m, d, sep in _drifts():
        cases.append(_case("ps", {"m": m, "d": d}, sep))
        cases.append(
            _case("show", {"state": state, "m": m, "d": {**d, "anchor": "t"}}, sep)
        )
    for origin in (
        {},
        {"a": None},
        {"cwd": "/w", "worktree_root": "/w"},
        {"cwd": "/w", "worktree_root": "/v"},
        {"worktree_root": "/w", "cwd": "/w", "b": 2},
        {"cwd": 1, "worktree_root": 1.0},
        {"cwd": "/w", "worktree_root": None},
        {"cwd_bytes_base64": "QQ==", "worktree_root_bytes_base64": "QQ=="},
        {"cwd_bytes_base64": "QQ==", "worktree_root_bytes_base64": "Qg=="},
        None,
        "x",
        [1],
    ):
        cases.append(_case("co", origin))
    for e, end in (
        ([], "target_id"),
        (
            [
                {"type": "a", "target_id": "1"},
                {"type": "a", "target_id": "2", "note": None},
            ],
            "target_id",
        ),
        (
            [
                {"type": "a", "note": "n", "s": "1"},
                {"type": "a", "s": "2"},
                {"type": "a", "note": "n", "s": "3"},
            ],
            "s",
        ),
        ([{"type": "a", "note": "n", "s": [1, {"k": "v"}]}], "s"),
        ([{"type": "a"}], "s"),
        ({"type": 1}, "s"),
        ("ab", "s"),
        (5, "s"),
        (
            [
                {"type": False, "note": 0, "s": "1"},
                {"type": 0, "note": False, "s": "2"},
                {"type": 0.0, "note": 0, "s": "3"},
            ],
            "s",
        ),
    ):
        cases.append(_case("gl", {"e": e, "end": end}))
    for p, sep in (("a/b", None), ("a\\b", None), ("a\\b\\c/d", "\\"), ("", "\\")):
        cases.append(_case("sl", {"p": p}, sep))
    return cases


def generate() -> dict[str, bytes]:
    """``response_render.jsonl``: not gated."""
    cases = _cases()
    return {
        "response_render.jsonl": jsonl([{"k": "header", "count": len(cases)}, *cases])
    }

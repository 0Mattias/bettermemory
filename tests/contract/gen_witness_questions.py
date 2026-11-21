"""Fixtures: the question half of `location_witness`
(``witness_questions.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/witness_questions.rs``. This pins it
to `AnsweredToken` (its call and `located`), `remint_answer`,
`remint_refused`, `AnsweredLiveness`, `asked_views` and, through it,
`_asked_head`, `_asked_stamps` and `location_git._key`.

`checkout_token.remint` is a stub that records its arguments and answers
the case's (token, status); the model of `located` is a stub that records
the tokens asked and answers the case's checkouts. Every JSON input is
written as the text the reference loads with `json.loads`, so key order
and non-finite numbers reach the port as they reach the reference, and
every mapping answered as the text `json.dumps` writes of it.

Rows, by ``f``:

- ``token``: ``a`` the answers, ``p`` posted, ``k`` the checkout's
  ``token``, ``status`` and ``key`` (device, inode, birth); ``o`` is
  ``{"r": [token, status]}``, ``{"r": null}`` or ``{"need": text}``.
- ``located``: ``a``, the location's ``machine``, ``token``,
  ``written`` and ``key``, ``m`` the model's checkouts
  (``[machine, key]``); ``o`` the result's token, token_written and
  token_status and ``asked``, the tokens the model was asked for.
- ``remint``: the capture's ``git_dir``, ``token``, ``written``, ``key``
  and ``kind``, ``q`` the question, ``w`` the stub's answer; ``o`` the
  answer's text and ``calls`` (git_dir, policy, bare, replacing).
- ``refused``: ``q``, ``a``; ``o`` a bool.
- ``liveness``: ``a``, the checkout ``k`` (kind, key, token, status,
  last_head, stamps, common), ``v`` the view's path; ``o`` is
  ``{"r": state}``, ``{"need": text}`` or ``{"raise": class}``.
- ``views``: ``q``; ``o`` is ``{"r": text}`` (each view as
  ``[path, fields]``) or ``{"raise": class}``.

Rows whose answer holds `str()` of a float are not gated: `float.__repr__`
is the shortest round trip on every release from 3.11 to 3.14.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

from bettermemory import checkout_token
from bettermemory import location_witness as lw
from bettermemory.location_git import Capture
from bettermemory.membership_records import (
    Checkout,
    InitStamp,
    Location,
    ObjectKey,
    View,
)

from ._common import jsonl

T1 = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
T2 = "01BX5ZZKBKACTAV9WEVGEMMVRY"
T3 = "7ZZZZZZZZZZZZZZZZZZZZZZZZZ"
H40 = "0123456789abcdef0123456789abcdef01234567"
H64 = "ab" * 32


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


def _token(a: str, p: bool, k: dict[str, Any]) -> dict[str, Any]:
    try:
        got = lw.AnsweredToken(json.loads(a), posted=p)(_checkout(k))
    except lw.NeedWitnesses as need:
        return {
            "f": "token",
            "a": a,
            "p": p,
            "k": k,
            "o": {"need": json.dumps(need.question)},
        }
    out = None if got is None else list(got)
    return {"f": "token", "a": a, "p": p, "k": k, "o": {"r": out}}


def _located(
    a: str, machine: str, token: str | None, written: str | None, key: Any, m: list[Any]
) -> dict[str, Any]:
    asked: list[str] = []

    def checkouts(*, token: str) -> list[Any]:
        asked.append(token)
        return [SimpleNamespace(machine=c[0], object_key=_okey(c[1])) for c in m]

    loc = Location(
        machine=machine,
        cwd="/r",
        state="repo",
        kind="primary",
        object_key=_okey(key),
        token=token,
        token_written=written,
        token_status="absent",
    )
    got = lw.AnsweredToken(json.loads(a), posted=True).located(
        loc, cast(Any, SimpleNamespace(checkouts=checkouts))
    )
    return {
        "f": "located",
        "a": a,
        "machine": machine,
        "token": token,
        "written": written,
        "key": key,
        "m": m,
        "o": [got.token, got.token_written, got.token_status, asked],
    }


def _remint(cap: dict[str, Any], q: str, w: list[Any]) -> dict[str, Any]:
    calls: list[Any] = []

    def stub(git_dir: Any, *, policy: bool, bare: bool, replacing: Any) -> Any:
        calls.append(
            [git_dir, policy, bare, None if replacing is None else list(replacing)]
        )
        return SimpleNamespace(token=w[0], status=w[1])

    key = cap.get("key")
    c = Capture(
        cwd="/r",
        state="repo",
        kind=cap.get("kind"),
        git_dir=cap.get("git_dir"),
        object_key=None if key is None else tuple(key),
        token=cap.get("token"),
        token_written=cap.get("written"),
    )
    with mock.patch.object(checkout_token, "remint", stub):
        got = lw.remint_answer(c, json.loads(q))
    return {"f": "remint", "cap": cap, "q": q, "w": w, "o": [json.dumps(got), calls]}


def _refused(q: str, a: str) -> dict[str, Any]:
    return {
        "f": "refused",
        "q": q,
        "a": a,
        "o": lw.remint_refused(json.loads(q), json.loads(a)),
    }


def _liveness(a: str, k: dict[str, Any], v: str) -> dict[str, Any]:
    row: dict[str, Any] = {"f": "liveness", "a": a, "k": k, "v": v}
    try:
        live = lw.AnsweredLiveness(json.loads(a))
    except (ValueError, TypeError, RecursionError, OverflowError) as exc:
        row["o"] = {"raise": type(exc).__name__}
        return row
    view = View(
        checkout="c1", machine="m1", path=v, first_seen="x", last_seen="x", order=0
    )
    try:
        row["o"] = {"r": live(_checkout(k), view)}
    except lw.NeedWitnesses as need:
        row["o"] = {"need": json.dumps(need.question)}
    return row


def _views(q: str) -> dict[str, Any]:
    try:
        got = lw.asked_views(json.loads(q))
    except (ValueError, TypeError, RecursionError, OverflowError) as exc:
        return {"f": "views", "q": q, "o": {"raise": type(exc).__name__}}
    return {"f": "views", "q": q, "o": {"r": json.dumps(got)}}


def _rows() -> list[dict[str, Any]]:
    k_full = {"token": T1, "key": ["7", "42", "9"]}
    rows = []
    # AnsweredToken.__call__: answered mappings, posted and not.
    for a in [
        '{"remint": {"token": "%s", "status": "written"}}' % T2,
        '{"remint": {"token": "%s", "status": ""}}' % T2,
        '{"remint": {"token": "short", "status": "exists"}}',
        '{"remint": {"token": null, "status": 5}}',
        '{"remint": {"token": "%s"}}' % T2.lower(),
        '{"remint": {}}',
        '{"remint": {"token": ["%s"], "status": "skipped:policy"}}' % T2,
        '{"remint": {"token": "%sX", "status": "written"}}' % T2,
        '{"remint": {"token": "%s", "status": "written"}, "remint": {"status": "late"}}'
        % T2,
    ]:
        rows.append(_token(a, True, k_full))
    for a, p in [
        ('{"remint": true}', True),
        ('{"remint": []}', False),
        ("{}", True),
        ('{"remint_ok": true}', True),
        ('{"remint_ok": 0}', True),
        ('{"remint_ok": "no"}', True),
        ('{"remint_ok": []}', True),
        ("{}", False),
        ('{"remint": null, "remint_ok": 1}', True),
    ]:
        rows.append(_token(a, p, k_full))
    ks: list[dict[str, Any]] = [
        {"token": None, "key": ["7", "42", None]},
        {"token": T1, "key": None},
        {"token": T1, "key": ["7x", "42", None]},
        {"token": "bad", "key": ["7", "42", None]},
        {"token": T1, "key": ["-7", "042", "b"], "status": "superseded"},
        {"token": "", "key": ["7", "42", None]},
    ]
    for k in ks:
        rows.append(_token("{}", False, k))
    # located: the five early returns, then the model's answer.
    w2 = '{"remint": {"token": "%s", "status": "written"}}' % T2
    key = ["7", "42", "9"]
    for a, tok, lk in [
        ("{}", T1, key),
        ('{"remint": "%s"}' % T2, T1, key),
        ('{"remint": {"token": "%s", "status": "exists"}}' % T2, T1, key),
        ('{"remint": {"token": "%s"}}' % T2, T1, key),
        ('{"remint": {"token": "bad", "status": "written"}}', T1, key),
        (w2, T2, key),
        (w2, T1, None),
    ]:
        rows.append(_located(a, "m1", tok, "W", lk, [["m1", key]]))
    ms: list[list[Any]] = [
        [["m1", key]],
        [["m1", ["7", "42", "other"]]],
        [["m1", ["7", "42", None]]],
        [["m2", key]],
        [["m1", ["7", "43", "9"]]],
        [["m1", ["8", "42", "9"]]],
        [["m1", None]],
        [],
        [["m2", key], ["m1", None], ["m1", ["7", "42", "z"]]],
    ]
    for m in ms:
        rows.append(_located(w2, "m1", T1, "W", key, m))
    rows.append(_located(w2, "m1", None, None, key, [["m1", key]]))
    # remint_answer.
    cap = {
        "git_dir": "/r/.git",
        "token": T1,
        "written": None,
        "key": ["7", "42", None],
        "kind": "primary",
    }
    for c in [
        dict(cap, git_dir=None),
        dict(cap, git_dir=""),
        dict(cap, kind="bare"),
        dict(cap, kind="linked"),
        dict(cap, kind=None),
        dict(cap, token=None, written=T2),
        dict(cap, token="", written=T2),
        dict(cap, token=None, written=None),
        dict(cap, key=None),
        dict(cap, key=["x", "42", None]),
        dict(cap, git_dir="/r/é/.git"),
    ]:
        rows.append(_remint(c, "{}", [T3, "written"]))
    for q in [
        '{"write_token": true}',
        '{"write_token": 0}',
        '{"write_token": "x"}',
        '{"write_token": []}',
        '{"replacing": null}',
        '{"replacing": ["%s", "1", "2"]}' % T2,
        '{"replacing": ["%s", "1", "2", "3"]}' % T2,
        '{"replacing": ["%s", "1", "-2"]}' % T2,
        '{"replacing": ["%s", 1, "2"]}' % T2,
        '{"replacing": "%s"}' % T2,
        '{"replacing": ["bad", "1", "2"], "write_token": 1}',
        '{"remint": true, "write_token": true, "replacing": ["%s", "7", "42"]}' % T1,
    ]:
        rows.append(_remint(cap, q, [T3, "written"]))
    ws: list[list[Any]] = [
        [None, "skipped:policy"],
        [T2, "exists"],
        [None, "failed:EACCES"],
    ]
    for w in ws:
        rows.append(_remint(cap, '{"write_token": true}', w))
    # remint_refused.
    for q, a in [
        ('{"remint": true}', '{"remint": {"status": "exists"}}'),
        ('{"remint": 1}', '{"remint": {"status": "exists", "token": null}}'),
        ('{"remint": false}', '{"remint": {"status": "exists"}}'),
        ("{}", '{"remint": {"status": "exists"}}'),
        ('{"remint": "x"}', '{"remint": {"status": "written"}}'),
        ('{"remint": true}', '{"remint": {"status": "EXISTS"}}'),
        ('{"remint": true}', '{"remint": ["exists"]}'),
        ('{"remint": true}', '{"remint": "exists"}'),
        ('{"remint": true}', "{}"),
        ('{"remint": []}', '{"remint": {"status": "exists"}}'),
        ('{"remint": [0]}', '{"remint": {"status": "exists"}}'),
    ]:
        rows.append(_refused(q, a))
    # AnsweredLiveness.
    k = {
        "kind": "linked",
        "key": ["7", "42", None],
        "token": T1,
        "status": "superseded",
        "last_head": H40,
        "stamps": [["description", 1, 2, 3, 4], ["info/exclude", 5, -6, 7, 10**18]],
        "common": ["7", "40", "b"],
    }
    for a, v in [
        ('{"views": {"/r": "live"}}', "/r"),
        ('{"views": {"/r": "live"}}', "/s"),
        ('{"views": {"/r": 5, "/s": null, "/t": [1, "a"], "/u": {"x": true}}}', "/t"),
        ('{"views": {"/r": 5, "/s": null, "/t": [1, "a"], "/u": {"x": true}}}', "/u"),
        ('{"views": {"/r": 5, "/s": null}}', "/s"),
        ('{"views": {"/r": 5}}', "/r"),
        ('{"views": [["/r", "live"]]}', "/r"),
        ('{"views": "/r"}', "/r"),
        ('{"views": {"/r": "a", "/r": "b"}}', "/r"),
        ('{"views": {"": "empty"}}', ""),
        ("{}", "/é"),
    ]:
        rows.append(_liveness(a, k, v))
    rows.append(_liveness("{}", {"key": None, "token": None, "status": "absent"}, "/r"))
    rows.append(
        _liveness(
            "{}",
            {
                "kind": "unresolved",
                "token": "x",
                "status": "present",
                "key": ["1", "2", "3"],
            },
            "/r",
        )
    )
    # asked_views.
    good = {
        "path": "/r",
        "kind": "linked",
        "object": ["7", "42", None],
        "token": T1,
        "last_head": H40.upper(),
        "stamps": [["description", 1, 2, 3, 4]],
        "common_object": [7, 40, 1],
        "superseded": True,
    }
    qs: list[dict[str, Any]] = [
        {"views": [good]},
        {"views": [good, {"path": "/s"}]},
        {"views": None},
        {"views": []},
        {"views": ""},
        {"views": "abc"},
        {"views": {"a": 1}},
        {"views": 0},
        {"views": 3},
        {"views": True},
        {"views": False},
        {},
        {"views": [1, None, "x", [], {"path": 5}, {"path": None}, {"kind": "bare"}]},
        {"views": [{"path": "/r", "object": [1, 2]}]},
        {"views": [{"path": "/r", "object": "1,2,3"}]},
        {"views": [{"path": "/r", "object": {"a": 1, "b": 2, "c": 3}}]},
        {"views": [{"path": "/r", "common_object": [1, 2, 3, 4]}]},
        {"views": [{"path": "/r", "object": [[1], {"a": None}, True]}]},
        {"views": [{"path": "/r", "object": [None, None, None]}]},
        {"views": [{"path": "/r", "object": None, "common_object": None}]},
        {"views": [{"path": "/r"}, {"path": "/s", "object": 5}]},
        {
            "views": [
                {"path": "/r", "kind": ""},
                {"path": "/s", "kind": 0},
                {"path": "/t", "kind": [1]},
            ]
        },
        {"views": [{"path": "/r", "kind": True}, {"path": "/s", "kind": {"k": "v"}}]},
        {"views": [{"path": "/r", "token": 5}, {"path": "/s", "token": ""}]},
        {
            "views": [
                {"path": "/r", "superseded": 1},
                {"path": "/s", "superseded": "true"},
            ]
        },
        {"views": [{"path": "/r", "superseded": False}]},
    ]
    for qd in qs:
        rows.append(_views(json.dumps(qd)))
    for head in [
        H40,
        H40.upper(),
        H64,
        H64.upper(),
        H40[:-1],
        H40 + "0",
        "g" * 40,
        H40 + "\n",
        " " + H40,
        H40[:-1] + "İ",
        "AB" * 32 + "0" * 24,
        "a" * 52,
        5,
        None,
        [H40],
    ]:
        rows.append(_views(json.dumps({"views": [{"path": "/r", "last_head": head}]})))
    for stamps in [
        [["n", 1, 2, 3, 4]] * 9,
        [["n%d" % i, i, 0, 0, 0] for i in range(10)],
        [["n", 1, 2, 3]],
        [["n", 1, 2, 3, 4, 5]],
        [["n", "1", " 2 ", "-3", "+4"]],
        [["n", "1_000", "x", 3, 4], ["m", 1, 2, 3, 4]],
        [["n", True, False, 3, 4]],
        [["n", None, 2, 3, 4], ["m", [1], 2, 3, 4], ["o", {}, 2, 3, 4]],
        [
            [5, 1, 2, 3, 4],
            [None, 1, 2, 3, 4],
            [[1, "a"], 1, 2, 3, 4],
            [True, 1, 2, 3, 4],
        ],
        [["n", "١٢", "３", 3, 4]],
        [["n", "", 2, 3, 4], ["m", "1__0", 2, 3, 4], ["o", "_1", 2, 3, 4]],
        [["n", 10**40, -(10**40), 0, 0]],
        "abc",
        {"a": 1},
        None,
        [None, 5, "x", {"a": 1}],
    ]:
        rows.append(_views(json.dumps({"views": [{"path": "/r", "stamps": stamps}]})))
    rows.append(
        _views('{"views": [{"path": "/r", "stamps": [["n", Infinity, 2, 3, 4]]}]}')
    )
    rows.append(_views('{"views": [{"path": "/r", "stamps": [["n", NaN, 2, 3, 4]]}]}'))
    rows.append(
        _views(
            '{"views": [{"path": "/r", "stamps": [["n", 1, 2, 3, 4]]}], "views": []}'
        )
    )
    return rows


def _rows_float() -> list[dict[str, Any]]:
    """Rows whose answer holds `str()` of a float."""
    k = {"kind": "primary", "key": ["7", "42", None], "token": T1}
    return [
        _liveness('{"views": {"/r": 1.5, "/s": 1e300, "/t": [0.1, -0.0]}}', k, "/r"),
        _liveness('{"views": {"/r": 1.5, "/s": 1e300, "/t": [0.1, -0.0]}}', k, "/t"),
        _liveness('{"views": {"/r": Infinity, "/s": NaN}}', k, "/s"),
        _views('{"views": [{"path": "/r", "kind": 2.5, "object": [1.0, 1e16, 0.1]}]}'),
        _views('{"views": [{"path": "/r", "stamps": [[1e-7, 1.9, -2.9, 1e20, 0.0]]}]}'),
        _views('{"views": [{"path": "/r", "common_object": [-Infinity, NaN, 2.0]}]}'),
    ]


def generate() -> dict[str, bytes]:
    return {"witness_questions.jsonl": jsonl(_rows() + _rows_float())}

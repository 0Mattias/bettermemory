"""Fixtures: the commit-drift bases and `verify`'s status leaves
(``verify_basis.jsonl``, not gated).

The native build reads it in
``native/crates/bettermemory-core/src/verify_basis.rs`` and
``src/verify/status.rs``. This pins them to `verify_basis` whole
(`unread_recommendation`, `dates_since`, `date_count_uncertainty`,
`_count_since_verify`) and to `verify`'s `CommitDriftStatus.to_dict`,
`_drift_recommendation`, `_claim_drift_recommendation`,
`commit_drift_status`, `_index_paths`, `_unreadable`, `_gated_claims` and
`staleness_verdict_from_report`.

No git runs. `_count_since_verify` reads `repo_toplevel_and_head`,
`commits_since_anchor` and `commit_author_timestamps` through
`verify_basis`'s own bindings, which a case scripts and restores; the
walks given up on (`origin_history._UNWALKED`) are restored too.

One JSON object per line, keys sorted, ASCII. A text that holds a
surrogate is written as ``<name>_cps``, its code points
(`_common.text_field`). A datetime is its ``isoformat()``. The first line
is the header ``{"k": "header", "count": n, "bases": [...]}``. Every
other line has ``k``:

- ``"unread"``: ``why`` a `HistoryUnread` value, ``o`` the
  recommendation, in the dict's order.
- ``"since"``: ``h`` the history (null, a `HistoryUnread` value or a list
  of datetimes), ``s`` the instant; ``o`` the answer (a count, a value,
  null, or ``"TypeError"``).
- ``"unc"``: ``c``, ``b``; ``o`` the value or null.
- ``"count"``: ``vh`` the verified head or null; ``loc`` what
  `repo_toplevel_and_head` answers (null or ``[toplevel, head]``);
  ``walk`` what `commits_since_anchor` answers (null or its commits);
  ``give`` a value the walk notes as given up on before answering, or
  null; ``pre`` walks noted before the call; ``h`` the dates and ``s``
  the stamp as in ``since``. ``o`` the answer: null, a value,
  ``"TypeError"`` or ``[count, basis, walk commits or null, toplevel or
  null]``; ``calls`` the reads made, each ``["loc", cwd]``, ``["walk",
  cwd, anchor, toplevel, head]`` or ``["dates", cwd]``.
- ``"status"``: ``r`` a `ResolvedCommitDrift` ``[count, checked,
  drifted, basis]``; ``o`` `commit_drift_status(r).to_dict()`.
- ``"cds"``: a `CommitDriftStatus` ``[status, count, recommendation,
  checked, drifted, basis]``; ``o`` its ``to_dict()``.
- ``"rec"``: ``c`` and ``d``; ``o`` `_drift_recommendation(c)`, ``oc``
  `_claim_drift_recommendation(c, d)`.
- ``"index"``: ``i`` the index's path-bearing keys (``files``,
  ``deleted``, ``changed_text``, ``changed_fragments``, ``header_only``
  and ``bindings`` as paths); ``o`` the sorted paths.
- ``"claims"``: ``p`` claim paths; ``u`` `_unreadable`, ``g`` the paths
  `_gated_claims` keeps.
- ``"verdict"``: ``st`` the status, ``cam``, ``m``, ``pa`` the lengths of
  ``claim_anchored_missing``, ``missing``,
  ``present_but_attested_absent``, ``c`` the commit count; ``o``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

from bettermemory import origin_history, verify, verify_basis
from bettermemory.claims import Claim
from bettermemory.origin_history import HistoryUnread, ReachableWalk
from bettermemory.verify_anchoring import PathDriftReport
from bettermemory.verify_status import VerificationStatus

from ._common import jsonl, text_field

_UTC = timezone.utc
_PLUS2 = timezone(timedelta(hours=2))
_MINUS = timezone(timedelta(hours=-5, minutes=-30, seconds=-7, microseconds=-1))


def _t(name: str, text: str) -> dict[str, Any]:
    return text_field(name, text)


def _dt(value: datetime) -> str:
    return value.isoformat()


def _hist(h: Any) -> Any:
    if h is None:
        return None
    if isinstance(h, HistoryUnread):
        return h.value
    return [_dt(d) for d in h]


def _ans(value: Any) -> Any:
    if isinstance(value, HistoryUnread):
        return value.value
    return value


_T0 = datetime(2026, 1, 1, 10, 0, 0, tzinfo=_UTC)
_DATES = [
    datetime(2025, 12, 31, 23, 59, 59, 999999, tzinfo=_UTC),
    datetime(2026, 1, 1, 12, 0, 0, tzinfo=_PLUS2),  # the same instant as _T0
    datetime(2026, 1, 1, 10, 0, 0, 1, tzinfo=_UTC),
    datetime(2026, 1, 1, 4, 29, 52, 999999, tzinfo=_MINUS),  # T0 - 1us, wrapped
    datetime(2027, 6, 1, tzinfo=_PLUS2),
]


def _since_cases() -> Iterator[tuple[Any, datetime]]:
    ascending = sorted(_DATES)
    yield None, _T0
    for why in HistoryUnread:
        yield why, _T0
    yield [], _T0
    yield [], _T0.replace(tzinfo=None)
    for s in [
        _T0,
        _T0 - timedelta(microseconds=1),
        _T0 + timedelta(microseconds=1),
        _T0.astimezone(_MINUS),
        datetime(1, 1, 1, tzinfo=_UTC),
        datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=_UTC),
        datetime(2026, 1, 1, 12, 0, 0, tzinfo=_PLUS2),
    ]:
        yield ascending, s
    yield [_T0] * 5, _T0
    yield [_T0] * 4, _T0 - timedelta(microseconds=1)
    # A naive stamp against aware dates: a TypeError where one is probed.
    yield ascending, _T0.replace(tzinfo=None)
    # Naive against naive orders by wall clock.
    yield [d.replace(tzinfo=None) for d in ascending], _T0.replace(tzinfo=None)
    # A mixed list: only the probed elements are compared.
    yield [_T0.replace(tzinfo=None), _T0, _T0 + timedelta(days=1)], _T0
    yield [_T0, _T0 + timedelta(days=1), _T0.replace(tzinfo=None)], _T0
    yield [_T0, _T0.replace(tzinfo=None)], _T0
    # Not ascending: bisect reads what it probes.
    yield [_T0 + timedelta(days=3), _T0 - timedelta(days=3), _T0], _T0


def _since() -> list[dict[str, Any]]:
    out = []
    for h, s in _since_cases():
        try:
            o = _ans(verify_basis.dates_since(h, s))
        except TypeError:
            o = "TypeError"
        out.append({"k": "since", "h": _hist(h), "s": _dt(s), "o": o})
    return out


def _count_case(
    vh: str | None,
    loc: tuple[str, str] | None,
    walk: list[str] | None,
    give: HistoryUnread | None,
    pre: int,
    h: Any,
    s: datetime,
) -> dict[str, Any]:
    calls: list[list[Any]] = []

    def located(cwd: Any) -> Any:
        calls.append(["loc", str(cwd)])
        return None if loc is None else (loc[0], loc[1])

    def walked(cwd: Any, anchor: str, *, toplevel: Any, head: str) -> Any:
        calls.append(["walk", str(cwd), anchor, str(toplevel), head])
        if give is not None:
            origin_history.note_unwalked(give)
        if walk is None:
            return None
        return ReachableWalk(anchor, head, tuple(walk), {})

    def dates(cwd: Any) -> Any:
        calls.append(["dates", str(cwd)])
        return h

    names = (
        "repo_toplevel_and_head",
        "commits_since_anchor",
        "commit_author_timestamps",
    )
    saved = [getattr(verify_basis, name) for name in names]
    state = origin_history._UNWALKED
    saved_state = (state.count, state.why)
    try:
        for _ in range(pre):
            origin_history.note_unwalked(HistoryUnread.ERROR)
        for name, fake in zip(names, (located, walked, dates), strict=True):
            setattr(verify_basis, name, fake)
        try:
            got: Any = verify_basis._count_since_verify("w/cwd", s, vh)  # type: ignore[arg-type]
        except TypeError:
            got = "TypeError"
    finally:
        for name, real in zip(names, saved, strict=True):
            setattr(verify_basis, name, real)
        state.count, state.why = saved_state
    if isinstance(got, tuple):
        count, basis, w, top = got
        o: Any = [count, basis, None if w is None else list(w.commits), top]
    else:
        o = _ans(got)
    return {
        "k": "count",
        "vh": vh,
        "loc": None if loc is None else list(loc),
        "walk": walk,
        "give": None if give is None else give.value,
        "pre": pre,
        "h": _hist(h),
        "s": _dt(s),
        "o": o,
        "calls": calls,
    }


def _counts() -> list[dict[str, Any]]:
    asc = sorted(_DATES)
    top = ("w/top", "H" * 40)
    rows = [
        # No anchor: the date count, nothing located.
        _count_case(None, top, ["a"], None, 0, asc, _T0),
        _count_case(None, None, None, None, 0, None, _T0),
        _count_case(None, None, None, None, 0, HistoryUnread.SHALLOW, _T0),
        _count_case(None, None, None, None, 0, [], _T0),
        _count_case(None, None, None, None, 0, asc, _T0.replace(tzinfo=None)),
        # An anchor outside a repository: the date count.
        _count_case("A" * 40, None, ["a"], None, 0, asc, _T0),
        # The walk: its length, the dates never read.
        _count_case("A" * 40, top, ["c1", "c2", "c3"], None, 0, asc, _T0),
        _count_case("A" * 40, top, [], None, 0, asc, _T0),
        _count_case("A" * 40, top, ["c1"], None, 3, None, _T0),
        # A walk that answers though one was noted: the walk stands.
        _count_case("A" * 40, top, ["c1"], HistoryUnread.TIMEOUT, 0, None, _T0),
        # Given up on: the reason, the dates never read.
        _count_case("A" * 40, top, None, HistoryUnread.TIMEOUT, 0, asc, _T0),
        _count_case("A" * 40, top, None, HistoryUnread.ERROR, 2, asc, _T0),
        # Not an ancestor: the date count, the toplevel kept.
        _count_case("A" * 40, top, None, None, 0, asc, _T0),
        _count_case("A" * 40, top, None, None, 4, asc, _T0 + timedelta(days=900)),
        _count_case("A" * 40, top, None, None, 1, None, _T0),
        _count_case("A" * 40, top, None, None, 0, HistoryUnread.CALL_BUDGET, _T0),
        _count_case("A" * 40, top, None, None, 0, asc, _T0.replace(tzinfo=None)),
        _count_case("", ("", ""), None, None, 0, [], _T0),
        _count_case("", ("", ""), [""], None, 0, [], _T0),
    ]
    return rows


def _resolved(
    count: int, checked: int, drifted: tuple[str, ...], basis: str
) -> verify.ResolvedCommitDrift:
    return verify.ResolvedCommitDrift(count, checked, drifted, basis)


def _spelled_dict(d: dict[str, Any]) -> Any:
    out: list[Any] = []
    for key, value in d.items():
        if isinstance(value, str):
            out.append([key, _t("v", value)])
        elif isinstance(value, list):
            out.append([key, [_t("v", v) for v in value]])
        elif isinstance(value, dict):
            out.append([key, _spelled_dict(value)])
        else:
            out.append([key, value])
    return out


def _statuses() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    rb = verify_basis.BASIS_REACHABILITY
    ad = verify_basis.BASIS_AUTHOR_DATE
    for count, checked, drifted, basis in [
        (0, 0, (), ad),
        (0, 2, ("a.py::f",), rb),
        (1, 0, (), ad),
        (2, 0, (), rb),
        (1, 1, ("a.py::f",), rb),
        (3, 4, ("a::x", "b::y"), ad),
        (5, 4, ("a", "b", "c"), ad),
        (5, 4, ("a", "b", "c", "d"), ad),
        (7, 0, ("x\udc80y", "\U0001f600", "z"), rb),
        (-1, -2, (), ad),
        (2**62, 0, (), "other"),
        (1, 3, ("",), ""),
    ]:
        status = verify.commit_drift_status(_resolved(count, checked, drifted, basis))
        rows.append(
            {
                "k": "status",
                "r": [count, checked, [_t("v", d) for d in drifted], basis],
                "o": _spelled_dict(status.to_dict()),
            }
        )
    for fields in [
        ("drift", 3, None, 0, ("a",), ad),
        ("clean", 0, "r\udcff", -1, ("a", "b"), rb),
        ("x", -4, "", 1, (), ""),
    ]:
        st, c, rec, chk, dr, basis = fields
        cds = verify.CommitDriftStatus(st, c, rec, chk, dr, basis)
        rows.append(
            {
                "k": "cds",
                "f": [
                    st,
                    c,
                    None if rec is None else _t("v", rec),
                    chk,
                    [_t("v", d) for d in dr],
                    basis,
                ],
                "o": _spelled_dict(cds.to_dict()),
            }
        )
    for c, d in [
        (1, ["a"]),
        (2, ["a"]),
        (0, ["a", "b"]),
        (1, ["a", "b", "c"]),
        (3, ["a", "b", "c", "d", "e"]),
        (-1, []),
        (10**15, ["\udc00", "x"]),
    ]:
        rows.append(
            {
                "k": "rec",
                "c": c,
                "d": [_t("v", x) for x in d],
                "o": verify._drift_recommendation(c),
                "oc": _t("v", verify._claim_drift_recommendation(c, d)),
            }
        )
    return rows


def _indexes() -> list[dict[str, Any]]:
    rows = []
    for spec in [
        {},
        {"files": ["a", "b"], "deleted": ["b", "c"]},
        {"changed_text": ["t"], "changed_fragments": ["f", "t"], "header_only": ["h"]},
        {"bindings": ["p", "q", "p"], "files": ["\udc80"]},
        {
            "files": ["z", "\U0001f600", "\uffff"],
            "deleted": ["a"],
            "changed_text": ["m"],
            "changed_fragments": ["n"],
            "header_only": ["z"],
            "bindings": ["y"],
        },
    ]:
        index = {
            "files": set(spec.get("files", [])),
            "deleted": set(spec.get("deleted", [])),
            "changed_text": {p: {} for p in spec.get("changed_text", [])},
            "changed_fragments": {p: {} for p in spec.get("changed_fragments", [])},
            "header_only": {p: set() for p in spec.get("header_only", [])},
            "bindings": {
                (p, "symbol", f"n{i}"): {}
                for i, p in enumerate(spec.get("bindings", []))
            },
        }
        got = sorted(verify._index_paths(index))
        rows.append(
            {
                "k": "index",
                "i": {
                    key: [_t("v", p) for p in value]
                    for key, value in sorted(spec.items())
                },
                "o": [_t("v", p) for p in got],
            }
        )
    return rows


def _claims() -> list[dict[str, Any]]:
    rows = []
    for paths in [
        [],
        ["a.py"],
        ["a\tb.py"],
        ["ok.py", "x\x7f"],
        ["x\udc80"],
        ["\x1f", "\x20"],
        ["../up.py", "a/../../b", "a/../b"],
        ["/abs", "C:x", "c:/y", "//host/s", "/\\h", "x\x00y", "http://h/p"],
        ["a\\..\\..\\b", "1:x", "é:x", "ok/dir/"],
        [""],
    ]:
        claims = [Claim("path", p, "", "") for p in paths]
        rows.append(
            {
                "k": "claims",
                "p": [_t("v", p) for p in paths],
                "u": verify._unreadable(claims),
                "g": [_t("v", c.rel_path) for c in verify._gated_claims(claims)],
            }
        )
    return rows


def _verdicts() -> list[dict[str, Any]]:
    rows = []
    for st in ["never", "stale", "fresh", "remote"]:
        for cam, m, pa, c in [
            (0, 0, 0, None),
            (0, 0, 0, 0),
            (0, 0, 0, 1),
            (1, 1, 0, None),
            (0, 2, 0, 0),
            (0, 0, 3, None),
            (2, 3, 1, 5),
        ]:
            verification = VerificationStatus(
                status=st,
                last_verified_at=None,
                age_days=None,
                recommendation=None,
                stale_after_days=30,
            )
            report = PathDriftReport(
                checked=(),
                missing=tuple(f"m{i}" for i in range(m)),
                claim_anchored_missing=tuple(f"m{i}" for i in range(cam)),
                present_but_attested_absent=tuple(f"p{i}" for i in range(pa)),
            )
            o = verify.staleness_verdict_from_report(
                verification=verification, path_drift=report, commit_drift_count=c
            )
            rows.append(
                {"k": "verdict", "st": st, "cam": cam, "m": m, "pa": pa, "c": c, "o": o}
            )
    return rows


def generate() -> dict[str, bytes]:
    """``verify_basis.jsonl``: not gated."""
    cases: list[dict[str, Any]] = [
        {"k": "unread", "why": why.value, "o": text}
        for why, text in verify_basis._UNREAD_RECOMMENDATIONS.items()
    ]
    cases += _since()
    cases += [
        {
            "k": "unc",
            "c": c,
            "b": b,
            "o": _ans(verify_basis.date_count_uncertainty(c, b)),
        }
        for c in (0, 1, -1)
        for b in (verify_basis.BASIS_AUTHOR_DATE, verify_basis.BASIS_REACHABILITY, "")
    ]
    cases += _counts()
    cases += _statuses()
    cases += _indexes()
    cases += _claims()
    cases += _verdicts()
    header = {
        "k": "header",
        "count": len(cases),
        "bases": [verify_basis.BASIS_REACHABILITY, verify_basis.BASIS_AUTHOR_DATE],
        "unread_n": len(verify_basis.UNREAD_RECOMMENDATIONS),
    }
    return {"verify_basis.jsonl": jsonl([header, *cases])}

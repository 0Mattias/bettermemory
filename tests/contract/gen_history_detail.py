"""Fixtures: `history`'s third unit, what one log row of a record's
history says (``history_detail.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/history_rows.rs``. This pins it to
`HistoryRow.to_dict`, `_changed`, `_body_diff`, `_set`, `_event_detail`,
`_RecordWalk`, `_record_row` and `_link_change_rows`.

Every payload is JSON text (``p``), read by `json.loads` as the log's
payloads are, so a ``NaN`` in it is `json.decoder`'s one object. Cases,
by ``f``:

- ``changed``: ``a`` and ``b`` two record texts; ``r`` `_changed(a, b)`.
- ``event``: ``p``; ``r`` `_event_detail(p)` in `_common.neutral` form.
- ``walk``: ``rows``, each ``[kind, p]``, read in order by
  `_record_row` with one fresh `_RecordWalk`. Row ``i`` has seq
  ``i + 1``, ts ``t<seq>``, session ``s`` for an odd seq and None for an
  even one; the stub verifier answers True, False, None by seq modulo
  3 (1, 2, 0); the stub ``adjacent(i, step)`` is row ``i + step`` of
  the case, or None past either end. ``r`` is each row's
  `to_dict(diff=True)`, neutral, and ``w`` the walk at the end
  (``record_kind``, ``previous_seq``, ``homed``, ``last_record_kind``).
- ``links``: ``rows``, each ``[kind, p, subject]``, read in order by
  `_link_change_rows` for record ``id`` with one shared
  ``links_by_source``; seq, ts, session and verifier as for ``walk``;
  ``r`` the rows' `to_dict(diff=True)`, row by row.
"""

from __future__ import annotations

import json
import types
from typing import Any

from bettermemory import history as h
from bettermemory.log import LogRow

from ._common import jsonl, neutral

_VERDICTS = (None, True, False)


def _row(index: int, kind: str, payload: str) -> LogRow:
    seq = index + 1
    return LogRow(
        seq=seq,
        ts=f"t{seq}",
        session="s" if seq % 2 else None,
        kind=kind,
        payload=payload,
        prev_mac="pm",
        mac="m",
    )


def _verifier() -> Any:
    return types.SimpleNamespace(verified=lambda row: _VERDICTS[row.seq % 3])


def _base(row: LogRow) -> dict[str, Any]:
    return {"seq": row.seq, "ts": row.ts, "session": row.session, "kind": row.kind}


def _walk(rows: list[tuple[str, str]]) -> dict[str, Any]:
    found = [(_row(i, k, p), json.loads(p)) for i, (k, p) in enumerate(rows)]

    def adjacent(index: int, step: int) -> tuple[str, dict[str, Any]] | None:
        at = index + step
        if 0 <= at < len(found):
            return found[at][0].kind, found[at][1]
        return None

    walk = h._RecordWalk()
    out = [
        h._record_row(
            walk,
            i,
            row,
            payload=payload,
            base=_base(row),
            adjacent=adjacent,
            verifier=_verifier(),
        ).to_dict(diff=True)
        for i, (row, payload) in enumerate(found)
    ]
    return {
        "f": "walk",
        "rows": [list(r) for r in rows],
        "r": [neutral(r) for r in out],
        "w": [walk.record_kind, walk.previous_seq, walk.homed, walk.last_record_kind],
    }


def _links(record_id: str, rows: list[tuple[str, str, str]]) -> dict[str, Any]:
    links_by_source: dict[str, set[str]] = {}
    out = []
    for i, (kind, p, subject) in enumerate(rows):
        row = _row(i, kind, p)
        out.append(
            [
                neutral(r.to_dict(diff=True))
                for r in h._link_change_rows(
                    row,
                    payload=json.loads(p),
                    base=_base(row),
                    subject=subject,
                    record_id=record_id,
                    links_by_source=links_by_source,
                    verifier=_verifier(),
                )
            ]
        )
    return {"f": "links", "id": record_id, "rows": [list(r) for r in rows], "r": out}


def _d(**fields: Any) -> str:
    return json.dumps(fields)


def _mem(kind: str = "memory", **record: Any) -> str:
    return json.dumps({kind: {"id": "R", **record}})


def _put(**record: Any) -> tuple[str, str]:
    return ("memory_put", _mem(**record))


def _walk_cases() -> list[dict[str, Any]]:
    actor = {"actor": {"name": "n", "kind": "agent"}}
    seqs: list[list[tuple[str, str]]] = [
        # Created, updated (body diff), corroborated, verified, updated.
        [
            _put(body="one\ntwo", tags=["a"], **actor),
            _put(body="one\nthree", tags=["a"]),
            _put(body="one\nthree", tags=["a"], corroborations=2),
            _put(
                body="one\nthree", tags=["a"], corroborations=2, last_corroborated="x"
            ),
            ("verification_put", _d(memory_id="R", machine_id="M", verified_head="H")),
            _put(body="one\nthree", tags=["b"], corroborations=3),
            _put(body="one\nthree", tags=["b"], corroborations=3, updated="u2"),
            ("verification_delete", _d(memory_id="R", machine_id="M")),
            ("verification_delete", _d(memory_id="R")),
        ],
        # Removed then restored, with a tombstone's reason.
        [
            _put(body="b", provenance="ignored-in-record"),
            ("memory_delete", _d(id="R")),
            ("tombstone_put", _mem("tombstone", body="b", removed_reason="stale")),
            ("tombstone_delete", _d(id="R")),
            _put(body="b"),
            ("memory_delete", _d(id="R")),
        ],
        # A tombstone first, then updated without a delete before it.
        [
            ("tombstone_put", _mem("tombstone", body="x", removed_reason=None)),
            ("tombstone_put", _mem("tombstone", body="x", removed_reason="")),
            ("tombstone_delete", _d(id="R")),
        ],
        # Imported, provenance labels, relinked.
        [
            (
                "memory_put",
                json.dumps(
                    {"memory": {"id": "R", "body": ""}, "provenance": "imported"}
                ),
            ),
            (
                "memory_put",
                json.dumps({"memory": {"id": "R", "body": ""}, "provenance": "local"}),
            ),
            (
                "memory_put",
                json.dumps(
                    {"memory": {"id": "R", "body": ""}, "provenance": "m:other"}
                ),
            ),
            ("memory_put", json.dumps({"memory": {"id": "R"}, "relink": [1, 2]})),
            (
                "memory_put",
                json.dumps({"memory": {"id": "R"}, "relink": None, "provenance": 0}),
            ),
            (
                "memory_put",
                json.dumps(
                    {"memory": {"id": "R"}, "relink": False, "provenance": None}
                ),
            ),
        ],
        [
            (
                "memory_put",
                json.dumps(
                    {"memory": {"id": "R", "body": 5}, "provenance": "imported"}
                ),
            )
        ],
        [
            (
                "memory_put",
                json.dumps({"memory": {"id": "R"}, "provenance": "local", "relink": 0}),
            )
        ],
        [("memory_put", json.dumps({"memory": "not a mapping"}))],
        [("memory_put", json.dumps({"memory": {"id": "R", "actor": {}}}))],
        [("memory_put", json.dumps({"memory": {"id": "R", "actor": [1]}}))],
        # Episodes.
        [
            ("episode_put", _mem("episode", body="e1")),
            ("episode_put", _mem("episode", body="e1\ne2", status="open")),
            ("episode_delete", _d(id="R")),
        ],
        [("episode_delete", _d(id="R")), ("memory_delete", _d(id="R"))],
        [("tombstone_delete", _d(id="R"))],
        # Home, moved, unhomed; standing; returned; capture; import.
        [
            (
                "home_put",
                _d(
                    home={
                        "record_id": "R",
                        "project": "p",
                        "checkout": None,
                        "rule": "r",
                    }
                ),
            ),
            ("home_put", _d(home={"record_id": "R", "project": "q"})),
            ("home_delete", _d(record_id="R")),
            ("home_put", _d(home="no")),
            (
                "standing_put",
                _d(
                    standing={
                        "record_id": "R",
                        "probability": 0.5,
                        "source": "s",
                        "answered_by": None,
                    }
                ),
            ),
            ("standing_put", _d(standing={"probability": 0, "answered_by": False})),
            ("standing_delete", _d(record_id="R")),
            ("returned_put", _d(returned={"record_id": "R", "via": "search"})),
            ("returned_put", _d(returned=[])),
            ("returned_delete", _d(record_id="R")),
            ("returned_delete", _d(record_id="R", dropped="removed")),
            ("returned_delete", _d(record_id="R", dropped=False)),
            ("returned_delete", _d(record_id="R", dropped=None)),
            ("capture_member", _d(memory_id="R", batch="b1", joined_by="j")),
            ("capture_member", _d(memory_id="R")),
            ("import_put", _d(memory_id="R", source="file")),
            ("import_put", _d(memory_id="R", source=None)),
            ("unknown_kind", _d(x=1)),
        ],
        # Values differing only across types, and NaN.
        [
            _put(body="b", a=1, b=0, c=[[]], d={"x": [1]}, e="NaN-free", n=1.5),
            _put(body="b", a=1.0, b=False, c=[[]], d={"x": [1.0]}, e="NaN-free", n=1.5),
            _put(body="b", a=True, b="", c=[[], []], d={"x": [True]}, e=None, n=1.5),
            (
                "memory_put",
                '{"memory": {"id": "R", "body": "b", "n": NaN, "l": [NaN], "o": {"k": NaN}}}',
            ),
            (
                "memory_put",
                '{"memory": {"id": "R", "body": "b", "n": NaN, "l": [NaN], "o": {"k": NaN}}}',
            ),
            (
                "memory_put",
                '{"memory": {"id": "R", "body": "b", "n": 1, "l": [Infinity], "o": {"k": -0.0}}}',
            ),
            (
                "memory_put",
                '{"memory": {"id": "R", "body": "b", "n": 1, "l": [1e999], "o": {"k": 0}}}',
            ),
            (
                "memory_put",
                '{"memory": {"id": "R", "body": "b", "n": 9007199254740993, "o": {"k": 0, "j": 1}}}',
            ),
            (
                "memory_put",
                '{"memory": {"id": "R", "body": "b", "n": 9007199254740992.0, "o": {"j": 1, "k": 0}}}',
            ),
            (
                "memory_put",
                '{"memory": {"id": "R", "body": "b", "n": 9007199254740993, "removed": 1, "created": 2}}',
            ),
        ],
    ]
    return [_walk(rows) for rows in seqs]


def _changed_cases() -> list[dict[str, Any]]:
    pairs = [
        ("{}", "{}"),
        ('{"a": 1}', '{"a": 1.0}'),
        ('{"a": 1}', '{"a": true}'),
        ('{"a": 0}', '{"a": false}'),
        ('{"a": 0}', '{"a": ""}'),
        ('{"a": 0}', '{"a": null}'),
        ('{"a": [[]]}', '{"a": [[]]}'),
        ('{"a": [[]]}', '{"a": [[], []]}'),
        ('{"a": [1]}', '{"a": [1.0]}'),
        ('{"a": {"x": 1}}', '{"a": {"x": 2}}'),
        ('{"a": {"x": 1, "y": 2}}', '{"a": {"y": 2, "x": 1}}'),
        ('{"a": {"x": 1}}', '{"a": {"x": 1, "y": 2}}'),
        ('{"a": NaN}', '{"a": NaN}'),
        ('{"a": [NaN]}', '{"a": [NaN]}'),
        ('{"a": {"k": NaN}}', '{"a": {"k": NaN}}'),
        ('{"a": -0.0}', '{"a": 0}'),
        ('{"a": 1e999}', '{"a": Infinity}'),
        ('{"a": 9007199254740993}', '{"a": 9007199254740992.0}'),
        ('{"a": 1, "only_a": 2}', '{"a": 2, "only_b": 3}'),
        (
            '{"id": 1, "created": 1, "updated": 1, "removed": 1, "removed_reason": 1, "removed_session": 1, "z": 1}',
            '{"id": 2, "created": 2, "updated": 2, "removed": 2, "removed_reason": 2, "removed_session": 2, "z": 2}',
        ),
        (
            '{"\\u00e9": 1, "z": 1, "Z": 1, "\\ud83d\\ude00": 1}',
            '{"\\u00e9": 2, "z": 2, "Z": 2, "\\ud83d\\ude00": 2}',
        ),
        ('{"a": "x"}', '{"a": ["x"]}'),
    ]
    out = []
    for a, b in pairs:
        r = h._changed(json.loads(a), json.loads(b))
        out.append({"f": "changed", "a": a, "b": b, "r": [neutral(x) for x in r]})
    return out


def _event_cases() -> list[dict[str, Any]]:
    payloads = [
        "{}",
        '{"status": null, "reason": "", "fields": [], "door": {}, "attribution": 0, "triggered_from": false}',
        '{"status": 0.0, "reason": [[]], "fields": [""], "door": {"a": null}, "attribution": "x", "triggered_from": NaN}',
        '{"triggered_from": 1, "status": "s", "other": 1}',
        '{"forced": true}',
        '{"forced": 1}',
        '{"forced": "true"}',
        '{"forced": false, "x_acknowledged": true, "a_acknowledged": 1, "b_acknowledged": 0, "c_acknowledged": [], "_acknowledged": "y"}',
        '{"d_acknowledged": [0], "e_acknowledged": {}, "f_acknowledged": NaN, "acknowledged": true, "g_Acknowledged": true}',
        '{"status": "a", "status": "b"}',
    ]
    return [
        {"f": "event", "p": p, "r": neutral(h._event_detail(json.loads(p)))}
        for p in payloads
    ]


def _link_cases() -> list[dict[str, Any]]:
    def mp(*links: Any) -> str:
        return json.dumps({"memory": {"id": "S", "links": list(links)}})

    def tp(*links: Any) -> str:
        return json.dumps({"tombstone": {"id": "S"}, "links": list(links)})

    sup = {"target_id": "R", "type": "supersedes"}
    rel = {"target_id": "R", "type": "related"}
    other = {"target_id": "X", "type": "related"}
    untyped = {"target_id": "R"}
    return [
        _links(
            "R",
            [
                ("memory_put", mp(sup, rel, other), "S"),
                ("memory_put", mp(sup, rel), "S"),
                ("memory_put", mp(rel, untyped), "S"),
                ("tombstone_put", tp(), "S"),
                ("tombstone_put", tp(sup, {"target_id": "R", "type": 3}), "S"),
                ("memory_put", mp(sup), "T"),
                ("memory_put", json.dumps({"memory": "x"}), "T"),
                ("memory_delete", json.dumps({"id": "S", "links": [sup]}), "S"),
                (
                    "memory_put",
                    mp({"target_id": "R", "type": "Supersedes"}, "not a link"),
                    "S",
                ),
            ],
        ),
    ]


def generate() -> dict[str, bytes]:
    cases = _changed_cases() + _event_cases() + _walk_cases() + _link_cases()
    return {"history_detail.jsonl": jsonl([{"cases": len(cases)}, *cases])}

"""Fixtures: the steps of ``eval.compute_eval``, unit eval_rates-1
(``eval_rates.jsonl``, ``eval_rates_unicode.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/eval_rates.rs``. This pins it to
`eval_rates._walk_eval_events` with `_count_search` and `_count_use`,
`_resolve_audit_telemetry` and `_cold_endorsement_rows`, over the
`_EvalWalk` and `_AuditTally` they fill.

The first line is a header with the count of rows and the memories the
cold rows read (``memories``: ``[key, id, scopes, body, category]``, the
dict ``by_id`` keyed by ``key``, each a `Memory` built with
``model_construct``, since the function reads only those four fields).
Then one row per log (``k`` ``log``):

- ``name``, a label; ``events``, the log as JSON values (dicts, and
  values that are not dicts); ``cutoff`` and ``end``, the window bounds
  as `fromisoformat` texts or null; ``reject``, the ids ``passes_scope``
  answers False for (every other id passes);
- ``calls``, each id ``passes_scope`` was called with, in order;
- ``walk``, the walk (below), or ``x``, the class of the exception the
  walk raised;
- ``resolve``, ``[[tombstoned ids, limit, tally or {"x": class}], ...]``,
  `_resolve_audit_telemetry` of the walk under each;
- ``cold``, ``[[floor, ratio threshold as its repr, rows], ...]``,
  `_cold_endorsement_rows` of the walk under each, over the header's
  memories.

A walk is ``review``, ``surface`` and ``audit``, the positions in
``events`` of the rows kept by reference; ``n``, the nine counts
(``retrieval_occurrences``, ``explicit_endorsements_with_excerpt``,
``applied_total``, ``applied_explicit``, ``applied_model``,
``applied_hook``, ``total_events_scanned``, ``events_in_window``,
``hook_telemetry_events``); ``search``, ``auto`` and ``explicit``, the
per-memory dicts as ``[id, count]`` pairs in insertion order;
``cutoff``, the latest miss cutoff's `isoformat` or null; ``acked``, the
acknowledged event ids sorted. A tally is ``n`` (``turns_audited``,
``turns_no_signal``, ``silent_misses``, ``repeat_audits``), ``rule``,
``models`` (``[model, audited, no_signal, misses]`` in insertion order)
and ``buffer`` (each candidate as ``[ts, session_id, top_missed_id,
top_missed_relevance, threshold_rule, str(recent_retrieval_count)]``).
Cold rows are ``rows`` (``[id, scopes, summary, retrieval_count, auto,
explicit]``), ``total`` and ``suppressed``.

The last line of ``eval_rates.jsonl`` is a sweep: ``count`` logs drawn by
`draw_log` from ``seed``, each answered as a row is (without its inputs)
over ``SWEEP_MEMORIES``, and the SHA-256 of each answer's compact JSON
line (sorted keys, ASCII) in turn.

``eval_rates.jsonl`` is not gated: its texts are ASCII but for two ids
past the BMP and one in it (their order by code point, which no Unicode
release changes), and its stamps are forms every release from 3.11 reads
alike. ``eval_rates_unicode.jsonl`` holds what reads the Unicode
database: excerpts whose `str.strip` meets whitespace past ASCII, and
bodies whose `first_summary_line` strips, and finds a sentence's end
before, whitespace past ASCII.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from bettermemory import eval_rates as er
from bettermemory.models import Category, Confidence, Memory, Source

from ._common import dumps_line, jsonl

_MASK = (1 << 64) - 1
SWEEP_SEED = 20261126
SWEEP_COUNT = 3000


class _Draw:
    """`nxt` steps x = x * 6364136223846793005 + 1442695040888963407 mod
    2**64 and gives x >> 33; `pick(n)` is `nxt() % n`."""

    def __init__(self, seed: int) -> None:
        self.x = seed

    def nxt(self) -> int:
        self.x = (self.x * 6364136223846793005 + 1442695040888963407) & _MASK
        return self.x >> 33

    def pick(self, n: int) -> int:
        return self.nxt() % n


# ---------------------------------------------------------------------------
# answers
# ---------------------------------------------------------------------------

Mem = tuple[str, str, list[str], str, str | None]


# The fields `_cold_endorsement_rows` does not read, set as a valid
# `Memory` would have them.
_CREATED = datetime(2026, 10, 1, tzinfo=timezone.utc)


def by_id(memories: list[Mem]) -> dict[str, Memory]:
    return {
        key: Memory.model_construct(
            id=mid,
            created=_CREATED,
            updated=_CREATED,
            scopes=list(scopes),
            confidence=Confidence.HIGH,
            source=Source.INFERRED,
            body=body,
            category=None if category is None else Category(category),
        )
        for key, mid, scopes, body, category in memories
    }


def _dt(text: str | None) -> datetime | None:
    return None if text is None else datetime.fromisoformat(text)


def _walk_out(walk: er._EvalWalk, events: list[Any]) -> dict[str, Any]:
    pos = {id(ev): i for i, ev in enumerate(events) if isinstance(ev, dict)}
    cutoff = walk.latest_miss_cutoff
    return {
        "review": [pos[id(ev)] for ev in walk.review_rows],
        "surface": [pos[id(ev)] for ev in walk.surface_rows],
        "audit": [pos[id(ev)] for ev in walk.audit_telemetry],
        "n": [
            walk.retrieval_occurrences,
            walk.explicit_endorsements_with_excerpt,
            walk.applied_total,
            walk.applied_explicit,
            walk.applied_model,
            walk.applied_hook,
            walk.total_events_scanned,
            walk.events_in_window,
            walk.hook_telemetry_events,
        ],
        "search": [[k, v] for k, v in walk.search_delivery_count.items()],
        "auto": [[k, v] for k, v in walk.auto_applied_count.items()],
        "explicit": [[k, v] for k, v in walk.explicit_applied_count.items()],
        "cutoff": None if cutoff is None else cutoff.isoformat(),
        "acked": sorted(walk.acknowledged_miss_event_ids),
    }


def _tally_out(walk: er._EvalWalk, tombstoned: list[str], limit: int) -> Any:
    try:
        tally = er._resolve_audit_telemetry(walk, set(tombstoned), limit)
    except Exception as exc:  # the class is the answer
        return {"x": type(exc).__name__}
    return {
        "n": [
            tally.turns_audited,
            tally.turns_no_signal,
            tally.silent_misses,
            tally.repeat_audits,
        ],
        "rule": tally.threshold_rule,
        "models": [
            [m, c["audited"], c["no_signal"], c["misses"]]
            for m, c in tally.by_model.items()
        ],
        "buffer": [
            [
                c.ts,
                c.session_id,
                c.top_missed_id,
                c.top_missed_relevance,
                c.threshold_rule,
                str(c.recent_retrieval_count),
            ]
            for c in tally.silent_miss_buffer
        ],
    }


def _cold_out(
    walk: er._EvalWalk, memories: dict[str, Memory], floor: int, ratio: float
) -> dict[str, Any]:
    rows, total, suppressed = er._cold_endorsement_rows(walk, memories, floor, ratio)
    return {
        "rows": [
            [
                r.id,
                list(r.scopes),
                r.summary,
                r.retrieval_count,
                r.auto_applied_count,
                r.explicit_applied_count,
            ]
            for r in rows
        ],
        "total": total,
        "suppressed": suppressed,
    }


def answer(
    events: list[Any],
    cutoff: str | None,
    end: str | None,
    reject: set[str],
    resolves: list[tuple[list[str], int]],
    colds: list[tuple[int, float]],
    memories: dict[str, Memory],
) -> dict[str, Any]:
    """Every step over one log: the walk, each resolve and each cold list."""
    calls: list[Any] = []

    def passes_scope(mid: str | None) -> bool:
        calls.append(mid)
        return mid not in reject

    out: dict[str, Any] = {"calls": calls}
    try:
        walk = er._walk_eval_events(events, _dt(cutoff), _dt(end), passes_scope)
    except Exception as exc:  # the class is the answer
        out["x"] = type(exc).__name__
        return out
    out["walk"] = _walk_out(walk, events)
    out["resolve"] = [[t, n, _tally_out(walk, t, n)] for t, n in resolves]
    out["cold"] = [[f, repr(r), _cold_out(walk, memories, f, r)] for f, r in colds]
    return out


# ---------------------------------------------------------------------------
# the hand-built logs
# ---------------------------------------------------------------------------


def T(day: int, hour: int = 0) -> str:
    return f"2026-10-{day:02d}T{hour:02d}:00:00Z"


MEMORIES: list[Mem] = [
    ("a", "a", ["global"], "Alpha holds. More text.", "fact"),
    ("b", "b", ["repo:x"], "Beta is ambient.", "ambient"),
    ("c", "c", ["global", "repo:y"], "Gamma has no category", None),
    (
        "d",
        "d-row",
        ["global"],
        "Delta's row id differs from its key.",
        "user-inference",
    ),
    ("f", "f", [], "e.g. this opens with an abbreviation. Then more.", "fact"),
    ("g", "g", ["global"], "word " * 30, "fact"),
    ("\uff5e", "\uff5e", ["global"], "Fullwidth tilde.", "fact"),
    ("\U0001f600", "\U0001f600", ["global"], "Astral.", "fact"),
]

DEFAULT_RESOLVES: list[tuple[list[str], int]] = [([], 20)]
DEFAULT_COLDS: list[tuple[int, float]] = [(1, 0.0)]

_CASES: list[dict[str, Any]] = []


def case(
    name: str,
    events: list[Any],
    cutoff: str | None = None,
    end: str | None = None,
    reject: tuple[str, ...] = (),
    resolves: list[tuple[list[str], int]] | None = None,
    colds: list[tuple[int, float]] | None = None,
) -> None:
    _CASES.append(
        {
            "name": name,
            "events": events,
            "cutoff": cutoff,
            "end": end,
            "reject": sorted(reject),
            "resolves": DEFAULT_RESOLVES if resolves is None else resolves,
            "colds": DEFAULT_COLDS if colds is None else colds,
        }
    )


HOOK = {"kind": "turn_audited", "triggered_from": "stop_hook", "ts": T(1)}


def use(ids: Any, **fields: Any) -> dict[str, Any]:
    return {"kind": "use", "outcome": "applied", "ids": ids, "ts": T(5), **fields}


def miss(event_id: Any, top: str | None = "a", **fields: Any) -> dict[str, Any]:
    ev: dict[str, Any] = {
        "kind": "search_miss",
        "ts": T(5),
        "session_id": "s1",
        "event_id": event_id,
        "recent_retrieval_count": 2,
    }
    if top is not None:
        ev["top_hits"] = [{"id": top, "relevance": "high"}]
    ev.update(fields)
    return ev


def audit(**fields: Any) -> dict[str, Any]:
    return {"kind": "turn_audited", "ts": T(5), **fields}


def _cases() -> None:
    case("empty log", [])
    case(
        "values that are not dicts are skipped uncounted",
        [None, "x", 3, [1], {"kind": "show", "id": "a"}, 2.5, True],
    )
    case(
        "search, list and show",
        [
            {"kind": "search", "returned": ["a", "b", "a"]},
            {"kind": "list", "returned": ["a", "c"]},
            {"kind": "show", "id": "c"},
            {"kind": "show", "id": 5},
            {"kind": "show"},
        ],
    )
    case(
        "legacy fields and falsy returned falling through",
        [
            {"kind": "search", "memory_ids": ["a"]},
            {"kind": "search", "hit_ids": ["b"]},
            {"kind": "search", "returned": [], "memory_ids": ["c"]},
            {"kind": "search", "returned": 0, "memory_ids": [], "hit_ids": ["d"]},
            {"kind": "search", "returned": "", "hit_ids": ["a"]},
            {"kind": "search", "returned": {}, "memory_ids": None, "hit_ids": ["b"]},
            {"kind": "search", "returned": None, "memory_ids": ["c"], "hit_ids": ["d"]},
        ],
    )
    case(
        "ids that are not a list",
        [
            {"kind": "search", "returned": "abc", "memory_ids": ["a"]},
            {"kind": "search", "returned": {"a": 1}},
            {"kind": "search", "returned": 7},
            {"kind": "list", "returned": [1, "a", None, ["b"], "b"]},
            use("a"),
            use({"a": 1}),
            use(0, memory_ids="b"),
        ],
    )
    case(
        "the scope is asked of each str id in order",
        [
            {"kind": "search", "returned": ["a", "b", "c", "b"]},
            {"kind": "show", "id": "b"},
            use(["b", "a", "b"]),
        ],
        reject=("b",),
    )
    case(
        "use tiers: auto is the bool True only, hook by attribution",
        [
            use(["a"], auto=True),
            use(["a"], auto=1),
            use(["a"], auto="true"),
            use(["a"], auto=False, attribution="hook"),
            use(["b"], attribution="hook", auto=True),
            use(["c"], attribution="model"),
            use(["c"], attribution="HOOK"),
            {**use(["d"]), "outcome": "ignored"},
            {**use(["d"]), "outcome": None},
            {"kind": "use", "ids": ["d"], "ts": T(5)},
            use([], memory_ids=["d"], auto=True),
            use(None, memory_ids=["a"]),
        ],
    )
    case(
        "excerpts: duplicates deduplicated, the first non-empty one kept",
        [
            use(["a", "a"], claim_excerpts=["", "x"]),
            use(["b", "b"], claim_excerpts=[" ", "y"]),
            use(["c", "c"], claim_excerpts=["x", ""]),
            use(["d", "d", "d"], claim_excerpts=[None, 5, " \t\n"]),
            use(["f", "f"], claim_excerpts=[None]),
            use(["g", 3, "g"], claim_excerpts=["", "z", "w"]),
            use(["a"], claim_excerpts="x"),
            use(["b"], claim_excerpts=[" x "]),
            use(["c", "d"], claim_excerpts={"0": "x"}),
            use(["c", "d"], claim_excerpts=["", "x", "y"]),
            use(["a", "a"], claim_excerpts=["x", "y"], attribution="hook"),
            use(["b", "b"], claim_excerpts=["x", "y"], auto=True),
        ],
    )
    case(
        "settlements count per memory and not in the trio",
        [
            use(["a", "b"], surface="brief", claim_excerpts=["x", "y"]),
            use(["a"], surface="recall", auto=True),
            use(["c"], surface="other"),
            use(["d"], surface=None),
            use(["a"], surface=5),
            {**use(["d"], surface=["brief"]), "outcome": "ignored"},
        ],
    )
    case(
        "an unhashable surface on an applied use raises",
        [{"kind": "search", "returned": ["a"]}, use(["a"], surface=["brief"])],
    )
    case(
        "the settlement test comes before the ids test",
        [use("not a list", surface={"brief": 1})],
    )
    case(
        "an unhashable kind raises at the surface rows",
        [{"kind": "search", "returned": ["a"]}, {"kind": ["search"]}],
    )
    case(
        "a kind of another hashable type matches nothing",
        [
            {"kind": 3, "returned": ["a"]},
            {"kind": None, "id": "a"},
            {"kind": "Search", "returned": ["a"]},
            {"kind": "search ", "returned": ["a"]},
            {"returned": ["a"]},
        ],
    )
    case(
        "review and surface rows are kept before the window filter",
        [
            {"kind": "review_issued", "ts": T(1)},
            {"kind": "review_grade"},
            {"kind": "write", "review_id": "r1", "ts": T(9)},
            {"kind": "write", "review_id": ""},
            {"kind": "write", "review_id": 5},
            {"kind": "session_brief", "ids": ["a"], "ts": T(1)},
            {"kind": "prompt_recall", "ts": "bad"},
            {"kind": "turn_audited", "ts": T(5)},
            {"kind": "segment_opened"},
        ],
        cutoff="2026-10-03T00:00:00+00:00",
        end="2026-10-07T00:00:00+00:00",
    )
    window: list[dict[str, Any]] = [
        {"kind": "search", "returned": ["a"], "ts": T(2, 23)},
        {"kind": "search", "returned": ["b"], "ts": T(3)},
        {"kind": "search", "returned": ["c"], "ts": "2026-10-03T01:00:00+02:00"},
        {"kind": "search", "returned": ["d"], "ts": "2026-10-04T00:00:00"},
        {"kind": "search", "returned": ["f"], "ts": T(6, 23)},
        {"kind": "search", "returned": ["g"], "ts": T(7)},
        {"kind": "search", "returned": ["a"], "ts": "2026-10-06T23:30:00-02:00"},
        {"kind": "search", "returned": ["b"], "ts": "bad"},
        {"kind": "search", "returned": ["c"], "ts": 5},
        {"kind": "search", "returned": ["d"]},
        {"kind": "search", "returned": ["f"], "ts": "2026-10-05"},
    ]
    case(
        "the window: at the cutoff in, at the end out, by instant",
        window,
        cutoff="2026-10-03T00:00:00+00:00",
        end="2026-10-07T00:00:00+00:00",
    )
    case(
        "only a cutoff",
        [dict(ev) for ev in window],
        cutoff="2026-10-03T00:00:00+00:00",
    )
    case(
        "only an end",
        [dict(ev) for ev in window],
        end="2026-10-07T00:00:00+00:00",
    )
    case(
        "a naive bound against an aware stamp",
        [{"kind": "show", "id": "a", "ts": T(5)}],
        cutoff="2026-10-03T00:00:00",
    )
    case(
        "a naive end not reached when the cutoff already excludes",
        [
            {"kind": "show", "id": "a", "ts": T(1)},
            {"kind": "show", "id": "b", "ts": "x"},
        ],
        cutoff="2026-10-03T00:00:00+00:00",
        end="2026-10-07T00:00:00",
    )
    tallies = [([], -1), ([], 0), ([], 1), ([], 20), (["a"], 20)]
    case(
        "markers outside the window still apply",
        [
            {"kind": "silent_miss_cutoff", "cutoff_ts": T(4), "ts": T(1)},
            {"kind": "miss_ack", "event_id": "e2", "ts": T(9)},
            audit(ts=T(3, 12)),
            audit(ts=T(4)),
            audit(ts="bad"),
            miss("e1", ts=T(3, 23)),
            miss("e2"),
            miss("e3"),
            miss("e4", ts=T(4)),
        ],
        cutoff="2026-10-02T00:00:00+00:00",
        end="2026-10-08T00:00:00+00:00",
        resolves=tallies,
    )
    case(
        "the latest cutoff wins, strictly later, in any order",
        [
            {"kind": "silent_miss_cutoff", "cutoff_ts": T(6)},
            {"kind": "silent_miss_cutoff", "cutoff_ts": T(4)},
            {"kind": "silent_miss_cutoff", "cutoff_ts": "2026-10-06T02:00:00+02:00"},
            {"kind": "silent_miss_cutoff", "cutoff_ts": "bad"},
            {"kind": "silent_miss_cutoff"},
            {"kind": "silent_miss_cutoff", "cutoff_ts": 7},
            audit(ts=T(5)),
            audit(ts=T(6)),
            miss("e1", ts=T(7)),
        ],
        resolves=tallies,
    )
    case(
        "a later marker retracts an earlier event",
        [
            miss("e1", ts=T(2)),
            audit(ts=T(2)),
            miss("e2", ts=T(5)),
            {"kind": "miss_ack", "event_id": "e2"},
            {"kind": "silent_miss_cutoff", "cutoff_ts": T(3)},
            {"kind": "miss_ack", "event_id": ""},
            {"kind": "miss_ack", "event_id": 5},
            {"kind": "miss_ack"},
            {"kind": "miss_ack", "event_id": "e9", "ts": "bad"},
        ],
        resolves=tallies,
    )
    case(
        "no cutoff keeps an unreadable stamp",
        [audit(ts="bad"), miss("e1", ts=None), audit()],
        resolves=tallies,
    )
    case(
        "repeats, no_signal and models in first-seen order",
        [
            audit(client_model="zeta", verdict="ok"),
            audit(client_model="alpha", verdict="no_signal"),
            audit(client_model="zeta", repeat=True),
            audit(client_model="mid", repeat=1),
            audit(client_model="alpha", repeat=0),
            audit(client_model="alpha", repeat=""),
            audit(client_model="alpha", repeat=0.0),
            audit(client_model="", verdict="no_signal"),
            audit(client_model=5),
            audit(verdict="NO_SIGNAL"),
            miss("e1", client_model="beta"),
            miss("e2", client_model="zeta"),
            miss("e3", client_model=""),
        ],
        resolves=tallies,
    )
    case(
        "the threshold rule is the last non-empty str",
        [
            audit(threshold_rule="r1"),
            miss("e1", threshold_rule="r2"),
            audit(threshold_rule=""),
            audit(threshold_rule=5),
            audit(threshold_rule="r3", repeat=True),
            miss("e2", threshold_rule=None),
        ],
        resolves=tallies,
    )
    case(
        "tombstoned top hits, acked ids, and the buffer's limits",
        [
            {"kind": "miss_ack", "event_id": "e3"},
            miss("e1", top="a"),
            miss("e2", top="b"),
            miss("e3", top="c"),
            miss("e4", top=None, top_hit_ids=["a"]),
            miss("e5", top="a", session_id=None),
            miss("e6", top="d", session_id=None, session="s2"),
            miss(5, top="b"),
            miss("e7", top_hits=["a"]),
            miss("e8", top_hits=[{"id": 5}], recent_retrieval_count=True),
            miss("e9", top_hits=[], top_hit_ids=["b"], recent_retrieval_count=-3),
            miss("e10", ts=5),
        ],
        resolves=[
            ([], -1),
            ([], 0),
            ([], 1),
            ([], 2),
            ([], 20),
            (["a"], 20),
            (["a", "b"], 1),
            (["c", "z"], 3),
        ],
    )
    hooks = [
        {"kind": "use", "outcome": "ignored", "attribution": "hook", "ts": T(1)},
        {"kind": "use", "triggered_from": "stop_hook"},
        {"kind": "turn_audited", "triggered_from": "stop_hook", "ts": "bad"},
        {"kind": "turn_audited", "attribution": "hook"},
        {"kind": "search", "triggered_from": "stop_hook"},
    ]
    case(
        "hook telemetry is counted over the whole log",
        hooks,
        cutoff="2026-10-03T00:00:00+00:00",
    )
    cold: list[dict[str, Any]] = [
        dict(HOOK),
        *[
            {"kind": "search", "returned": ["a", "b", "c", "d", "e", "f"]}
            for _ in range(3)
        ],
        {"kind": "search", "returned": ["g", "g", "a"]},
        {"kind": "list", "returned": ["c", "c", "c"]},
        {"kind": "show", "id": "c"},
        use(["a", "b", "c", "d", "e", "f", "g"], auto=True),
        use(["c"], attribution="hook"),
        use(["d", "d"], auto=True),
        use(["f"], auto=True, surface="brief"),
        use(["f"], surface="recall"),
        use(["f"], auto=True),
        use(["f"], auto=True),
    ]
    colds = [
        (0, 0.0),
        (1, 0.0),
        (2, 0.0),
        (3, 0.0),
        (4, 0.0),
        (-5, 0.0),
        (3, 0.25),
        (3, 0.2),
        (3, 0.5),
        (3, -1.0),
        (3, float("nan")),
        (3, float("inf")),
    ]
    case("cold rows at the floor and the ratio threshold", cold, colds=colds)
    case(
        "cold rows suppressed without hook telemetry",
        [
            ev
            for ev in (dict(e) for e in cold)
            if ev.get("kind") != "turn_audited" and ev.get("attribution") != "hook"
        ],
        colds=colds[:2],
    )
    case(
        "cold rows ties sort by id in code point order",
        [
            dict(HOOK),
            {"kind": "search", "returned": ["\U0001f600", "\uff5e", "g", "a", "f"]},
            {"kind": "search", "returned": ["f"]},
            use(["\U0001f600", "\uff5e", "g", "a", "f"], auto=True),
        ],
        colds=[(1, 0.0), (2, 0.0)],
    )
    case(
        "the scope gates the cold counts",
        [
            dict(HOOK),
            {"kind": "search", "returned": ["a", "c"]},
            use(["a", "c"], auto=True),
        ],
        reject=("c",),
    )


# ---------------------------------------------------------------------------
# the sweep
# ---------------------------------------------------------------------------

S_KINDS = [
    "search",
    "list",
    "show",
    "use",
    "use",
    "turn_audited",
    "search_miss",
    "silent_miss_cutoff",
    "miss_ack",
    "review_issued",
    "write",
    "session_brief",
    "other",
]
S_IDS = ["a", "b", "c", "d", "e"]
S_STAMPS = [
    "2026-10-01T00:00:00Z",
    "2026-10-04T12:00:00+00:00",
    "2026-10-06T01:00:00+02:00",
    "2026-10-08T00:00:00",
    "2026-10-12T00:00:00.500000Z",
    "bad",
]
S_BOUNDS = [
    None,
    None,
    "2026-10-03T00:00:00+00:00",
    "2026-10-06T00:00:00+00:00",
    "2026-10-09T00:00:00+00:00",
]
S_EXCERPTS: list[Any] = [None, "", " ", "x", 5]
S_MODELS = ["m1", "m2", ""]
S_RULES = ["r1", "r2", ""]
S_ACKS = ["e0", "e1", "e2", "e3", ""]
S_LIMITS = [-1, 0, 1, 2, 20]
S_RATIOS = [0.0, 0.5, 1.0]
SWEEP_MEMORIES: list[Mem] = [
    ("a", "a", ["global"], "Alpha. Beta.", "fact"),
    ("b", "b", ["global"], "Ambient one.", "ambient"),
    ("c", "c", ["repo:x"], "No category here", None),
    ("d", "d", ["global", "repo:x"], "User. Inference.", "user-inference"),
]


def _ids(d: _Draw, n: int) -> list[str]:
    return [S_IDS[d.pick(5)] for _ in range(d.pick(n))]


def draw_event(d: _Draw) -> Any:
    """One event of the sweep; the native test draws it by the same steps."""
    if d.pick(25) == 0:
        return [None, "s", 3, [1]][d.pick(4)]
    kind = S_KINDS[d.pick(len(S_KINDS))]
    ev: dict[str, Any] = {"kind": kind}
    s = d.pick(len(S_STAMPS) + 1)
    if s < len(S_STAMPS):
        ev["ts"] = S_STAMPS[s]
    if kind in ("search", "list"):
        ev[["returned", "memory_ids", "hit_ids"][d.pick(3)]] = _ids(d, 4)
        if d.pick(4) == 0:
            ev["returned"] = []
    elif kind == "show":
        ev["id"] = S_IDS[d.pick(5)]
    elif kind == "use":
        ev["outcome"] = "applied" if d.pick(4) else "ignored"
        a = d.pick(3)
        if a == 0:
            ev["auto"] = True
        elif a == 1:
            ev["auto"] = 1
        t = d.pick(3)
        if t == 0:
            ev["attribution"] = "hook"
        elif t == 1:
            ev["attribution"] = "model"
        if d.pick(3) == 0:
            ev["triggered_from"] = "stop_hook"
        sf = d.pick(4)
        if sf == 0:
            ev["surface"] = "brief"
        elif sf == 1:
            ev["surface"] = "recall"
        ev["ids" if d.pick(3) else "memory_ids"] = _ids(d, 5)
        if d.pick(3) == 0:
            ev["claim_excerpts"] = [S_EXCERPTS[d.pick(5)] for _ in range(d.pick(6))]
    elif kind in ("turn_audited", "search_miss"):
        if kind == "turn_audited":
            if d.pick(5) == 0:
                ev["repeat"] = True
            if d.pick(3) == 0:
                ev["verdict"] = "no_signal"
            if d.pick(2) == 0:
                ev["triggered_from"] = "stop_hook"
        else:
            if d.pick(4):
                ev["session_id"] = "s1"
            if d.pick(3):
                ev["top_hits"] = [{"id": S_IDS[d.pick(5)], "relevance": "high"}]
            ev["event_id"] = S_ACKS[d.pick(4)]
            ev["recent_retrieval_count"] = d.pick(3)
        m = d.pick(4)
        if m < 3:
            ev["client_model"] = S_MODELS[m]
        r = d.pick(4)
        if r < 3:
            ev["threshold_rule"] = S_RULES[r]
    elif kind == "silent_miss_cutoff":
        ev["cutoff_ts"] = S_STAMPS[d.pick(len(S_STAMPS))]
    elif kind == "miss_ack":
        ev["event_id"] = S_ACKS[d.pick(5)]
    elif kind in ("review_issued", "write"):
        if d.pick(2):
            ev["review_id"] = "r"
    elif kind == "session_brief":
        ev["ids"] = _ids(d, 3)
    return ev


def draw_log(d: _Draw) -> dict[str, Any]:
    """One log of the sweep and its parameters."""
    events = [draw_event(d) for _ in range(d.pick(30))]
    cutoff = S_BOUNDS[d.pick(len(S_BOUNDS))]
    end = S_BOUNDS[d.pick(len(S_BOUNDS))]
    reject = {i for i in S_IDS if d.pick(4) == 0}
    tombstoned = [i for i in S_IDS if d.pick(3) == 0]
    limit = S_LIMITS[d.pick(5)]
    floor = d.pick(3)
    ratio = S_RATIOS[d.pick(3)]
    return {
        "events": events,
        "cutoff": cutoff,
        "end": end,
        "reject": reject,
        "resolves": [(tombstoned, limit)],
        "colds": [(floor, ratio)],
    }


def sweep_digest() -> str:
    d = _Draw(SWEEP_SEED)
    memories = by_id(SWEEP_MEMORIES)
    digest = hashlib.sha256()
    for _ in range(SWEEP_COUNT):
        log = draw_log(d)
        out = answer(memories=memories, **log)
        digest.update((dumps_line(out) + "\n").encode("ascii"))
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# the files
# ---------------------------------------------------------------------------


def _rows(cases: list[dict[str, Any]], memories: list[Mem]) -> list[dict[str, Any]]:
    table = by_id(memories)
    rows = []
    for c in cases:
        out = answer(
            c["events"],
            c["cutoff"],
            c["end"],
            set(c["reject"]),
            c["resolves"],
            c["colds"],
            table,
        )
        rows.append(
            {
                "k": "log",
                "name": c["name"],
                "events": c["events"],
                "cutoff": c["cutoff"],
                "end": c["end"],
                "reject": c["reject"],
                **out,
            }
        )
    return rows


def _header(fixture: str, count: int, memories: list[Mem]) -> dict[str, Any]:
    return {
        "fixture": fixture,
        "count": count,
        "memories": [list(m) for m in memories],
    }


def generate() -> dict[str, bytes]:
    _CASES.clear()
    _cases()
    rows = _rows(_CASES, MEMORIES)
    sweep = {
        "k": "sweep",
        "seed": SWEEP_SEED,
        "count": SWEEP_COUNT,
        "memories": [list(m) for m in SWEEP_MEMORIES],
        "sha256": sweep_digest(),
    }
    header = _header("eval_rates", len(rows), MEMORIES)
    return {"eval_rates.jsonl": jsonl([header, *rows, sweep])}


# ---------------------------------------------------------------------------
# past ASCII: eval_rates_unicode.jsonl
# ---------------------------------------------------------------------------

# Whitespace to `str.isspace` past ASCII (no-break space, NEL, ogham
# space mark, ideographic space, line and paragraph separators), and
# characters that are not (zero-width space, U+180E since Unicode 6.3,
# the byte-order mark, a combining acute).
_SPACES = ["\u00a0", "\x85", "\u1680", "\u3000", "\u2028", "\u2029", "\u202f"]
_NOT_SPACES = ["\u200b", "\u180e", "\ufeff", "\u0301"]

UNICODE_MEMORIES: list[Mem] = [
    ("a", "a", ["global"], "\u3000R\u00e9sum\u00e9 is ready.\u3000Next part.", "fact"),
    ("b", "b", ["global"], "\u00a0Stop!\u2029Then more", "fact"),
    ("c", "c", ["global"], "Ends here.\u200bNot a break. Done.", "fact"),
    ("d", "d", ["global"], "\u8a18\u61b6 " * 30, "fact"),
    ("e", "e", ["global"], "Line one.\x85line two", "fact"),
    ("\u00e9", "\u00e9", ["\u00e9t\u00e9"], "\u180eLeading U+180E.", "fact"),
]


def _unicode_cases() -> list[dict[str, Any]]:
    _CASES.clear()
    for ch in [*_SPACES, *_NOT_SPACES]:
        case(
            f"an excerpt of U+{ord(ch):04X} alone",
            [
                use(["a"], claim_excerpts=[ch]),
                use(["b"], claim_excerpts=[f"{ch}x{ch}"]),
            ],
        )
    case(
        "an excerpt of whitespace past ASCII is upgraded",
        [
            use(["a", "a"], claim_excerpts=["\u3000\u00a0", "x"]),
            use(["b", "b"], claim_excerpts=["\u200b", "\u3000"]),
        ],
    )
    case(
        "ids and models past ASCII",
        [
            dict(HOOK),
            {"kind": "search", "returned": ["\u00e9", "a", "\u00e9"]},
            use(["\u00e9", "\u00e9"], auto=True, claim_excerpts=["\u3000", "\u00e9"]),
            audit(client_model="\u00e9"),
            miss("\u00e91", client_model="\u8a18"),
            miss("\u00e92", client_model="\u8a18", threshold_rule="\u00e9"),
            {"kind": "miss_ack", "event_id": "\u00e91"},
        ],
        reject=("a",),
    )
    case(
        "summaries of bodies past ASCII",
        [
            dict(HOOK),
            {"kind": "search", "returned": ["a", "b", "c", "d", "e", "\u00e9"]},
            use(["a", "b", "c", "d", "e", "\u00e9"], auto=True),
        ],
    )
    return list(_CASES)


def generate_unicode() -> dict[str, bytes]:
    """What reads the Unicode database: `str.strip` and the summary line
    past ASCII."""
    rows = _rows(_unicode_cases(), UNICODE_MEMORIES)
    header = _header("eval_rates_unicode", len(rows), UNICODE_MEMORIES)
    return {"eval_rates_unicode.jsonl": jsonl([header, *rows])}

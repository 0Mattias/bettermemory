"""Fixtures: the review's answers (``review_answers.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/review/answers.rs``. This pins
`review`'s `parse_answers` and `parse_update_answers` (through them
`_score` and `_chain_keys`), every teaching error they raise,
`ReviewAnswers.to_event` and `is_zero_attestation`, `check_answered_ids`
and `check_named`, and the formats `ANSWER_FORMAT`, `NAMED_FORMAT` and
`UPDATE_ANSWER_FORMAT`.

A row is ``{"k": <kind>, "raw": <the review argument as JSON text, or
null for None>, "r": <answer>}``. Kind ``p`` is `parse_answers`, ``u``
`parse_update_answers`. An answer is null (no answers), ``{"e": text}``
(a `ReviewAnswerError`), ``{"x": class}`` (another exception), or the
answers: ``ev`` (`json.dumps(to_event())`), ``rid`` (the review_id) and
``z`` (`is_zero_attestation()`). Kind ``ids`` reads ``raw`` with
`parse_answers` and checks it against ``allowed``; kind ``named`` reads
it with the reader ``u`` names and calls `check_named` with ``roster``,
``shown`` and ``req``, answering the named ids or the error.

The sweep row holds the rule (`_Lcg` with its seed and the fragment
tables), the count and the SHA-256 of the answers in order, each
spelled by `_spell` and ended by a newline.

Every text is ASCII or an escaped surrogate, so no row follows the
Unicode data or the Python release: the whitespace `said` folds is
ASCII here (past ASCII it is `str.isspace`, the Unicode data's), and
no int reaches `int`'s 4,300 digit limit. No module global is patched.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from bettermemory import review

from ._common import jsonl

ULID = "01J00000000000000000000000"
SEED = 20261122
COUNT = 3000

IDS = ['"a"', '"b"', '"a"', f'"{ULID}"']
LABS = ['"update"', '"extends"', '"conflict"', '"restates"', '"unrelated"', '"x"', "1"]
SCORES = [
    None,
    "0",
    "1",
    "0.5",
    "0.3",
    "0.30000000000000004",
    "-0.0",
    "-1",
    "2",
    "1e-320",
    "true",
    '"0.5"',
    "null",
    "1.0000000000000002",
    "1e999",
    "0.0",
]
NAMED = [None, "[]", '["a", "a", "b"]', '["a", 1]', '"a"']
SAID = [None, '" x \\t y "', '"  "', "1", '"w"']
RIDS = [None, f'"{ULID}"', '"bad"', "7"]
EXTRA = [None, '"zz": 1', '"said": "s"']


class _Lcg:
    """The sweep's numbers: a 64-bit LCG, its high 31 bits."""

    def __init__(self, seed: int) -> None:
        self.x = seed

    def next(self) -> int:
        self.x = (self.x * 6364136223846793005 + 1442695040888963407) % 2**64
        return self.x >> 33

    def pick(self, items: list[Any]) -> Any:
        return items[self.next() % len(items)]


def _sweep_case(g: _Lcg) -> tuple[str, str]:
    kind = "p" if g.next() % 2 == 0 else "u"
    parts = []
    rel = [f"{g.pick(IDS)}: {g.pick(LABS)}" for _ in range(g.next() % 4)]
    parts.append('"relation": {' + ", ".join(rel) + "}")
    for key, table in (
        ("unsafe", SCORES),
        ("secret", SCORES),
        ("standing", SCORES),
        ("named", NAMED),
        ("said", SAID),
        ("review_id", RIDS),
    ):
        v = g.pick(table)
        if v is not None:
            parts.append(f'"{key}": {v}')
    extra = g.pick(EXTRA)
    if extra is not None:
        parts.append(extra)
    return kind, "{" + ", ".join(parts) + "}"


def _read(kind: str, raw: Any) -> Any:
    reader = review.parse_answers if kind == "p" else review.parse_update_answers
    return reader(raw)


def _answer(kind: str, text: str | None) -> Any:
    raw = None if text is None else json.loads(text)
    try:
        got = _read(kind, raw)
    except review.ReviewAnswerError as e:
        return {"e": str(e)}
    except Exception as e:  # noqa: BLE001 - the class is the answer
        return {"x": type(e).__name__}
    if got is None:
        return None
    return {
        "ev": json.dumps(got.to_event()),
        "rid": got.review_id,
        "z": got.is_zero_attestation(),
    }


def _spell(r: Any) -> str:
    if r is None:
        return "N"
    if "e" in r:
        return "E" + r["e"]
    if "x" in r:
        return "X" + r["x"]
    return f"O{r['ev']}|{r['rid']}|{r['z']}"


def _check(row: dict[str, Any]) -> Any:
    raw = json.loads(row["raw"])
    try:
        got = _read("u" if row.get("u") else "p", raw)
        assert got is not None
        if row["k"] == "ids":
            review.check_answered_ids(got, row["allowed"])
            return None
        return list(
            review.check_named(
                got, set(row["roster"]), shown=set(row["shown"]), required=row["req"]
            )
        )
    except review.ReviewAnswerError as e:
        return {"e": str(e)}


_P = [
    None,
    "{}",
    "[]",
    "[1]",
    '"x"',
    "0",
    "null",
    '{"zz": 1}',
    '{"zz": 1, "aa": 2, "unsafe": 0}',
    '{"unsafe": 0, "secret": 0}',
    '{"unsafe": 0, "secret": 0, "standing": 0}',
    '{"unsafe": 0.1, "secret": 0.2, "standing": 1}',
    '{"unsafe": 1.0, "secret": 0, "standing": 0.5, "relation": null}',
    '{"unsafe": true, "secret": 0, "standing": 0}',
    '{"unsafe": false, "secret": 0, "standing": 0}',
    '{"unsafe": "0", "secret": 0, "standing": 0}',
    '{"unsafe": null, "secret": 0, "standing": 0}',
    '{"unsafe": [], "secret": 0, "standing": 0}',
    '{"unsafe": {}, "secret": 0, "standing": 0}',
    '{"unsafe": 0, "secret": -1, "standing": 0}',
    '{"unsafe": 0, "secret": 1.5, "standing": 0}',
    '{"unsafe": 0, "secret": 0, "standing": 2}',
    '{"unsafe": 0, "secret": 0, "standing": -0.0}',
    '{"unsafe": 0, "secret": 0, "standing": -1e-300}',
    '{"unsafe": 0, "secret": 0, "standing": 1.0000000000000002}',
    '{"unsafe": 0, "secret": 0, "standing": 1e999}',
    '{"unsafe": 0, "secret": 0, "standing": -1e999}',
    '{"unsafe": 0, "secret": 0, "standing": NaN}',
    '{"unsafe": 0, "secret": 0, "standing": Infinity}',
    '{"unsafe": 0, "secret": 0, "standing": 100000000000000000000}',
    '{"unsafe": 0, "secret": 0, "standing": -100000000000000000000}',
    '{"unsafe": 0, "secret": 0, "standing": 1' + "0" * 400 + "}",
    '{"unsafe": 0, "secret": 0, "standing": 1' + "0" * 308 + "}",
    '{"unsafe": 0, "secret": 0, "standing": 0.3, "said": "  a \\t b\\n c  "}',
    '{"unsafe": 0, "secret": 0, "standing": 0.3, "said": "   "}',
    '{"unsafe": 0, "secret": 0, "standing": 0.3, "said": ""}',
    '{"unsafe": 0, "secret": 0, "standing": 0.3, "said": 5}',
    '{"unsafe": 0, "secret": 0, "standing": 0.3, "said": null}',
    '{"unsafe": 0, "secret": 0, "standing": 0.3, "said": "a\\u001cb\\u001fc\\u000bd"}',
    '{"said": 5}',
    '{"said": "x", "unsafe": 0}',
    f'{{"review_id": "{ULID}", "unsafe": 0, "secret": 0, "standing": 0}}',
    '{"review_id": null, "unsafe": 0, "secret": 0, "standing": 0}',
    '{"review_id": "01J0000000000000000000000I", "unsafe": 0}',
    '{"review_id": "01j00000000000000000000000", "unsafe": 0}',
    '{"review_id": "01J0000000000000000000000", "unsafe": 0}',
    '{"review_id": 5, "unsafe": 0}',
    '{"review_id": ["a"], "unsafe": 0}',
    '{"review_id": "\\ud800", "unsafe": 0}',
    '{"review_id": "", "unsafe": 0}',
    '{"relation": [], "unsafe": 0}',
    '{"relation": "x", "unsafe": 0}',
    '{"relation": {"a": 1}, "unsafe": 0}',
    '{"relation": {"a": "nope"}, "unsafe": 0}',
    '{"relation": {"it\'s": "Update"}, "unsafe": 0}',
    '{"relation": {"\\ud800": "\\udfff"}, "unsafe": 0}',
    '{"relation": {"a": "update", "b": "x"}, "unsafe": 0}',
    '{"relation": {"a": "unrelated", "b": "unrelated"}, "unsafe": 0, "secret": 0, '
    '"standing": 0.9}',
    '{"relation": {"a": "unrelated", "b": "restates"}, "unsafe": 0, "secret": 0, '
    '"standing": 0}',
    '{"relation": {"a": "update", "a": "unrelated", "b": "extends"}, "unsafe": 0, '
    '"secret": 0, "standing": 0}',
    '{"relation": {"a": "conflict"}, "unsafe": 0, "secret": 0.0, "standing": 0}',
    '{"relation": {}, "unsafe": 0, "secret": 0.5, "standing": 0}',
    '{"named": "a", "unsafe": 0}',
    '{"named": ["a", 1], "unsafe": 0}',
    '{"named": {"a": 1}, "unsafe": 0}',
    '{"named": [], "unsafe": 0, "secret": 0, "standing": 0}',
    '{"named": ["b", "a", "b", "c", "a"], "unsafe": 0, "secret": 0, "standing": 0}',
    '{"named": null, "unsafe": 0, "secret": 0, "standing": 0}',
    '{"unsafe": 0, "secret": 0, "standing": 0, "relation": {"a": "update"}, '
    '"named": ["a"], "said": "go", "review_id": "' + ULID + '"}',
    '{"relation": {"a": "nope"}, "review_id": 5}',
    '{"named": 5, "relation": {"a": "nope"}}',
    '{"relation": {"a": "x"}, "said": 5}',
    '{"unsafe": "x", "said": 5}',
]

_U = [
    None,
    "{}",
    "[]",
    "1",
    '{"unsafe": 0}',
    '{"said": "x", "standing": 0, "zz": 1}',
    '{"relation": {"a": "update"}}',
    '{"relation": {"a": "unrelated"}}',
    '{"relation": null, "named": null, "review_id": null}',
    '{"named": ["a", "b", "a"]}',
    '{"named": [1]}',
    '{"review_id": "bad"}',
    '{"relation": {"a": "bogus"}}',
    f'{{"review_id": "{ULID}", "relation": {{"b": "conflict"}}, "named": ["b"]}}',
]

_CHECKS: list[dict[str, Any]] = [
    {"k": "ids", "raw": '{"unsafe": 0, "secret": 0, "standing": 0}', "allowed": []},
    {
        "k": "ids",
        "raw": '{"relation": {"a": "update", "b": "unrelated"}, "unsafe": 0, '
        '"secret": 0, "standing": 0}',
        "allowed": ["a", "b"],
    },
    {
        "k": "ids",
        "raw": '{"relation": {"c": "update", "b": "unrelated", "a": "extends"}, '
        '"unsafe": 0, "secret": 0, "standing": 0}',
        "allowed": ["b"],
    },
    {
        "k": "ids",
        "raw": '{"relation": {"\\ud800": "update"}, "unsafe": 0, "secret": 0, '
        '"standing": 0}',
        "allowed": [],
    },
    {
        "k": "named",
        "raw": '{"unsafe": 0}',
        "u": True,
        "roster": [],
        "shown": [],
        "req": True,
    },
    {
        "k": "named",
        "raw": '{"relation": {}}',
        "u": True,
        "roster": [],
        "shown": [],
        "req": False,
    },
    {
        "k": "named",
        "raw": '{"named": ["b", "a", "b"]}',
        "u": True,
        "roster": ["a", "b"],
        "shown": [],
        "req": True,
    },
    {
        "k": "named",
        "raw": '{"named": ["z", "a", "y"]}',
        "u": True,
        "roster": ["a"],
        "shown": [],
        "req": True,
    },
    {
        "k": "named",
        "raw": '{"named": ["a"], "relation": {"c": "update", "b": "update", "a": "update", '
        '"s": "update", "o": "update"}}',
        "u": True,
        "roster": ["a", "b", "c", "s"],
        "shown": ["s"],
        "req": True,
    },
    {
        "k": "named",
        "raw": '{"relation": {"b": "update"}}',
        "u": True,
        "roster": ["b"],
        "shown": [],
        "req": False,
    },
    {
        "k": "named",
        "raw": '{"named": ["a"], "relation": {"b": "update"}, "unsafe": 0, "secret": 0, '
        '"standing": 0}',
        "roster": ["a", "b"],
        "shown": [],
        "req": True,
    },
    {
        "k": "named",
        "raw": '{"named": [], "relation": {"b": "update"}, "unsafe": 0, "secret": 0, '
        '"standing": 0}',
        "roster": ["a"],
        "shown": [],
        "req": True,
    },
]


def generate() -> dict[str, bytes]:
    rows: list[Any] = [
        {
            "k": "formats",
            "answer": review.ANSWER_FORMAT,
            "named": review.NAMED_FORMAT,
            "update": review.UPDATE_ANSWER_FORMAT,
        }
    ]
    for kind, texts in (("p", _P), ("u", _U)):
        rows.extend({"k": kind, "raw": t, "r": _answer(kind, t)} for t in texts)
    for row in _CHECKS:
        rows.append({**row, "r": _check(row)})
    g = _Lcg(SEED)
    digest = hashlib.sha256()
    for _ in range(COUNT):
        kind, text = _sweep_case(g)
        digest.update(
            (_spell(_answer(kind, text)) + "\n").encode("utf-8", "surrogatepass")
        )
    rows.append(
        {
            "k": "sweep",
            "seed": SEED,
            "n": COUNT,
            "ids": IDS,
            "labs": LABS,
            "scores": SCORES,
            "named": NAMED,
            "said": SAID,
            "rids": RIDS,
            "extra": EXTRA,
            "sha256": digest.hexdigest(),
        }
    )
    return {"review_answers.jsonl": jsonl(rows)}

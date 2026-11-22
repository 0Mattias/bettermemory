"""Fixtures: the sealed tables of `review` and the text a related entry
shows (``review_tables.jsonl``, ``review_tables_unicode.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/review/``. This pins the constants
(`RELATED_TOP`, `RELATED_CAP`, `RELATED_TEXT_CHARS`, `RELATED_CONSTANTS`,
`ROSTER_TEXT_CHARS`, `TAU_UNSAFE`, `TAU_SECRET`, the labels and
`LABEL_CHOICES`, the four statements, the action and detector names and
`REVIEW_LINK_NOTE`), `questions_payload` with `questions_sha256`,
`constants_sha256` and `seal_shas`, `current_tau` under `tau_override` and
`tau_unset`, `RelatedEntry.to_dict`, `RelatedSet` (`ids`,
`cue_supersedes`, `cue_conflicts`, `only`, `plus`), `RosterLine.to_line`,
`related_text`, `related_entry` and `_write_sentences`.

A row is ``{"k": <function>, ...inputs, "r": <answer>}``. A body is
spelled ``[[piece, times], ...]``, joined. ``related_entry`` reads a
`models.Memory` built from the row (``u``: `updated`'s ISO text); an
exception is ``{"e": <class>}``. The sweep row ``k: "sweep"`` holds the
rule (`_Lcg`, its seed and the vocabulary), the count and the SHA-256 of
the answers in order: per case the UTF-8 of `related_text` and of each
`_write_sentences` piece, each piece ended by 0x01, the case by 0x00.

The thresholds are read only inside a ``with`` block of `tau_override`
or `tau_unset`, so `TAU_UNSAFE` and `TAU_SECRET` are left as found. The
``_unicode`` file holds the rows whose answer follows the Unicode data:
``\\s`` and the tokenizer past ASCII.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from bettermemory import review
from bettermemory.models import Memory

from ._common import jsonl

_ID = "01J00000000000000000000000"


def _body(spec: list[list[Any]]) -> str:
    return "".join(piece * times for piece, times in spec)


def _header() -> dict[str, Any]:
    with review.tau_override(0.25) as tau:
        overridden = [tau.unsafe, tau.secret, list(review.current_tau() or [])]
    with review.tau_override(0.2, 0.7) as tau:
        both = [tau.unsafe, tau.secret, list(review.current_tau() or [])]
        with review.tau_unset():
            nested = review.current_tau()
        after_nested = list(review.current_tau() or [])
    with review.tau_unset():
        unset = review.current_tau()
    return {
        "k": "header",
        "related_top": review.RELATED_TOP,
        "related_cap": review.RELATED_CAP,
        "related_text_chars": review.RELATED_TEXT_CHARS,
        "related_constants": dict(review.RELATED_CONSTANTS),
        "roster_text_chars": review.ROSTER_TEXT_CHARS,
        "tau": [review.TAU_UNSAFE, review.TAU_SECRET],
        "tau_now": list(review.current_tau() or []),
        "tau_override_one": overridden,
        "tau_override_two": both,
        "tau_nested": [nested, after_nested],
        "tau_unset": unset,
        "labels": list(review.LABELS),
        "label_choices": review.LABEL_CHOICES,
        "statements": [
            review.UNSAFE_STATEMENT,
            review.SECRET_STATEMENT,
            review.STANDING_STATEMENT,
            review.ROSTER_STATEMENT,
        ],
        "actions": [
            review.ACTION_SECRET,
            review.ACTION_UNSAFE,
            review.ACTION_STANDING,
            review.ACTION_RESTATES,
            review.ACTION_COMMIT,
        ],
        "detectors": [review.DETECTOR_REVIEW, review.DETECTOR_REVIEW_DISAGREES],
        "link_note": review.REVIEW_LINK_NOTE,
        "payload": review.questions_payload(),
        "payload_json": review.canonical_json(review.QUESTIONS).decode("utf-8"),
        # The payload in the reference's key order, which the sorted
        # forms above lose.
        "payload_ordered": json.dumps(
            review.questions_payload(), separators=(",", ":")
        ),
        "constants_ordered": json.dumps(
            dict(review.RELATED_CONSTANTS), separators=(",", ":")
        ),
        "questions_sha256": review.questions_sha256(),
        "constants_sha256": review.constants_sha256(),
        "seal_shas": review.seal_shas(),
    }


def _entry(i: str, scopes: list[str], text: str) -> review.RelatedEntry:
    return review.RelatedEntry(
        id=i, scopes=tuple(scopes), updated="2026-01-02", claim_sized=False, text=text
    )


def _set_rows() -> list[dict[str, Any]]:
    a, b, c = (_entry(n, ["s"], n.upper()) for n in ("a", "b", "c"))
    rows: list[dict[str, Any]] = [
        {"k": "to_dict", "i": "x", "s": [], "r": _entry("x", [], "").to_dict()},
        {
            "k": "to_dict",
            "i": "y",
            "s": ["p", "q:r"],
            "r": _entry("y", ["p", "q:r"], "t\nu").to_dict(),
        },
    ]
    base = review.RelatedSet(entries=(a, b, c))
    rows.append(
        {
            "k": "set",
            "do": "only",
            "arg": ["c", "a", "z", "a"],
            "r": base.only(["c", "a", "z", "a"]).ids,
        }
    )
    rows.append({"k": "set", "do": "only", "arg": [], "r": base.only([]).ids})
    rows.append(
        {
            "k": "set",
            "do": "plus",
            "arg": ["b", "d", "d", "e"],
            "r": base.plus(_entry(n, [], n) for n in ("b", "d", "d", "e")).ids,
        }
    )
    for line in (("x", "2026-01-02", "t"), ("", "", ""), ("a b", "d", "x (y) z")):
        rows.append(
            {"k": "line", "a": list(line), "r": review.RosterLine(*line).to_line()}
        )
    return rows


_LONG = [["word ", 130]]
_TEXT_CASES: list[tuple[str, list[list[Any]]]] = [
    ("anything", [["  short body \n", 1]]),
    ("anything", [["x", 600]]),
    ("anything", [["x", 601]]),
    ("anything", [[" ", 5], ["x", 600], ["\n", 3]]),
    ("ruff config", _LONG),
    ("ruff config", [*_LONG, ["The ruff config is strict. ", 1]]),
    ("ruff", [*_LONG, ["end. Ruff here.", 1]]),
    ("ruff", [*_LONG, ["end.Ruff here", 1]]),
    ("ruff", [*_LONG, ["end!?  Ruff here", 1]]),
    ("ruff", [*_LONG, ["end?!.\t\tRuff here", 1]]),
    ("ruff", [*_LONG, ["no break\n\n\nruff on a line", 1]]),
    ("ruff", [*_LONG, ["a.\nruff", 1]]),
    ("ruff", [["ruff starts it. ", 1], *_LONG]),
    ("ruff", [*_LONG, ["... ruff", 1]]),
    ("ruff", [*_LONG, ["x. ruff-config here", 1]]),
    ("ruff-config", [*_LONG, ["x. ruff config here", 1]]),
    ("ruff-config", [*_LONG, ["x. the ruff-config here", 1]]),
    ("the of and", [*_LONG, ["x. the of and", 1]]),
    ("word", [["one. ", 1], *_LONG]),
    ("deep", [["a" * 700, 1], [". deep", 1]]),
    ("deep", [["a. ", 300], ["deep", 1]]),
    ("tail", [*_LONG, ["x. ", 1], ["tail " * 200, 1]]),
    ("", [*_LONG]),
]

_SENTENCE_CASES = [
    "",
    "   ",
    "One. Two! Three? four",
    "a.b c. the of\n\nruff is set.  ruff is set.\nruff is set.",
    "x...  y!?z\n",
    "the. of. and.",
    "No break here at all",
    ".\n.\n",
]

_UNICODE_TEXT: list[tuple[str, list[list[Any]]]] = [
    ("café", [*_LONG, ["end.　café here", 1]]),
    ("café", [*_LONG, ["end.  café here", 1]]),
    ("ruff", [*_LONG, ["end.\x1cruff here", 1]]),
    ("ruff", [*_LONG, ["end. ruff here", 1]]),
    ("λόγος", [*_LONG, ["x. Λόγος y", 1]]),
    ("ruff", [*_LONG, ["é" * 10, 1], ["　ruff", 1]]),
    ("naïve", [["été ", 150], ["x. naïve", 1]]),
]

_UNICODE_SENTENCES = [
    "Un.　Deux. trois\u0085quatre",
    "café. café.\x1fcafé",
    "été   λόγος",
]


def _text_rows(cases: list[tuple[str, list[list[Any]]]]) -> list[dict[str, Any]]:
    return [
        {"k": "text", "c": c, "b": b, "r": review.related_text(c, _body(b))}
        for c, b in cases
    ]


def _sentence_rows(cases: list[str]) -> list[dict[str, Any]]:
    return [{"k": "sent", "c": c, "r": review._write_sentences(c)} for c in cases]


_ENTRY_CASES: list[tuple[str, list[str], list[list[Any]], str]] = [
    ("ruff", ["global"], [["Ruff is the linter.", 1]], "2026-03-04T05:06:07+00:00"),
    ("ruff", ["a", "b:c"], [*_LONG, ["x. ruff", 1]], "2026-03-04T23:30:00-05:00"),
    ("x", ["p"], [["One. Two. Three. Four.", 1]], "2026-03-05T01:00:00+05:00"),
    ("x", ["p"], [[" ", 1]], "9999-12-31T23:59:59-00:01"),
    ("x", ["p"], [["one claim", 1]], "0001-01-01T00:00:00+01:00"),
    ("x", ["p"], [["one claim", 1]], "0001-01-01T00:59:00+00:59"),
]


def _entry_rows() -> list[dict[str, Any]]:
    rows = []
    for content, scopes, spec, updated in _ENTRY_CASES:
        memory = Memory(
            id=_ID,
            created=datetime.fromisoformat("2026-01-01T00:00:00+00:00"),
            updated=datetime.fromisoformat(updated),
            scopes=scopes,
            confidence="high",
            source="explicit-statement",
            body=_body(spec),
        )
        try:
            r: Any = review.related_entry(content, memory).to_dict()
        except OverflowError as exc:
            r = {"e": type(exc).__name__}
        rows.append(
            {"k": "entry", "c": content, "s": scopes, "b": spec, "u": updated, "r": r}
        )
    return rows


class _Lcg:
    """Knuth's MMIX generator; `draw(n)` is the high 31 bits mod n."""

    def __init__(self, seed: int) -> None:
        self.x = seed

    def draw(self, n: int) -> int:
        self.x = (self.x * 6364136223846793005 + 1442695040888963407) % 2**64
        return (self.x >> 33) % n


_SEED = 20261122
_COUNT = 2000
_WORDS = ["ruff", "config", "the", "of", "alpha", "beta-gamma", "x", "deep", "7"]
_SEPS = [" ", " ", " ", ". ", "! ", "? ", "\n", "\n\n", "...", ".\t", "?!  ", "."]


def _sweep() -> dict[str, Any]:
    rng = _Lcg(_SEED)
    h = hashlib.sha256()
    for _ in range(_COUNT):
        content = " ".join(
            _WORDS[rng.draw(len(_WORDS))] for _ in range(1 + rng.draw(4))
        )
        parts = []
        for _ in range(1 + rng.draw(260)):
            parts.append(_WORDS[rng.draw(len(_WORDS))])
            parts.append(_SEPS[rng.draw(len(_SEPS))])
        body = "".join(parts)
        h.update(review.related_text(content, body).encode("utf-8"))
        for piece in review._write_sentences(body):
            h.update(piece.encode("utf-8") + b"\x01")
        h.update(b"\x00")
    return {
        "k": "sweep",
        "seed": _SEED,
        "count": _COUNT,
        "words": _WORDS,
        "seps": _SEPS,
        "sha256": h.hexdigest(),
    }


def generate() -> dict[str, bytes]:
    rows = [
        _header(),
        *_set_rows(),
        *_text_rows(_TEXT_CASES),
        *_sentence_rows(_SENTENCE_CASES),
        *_entry_rows(),
        _sweep(),
    ]
    return {"review_tables.jsonl": jsonl(rows)}


def generate_unicode() -> dict[str, bytes]:
    rows = [*_text_rows(_UNICODE_TEXT), *_sentence_rows(_UNICODE_SENTENCES)]
    return {"review_tables_unicode.jsonl": jsonl(rows)}

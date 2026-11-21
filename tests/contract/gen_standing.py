"""Fixtures: the store-free half of `standing` (``standing.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/standing.rs``. This pins the
constants (`TAU_STANDING`, `SOURCES`, `BATCH`, `_CALL`, `_ANSWER_FORMAT`
and the three of `review` it reads: `RELATED_CAP`, `RELATED_TEXT_CHARS`,
`STANDING_STATEMENT`) in the header, and every branch of `asking`,
`is_standing`, `check_probability`, `answered_by`, `answer_of`,
`record_text`, `backlog_line`, `held_lines`, `held_line`, `question` and
`batch_hint`.

A row is ``{"k": <function>, ...inputs, "r": <answer>}``. An exception is
``{"e": <class>, "m": <message>}``; a float is `_common.neutral`'s form.
`answered_by` reads `identity.current_actor`, which a row sets (``a``:
null for no actor, else ``[client, model]``). `asking` reads
`review.current_tau`, which a row sets (``tau``). No row depends on the
platform or the Python release.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any
from unittest import mock

from bettermemory import identity, review, standing

from ._common import jsonl, neutral


def _err(exc: Exception) -> dict[str, Any]:
    return {"e": type(exc).__name__, "m": str(exc)}


def _actor(a: list[str | None] | None) -> identity.Actor | None:
    if a is None:
        return None
    return identity.Actor(client=a[0], model=a[1])


def _probability_values() -> list[Any]:
    big = 2**1024
    half = 2**970
    return [
        None,
        True,
        False,
        0,
        1,
        2,
        -1,
        10**30,
        big,
        -big,
        big - half,
        big - half - 1,
        -(big - half),
        -(big - half - 1),
        0.0,
        -0.0,
        0.3,
        0.29999999999999993,
        1.0,
        1.0000000000000002,
        -5e-324,
        5e-324,
        float("nan"),
        float("inf"),
        float("-inf"),
        1e308,
        "0.5",
        "",
        [0.5],
        (0.5,),
        {"p": 0.5},
        datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        PurePosixPath("/a"),
    ]


def _ids(n: int) -> list[str]:
    return [f"01J{i:023d}" for i in range(n)]


def _cases() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for tau in (None, (0.5, 0.5)):
        with mock.patch.object(standing, "current_tau", lambda tau=tau: tau):
            out.append({"k": "asking", "tau": tau is not None, "r": standing.asking()})
    for cat in (None, "ambient", "fact", "user-inference", "Ambient", "ambient "):
        for p in (None, 0.0, 0.29999999999999993, 0.3, 0.30000000000000004, 1.0, 2.0):
            out.append(
                {
                    "k": "is",
                    "c": cat,
                    "p": None if p is None else neutral(p),
                    "r": standing.is_standing(cat, p),
                }
            )
    for name in ("standing", "probability", "xé"):
        values = _probability_values() if name == "standing" else [True, 2, 0.5]
        for value in values:
            try:
                r: Any = neutral(standing.check_probability(value, name))
            except (ValueError, OverflowError) as exc:
                r = _err(exc)
            out.append({"k": "cp", "v": neutral(value), "n": name, "r": r})
    actors: list[list[str | None] | None] = [
        None,
        [None, None],
        ["claude-code", None],
        [None, "opus"],
        ["", ""],
        ["cé", "m \U0001f600"],
    ]
    for a in actors:
        with mock.patch.object(identity, "current_actor", lambda a=a: _actor(a)):
            out.append({"k": "by", "a": a, "r": standing.answered_by()})
    sources = ["review", "update", "asked", "Review", "", "x'y", "é\n"]
    for source in sources:
        tried: list[Any] = [0.5, 1, True, "a", 2**1024, 1.5]
        givers: list[list[str | None] | None] = [None, ["cc", "m"]]
        for value in tried:
            for a in givers:
                with mock.patch.object(
                    identity, "current_actor", lambda a=a: _actor(a)
                ):
                    try:
                        ans = standing.answer_of(value, source)
                        r = {
                            "p": neutral(ans.probability),
                            "s": ans.source,
                            "b": ans.answered_by,
                        }
                    except (ValueError, OverflowError) as exc:
                        r = _err(exc)
                out.append(
                    {"k": "ans", "v": neutral(value), "s": source, "a": a, "r": r}
                )
    for body in (
        "",
        "   ",
        "  short body \n",
        "x" * 599,
        "x" * 600,
        "x" * 601,
        " \t" + "y" * 600 + "\n ",
        ("word " * 130).strip(),
        "é" * 601,
        "a." + " b" * 400,
        "　lead and trail　",
    ):
        out.append({"k": "rt", "b": body, "r": standing.record_text(body)})
    for count in (-1, 0, 1, 2, 11, 12, 10**20):
        out.append({"k": "bl", "n": str(count), "r": standing.backlog_line(count)})
    for n in (0, 1, 11, 12):
        ids = _ids(n)
        out.append({"k": "hls", "ids": ids, "r": standing.held_lines(ids)})
        out.append({"k": "hl", "ids": ids, "r": standing.held_line(ids)})
    odd = ["é", "a, b", ""]
    out.append({"k": "hls", "ids": odd, "r": standing.held_lines(odd)})
    out.append({"k": "hl", "ids": odd, "r": standing.held_line(odd)})
    for rid in ("01JABC", "", 'q"x', "é"):
        q = standing.question(rid)
        out.append({"k": "q", "id": rid, "r": [[k, v] for k, v in q.items()]})
    for remaining in (-1, 0, 1, 11, 12):
        out.append({"k": "bh", "n": remaining, "r": standing.batch_hint(remaining)})
    return out


def generate() -> dict[str, bytes]:
    """``standing.jsonl``: not gated."""
    cases = _cases()
    header = {
        "k": "header",
        "cases": len(cases),
        "tau": standing.TAU_STANDING,
        "sources": list(standing.SOURCES),
        "batch": standing.BATCH,
        "call": standing._CALL,
        "format": standing._ANSWER_FORMAT,
        "related_cap": review.RELATED_CAP,
        "related_text_chars": review.RELATED_TEXT_CHARS,
        "statement": review.STANDING_STATEMENT,
    }
    return {"standing.jsonl": jsonl([header, *cases])}

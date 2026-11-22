"""Fixtures: `ResponseBuilder`'s write and review shapes
(``response_write.jsonl``, not gated, and ``response_write_posix.jsonl``,
POSIX only).

The native build reads them in
``native/crates/bettermemory-core/src/response_write.rs``. This pins
`_response.ResponseBuilder.committed`, `review`, `roster`,
`similar_to_dict`, `add_similar`, `transient_to_dict`,
`credential_to_dict`, `origin_to_dict` and `actor_to_dict`.

One JSON object per line, keys sorted, ASCII. The first line is the
header: ``count`` the rows after it, ``shown`` `SIMILAR_HITS_SHOWN`, and in
the ungated file ``extras``, the eight optional arguments of `committed`
used by the digest row, each as a `neutral` value. Every other line has
``k`` the method, ``i`` its input and ``o`` the text ``json.dumps`` writes
for the answer with its defaults (insertion order, ASCII escapes), or
``e`` the exception class raised. Texts that may hold a surrogate go
through `text_field` or `neutral`; datetimes are ``isoformat()`` texts;
a `SimilarHit`'s ``similarity`` is a `neutral` float.

The ``committed_sweep`` row is a digest: for each mask from 0 to 255, bit
``b`` set passes the ``b``-th optional argument of ``EXTRA_ORDER`` from
the header and clear passes None; the answers' texts joined by newlines
are hashed with SHA-256.

``response_write_posix.jsonl`` holds the origins whose fields hold a
surrogate: `os.fsencode` encodes them with ``surrogateescape`` on POSIX
and with ``surrogatepass`` on Windows, so the bytes follow the system.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from bettermemory import _response
from bettermemory.durability import TransientMatch
from bettermemory.identity import Actor
from bettermemory.models import Category, Confidence, Memory, SimilarHit, Source
from bettermemory.origin_model import Origin

from ._common import jsonl, neutral, text_field

UTC = timezone.utc
ID = "01J0000000000000000000000A"
B = _response.ResponseBuilder(stale_after_days=30)

EXTRA_ORDER = (
    "related",
    "removed_related",
    "warnings",
    "supersedes",
    "conflicts_filed",
    "hint",
    "update_suggested",
    "extends",
)


def _iso(d: datetime | None) -> str | None:
    return None if d is None else d.isoformat()


def _answer(call: Any) -> dict[str, Any]:
    try:
        out = call()
    except (UnicodeEncodeError, TypeError) as exc:
        return {"e": type(exc).__name__}
    return {"o": json.dumps(out)}


def _hit(n: int = 0, **over: Any) -> SimilarHit:
    fields: dict[str, Any] = {
        "id": f"01J00000000000000000000{n:03d}",
        "scopes": ["global"],
        "confidence": Confidence.MEDIUM,
        "snippet": f"snippet {n}",
        "similarity": 0.5 + n / 100,
        "relevance": "medium",
        "created": datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC),
        "updated": datetime(2025, 1, 2, 3, 4, 5, 6, tzinfo=UTC),
        "removed_at": None,
        "removed_reason": None,
    }
    fields.update(over)
    return SimilarHit.model_construct(**fields)


def _spell_hit(h: SimilarHit) -> dict[str, Any]:
    return {
        "id": h.id,
        "scopes": h.scopes,
        "confidence": h.confidence.value,
        **text_field("snippet", h.snippet),
        "similarity": neutral(h.similarity),
        "relevance": h.relevance,
        "created": _iso(h.created),
        "updated": _iso(h.updated),
        "removed_at": _iso(h.removed_at),
        "removed_reason": None
        if h.removed_reason is None
        else text_field("v", h.removed_reason),
    }


def _actor(spec: dict[str, Any] | None) -> Actor | None:
    return None if spec is None else Actor(**spec)


def _memory(actor: dict[str, Any] | None = None, **over: Any) -> tuple[Memory, dict]:
    fields: dict[str, Any] = {
        "id": ID,
        "scopes": ["global", "repo:x"],
        "confidence": Confidence.HIGH,
        "source": Source.EXPLICIT,
        "category": None,
        "created": datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC),
        "updated": datetime(2025, 6, 7, 8, 9, 10, 123456, tzinfo=UTC),
        "last_verified_at": None,
    }
    fields.update(over)
    memory = Memory.model_construct(actor=_actor(actor), **fields)
    spelled = {
        "id": fields["id"],
        "scopes": fields["scopes"],
        "confidence": fields["confidence"].value,
        "source": fields["source"].value,
        "category": None if fields["category"] is None else fields["category"].value,
        "created": _iso(fields["created"]),
        "updated": _iso(fields["updated"]),
        "last_verified_at": _iso(fields["last_verified_at"]),
        "actor": actor,
    }
    return memory, spelled


EXTRAS: dict[str, Any] = {
    "related": [_hit(1), _hit(2)],
    "removed_related": [
        _hit(
            3,
            relevance="medium-removed",
            removed_at=datetime(2026, 1, 1, tzinfo=UTC),
            removed_reason="obsolete",
        )
    ],
    "warnings": ["ambient_body_long"],
    "supersedes": [{"id": "01J0000000000000000000000B", "note": "older"}],
    "conflicts_filed": [{"a": "x", "b": "y", "n": 2}],
    "hint": "file it",
    "update_suggested": ["01J0000000000000000000000C"],
    "extends": [{"id": "01J0000000000000000000000D"}],
}


def _spell_extra(name: str, value: Any) -> Any:
    if value is None:
        return None
    if name in ("related", "removed_related"):
        return [_spell_hit(h) for h in value]
    return neutral(value)


def _committed(
    actor: dict[str, Any] | None = None,
    mem: dict[str, Any] | None = None,
    **extras: Any,
) -> dict[str, Any]:
    memory, spelled = _memory(actor, **(mem or {}))
    spelled["extras"] = {k: _spell_extra(k, v) for k, v in extras.items()}
    return {
        "k": "committed",
        "i": spelled,
        **_answer(lambda: B.committed(memory, **extras)),
    }


def _sweep() -> dict[str, Any]:
    memory, spelled = _memory({"client": "c"})
    texts = []
    for mask in range(256):
        kw = {
            name: (EXTRAS[name] if mask >> b & 1 else None)
            for b, name in enumerate(EXTRA_ORDER)
        }
        texts.append(json.dumps(B.committed(memory, **kw)))
    digest = hashlib.sha256("\n".join(texts).encode("ascii")).hexdigest()
    return {"k": "committed_sweep", "i": spelled, "count": 256, "sha256": digest}


def _committed_rows() -> list[dict[str, Any]]:
    stamp = datetime(2026, 2, 3, 4, 5, 6, tzinfo=timezone(timedelta(hours=2)))
    rows = [
        _committed(),
        _committed(
            {"client": "c", "model": "m"},
            mem={"category": Category.AMBIENT, "last_verified_at": stamp},
        ),
        # An actor whose only content is `sources` is empty.
        _committed({"sources": {"client": "header"}}),
        _committed(
            {
                "client": "c",
                "client_version": "1",
                "model": "m",
                "principal": "p",
                "session": "s",
                "sources": {"client": "header", "model": "env"},
            },
            mem={"source": Source.CORRECTION, "category": Category.FACT},
        ),
        # A naive stamp is rendered without an offset.
        _committed(
            mem={"created": datetime(2025, 1, 1), "updated": datetime(2025, 1, 1)}
        ),
    ]
    # Each optional argument alone, empty and filled.
    for name in EXTRA_ORDER:
        empty: Any = "" if name == "hint" else []
        rows.append(_committed(**{name: empty}))
        rows.append(_committed(**{name: EXTRAS[name]}))
    rows.append(_committed({"client": "c"}, **EXTRAS))
    # add_similar through `related` and `removed_related` at the bound.
    for n in (0, 5, 6, 12):
        hits = [_hit(i) for i in range(n)]
        rows.append(_committed(related=hits, removed_related=hits))
    return rows


def _similar_rows() -> list[dict[str, Any]]:
    removed = datetime(2026, 1, 1, tzinfo=UTC)
    hits = [
        _hit(),
        _hit(removed_at=removed, removed_reason="gone", relevance="high-removed"),
        _hit(removed_at=removed),
        _hit(removed_reason=""),
        _hit(similarity=float("nan")),
        _hit(similarity=float("inf")),
        _hit(similarity=-0.0),
        _hit(similarity=1e-320),
        _hit(snippet="café \ud800 \U0001f600", removed_reason="r\udcff"),
        _hit(
            scopes=[],
            created=datetime(1, 1, 1),
            updated=datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=UTC),
        ),
    ]
    rows = [
        {
            "k": "similar",
            "i": _spell_hit(h),
            **_answer(lambda h=h: B.similar_to_dict(h)),
        }
        for h in hits
    ]
    for n in (0, 1, 5, 6, 12):
        group = [_hit(i) for i in range(n)]

        def call(group: list[SimilarHit] = group) -> dict[str, Any]:
            out: dict[str, Any] = {}
            B.add_similar(out, "near", group)
            return out

        rows.append(
            {
                "k": "add_similar",
                "i": {"key": "near", "hits": [_spell_hit(h) for h in group]},
                **_answer(call),
            }
        )
    return rows


def _small_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for marker, snippet in (
        ("todo", "a todo here"),
        ("sha:<commit>", "\ud800"),
        ("", ""),
    ):
        rows.append(
            {
                "k": "transient",
                "i": {**text_field("marker", marker), **text_field("snippet", snippet)},
                **_answer(
                    lambda m=marker, s=snippet: B.transient_to_dict(
                        TransientMatch(marker=m, snippet=s)
                    )
                ),
            }
        )
    for kind, snippet in (("aws", "key [redacted:aws]"), ("", "\udcff")):
        hit = type("Hit", (), {"kind": kind, "snippet": snippet})()
        rows.append(
            {
                "k": "credential",
                "i": {**text_field("kind", kind), **text_field("snippet", snippet)},
                **_answer(lambda h=hit: B.credential_to_dict(h)),
            }
        )
    reviews: tuple[tuple[str, list[Any], dict[str, Any], str], ...] = (
        ("r1", [], {}, ""),
        (
            "r2",
            [{"id": "x", "n": 1}, {"t": None}],
            {"q1": "why?", "n": [1, 2]},
            "answer with review",
        ),
        ("\ud800", [{"s": "\udcff"}], {"\udcff": True}, "h\ud800"),
    )
    for rid, related, questions, hint in reviews:
        rows.append(
            {
                "k": "review",
                "i": {
                    "review_id": neutral(rid),
                    "related": neutral(related),
                    "questions": neutral(questions),
                    "hint": neutral(hint),
                },
                **_answer(
                    lambda a=rid, b=related, c=questions, d=hint: B.review(
                        review_id=a, related=b, questions=c, hint=d
                    )
                ),
            }
        )
    rosters: tuple[tuple[str, list[str], str, str], ...] = (
        ("r1", [], "", ""),
        ("r2", ["id1 (2026-01-01) one", "id2 (2026-01-02) two"], "which?", "name them"),
        ("\udcff", ["\ud800"], "q\ud800", "h"),
    )
    for rid, roster, question, hint in rosters:
        rows.append(
            {
                "k": "roster",
                "i": {
                    "review_id": neutral(rid),
                    "roster": neutral(roster),
                    "question": neutral(question),
                    "hint": neutral(hint),
                },
                **_answer(
                    lambda a=rid, b=roster, c=question, d=hint: B.roster(
                        review_id=a, roster=b, question=c, hint=d
                    )
                ),
            }
        )
    actors: tuple[dict[str, Any] | None, ...] = (
        None,
        {},
        {"sources": {"client": "header"}},
        {"client": ""},
        {"session": "s"},
        {
            "client": "c",
            "client_version": "1",
            "model": "m",
            "principal": "p",
            "session": "s",
            "sources": {"model": "env", "client": "header"},
        },
        {"model": "m\ud800"},
    )
    for spec in actors:
        rows.append(
            {
                "k": "actor",
                "i": neutral(spec),
                **_answer(lambda s=spec: B.actor_to_dict(_actor(s))),
            }
        )
    return rows


def _origin_row(spec: dict[str, Any] | None) -> dict[str, Any]:
    origin = None if spec is None else Origin(**spec)
    return {
        "k": "origin",
        "i": neutral(spec),
        **_answer(lambda: B.origin_to_dict(origin)),
    }


def _origin_rows() -> list[dict[str, Any]]:
    specs: tuple[dict[str, Any] | None, ...] = (
        None,
        {},
        {"cwd": ""},
        {"cwd": "/w"},
        {"cwd": "/w", "source": "declared"},
        {"source": "declared"},
        {"repo": "git@h:x/y.git", "branch": "main"},
        {"cwd": "/w", "repo": "r", "branch": "b", "worktree_root": "/w", "source": "s"},
        {"worktree_root": "/w/é\U0001f600"},
    )
    return [_origin_row(s) for s in specs]


def _origin_posix_rows() -> list[dict[str, Any]]:
    specs: tuple[dict[str, Any], ...] = (
        {"cwd": "/a\udcff"},
        {"cwd": "/a\udc80", "source": "declared"},
        {"repo": "\udce9", "branch": "b\udcff", "worktree_root": "/\udcc3\udca9"},
        {
            "cwd": "/\udcff",
            "repo": "\udcfe",
            "branch": "\udcfd",
            "worktree_root": "/\udcfc",
            "source": "s",
        },
        {"source": "s\udcff"},
        {"cwd": "\udcff", "branch": "ok"},
        {"cwd": "abc\udcffd", "repo": "ab\udcff"},
        # A surrogate past U+DCFF: `surrogateescape` cannot encode it.
        {"cwd": "/\ud800"},
        {"branch": "\udc7f"},
        {"cwd": "/ok\udcff", "branch": "\udfff"},
    )
    return [_origin_row(s) for s in specs]


def generate() -> dict[str, bytes]:
    """``response_write.jsonl``: not gated."""
    rows = (
        _committed_rows()
        + [_sweep()]
        + _similar_rows()
        + _small_rows()
        + _origin_rows()
    )
    header = {
        "k": "header",
        "count": len(rows),
        "shown": _response.SIMILAR_HITS_SHOWN,
        "extra_order": list(EXTRA_ORDER),
        "extras": {k: _spell_extra(k, v) for k, v in EXTRAS.items()},
    }
    return {"response_write.jsonl": jsonl([header, *rows])}


def generate_posix() -> dict[str, bytes]:
    """``response_write_posix.jsonl``: POSIX only (`os.fsencode`)."""
    posix = _origin_posix_rows()
    header = {"k": "header", "count": len(posix)}
    return {"response_write_posix.jsonl": jsonl([header, *posix])}

"""Fixture: the in-memory ranker of `search.search`, widely.

``search_ranker.jsonl``: a synthetic corpus (``_N_RECORDS`` records) and
queries (``_QUERIES`` and ``_generated_queries()``), built from fixed lists
and a fixed linear congruence (no `random`, no clock), with the hits
`search.search` returns for each query in each mode. The header counts the
records, queries and cases (``counts``). Each hit is its id, the IEEE-754
bits of the score it carries (``bits``) and of the unrounded score
(``raw_bits``), its relevance and its match terms, in rank order:
`search.jsonl`'s shape without what follows from those (the rank is the
position, the reprs are the bits'), and without the lexicon's
``match_query``, which ``lexicon.jsonl`` pins. A mode the reference refuses
is recorded as ``{"error": "<exception type>"}`` in place of the hit list.
There is no FTS5 prefilter here (``search.jsonl`` pins it), so the file
reads no SQLite; it reads the Unicode database through the tokenizer and
`re`, and is gated on that version.

The scores read the platform's ``log``, ``exp`` and ``log1p``. Every input
the generator hands them is checked against the exact value (`decimal`, 60
digits): none lies within ``_LIBM_MARGIN`` ULP of a rounding midpoint, so a
libm that rounds correctly, or nearly, gives the same bits on any platform.
A few records carry a word or a few seconds added for that
(``_FIXUP_WORDS``, ``_FIXUP_SECONDS``); the header records the inputs'
count and the smallest margin (``libm``).

The header holds the corpus (each record's fields as `isoformat` text, a
body holding a lone surrogate as ``body_cps``), ``now``, the default
``max_results`` and ``half_life_days``, every mode's keyword arguments
(``modes``), which modes each query runs (``core_modes`` for all of them;
of ``extra_modes``, those a query's record lists under ``extra``), the
corpus-stats provider the ``*_stats`` modes pass (``provider``), and the
word tables the ranker reads, as the reference builds them (``tables``), so
a port can check its own copies against them.

Accidents of the reference this fixture pins (a port copies them):

- a record's anchor day is the date of ``created`` in its own UTC offset,
  while a query's window is read from the date of ``now`` in its offset
  (`search_lane._memory_anchor_day`, `_temporal_reading`);
- a month name's year is ``\\s+\\d{4}`` with no word boundary after it, so
  "may 20245" reads May 2024 (`search_lane._CONV_MONTH_RE`);
- the order ask ``which ... (first|last)\\b`` has no word boundary before
  first/last, so "which atlast" is an order ask (`_CONV_ORDER_RE`);
- `\\d` and `int()` read every Unicode decimal digit, so Arabic-Indic dates
  are dates;
- "last year" at year 1 raises ValueError, and the relative periods near
  years 1 and 9999 raise OverflowError, out of `search` itself
  (`_temporal_reading`), only in hybrid mode with the lane on.
- the scaffold df-floor rebuilds the corpus statistics without their
  ``avgdl`` (`search_lane._scaffold_floor_stats`), so a temporal query with
  a provider prices length against the pool's mean, not the provider's
  (the ``*_stats`` modes on "this year" and kin);
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from decimal import Context, Decimal
from typing import Any
from unittest.mock import patch

from bettermemory import search as bsearch
from bettermemory import search_bm25, search_lane
from bettermemory.models import Category, Confidence, Memory, Source
from bettermemory.search_bm25 import _EXPANSION_TABLES, CorpusStats, _scope_tokens
from bettermemory.search_lexicon import (
    _expand_kebab,
    _strip_stopwords,
    tokenize,
)

from ._common import float_bits, jsonl, raw_scores, text_field

_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
_MAX_RESULTS = 5
_HALF_LIFE = 30.0
_N_RECORDS = 324
_N_GENERATED_QUERIES = 42
_LIBM_MARGIN = 0.05

# Inputs to libm near a rounding midpoint, moved off it: a word appended to
# a record's body (it changes a term's document frequency), or seconds added
# to a record's created and updated (they change its age).
# Found by moving each offending input and generating again until none was
# left; applied in order, so a record may take more than one.
_FIXUP_WORDS: tuple[tuple[int, str], ...] = (
    (26, "WEEK"),
    (0, "2026-02-30"),
    (1, "٢٠٢٦"),
    (278, "SMOKER"),
    (2, "TOKENIZER"),
    (3, "lang"),
    (4, "TO-DO"),
    (5, "ΟΔΥΣΣΕΥΣ"),
    (7, "BUILT"),
    (9, "case"),
    (6, "COMPOSE"),
    (11, "YEAR"),
    (8, "DON'T"),
    (10, "TO-DO"),
    (31, "COMPOSE"),
    (13, "DAY"),
    (15, "MARCH"),
    (14, "YEAR"),
    (17, "2026-02-30"),
    (0, "BUILT"),
    (1, "KUBERNETES"),
    (117, "LIBRARY"),
    (2, "lang"),
    (37, "WEEK"),
    (3, "9999"),
    (4, "zero"),
    (5, "QUERY"),
    (9, "Kebab-case-name"),
    (6, "٢٠٢٦"),
    (12, "INDEX"),
    (7, "2026"),
    (8, "2026"),
    (10, "2"),
    (14, "WEEK"),
    (0, "Kebab-case-name"),
    (1, "kebab"),
    (2, "KUBERNETES"),
    (11, "MARCH"),
    (3, "Client"),
    (6, "WEEK"),
    (0, "Client"),
    (1, "MARCH"),
    (2, "width"),
    (6, "2026-02-30"),
    (0, "9999"),
    (1, "12"),
    (2, "2026"),
    (1, "Client"),
    (10, "2026"),
    (2, "Client"),
    (0, "SERVER"),
)
_FIXUP_SECONDS: tuple[tuple[int, int], ...] = (
    (235, 7),
    (120, 7),
    (12, 7),
    (237, 7),
    (271, 7),
    (250, 7),
    (251, 7),
    (284, 7),
    (64, 7),
    (173, 7),
    (45, 7),
    (240, 7),
    (290, 7),
    (64, 7),
    (64, 7),
    (5, 7),
    (5, 7),
)


class _Lcg:
    """A 64-bit linear congruence (Knuth's MMIX constants); the high bits."""

    def __init__(self, seed: int) -> None:
        self.state = seed & 0xFFFFFFFFFFFFFFFF

    def next(self) -> int:
        self.state = (
            self.state * 6364136223846793005 + 1442695040888963407
        ) & 0xFFFFFFFFFFFFFFFF
        return self.state >> 33

    def below(self, n: int) -> int:
        return self.next() % n

    def pick(self, items: tuple[str, ...] | list[str]) -> str:
        return items[self.below(len(items))]


_WORDS = (
    "docker",
    "compose",
    "staging",
    "cluster",
    "postgres",
    "upgrade",
    "connection",
    "pool",
    "cache",
    "policy",
    "policies",
    "deploy",
    "deployment",
    "release",
    "rollback",
    "backup",
    "restore",
    "toggle",
    "flags",
    "feature",
    "hash",
    "digest",
    "checksum",
    "credentials",
    "secret",
    "repository",
    "library",
    "dependency",
    "package",
    "configuration",
    "kubernetes",
    "production",
    "development",
    "documentation",
    "authentication",
    "performance",
    "pull",
    "request",
    "ranking",
    "tokenizer",
    "index",
    "query",
    "server",
    "client",
    "timeout",
    "retry",
    "logging",
    "metrics",
    "alerts",
    "splitting",
    "switched",
    "written",
    "built",
    "phase",
    "legacy",
    "shutdown",
    "remember",
    "stuff",
    "thing",
    "smoker",
    "bought",
    "visited",
    "meeting",
    "standup",
    "smokers",
    "day",
    "days",
    "week",
    "month",
    "year",
    "time",
    "total",
    "many",
    "long",
)
_FOREIGN = (
    "Zürich",
    "Tjörn",
    "café",
    "naïve",
    "façade",
    "Kraków",
    "Ærø",
    "São Paulo",
    "Øresund",
    "München",
    "straße",
    "東京",
    "移転",
    "東京オフィス",
    "日本語",
    "中文分词",
    "한국어",
    "ภาษาไทย",
    "Москва",
    "ёлка",
    "привет",
    "ΣΟΦΟΣ",
    "σοφός",
    "λόγος",
    "ΟΔΥΣΣΕΥΣ",
    "٢٠٢٦",
    "١٢",
    "😀",
    "🚀",
    "doc​ker",
    "zero‍width",
    "﻿bom",
    "ﬁle",
    "Ⅻ",
    "²",
)
_CODE = (
    "foo.bar.baz",
    "kebab-case-name",
    "snake_case_name",
    "v1.2.3",
    "3.11.4",
    "16.3",
    "C++",
    "C#",
    ".NET",
    "F#",
    "node.js",
    "python-frontmatter",
    "end-to-end",
    "to-do",
    "docker-compose",
    "claude-code",
    "status.example.com",
    "x86_64",
    "utf-8",
    "--force",
    "a.b",
    "1.0",
)
_STOP = (
    "the",
    "a",
    "is",
    "what",
    "it's",
    "don't",
    "we'll",
    "of",
    "and",
    "to",
    "in",
    "on",
    "it",
    "you",
    "I",
)
_DATES = (
    "2026-05-03",
    "2026/05/30",
    "2026-06-01",
    "2025-12-24",
    "2026-02-30",
    "2026-13-01",
    "0000-01-01",
    "9999-12-31",
    "٢٠٢٦-٠٥-٠٣",
)
_TEMPORAL = (
    "last week",
    "last month",
    "last year",
    "this week",
    "this month",
    "this year",
    "yesterday",
    "3 days ago",
    "two weeks ago",
    "a month ago",
    "in may",
    "may 2026",
    "this may need",
    "mid-may",
    "march",
    "december",
    "between march and may",
    "first",
    "the last time",
)
_SCOPES = (
    ("tools",),
    ("db",),
    ("projects:alpha-beta",),
    ("projects:bettermemory", "lang:python"),
    ("a",),
    ("x1:y2:z3",),
    ("kebab-case-scope",),
    ("db", "api"),
    ("2026",),
    ("projects:docker-compose",),
    ("style", "projects:end-to-end", "to-do"),
    ("projects:staging",),
    ("the",),
    ("lang:rust", "lang:python", "projects:alpha-beta"),
)
_OFFSETS = (
    timezone.utc,
    timezone(timedelta(hours=5, minutes=30)),
    timezone(timedelta(hours=-8)),
    timezone(timedelta(hours=14)),
    timezone(timedelta(hours=-12)),
    timezone(timedelta(hours=23, minutes=59, seconds=59)),
)
_AGES_DAYS = (-3650, -400, -365, -90, -31, -30, -7, -1, 0, 0, 1, 2, 30, 31, 400, 3650)


def _ulid(i: int) -> str:
    return f"01J{i:023d}"


def _sentence(rng: _Lcg) -> str:
    kind = rng.below(10)
    n = 3 + rng.below(8)
    words: list[str] = []
    for _ in range(n):
        pool = rng.below(10)
        if pool < 5:
            words.append(rng.pick(_WORDS))
        elif pool == 5:
            words.append(rng.pick(_FOREIGN))
        elif pool == 6:
            words.append(rng.pick(_CODE))
        elif pool == 7:
            words.append(rng.pick(_STOP))
        elif pool == 8 and kind < 3:
            words.append(rng.pick(_DATES))
        else:
            words.append(rng.pick(_TEMPORAL))
    text = " ".join(words)
    if kind == 9:
        text = text.upper()
    return text[:1].upper() + text[1:] + rng.pick((".", ";", "!", "?", ",", ""))


def _body(i: int, rng: _Lcg) -> str:
    special = i % 23
    if special == 0:
        return ""
    if special == 1:
        return "   \n\t "
    if special == 2:
        word = rng.pick(_WORDS)
        return " ".join([word] * (3 + rng.below(9)))
    if special == 3:
        return f"[{rng.pick(_DATES)}] " + _sentence(rng)
    if special == 4:
        return " ".join(_sentence(rng) for _ in range(15))
    if special == 5:
        return "x" * 230 + " 2026-05-20 " + _sentence(rng)
    if special == 6:
        return "lone \ud800 surrogate " + _sentence(rng)
    return " ".join(_sentence(rng) for _ in range(1 + rng.below(4)))


def _instant(base: datetime, rng: _Lcg) -> datetime:
    tz = _OFFSETS[rng.below(len(_OFFSETS))]
    delta = timedelta(
        days=_AGES_DAYS[rng.below(len(_AGES_DAYS))],
        hours=rng.below(24),
        minutes=rng.below(60),
        microseconds=rng.below(3) * rng.below(1_000_000),
    )
    return (base - delta).astimezone(tz)


def _memories() -> list[Memory]:
    rng = _Lcg(20260601)
    out: list[Memory] = []
    for i in range(_N_RECORDS):
        if i % 41 == 1 and out:
            # A twin: the same body, scopes and created as the record
            # before it, so only the id breaks the tie.
            prev = out[-1]
            body, scopes, created = prev.body, list(prev.scopes), prev.created
            updated = prev.updated
        else:
            body = _body(i, rng)
            scopes = list(_SCOPES[rng.below(len(_SCOPES))])
            if i % 53 == 7:
                created = datetime(1, 1, 1, tzinfo=timezone.utc)
            elif i % 53 == 8:
                created = datetime(
                    9999, 12, 31, 23, 59, 59, 999999, tzinfo=timezone.utc
                )
            elif i % 19 == 4 and out:
                created = out[-1].created
            else:
                created = _instant(_NOW, rng)
            kind = rng.below(4)
            if kind == 0 or created.year in (1, 9999):
                updated = created
            elif kind == 1:
                updated = created + timedelta(hours=rng.below(5000))
            elif kind == 2:
                updated = _instant(_NOW, rng)
            else:
                updated = created - timedelta(minutes=rng.below(600))
        verified = None if rng.below(3) == 0 else _instant(_NOW, rng)
        categories = [None, *Category]
        out.append(
            Memory(
                id=_ulid(i),
                created=created,
                updated=updated,
                scopes=scopes,
                confidence=list(Confidence)[i % 3],
                source=list(Source)[(i // 3) % 3],
                category=categories[(i // 9) % 4],
                last_verified_at=verified,
                body=body,
            )
        )
    for i, word in _FIXUP_WORDS:
        out[i] = out[i].model_copy(update={"body": f"{out[i].body} {word}"})
    for i, seconds in _FIXUP_SECONDS:
        shift = timedelta(seconds=seconds)
        m = out[i]
        out[i] = m.model_copy(
            update={"created": m.created + shift, "updated": m.updated + shift}
        )
    return out


_QUERIES = (
    "",
    "   ",
    "\t\n",
    "the",
    "what is the",
    "it's",
    "don't",
    "a an the of",
    "xyzzy plugh",
    "nothing matches this query qwertyuiop",
    "docker",
    "docker docker docker",
    "docker compose",
    "docker-compose staging",
    "compose docker",
    "postgres upgrade 16.3",
    "16.3",
    "3.11.4",
    "v1.2.3",
    "1.0",
    "foo.bar.baz",
    "foo bar",
    "kebab-case-name",
    "kebab case",
    "snake_case_name",
    "snake-case-name",
    "end-to-end",
    "end to end",
    "to-do",
    "python-frontmatter",
    "frontmatter",
    "claude-code",
    "c++",
    "C#",
    ".net",
    "f#",
    "node.js",
    "status.example.com",
    "x86_64",
    "utf-8",
    "--force",
    "zurich",
    "Zürich café",
    "naive facade",
    "krakow",
    "aero",
    "sao paulo",
    "munchen strasse",
    "東京",
    "移転",
    "東京オフィス",
    "日本",
    "中文",
    "한국어",
    "ภาษา",
    "москва",
    "ЁЛКА",
    "σοφος",
    "ΣΟΦΟΣ",
    "οδυσσευς",
    "٢٠٢٦",
    "2026",
    "😀",
    "🚀 deploy",
    "doc​ker",
    "zero width",
    "file",
    "xii",
    "2",
    "lang python",
    "projects",
    "alpha-beta",
    "alpha beta",
    "bettermemory",
    "x1 y2",
    "api db",
    "scope",
    "repo creds",
    "k8s db",
    "prod config",
    "deps libs",
    "pr review",
    "toggle rollback",
    "toggles",
    "undo the release",
    "hash digest",
    "old stuff",
    "cutover",
    "splitting staging",
    "switched",
    "written",
    "built",
    "staged rollout",
    "i actually remember stuff about docker",
    "i vaguely remember the thing with the cache",
    "really just whatever",
    "what did i do last week",
    "last week deploy",
    "last month release",
    "last year backup",
    "this week standup",
    "this month metrics",
    "this year",
    "yesterday standup",
    "in may we migrated postgres",
    "may 2026 deploy",
    "may 20245",
    "this may need sudo",
    "this may 2026 docker",
    "mid-may release",
    "since may",
    "and may crash",
    "between march and may",
    "december 9999",
    "january 0000",
    "3 days ago backup",
    "two weeks ago",
    "a month ago restore",
    "how many days ago did i buy a smoker",
    "how many smokers",
    "how much time",
    "how long did the upgrade take",
    "which happened first the deploy or the backup",
    "which atlast",
    "which\nfirst",
    "order of the releases",
    "the order from staging",
    "earliest to latest",
    "first to last",
    "chronologically",
    "when did we upgrade postgres",
    "when",
    "what day was the standup",
    "the last time i deployed",
    "since i last visited",
    "we last met",
    "most recently",
    "most recent backup",
    "2026-05-03 incident",
    "2026/05/30",
    "2026-02-30",
    "٢٠٢٦-٠٥-٠٣",
    "3 months ago",
    "12 weeks ago",
    "123 days ago",
    "0 days ago",
    "the last 2 weeks",
    "schema 8",
    "port 80",
    "day day day",
    "many days total",
    "one two three",
    "smoker",
    "the smoker",
    "bought smoker",
    "connection pool timeout",
    "retry timeout logging metrics alerts",
    "server client query index ranking tokenizer",
    "release",
    "releases",
    "policy policies",
    "cache",
    "caches",
    "library libraries",
    "DOCKER",
    "Docker Compose Staging Cluster",
)


def _generated_queries() -> list[str]:
    rng = _Lcg(9001)
    out = []
    for _ in range(_N_GENERATED_QUERIES - 2):
        n = 1 + rng.below(6)
        words = []
        for _ in range(n):
            pool = rng.below(8)
            if pool < 4:
                words.append(rng.pick(_WORDS))
            elif pool == 4:
                words.append(rng.pick(_FOREIGN))
            elif pool == 5:
                words.append(rng.pick(_CODE))
            elif pool == 6:
                words.append(rng.pick(_STOP))
            else:
                words.append(rng.pick(_TEMPORAL))
        out.append(" ".join(words))
    # A very long query: every word of the vocabulary, twice.
    out.append(" ".join(_WORDS + _CODE + _WORDS))
    out.append(" ".join(["docker"] * 300))
    return out


_CORE_MODES = ("keyword", "bm25", "hybrid", "hybrid_rescue")

_MODES: dict[str, dict[str, Any]] = {
    "keyword": {"mode": "keyword"},
    "bm25": {"mode": "bm25"},
    "hybrid": {"mode": "hybrid"},
    "hybrid_plain": {"mode": "hybrid", "conversational": False, "max_results": 3},
    "hybrid_rescue": {"mode": "hybrid", "rescue_expansion": True},
    "keyword_max0": {"mode": "keyword", "max_results": 0},
    "hybrid_max1": {"mode": "hybrid", "max_results": 1},
    "bm25_max_neg2": {"mode": "bm25", "max_results": -2},
    "hybrid_max_all": {"mode": "hybrid", "max_results": 1000},
    "hybrid_k1": {"mode": "hybrid", "rrf_k": 1},
    "hybrid_k0": {"mode": "hybrid", "rrf_k": 0},
    "keyword_hl0": {"mode": "keyword", "half_life_days": 0.0},
    "invalid_mode": {"mode": "semantic"},
    "mode_case": {"mode": "Hybrid"},
    "hybrid_browse": {"mode": "hybrid", "allow_empty_query": True},
    "keyword_scoped": {"mode": "keyword", "scopes": ["db", "lang:python"]},
    "hybrid_excluded": {
        "mode": "hybrid",
        "excluded_scopes": ["tools", "projects:alpha-beta"],
    },
    "hybrid_empty_scopes": {"mode": "hybrid", "scopes": [], "max_results": 1},
    "hybrid_stats": {"mode": "hybrid", "scopes": ["db", "tools"], "provider": True},
    "bm25_stats": {"mode": "bm25", "scopes": ["the", "a"], "provider": True},
    "rescue_stats": {
        "mode": "hybrid",
        "rescue_expansion": True,
        "provider": True,
        "excluded_scopes": ["db"],
    },
    "hybrid_now_jan": {
        "mode": "hybrid",
        "now": "2026-01-03T00:30:00-05:00",
        "max_results": 1,
    },
    "hybrid_now_min": {
        "mode": "hybrid",
        "now": "0001-01-02T00:00:00+00:00",
        "max_results": 1,
    },
    "hybrid_now_max": {
        "mode": "hybrid",
        "now": "9999-12-30T12:00:00+00:00",
        "max_results": 1,
    },
}
_EXTRA_MODES = tuple(m for m in _MODES if m not in _CORE_MODES)
# `hybrid_max_all` and `bm25_max_neg2` return (nearly) every hit, so they
# run on the queries that match few records only, with every other extra.
_FEW_MATCHES = frozenset({"c++", "to-do", "xyzzy plugh", "2026-02-30", "which atlast"})
_WIDE_MODES = frozenset({"hybrid_max_all", "bm25_max_neg2"})
# The lane's modes run on every query that reads as temporal (or nearly).
_LANE_MODES = ("hybrid_plain", "hybrid_now_jan", "hybrid_now_min", "hybrid_now_max")
_LANE_WORDS = re.compile(
    r"\b(last|this|yesterday|ago|may|march|december|january|how|which|order|"
    r"earliest|first|chronolog|when|what day|most recent|\d{4})"
)


def _extra_modes(i: int, query: str) -> list[str]:
    if query in _FEW_MATCHES:
        return list(_EXTRA_MODES)
    if i % 11 == 3:
        return [m for m in _EXTRA_MODES if m not in _WIDE_MODES]
    if _LANE_WORDS.search(query.lower()):
        return list(_LANE_MODES)
    return []


def _provider(memories: list[Memory]) -> Any:
    """The corpus statistics of the whole corpus, for a search over a
    filtered subset of it: each asked term's document frequency over the
    records' content streams (`body_df`), over content or scope tokens
    (`scope_df`), and the mean content-stream length (`avgdl`). Terms no
    record carries are left out."""
    streams = []
    for m in memories:
        content = set(_strip_stopwords(_expand_kebab(tokenize(m.body))))
        scope: set[str] = set()
        for s in m.scopes:
            scope.update(_scope_tokens(s))
        streams.append((content, content | scope))
    total = sum(
        len(_strip_stopwords(_expand_kebab(tokenize(m.body)))) for m in memories
    )
    avgdl = total / len(memories)

    def provide(terms: list[str]) -> CorpusStats:
        body_df = {}
        scope_df = {}
        for t in terms:
            b = sum(1 for c, _ in streams if t in c)
            s = sum(1 for _, u in streams if t in u)
            if b:
                body_df[t] = b
            if s:
                scope_df[t] = s
        return CorpusStats(
            size=len(memories), body_df=body_df, scope_df=scope_df, avgdl=avgdl
        )

    return provide


def _run(memories: list[Memory], query: str, options: dict[str, Any]) -> Any:
    kwargs = dict(options)
    now = datetime.fromisoformat(kwargs.pop("now")) if "now" in kwargs else _NOW
    if "excluded_scopes" in kwargs:
        kwargs["excluded_scopes"] = set(kwargs["excluded_scopes"])
    if kwargs.pop("provider", False):
        kwargs["corpus_stats_provider"] = _provider(memories)
    kwargs.setdefault("max_results", _MAX_RESULTS)
    bsearch.clear_token_cache()
    try:
        with raw_scores() as raw:
            hits = bsearch.search(
                list(memories),
                query,
                now=now,
                half_life_days=kwargs.pop("half_life_days", _HALF_LIFE),
                path_drift=False,
                **kwargs,
            )
    except (ValueError, OverflowError, TypeError) as exc:
        return {"error": type(exc).__name__}
    return [
        {
            "id": h.id,
            "bits": float_bits(h.score),
            "raw_bits": float_bits(raw[h.id]),
            "relevance": h.relevance,
            "match_terms": list(h.match_terms),
        }
        for h in hits
    ]


class _LibmInputs:
    """`math` for `search_bm25`, noting each input of ``log``, ``exp`` and
    ``log1p``."""

    def __init__(self) -> None:
        self.inputs: dict[str, set[float]] = {
            "log": set(),
            "exp": set(),
            "log1p": set(),
        }

    def __getattr__(self, name: str) -> Any:
        return getattr(math, name)

    def log(self, x: float) -> float:
        self.inputs["log"].add(float(x))
        return math.log(x)

    def exp(self, x: float) -> float:
        self.inputs["exp"].add(float(x))
        return math.exp(x)

    def log1p(self, x: float) -> float:
        self.inputs["log1p"].add(float(x))
        return math.log1p(x)


def _midpoint_margin(name: str, x: float) -> float:
    """How far the exact ``name(x)`` lies from the nearest midpoint between
    two doubles, in units of their spacing (0.5 when it is a double)."""
    ctx = Context(prec=60)
    d = Decimal(x)
    if name == "log":
        exact = d.ln(ctx)
    elif name == "exp":
        exact = d.exp(ctx)
    else:
        exact = ctx.add(1, d).ln(ctx)
    near = float(exact)
    lo = near if Decimal(near) <= exact else math.nextafter(near, -math.inf)
    hi = math.nextafter(lo, math.inf)
    if Decimal(lo) == exact:
        return 0.5
    frac = ctx.divide(exact - Decimal(lo), Decimal(hi) - Decimal(lo))
    return float(abs(frac - Decimal("0.5")))


def _libm_report(inputs: dict[str, set[float]]) -> dict[str, Any]:
    margins = [(_midpoint_margin(n, x), n, x) for n, xs in inputs.items() for x in xs]
    low = sorted(m for m in margins if m[0] < _LIBM_MARGIN)
    assert not low, f"libm inputs near a rounding midpoint: {low}"
    return {
        "functions": {n: len(xs) for n, xs in sorted(inputs.items())},
        "inputs": len(margins),
        "required_margin_ulp": _LIBM_MARGIN,
        "smallest_margin_ulp": f"{min(m[0] for m in margins):.4f}",
    }


def _tables() -> dict[str, Any]:
    t = _EXPANSION_TABLES
    return {
        "filler_stems": sorted(t.filler_stems),
        "irregular": {k: list(v) for k, v in sorted(t.irregular.items())},
        "clippings": {k: list(v) for k, v in sorted(t.clippings.items())},
        "synonyms": {k: sorted(v) for k, v in sorted(t.synonyms.items())},
        "scaffold_stems": sorted(search_lane._CONV_SCAFFOLD_STEMS),
        "time_unit_stems": sorted(search_lane._CONV_TIME_UNIT_STEMS),
        "month_names": list(search_lane._MONTH_NAMES),
        "may_context_words": list(search_lane._CONV_MAY_CONTEXT_WORDS),
        "may_modal_followers": list(search_lane._CONV_MAY_MODAL_FOLLOWERS),
    }


def _records() -> list[dict[str, Any]]:
    memories = _memories()
    queries = list(_QUERIES) + _generated_queries()
    header: dict[str, Any] = {
        "fixture": "search_ranker",
        "now": _NOW.isoformat(),
        "half_life_days": _HALF_LIFE,
        "max_results": _MAX_RESULTS,
        "modes": _MODES,
        "core_modes": list(_CORE_MODES),
        "extra_modes": list(_EXTRA_MODES),
        "provider": (
            "corpus statistics of the whole corpus: size, body_df and scope_df "
            "of each asked term a record carries (content stream; content or "
            "scope tokens), avgdl the mean content-stream length"
        ),
        "tables": _tables(),
        "memories": [
            {
                "id": m.id,
                "created": m.created.isoformat(),
                "updated": m.updated.isoformat(),
                "last_verified_at": (
                    None
                    if m.last_verified_at is None
                    else m.last_verified_at.isoformat()
                ),
                "scopes": m.scopes,
                "confidence": m.confidence.value,
                "source": m.source.value,
                "category": None if m.category is None else m.category.value,
                **text_field("body", m.body),
            }
            for m in memories
        ],
    }
    records: list[dict[str, Any]] = [header]
    libm = _LibmInputs()
    with patch.object(search_bm25, "math", libm):
        for i, query in enumerate(queries):
            extra = _extra_modes(i, query)
            hits = {
                name: _run(memories, query, _MODES[name])
                for name in (*_CORE_MODES, *extra)
            }
            records.append({**text_field("query", query), "extra": extra, "hits": hits})
    header["libm"] = _libm_report(libm.inputs)
    header["counts"] = {
        "records": len(memories),
        "queries": len(queries),
        "cases": sum(len(r["hits"]) for r in records[1:]),
    }
    return records


def generate() -> dict[str, bytes]:
    return {"search_ranker.jsonl": jsonl(_records())}

"""Fixtures: write-time dedup (``search_dedup.jsonl``,
``search_dedup_unicode.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/search_dedup.rs``. This pins it to
`search_dedup`'s `_content_token_set`, `_raw_content_token_set`,
`raw_content_tokens` and its cache, `_pairwise_content_jaccard`,
`similarity_bound`, `find_similar`, `find_similar_tombstones`,
`find_similar_in` and `find_similar_tombstones_in`, and through them
`_score_similar_jaccard`, the hit builders and sort keys, and
`models.snippet_for`.

Each line after the header is one case, by ``k``:

- ``tok``: the text ``t`` and its ``raw`` and ``content``
  token sets, sorted;
- ``jac``: two token sets ``a`` and ``b`` and the score's bits ``r``;
- ``bound``: ``a`` (``[shared, n, m]``) and the bound's bits ``r``;
- ``snip``: the body ``t`` and `snippet_for`'s answer ``r``;
- ``cache``: under ``entries`` (`RAW_TOKEN_SET_ENTRIES`), from an empty
  cache, `raw_content_tokens` of each text of ``seq``; ``r`` each answer
  (sorted) or the class raised, ``keys`` the cache's keys after, least
  recently used first;
- ``fs`` / ``ft``: `find_similar` / `find_similar_tombstones` of the body
  ``t`` over the records ``recs`` with thresholds ``hi`` and ``me`` (bits,
  or null for the default), from an empty cache; ``r`` the hits or
  ``{"err": class}``, ``keys`` the cache's keys after;
- ``in``: `find_similar_in` (``removed`` false) or
  `find_similar_tombstones_in` called ``n`` times with the body ``t`` on a
  scripted store, from an empty cache: ``docs`` (null, or the digest's
  ``[id, tokens]``), ``light`` (records; one with ``x`` is returned whatever
  the ids), ``all``, ``tomb``, ``memo`` (``"keep"``: the store holds each
  answer not None by key; ``"none"``: answers None without computing).
  ``log`` is every store call in order, ``r`` each call's answer.

A record is ``{"id", "b" (body), "c", "u" (isoformat), "s" (scopes),
"conf", "rm" (isoformat, a tombstone's), "rr"}``; a hit is the same with
``sim`` (bits), ``rel``, ``snip`` and ``rm``/``rr`` as null where unset.
A text holding a lone surrogate is ``{"cps": [...]}``. Texts outside ASCII
go in ``_unicode`` (the tokenizer and `str.strip` read
the Unicode data).
"""

from __future__ import annotations

import struct
from datetime import datetime, timedelta, timezone
from typing import Any

from bettermemory import search_dedup as sd
from bettermemory.models import Memory, TombstonedMemory
from bettermemory.store_digest import DigestDoc, LightRecord

from ._common import has_surrogate, jsonl


def _bits(x: float) -> str:
    return f"{struct.unpack('>Q', struct.pack('>d', x))[0]:016x}"


def _id(i: int) -> str:
    return f"{i:026d}"


T0 = datetime(2024, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
NAIVE = datetime(2024, 3, 1, 12, 0, 0)


def _rec(
    i: int,
    body: str,
    *,
    u: datetime | None = None,
    rm: datetime | None = None,
    rr: str = "turned out wrong",
    conf: str = "high",
    scopes: list[str] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": _id(i),
        "b": body,
        "c": (T0 - timedelta(days=30)).isoformat(),
        "u": (u or T0 + timedelta(minutes=i)).isoformat(),
        "s": scopes or ["global"],
        "conf": conf,
    }
    if rm is not None:
        out["rm"] = rm.isoformat()
        out["rr"] = rr
    return out


def _memory(r: dict[str, Any]) -> Memory:
    return Memory(
        id=r["id"],
        created=datetime.fromisoformat(r["c"]),
        updated=datetime.fromisoformat(r["u"]),
        scopes=r["s"],
        confidence=r["conf"],
        source="inferred",
        body=r["b"],
    )


def _tomb(r: dict[str, Any]) -> TombstonedMemory:
    m = _memory(r)
    return TombstonedMemory(
        **m.model_dump(),
        removed=datetime.fromisoformat(r["rm"]),
        removed_reason=r["rr"],
    )


def _light(r: dict[str, Any]) -> LightRecord:
    return LightRecord(
        id=r["id"],
        scopes=r["s"],
        confidence=r["conf"],
        body=r["b"],
        created=datetime.fromisoformat(r["c"]),
        updated=datetime.fromisoformat(r["u"]),
        removed=datetime.fromisoformat(r["rm"]) if "rm" in r else None,
        removed_reason=r.get("rr"),
    )


def _hit(h: Any) -> dict[str, Any]:
    return {
        "id": h.id,
        "s": h.scopes,
        "conf": h.confidence.value,
        "snip": h.snippet,
        "sim": _bits(h.similarity),
        "rel": h.relevance,
        "c": h.created.isoformat(),
        "u": h.updated.isoformat(),
        "rm": h.removed_at.isoformat() if h.removed_at is not None else None,
        "rr": h.removed_reason,
    }


def _keys() -> list[str]:
    return list(sd._RAW_TOKEN_SETS)


def _run(call: Any) -> Any:
    try:
        return [_hit(h) for h in call()]
    except (KeyError, TypeError) as e:
        return {"err": type(e).__name__}


class _Store:
    def __init__(self, case: dict[str, Any]) -> None:
        self.case = case
        self.log: list[Any] = []
        self.held: dict[Any, Any] = {}

    def digest_candidates(
        self, tokens: Any, *, removed: bool, floor: float
    ) -> list[DigestDoc] | None:
        self.log.append(["dc", sorted(tokens), removed, _bits(floor)])
        docs = self.case["docs"]
        if docs is None:
            return None
        return [DigestDoc(i, len(t), 0, False, frozenset(t)) for i, t in docs]

    def light_records(self, ids: Any, *, removed: bool) -> list[LightRecord]:
        wanted = list(ids)
        self.log.append(["lr", wanted, removed])
        return [
            _light(r) for r in self.case["light"] if r["id"] in wanted or r.get("x")
        ]

    def digest_memo(self, key: Any, compute: Any) -> Any:
        self.log.append(["memo", key[1]])
        if self.case["memo"] == "none":
            return None
        if key in self.held:
            return self.held[key]
        answer = compute()
        if answer is not None:
            self.held[key] = answer
        return answer

    def load_all(self) -> list[Memory]:
        self.log.append(["all"])
        return [_memory(r) for r in self.case["all"]]

    def load_tombstones(self) -> list[TombstonedMemory]:
        self.log.append(["tomb"])
        return [_tomb(r) for r in self.case["tomb"]]


def _case(c: dict[str, Any]) -> dict[str, Any]:
    sd.clear_raw_token_set_cache()
    k = c["k"]
    out: dict[str, Any] = dict(c)
    if k == "tok":
        out["raw"] = sorted(sd._raw_content_token_set(c["t"]))
        out["content"] = sorted(sd._content_token_set(c["t"]))
    elif k == "jac":
        out["r"] = _bits(sd._pairwise_content_jaccard(set(c["a"]), set(c["b"])))
    elif k == "bound":
        out["r"] = _bits(sd.similarity_bound(*c["a"]))
    elif k == "snip":
        from bettermemory.models import snippet_for

        out["r"] = snippet_for(c["t"])
    elif k == "cache":
        old = sd.RAW_TOKEN_SET_ENTRIES
        sd.RAW_TOKEN_SET_ENTRIES = c["entries"]
        answers: list[Any] = []
        try:
            for text in c["seq"]:
                try:
                    answers.append(sorted(sd.raw_content_tokens(text)))
                except KeyError:
                    answers.append({"err": "KeyError"})
        finally:
            sd.RAW_TOKEN_SET_ENTRIES = old
        out["r"] = answers
        out["keys"] = _keys()
    elif k in ("fs", "ft"):
        hi = c.get("hi")
        me = c.get("me")
        if k == "fs":
            recs: list[Any] = [_memory(r) for r in c["recs"]]
            fn: Any = sd.find_similar
        else:
            recs = [_tomb(r) for r in c["recs"]]
            fn = sd.find_similar_tombstones
        out["r"] = _run(
            lambda: fn(c["t"], recs, high_threshold=hi, medium_threshold=me)
        )
        out["hi"] = None if hi is None else _bits(hi)
        out["me"] = None if me is None else _bits(me)
        out["keys"] = _keys()
    elif k == "in":
        store = _Store(c)
        fn = sd.find_similar_tombstones_in if c["removed"] else sd.find_similar_in
        out["r"] = [_run(lambda: fn(store, c["t"])) for _ in range(c.get("n", 1))]
        out["log"] = store.log
        out["keys"] = _keys()
    sd.clear_raw_token_set_cache()
    return out


_LONG = (
    "The deploy pipeline runs the integration suite against staging before "
    "promoting any build; flaky network tests are retried twice and a third "
    "failure blocks promotion until the on-call engineer signs off manually."
)

_ASCII_TEXTS = [
    "",
    "   ",
    "the and of",
    "docker-compose up prod",
    "python-frontmatter parses YAML front matter",
    "snake_case_name and kebab-case-name with a.b.c dotted",
    "Running runs ran caches cached",
    "a-the-of",
    "x",
    "URLs APIs alias news",
    "foo--bar__baz ..dots.. -lead trail-",
]


def _ascii_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for t in _ASCII_TEXTS:
        cases.append({"k": "tok", "t": t})
    # Jaccard: empties, disjoint, shared compound, cross notation,
    # stopword parts, the containment gate at 7, 8 and its MEDIUM floor
    # and ceiling.
    many = [f"w{i}" for i in range(30)]
    jac = [
        ([], ["a"]),
        (["a"], []),
        (["a"], ["b"]),
        (["a"], ["a"]),
        (["docker-compose", "prod"], ["docker-compose", "dev"]),
        (["python-frontmatter"], ["python", "frontmatter"]),
        (["python-frontmatter", "x"], ["python-frontmatter", "python"]),
        (["the-and", "x"], ["x"]),
        (["of-cake"], ["cake"]),
        (["a.b"], ["a", "b"]),
        (["a-b-c"], ["a_b_c"]),
        (many[:7], many[:7] + many[10:30]),
        (many[:8], many[:8] + many[10:30]),
        (many[:8], many[:3] + many[10:30]),
        (many[:8], many[:4] + many[10:30]),
        (many[:10], many[:4] + many[10:30]),
        (many[:10], many[:6] + many[10:13]),
        (many[:10], many[:10] + many[10:12]),
        (many[:20], many[5:25]),
        (many[:9], many[:9] + many[9:28]),
        (["a-b", "c"], ["a", "b", "c-d"]),
        (["-", "a"], ["a"]),
    ]
    for a, b in jac:
        cases.append({"k": "jac", "a": a, "b": b})
    bounds = [
        [0, 1, 1],
        [1, 0, 1],
        [1, 1, 0],
        [-1, 5, 5],
        [1, 1, 1],
        [3, 4, 5],
        [5, 5, 5],
        [7, 3, 3],
        [8, 8, 30],
        [4, 10, 30],
        [3, 8, 100],
        [2, 5, 5],
        [9, 10, 10],
        [1, 3, 10],
        [10, 3, 3],
        [2**62, 2**62, 2**62 + 1],
        [2**63 - 1, 2**63 - 1, 2**63 - 1],
        [2**63 - 1, 1, 2**63 - 1],
        [123456789, 987654321, 555555555],
    ]
    for ab in bounds:
        cases.append({"k": "bound", "a": ab})
    snips = [
        "",
        "  short body  ",
        "x" * 200,
        "x" * 201,
        ("word " * 50).strip(),
        "y" * 170 + " " + "z" * 40,
        "y" * 150 + " " + "z" * 60,
        "y" * 159 + " " + "z" * 60,
        "y" * 160 + " " + "z" * 60,
        "y" * 165 + "\n" + "z" * 60,
        "y" * 165 + "\t" + "z" * 60,
        "y" * 165 + "\r" + "z" * 60,
        "y" * 190 + ",;:.- " + "z" * 30,
        "-" * 250,
        "a" * 199 + ".." + "b" * 20,
        "\n\t " + "q" * 230 + " \n",
    ]
    for t in snips:
        cases.append({"k": "snip", "t": t})
    seq = ["alpha beta", "gamma", "alpha beta", "delta", "", "gamma", "eps"]
    for entries in (5000, 3, 1, 0, -1, -5):
        cases.append({"k": "cache", "entries": entries, "seq": seq})
    cases.append({"k": "cache", "entries": 2, "seq": ["a b", "a  b", "a b", "c"]})

    body = "docker-compose up runs the prod stack on port 8080"
    recs = [
        _rec(1, "docker-compose up runs the prod stack on port 8080"),
        _rec(2, "docker-compose up runs the dev stack on port 8080"),
        _rec(3, "docker compose up runs the prod stack on port 8080"),
        _rec(4, "something else entirely unrelated"),
        _rec(5, ""),
        _rec(6, "the and of"),
        _rec(7, "docker-compose up runs the prod stack on port 8080", u=T0),
        _rec(8, "docker-compose up runs the prod stack on port 8080", u=T0),
    ]
    cases.append({"k": "fs", "t": body, "recs": recs})
    cases.append({"k": "fs", "t": body, "recs": []})
    cases.append({"k": "fs", "t": "the and of", "recs": recs})
    cases.append({"k": "fs", "t": "", "recs": recs})
    cases.append({"k": "fs", "t": body, "recs": recs, "hi": 0.9, "me": 0.1})
    cases.append({"k": "fs", "t": body, "recs": recs, "hi": 0.0, "me": 0.0})
    cases.append({"k": "fs", "t": body, "recs": recs, "hi": 2.0, "me": 1.0})
    cases.append({"k": "fs", "t": body, "recs": recs, "hi": float("nan")})
    cases.append({"k": "fs", "t": body, "recs": recs, "me": float("inf")})
    cases.append({"k": "fs", "t": body, "recs": recs, "hi": -1.0, "me": -1.0})
    # Naive against aware: tied similarity raises, untied does not.
    mixed = [
        _rec(1, body, u=NAIVE),
        _rec(2, body),
        _rec(3, "docker-compose up runs the dev stack on port 8080", u=NAIVE),
    ]
    cases.append({"k": "fs", "t": body, "recs": mixed})
    cases.append({"k": "fs", "t": body, "recs": [mixed[0], mixed[2]]})
    cases.append({"k": "fs", "t": body, "recs": [mixed[0], _rec(9, body, u=NAIVE)]})
    plus2 = datetime(2024, 3, 1, 14, 0, tzinfo=timezone(timedelta(hours=2)))
    cases.append(
        {"k": "fs", "t": body, "recs": [_rec(1, body), _rec(2, body, u=plus2)]}
    )
    # Containment: a short restatement of one sentence of a long body.
    short = (
        "integration suite against staging before promoting any build "
        "flaky network tests retried"
    )
    cases.append({"k": "fs", "t": short, "recs": [_rec(1, _LONG)]})
    cases.append({"k": "fs", "t": _LONG, "recs": [_rec(1, short)]})
    cases.append({"k": "fs", "t": "deploy staging", "recs": [_rec(1, _LONG)]})
    # Rounding ties: 12/119 and 13/129 differ and both round to 0.1008.
    new_words = [f"w{i}" for i in range(100)]
    rec_a = new_words[:12] + [f"xa{j}" for j in range(19)]
    rec_b = new_words[:13] + [f"xb{j}" for j in range(29)]
    cases.append(
        {
            "k": "fs",
            "t": " ".join(new_words),
            "recs": [
                _rec(1, " ".join(rec_a), u=T0),
                _rec(2, " ".join(rec_b), u=T0),
                _rec(3, " ".join(rec_a), u=T0),
            ],
            "me": 0.0,
        }
    )
    # A long tied group (past list.sort's 64-item small sort).
    tied = [_rec(i, body, u=T0 + timedelta(seconds=i % 7)) for i in range(70)]
    cases.append({"k": "fs", "t": body, "recs": tied})
    tied_mixed = list(tied)
    tied_mixed[40] = _rec(40, body, u=NAIVE)
    cases.append({"k": "fs", "t": body, "recs": tied_mixed})
    long_body = ("lorem ipsum dolor " * 30) + "docker-compose"
    cases.append({"k": "fs", "t": "lorem ipsum", "recs": [_rec(1, long_body)]})

    rm = T0 + timedelta(days=3)
    tombs = [
        _rec(1, body, rm=rm),
        _rec(2, body, rm=rm + timedelta(hours=1), rr=""),
        _rec(3, "docker compose up runs the dev stack", rm=rm),
        _rec(4, "nothing in common here", rm=rm),
    ]
    cases.append({"k": "ft", "t": body, "recs": tombs})
    cases.append({"k": "ft", "t": body, "recs": []})
    cases.append({"k": "ft", "t": "", "recs": tombs})
    cases.append({"k": "ft", "t": body, "recs": tombs, "hi": 0.5, "me": 0.2})
    cases.append(
        {
            "k": "ft",
            "t": body,
            "recs": [_rec(1, body, rm=NAIVE), _rec(2, body, rm=rm)],
        }
    )
    cases.append(
        {
            "k": "ft",
            "t": body,
            "recs": [_rec(1, body, u=NAIVE, rm=rm), _rec(2, body, rm=rm)],
        }
    )

    toks = sorted(sd._raw_content_token_set(body))
    dev = sorted(sd._raw_content_token_set(recs[1]["b"]))
    store_cases: list[dict[str, Any]] = [
        {"docs": None, "memo": "keep"},
        {"docs": [], "memo": "keep"},
        {"docs": [[_id(1), toks], [_id(2), dev], [_id(4), ["else"]]], "memo": "keep"},
        {"docs": [[_id(1), toks], [_id(1), dev], [_id(2), toks]], "memo": "keep"},
        {"docs": [[_id(1), toks]], "memo": "none"},
        {"docs": [[_id(1), []]], "memo": "keep"},
        {"docs": [[_id(1), toks]], "memo": "keep", "extra": True},
        {"docs": [[_id(7), toks], [_id(8), toks]], "memo": "keep"},
    ]
    for removed in (False, True):
        for sc in store_cases:
            light = [dict(r) for r in recs]
            if removed:
                light = [dict(r, rm=rm.isoformat(), rr="why") for r in recs]
            if sc.get("extra"):
                light[3] = dict(light[3], x=True)
            case = {
                "k": "in",
                "removed": removed,
                "t": body,
                "n": 2,
                "docs": sc["docs"],
                "memo": sc["memo"],
                "light": light,
                "all": recs[:4],
                "tomb": tombs,
            }
            cases.append(case)
    cases.append(
        {
            "k": "in",
            "removed": False,
            "t": "the of",
            "docs": None,
            "memo": "keep",
            "light": [],
            "all": recs,
            "tomb": [],
        }
    )
    cases.append(
        {
            "k": "in",
            "removed": True,
            "t": body,
            "docs": None,
            "memo": "keep",
            "light": [],
            "all": recs,
            "tomb": [],
        }
    )
    return cases


def _unicode_cases() -> list[dict[str, Any]]:
    texts = [
        "Größe ändern straße",
        "日本語 テキスト 中文",
        "café-crème naïve_résumé",
        " nbsp em　ideo",
        "lone \ud800 surrogate",
        "ﬁle ligature Ⅻ",
        "Ωmega ΣΊΣΥΦΟΣ",
    ]
    cases: list[dict[str, Any]] = [{"k": "tok", "t": t} for t in texts]
    cases.append({"k": "jac", "a": ["日本", "語"], "b": ["日本"]})
    cases.append({"k": "jac", "a": ["café-crème"], "b": ["café", "crème"]})
    for t in [
        "   body 　",
        "\u0085x\u001c",
        "é" * 201,
        "é" * 170 + " " + "z" * 40,
        "\ud800" * 205,
        "​" + "x" * 10 + "​",
    ]:
        cases.append({"k": "snip", "t": t})
    cases.append({"k": "cache", "entries": 2, "seq": ["é", "é", "é", "\udc00 x"]})
    body = "Größe ändern straße für café-crème"
    recs = [
        _rec(1, "Grösse andern strasse fur cafe creme"),
        _rec(2, body + " \ud800"),
        _rec(3, "　" + body + " "),
    ]
    cases.append({"k": "fs", "t": body, "recs": recs})
    cases.append(
        {
            "k": "ft",
            "t": body,
            "recs": [dict(r, rm=T0.isoformat(), rr="é") for r in recs],
        }
    )
    return cases


def _sur(v: Any) -> Any:
    """A text holding a lone surrogate as ``{"cps": [...]}``."""
    if isinstance(v, str):
        return {"cps": [ord(ch) for ch in v]} if has_surrogate(v) else v
    if isinstance(v, list):
        return [_sur(x) for x in v]
    if isinstance(v, dict):
        return {k: _sur(x) for k, x in v.items()}
    return v


def _file(name: str, cases: list[dict[str, Any]]) -> bytes:
    lines = [_sur(_case(c)) for c in cases]
    header = {
        "fixture": name,
        "cases": len(lines),
        "high": _bits(sd.HIGH_SIMILARITY),
        "medium": _bits(sd.MEDIUM_SIMILARITY),
        "ceiling": _bits(sd._CONTAINMENT_CEILING),
        "min_tokens": sd._CONTAINMENT_MIN_TOKENS,
        "entries": sd.RAW_TOKEN_SET_ENTRIES,
    }
    return jsonl([header, *lines])


def generate() -> dict[str, bytes]:
    return {"search_dedup.jsonl": _file("search_dedup", _ascii_cases())}


def generate_unicode() -> dict[str, bytes]:
    return {
        "search_dedup_unicode.jsonl": _file("search_dedup_unicode", _unicode_cases())
    }

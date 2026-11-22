"""Fixtures: the snippet path and the token cache of `search`
(``search_snippet.jsonl``, ``search_snippet_unicode.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/search/snippet.rs`` and
``token_cache.rs``. This pins `_query_biased_snippet` and its memos
(`_snippet_tokens`, `_token_surfaces`, `clear_snippet_caches`),
`_memory_tokens`, `seed_memory_tokens` and `clear_token_cache` with their
bounds and eviction order, and `top_hit_leads_runner_up`.

Each line after the header is one case, by ``k``:

- ``top``: ``a`` the top and runner-up scores (bits), ``r`` the answer;
- ``snip``: from empty memos, `_query_biased_snippet` of the body ``t``,
  the matched terms ``m`` and ``mc`` (``max_chars``); ``r`` the snippet;
- ``seq``: under the bounds ``et`` (`SNIPPET_TOKEN_ENTRIES`) and ``es``
  (`TOKEN_SURFACE_ENTRIES`), from empty memos, each call of ``calls``
  (``[t, m, mc]``, or ``"clear"`` for `clear_snippet_caches`); ``r`` each
  answer (null for a clear), ``scans`` and ``surf`` the memos' keys after,
  least recently used first (a scan past 200 characters as its length and
  the first 16 hexadecimal digits of its UTF-8's SHA-256);
- ``mt``: under the bound ``e`` (`TOKEN_CACHE_ENTRIES`), from an empty
  cache, each op of ``ops``: ``["m", body, scopes]`` (`_memory_tokens`,
  answering ``[body, content, scope_set sorted, body_tf as pairs]``),
  ``["s", body, scopes, index_text]`` (`seed_memory_tokens`, answering
  null) or ``["c"]`` (`clear_token_cache`); ``r`` each answer, ``keys``
  the cache's keys after as ``[body, scopes]``;
- ``sweep``: ``n`` snippets of bodies drawn from ``words`` by the 64-bit
  LCG ``x = x * 6364136223846793005 + 1442695040888963407`` from
  ``seed`` (each draw is ``(x >> 33) % len``), each from empty memos;
  ``sha`` the SHA-256 of the answers joined by a NUL.

A body ``t`` is a text or ``[[piece, count], ...]``, the pieces repeated
and joined. A text holding a lone surrogate is ``{"cps": [...]}``. Texts outside ASCII
go in ``_unicode`` (the tokenizer, `str.strip` and `\\w` read the Unicode
data).
"""

from __future__ import annotations

import hashlib
import struct
from types import SimpleNamespace
from typing import Any

from bettermemory import search as s
from bettermemory import search_lexicon as lexicon

from ._common import has_surrogate, jsonl

LONG_PAD = "Opening paragraph about nothing in particular here. " * 6


def _bits(x: float) -> str:
    return f"{struct.unpack('>Q', struct.pack('>d', x))[0]:016x}"


def _sur(v: Any) -> Any:
    if isinstance(v, str):
        return {"cps": [ord(ch) for ch in v]} if has_surrogate(v) else v
    if isinstance(v, list):
        return [_sur(x) for x in v]
    if isinstance(v, dict):
        return {k: _sur(x) for k, x in v.items()}
    return v


def _bounds(et: int, es: int, e: int) -> tuple[int, int, int]:
    old = (s.SNIPPET_TOKEN_ENTRIES, s.TOKEN_SURFACE_ENTRIES, s.TOKEN_CACHE_ENTRIES)
    s.SNIPPET_TOKEN_ENTRIES, s.TOKEN_SURFACE_ENTRIES, s.TOKEN_CACHE_ENTRIES = et, es, e
    return old


def _text(t: Any) -> str:
    """A text, or ``[[piece, count], ...]`` spelling a long one compactly."""
    return t if isinstance(t, str) else "".join(p * n for p, n in t)


def _key(t: str) -> Any:
    """A memo key: the text, or past 200 characters its length and the
    first 16 hexadecimal digits of the SHA-256 of its UTF-8."""
    if len(t) <= 200:
        return t
    return [len(t), hashlib.sha256(t.encode("utf-8", "surrogatepass")).hexdigest()[:16]]


def _snip(t: str, m: list[str], mc: int) -> str:
    s.clear_snippet_caches()
    return s._query_biased_snippet(t, m, mc)


def _toks(t: Any) -> list[Any]:
    return [list(t.body), list(t.content), sorted(t.scope_set), list(t.body_tf.items())]


def _case(c: dict[str, Any]) -> dict[str, Any]:
    k = c["k"]
    out = dict(c)
    if k == "top":
        out["a"] = [_bits(x) for x in c["a"]]
        out["r"] = s.top_hit_leads_runner_up(*c["a"])
    elif k == "snip":
        out["r"] = _snip(_text(c["t"]), c["m"], c["mc"])
    elif k == "seq":
        old = _bounds(c["et"], c["es"], s.TOKEN_CACHE_ENTRIES)
        try:
            s.clear_snippet_caches()
            r: list[Any] = []
            for call in c["calls"]:
                if call == "clear":
                    s.clear_snippet_caches()
                    r.append(None)
                else:
                    r.append(s._query_biased_snippet(_text(call[0]), call[1], call[2]))
            out["r"] = r
            out["scans"] = [_key(k) for k in s._SNIPPET_TOKENS]
            out["surf"] = list(s._TOKEN_SURFACES)
        finally:
            _bounds(*old)
            s.clear_snippet_caches()
    elif k == "mt":
        old = _bounds(s.SNIPPET_TOKEN_ENTRIES, s.TOKEN_SURFACE_ENTRIES, c["e"])
        try:
            s.clear_token_cache()
            r = []
            for op in c["ops"]:
                if op[0] == "c":
                    s.clear_token_cache()
                    r.append(None)
                    continue
                mem = SimpleNamespace(body=op[1], scopes=list(op[2]))
                if op[0] == "m":
                    r.append(_toks(s._memory_tokens(mem)))  # type: ignore[arg-type]
                else:
                    s.seed_memory_tokens(mem, op[3])  # type: ignore[arg-type]
                    r.append(None)
            out["r"] = r
            out["keys"] = [[b, list(sc)] for b, sc in s._TOKEN_CACHE]
        finally:
            _bounds(*old)
            s.clear_token_cache()
    elif k == "sweep":
        out["sha"] = _sweep(c["seed"], c["n"], c["words"])
    return out


def _sweep(seed: int, n: int, words: list[str]) -> str:
    x = seed
    mask = (1 << 64) - 1

    def draw(m: int) -> int:
        nonlocal x
        x = (x * 6364136223846793005 + 1442695040888963407) & mask
        return (x >> 33) % m

    h = hashlib.sha256()
    for i in range(n):
        length = 5 + draw(120)
        body = " ".join(words[draw(len(words))] for _ in range(length))
        matched = [words[draw(len(words))] for _ in range(1 + draw(3))]
        mc = 20 + draw(200)
        if i:
            h.update(b"\0")
        h.update(_snip(body, matched, mc).encode("utf-8", "surrogatepass"))
    return h.hexdigest()


SWEEP_WORDS = [
    "alpha", "beta", "c++", "C#", "f#", ".NET", "make-target", "make", "target",
    "python", "3.12.1", "v1.2", "the", "and", "foo_bar", "foo", "bar", "x",
    "deploy-script", "script", "Rust.", "(gamma)", "delta,", "--", "a-b-c",
    "\n", "ok", "running", "runs", "API", "apis",
]  # fmt: skip


def _snip_rows(cases: list[dict[str, Any]], t: Any, m: list[str], mc: int) -> None:
    """A snippet row for the terms as written, and one for each term's first
    token (the stemmed form a search's ``matched`` holds) when that differs."""
    cases.append({"k": "snip", "t": t, "m": m, "mc": mc})
    norm = [(lexicon.tokenize(w) or [w])[0] for w in m]
    if norm != m:
        cases.append({"k": "snip", "t": t, "m": norm, "mc": mc})


def _ascii_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for a in [
        (1.0, 0.0), (0.0, 0.0), (-1.0, -2.0), (0.0, -1.0), (1.0, 1.0),
        (1.0328, 1.0), (1.0327868852459017, 1.0), (1.0327868852459016, 1.0),
        (63.0, 61.0), (62.999999, 61.0), (5e-324, 0.0), (float("inf"), 1.0),
        (float("inf"), float("inf")), (float("nan"), 1.0), (1.0, float("nan")),
        (2.0, 1e308), (1e308, 1e308 / 1.03), (-0.0, -0.0), (1.0, -0.0),
    ]:  # fmt: skip
        cases.append({"k": "top", "a": list(a)})

    def snip(t: Any, m: list[str], mc: int = 200) -> None:
        _snip_rows(cases, t, m, mc)

    far = LONG_PAD + "The deploy script now runs with python 3.12.1 on the box."
    snip("short body", ["short"])
    snip("  short body  ", [])
    snip(far, [])
    snip(far, ["deploy"])
    snip(far, ["python"])
    snip(far, ["3.12.1"])
    snip(far, ["3.12"])
    snip(far, ["absent"])
    snip(far, ["script", "deploy", "box"])
    snip(far, ["opening"])
    snip(far, ["paragraph"])
    snip(far, ["deploy"], 60)
    snip(far, ["deploy"], 43)
    snip(far, ["deploy"], 10)
    snip(far, ["deploy"], 3)
    snip(far, ["deploy"], 0)
    snip(far, ["deploy"], -5)
    snip(far, ["deploy"], len(far.strip()))
    snip(far, ["deploy"], len(far.strip()) - 1)
    kebab = LONG_PAD + "We moved to Make Target last week; make-target is fine."
    snip(kebab, ["make-target"])
    snip(LONG_PAD + "We moved to Make Target last week, it is fine.", ["make-target"])
    # best anchor at exactly offset 40 after a non-token first character
    snip("(" + "z" * 38 + " alpha " + "y " * 200, ["alpha"])
    # the alias start of ".net" is the snap target inside a run of non-token characters
    snip("word " * 60 + "!" * 50 + " .net rest " + "q " * 100, ["dotnet"])
    snip(LONG_PAD + "Use the foo_bar helper and foo later.", ["foo-bar"])
    snip(LONG_PAD + "Use the foo_bar helper and foo later.", ["foo_bar"])
    snip(LONG_PAD + "Version v1.2.3 and 1.2.3 shipped.", ["1.2.3"])
    snip(LONG_PAD + "Version v1.2.3 and 1.2.3 shipped.", ["v1.2.3"])
    snip(LONG_PAD + "Version 1.2.3 shipped.", ["2"])
    snip(LONG_PAD + "Deployed deploys deploying.", ["deploy"])
    for spelled in [
        "C++",
        "c++",
        "C#",
        "c#",
        "F#",
        ".NET",
        ".Net",
        "x.net",
        "C#x",
        "aC++",
    ]:
        alias = {
            "c+": "cpp",
            "c#": "csharp",
            "f#": "fsharp",
            ".n": "dotnet",
            "x.": "dotnet",
            "ac": "cpp",
        }[spelled[:2].lower()]
        snip(LONG_PAD + f"We write {spelled} code here, mostly {spelled}.", [alias])
    snip(LONG_PAD + "Build with C++ and C# daily.", ["cpp-csharp"])
    snip(
        LONG_PAD + "Build with C++C++ and c#c# and .NET.NET daily.",
        ["cpp", "csharp", "dotnet"],
    )
    snip("c++ " + LONG_PAD + " c++ tail", ["cpp"])
    dense = (
        LONG_PAD
        + " ".join(["alpha beta"] * 10)
        + " "
        + "gap " * 60
        + " ".join(["alpha"] * 30)
    )
    snip(dense, ["alpha"])
    snip(dense, ["alpha"], 80)
    snip(dense, ["beta", "alpha"], 120)
    uniform = " ".join(f"w{i} key" for i in range(80))
    snip(uniform, ["key"])
    snip(uniform, ["w40"])
    snip(uniform, ["w40"], 45)
    snip(LONG_PAD + "-- -- ---dash-led token here.", ["dash-led"])
    snip([[LONG_PAD, 1], ["x", 300], [" needle", 1]], ["needle"])
    snip([[LONG_PAD, 1], ["needle", 1], ["y", 400]], ["needle"])
    snip([["x", 8100], [" needle", 1]], ["needle"])
    snip([["pad ", 1990], ["needle tail and more words after it", 1]], ["needle"])
    snip([["pad ", 1999], ["needle", 1]], ["needle"])
    snip([["pad ", 1998], ["c++ end", 1]], ["cpp"])
    snip([["pad ", 1998], [".NET end", 1]], ["dotnet"])
    snip("\n\n" + LONG_PAD + "\n\nThe needle here.\n\n", ["needle"])
    snip("First paragraph line.\n\n" + LONG_PAD + "Needle sits here.", ["needle"])
    snip(LONG_PAD + "Isn't it the needle's eye?", ["needle"])
    snip(LONG_PAD + "the and of needle", ["the", "needle"])
    snip(LONG_PAD + "aaaa\udc80bbbb needle", ["needle"])
    snip(LONG_PAD + "needle", ["", "needle"])
    snip(LONG_PAD + "needle", ["needle", "needle"])
    snip(LONG_PAD + "a-b and b-a then needle", ["a-b", "b-a"])

    b1 = LONG_PAD + "alpha needle"
    b2 = LONG_PAD + "beta needle"
    b3 = LONG_PAD + "gamma needle"
    cases.append({"k": "seq", "et": 2, "es": 5, "calls": [
        [b1, ["needl"], 200], [b2, ["needl"], 200], [b1, ["alpha"], 200],
        [b3, ["needl"], 200], [b2, ["beta"], 200],
    ]})  # fmt: skip
    cases.append({"k": "seq", "et": 0, "es": 0, "calls": [[b1, ["needl"], 200]]})
    cases.append({"k": "seq", "et": 1, "es": 3, "calls": [
        [b1, ["needl"], 200], "clear", [b2, ["needl"], 200], ["short", ["x"], 200],
    ]})  # fmt: skip
    cases.append({"k": "seq", "et": 5000, "es": 20000, "calls": [
        [[["pad ", 2100], ["x", 1]], ["pad"], 200], [[["pad ", 2200], ["y", 1]], ["pad"], 200],
    ]})  # fmt: skip

    cases.append({"k": "mt", "e": 2, "ops": [
        ["m", "Deploy the make-target script", ["projects:foo-bar"]],
        ["m", "Deploy the make-target script", ["projects:foo-bar"]],
        ["m", "Deploy the make-target script", []],
        ["m", "Deploy the make-target script", ["projects:foo-bar"]],
        ["m", "second body body body", ["a", "b"]],
        ["m", "second body body body", ["b", "a"]],
    ]})  # fmt: skip
    cases.append({"k": "mt", "e": 3, "ops": [
        ["s", "one", ["s1"], "uno dos  uno"],
        ["m", "one", ["s1"]],
        ["s", "one", ["s1"], "other"],
        ["s", "two", [], ""],
        ["m", "two", []],
        ["s", "three", ["x_y"], "the cat the"],
        ["m", "four", ["x"]],
        ["m", "one", ["s1"]],
        ["c"],
        ["m", "one", ["s1"]],
    ]})  # fmt: skip
    cases.append(
        {"k": "mt", "e": 0, "ops": [["m", "zero", []], ["s", "zero", [], "z"]]}
    )
    cases.append({"k": "mt", "e": 5000, "ops": [
        ["m", "", []], ["m", "C++ and .NET with Isn't it", ["c#", "repo:a.b"]],
        ["m", "b\udc80ody", []],
    ]})  # fmt: skip
    cases.append({"k": "sweep", "seed": 20261121, "n": 1500, "words": SWEEP_WORDS})
    return cases


def _unicode_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []

    def snip(t: Any, m: list[str], mc: int = 200) -> None:
        _snip_rows(cases, t, m, mc)

    snip(LONG_PAD + "Wir nutzen éC++ und C++ täglich.", ["cpp"])
    snip(LONG_PAD + "Café .NETé und .NET.", ["dotnet"])
    snip(LONG_PAD + "Zürich café naïve needle", ["zurich"])
    snip(LONG_PAD + "Zürich café naïve needle", ["cafe"])
    snip(LONG_PAD + "ＧＰＵ２０２６ der Server", ["gpu2026"])
    snip(LONG_PAD + "東京オフィスは移転する予定", ["東京"])
    snip(LONG_PAD + "東京オフィスは移転する予定", ["移転"])
    snip("　 " + LONG_PAD + "needle  ", ["needle"])
    snip(LONG_PAD + "Ｃ＋＋ and C＋＋ both", ["cpp"])
    snip(LONG_PAD + "ﬁle ﬂow office", ["file"])
    snip(LONG_PAD + "café decomposed", ["cafe"])
    snip(LONG_PAD + "STRASSE straße", ["strasse"])
    snip(LONG_PAD + "İstanbul and ıstanbul", ["istanbul"])
    snip(LONG_PAD + "٣.١٢ arabic digits", ["3.12"])
    snip([["ä", 8100], [" needle", 1]], ["needle"])
    cases.append({"k": "mt", "e": 5000, "ops": [
        ["m", "Zürich ＧＰＵ東京オフィス", ["projects:café-bar"]],
        ["s", "x", ["Ünï"], "zurich gpu"],
    ]})  # fmt: skip
    cases.append({"k": "sweep", "seed": 7, "n": 300, "words": [
        *SWEEP_WORDS, "Zürich", "café", "東京", "ＧＰＵ", "éc++", "naïve",
    ]})  # fmt: skip
    return cases


def _file(name: str, cases: list[dict[str, Any]]) -> bytes:
    lines = [_sur(_case(c)) for c in cases]
    header = {
        "fixture": name,
        "cases": len(lines),
        "scan": s._SNIPPET_SCAN_CHARS,
        "lead": s._SNIPPET_LEAD_CHARS,
        "token_entries": s.SNIPPET_TOKEN_ENTRIES,
        "surface_entries": s.TOKEN_SURFACE_ENTRIES,
        "cache_entries": s.TOKEN_CACHE_ENTRIES,
        "margin": _bits(s.EXPAND_TOP_SCORE_MARGIN),
    }
    return jsonl([header, *lines])


def generate() -> dict[str, bytes]:
    return {"search_snippet.jsonl": _file("search_snippet", _ascii_cases())}


def generate_unicode() -> dict[str, bytes]:
    return {
        "search_snippet_unicode.jsonl": _file(
            "search_snippet_unicode", _unicode_cases()
        )
    }

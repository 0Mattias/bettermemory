"""Fixtures: phrase-match attribution (``attribution.jsonl``, not gated, and
``attribution_unicode.jsonl``, gated on the Unicode version).

The native build reads them in
``native/crates/bettermemory-core/src/attribution.rs``. This pins it to
`attribution`'s `attribute_uses` and, each on its own, `_candidate_sentences`,
`_normalize`, `_content_token_set` and `_sentence_matches`, and through them
`_SENTENCE_SPLIT_RE`, `_PUNCT_STRIP_RE`, `_TOKEN_RE` and `_STOPWORDS`.

One JSON object per line, keys sorted, ASCII. The first line of each file
is a header: ``{"k": "header", "count": n, ...}``; the ungated file's also
holds ``stopwords`` (`_STOPWORDS`, sorted) and ``limits`` (the six
thresholds by name). Every other line is a case ``{"f": function, ...,
"o": answer}``:

- ``"cand"``: ``a`` a body, ``o`` the candidate sentences;
- ``"norm"``: ``a`` a text, ``o`` `_normalize` of it;
- ``"tok"``: ``a`` a text, ``o`` the token set, sorted;
- ``"match"``: ``a`` ``[sentence, reply]``, ``o`` `_sentence_matches`
  with the haystack and reply tokens `attribute_uses` derives from
  ``reply``;
- ``"uses"``: ``m`` the memories (``[[id, body], ...]``, the dict's
  order), ``t`` the reply, ``o`` ``[[memory_id, claim_excerpt], ...]``.

A text is a JSON string when it holds no surrogate, else ``{"c": [code
points]}``; a long one is a recipe ``{"r": [[text, n], ...]}``, the
concatenation of each text ``n`` times. A case whose inputs hold only
ASCII goes in ``attribution.jsonl`` (its answer reads no Unicode table:
`str.lower`, NFC, `str.isspace` and `re`'s ``\\s`` and ``\\w`` of ASCII
are the same in every version); every other case goes in
``attribution_unicode.jsonl``, whose answers read the running Python's
`unicodedata` (NFC, `str.lower`, `str.isspace`, ``\\w``). Nothing depends
on the Python release or the platform.
"""

from __future__ import annotations

import random
from typing import Any

from bettermemory import attribution as at

from ._common import jsonl

_Text = Any  # a str, or ("r", [(str, n), ...])


def _built(t: _Text) -> str:
    if isinstance(t, str):
        return t
    return "".join(piece * n for piece, n in t[1])


def _spelled(t: _Text) -> Any:
    if isinstance(t, str):
        if any(0xD800 <= ord(c) <= 0xDFFF for c in t):
            return {"c": [ord(c) for c in t]}
        return t
    return {"r": [[_spelled(piece), n] for piece, n in t[1]]}


def _out(text: str) -> Any:
    return _spelled(text)


def _ascii(*texts: _Text) -> bool:
    return all(_built(t).isascii() for t in texts)


class _Cases:
    def __init__(self) -> None:
        self.plain: list[dict[str, Any]] = []
        self.unicode: list[dict[str, Any]] = []

    def _put(self, record: dict[str, Any], *texts: _Text) -> None:
        (self.plain if _ascii(*texts) else self.unicode).append(record)

    def cand(self, body: _Text) -> None:
        got = at._candidate_sentences(_built(body))
        self._put({"f": "cand", "a": _spelled(body), "o": [_out(s) for s in got]}, body)

    def norm(self, text: _Text) -> None:
        got = at._normalize(_built(text))
        self._put({"f": "norm", "a": _spelled(text), "o": _out(got)}, text)

    def tok(self, text: _Text) -> None:
        got = sorted(at._content_token_set(_built(text)))
        self._put({"f": "tok", "a": _spelled(text), "o": got}, text)

    def match(self, sentence: _Text, reply: _Text) -> None:
        r = _built(reply)
        got = at._sentence_matches(
            _built(sentence), at._normalize(r), at._content_token_set(r)
        )
        self._put(
            {"f": "match", "a": [_spelled(sentence), _spelled(reply)], "o": got},
            sentence,
            reply,
        )

    def uses(self, memories: list[tuple[str, _Text]], reply: _Text) -> None:
        got = at.attribute_uses({k: _built(b) for k, b in memories}, _built(reply))
        record = {
            "f": "uses",
            "m": [[_spelled(k), _spelled(b)] for k, b in memories],
            "t": _spelled(reply),
            "o": [[_out(m.memory_id), _out(m.claim_excerpt)] for m in got],
        }
        self._put(record, reply, *(k for k, _ in memories), *(b for _, b in memories))

    def every(self, text: _Text) -> None:
        self.cand(text)
        self.norm(text)
        self.tok(text)


_S1 = "The auth middleware lives in src/auth/middleware.py today."
_S2 = "Deploys run from the release branch through the staging pipeline."
_S3 = "this is the thing that we did and it was fine and good"
_FILLER = "a b c d e f g h i j"

_TEXTS: list[_Text] = [
    "",
    " ",
    "\n\n",
    _S1,
    _S2,
    _S3,
    _S1 + " " + _S2,
    _S1 + "\n" + _S2,
    _S1 + "\n\n\n" + _S2,
    "Version v2.6.0 shipped on the 1.5 branch; see docs/release.md for details. Next.",
    "Dr. Smith wrote the parser for the config loader in 2024! Really? Yes indeed it works.",
    "e.g. foo bar baz qux quux corge grault garply waldo fred plugh",
    "  leading and trailing whitespace around this sentence of words  ",
    "Ends with many marks of punctuation at the end here!!!???...",
    "Ends with marks then space at the end of this one ...  ",
    "...Leading dots before the words begin in this odd sentence here",
    "(quoted) [bracket] {brace} 'single' \"double\" tokens, with commas; semicolons.",
    "THE CONFIG FILE LIVES UNDER ~/.config/bettermemory/config.toml ON LINUX",
    "Tabs\tbetween\tthe\twords\tof\tthis\tsentence\there\tnow",
    "Form\x0cfeeds\x0band\x1cfile\x1dgroup\x1erecord\x1funit separators split these",
    "A.B.C. D.E.F. tokens with dots inside and a final dot.",
    "short. tiny! odd? but this sentence is long enough to count as one",
    "Exactly six tokens and thirty chars x",
    "five tokens of thirty characters",
    "abcdefghijklmnopqrstuvwxyz0123456789 a b c d e",
    "a an the and or but if then is are was were be been being do does did",
    "a an the and or but if then is are was were be been being do does did zebra",
    "a an the and or but zebra yak xenon wolf",
    "a an the and or but if zebra yak xenon",
    "the the the the zebra",
    "the. the, the; (the) \"the\" 'the' zebra yak quagga",
    "snake_case_names and CamelCaseNames with digits 123 and 4.56 too",
    "under_score __dunder__ _private trailing_ ___ and more words",
    "x" * 29 + " a b c d e",
    "x" * 30 + " a b c d e",
    "y" * 25 + " a b c d e.",
    "y" * 26 + " a b c d e.",
    "z" * 26 + " a b c d e ...",
    "One sentence.Two sentence.Three sentence with no spaces after dots",
    "Question? Answer! Statement. Another statement with many words in it",
    "\n\nblank lines lead this body which has enough words in it\n\n",
    "line one has words enough to be a sentence today\rcarriage return",
    "Windows line endings are here with words enough\r\nand the next line too",
]


def _random_texts(rng: random.Random, n: int) -> list[str]:
    words = [
        "the",
        "The",
        "THE",
        "auth",
        "middleware",
        "src/auth.py",
        "v1.5",
        "config.",
        "deploy,",
        "(staging)",
        "a",
        "is",
        "of",
        "release",
        "branch",
        "pipeline",
        "token_x",
        "42",
        "--flag",
        "...",
        "it",
        "with",
        "zebra",
        "Yak",
    ]
    seps = [" ", " ", " ", "  ", "\t", "\n", "\n\n", ". ", "! ", "? ", ".", "\x0b"]
    out = []
    for _ in range(n):
        parts = []
        for _ in range(rng.randrange(0, 40)):
            parts.append(rng.choice(words))
            parts.append(rng.choice(seps))
        out.append("".join(parts))
    return out


_UNICODE: list[_Text] = [
    "Café au lait is served at the corner shop every single morning",
    "Café au lait is served at the corner shop every single morning",
    "naïve résumé handling in the parser module for this release",
    "İstanbul office runs the deploy pipeline for every release now",
    "The K scale and Ångström units appear in the physics module here",
    "ΟΔΥΣΣΕΥΣ Σ sigma words in greek text here are long",
    "Straße names in German addresses get folded by the lowercase step",
    "Non breaking spaces join these words together now",
    "Ideographic　space　between　these　words　of　text",
    "Line separator and paragraph separator split these words apart",
    "Next\u0085line control between words of this sentence goes here",
    "Zero​width space does not split these words of the sentence at all",
    "ＡＢＣ fullwidth letters and １２３ digits in the text here",
    "¿Qué? ¡Hola! señor, the punctuation is not ASCII here at all",
    "“Curly quotes” and ‘single ones’ wrap these tokens of words",
    "Lone \ud800 surrogate inside a sentence of enough words to count here",
    "\udfff trailing low surrogate sentence with enough words to count here",
    "Hangul 각 jamo compose under NFC into one syllable here now",
    "Combining marks ą́ reorder under NFC canonical ordering rules here",
    "١٢٣ Arabic-Indic digits are word characters in re here",
    "Emoji \U0001f600 and \U0001f680 rockets are not word characters at all",
    "Deseret \U00010400\U00010401 capitals lower to small letters in this text",
    "Ω ohm sign maps to omega under NFC normalization in this text",
    "ÀÁÂ accented capitals lowercase into accented small letters",
    "dash—joined–words and…ellipsis inside this long sentence",
]


def _cases() -> _Cases:
    c = _Cases()
    for t in _TEXTS + _UNICODE:
        c.every(t)
    rng = random.Random(20261119)
    for t in _random_texts(rng, 120):
        c.every(t)
    # Long inputs: the 500-character excerpt cap, a long reply.
    long_sentence = "word" + " alpha" * 120 + " end"
    c.cand(long_sentence)
    c.cand(("r", [("alpha beta gamma delta ", 30), ("end.", 1)]))
    c.cand(("r", [("x", 499), (" a b c d e", 1)]))
    c.cand(("r", [("x", 501), (" a b c d e", 1)]))
    c.norm(("r", [("A  \t\n", 200)]))
    # The two tiers of a match.
    pairs: list[tuple[_Text, _Text]] = [
        (_S1, "As noted, " + _S1.lower() + " Fine."),
        (_S1, _S1.replace(" ", "\n  ")),
        (_S1, "Middleware for auth lives in src auth middleware py"),
        (_S1, "the middleware lives in src"),
        (_S1, "auth middleware lives src"),
        (_S1, "auth middleware lives src middleware py"),
        (_S2, "Staging pipeline deploys release branch"),
        (_S2, "deploys run release"),
        (_S2, "deploys run release branch"),
        (
            "one two three four five six seven eight nine ten",
            "one two three four five six",
        ),
        ("one two three four five six seven eight nine ten", "one two three four five"),
        ("one two three four five six seven eight nine ten", "one two three four"),
        ("one two three four five", "one two three four"),
        ("one two three four five six seven", "one two three four"),
        ("the a an of", "the a an of"),
        ("alpha alpha alpha alpha beta", "alpha beta gamma"),
        ("...", "..."),
        ("   ", "anything"),
        (_S1, ""),
        (_S1, ("r", [("auth middleware lives ", 300), ("src", 1)])),
        (("r", [("ab", 50)]), ("r", [("ab", 49), ("a", 1)])),
        (("r", [("ab", 50)]), ("r", [("ab", 51)])),
        ("café crème brûlée dessert menu", "café crème"),
        ("café au lait menu item", "CAFÉ AU LAIT MENU ITEM"),
        ("café menu item list today", "caf menu item list today"),
    ]
    for s, r in pairs:
        c.match(s, r)
    # Whole calls.
    m1 = ("m1", _S1)
    m2 = ("m2", _S2 + " " + _S1)
    m3 = ("m3", _S3)
    m4 = ("m4", "")
    reply = "Here is what I found. " + _S1 + " Also " + _S2.lower()
    c.uses([], reply)
    c.uses([m1], "")
    c.uses([m1], " \n\t ")
    c.uses([m1, m2, m3, m4], reply)
    c.uses([m3, m2, m1], reply)
    c.uses([m1, m2], "unrelated text with nothing in common")
    c.uses([m1, m2], "auth middleware lives src staging pipeline release branch")
    c.uses([("", _S1), ("dup", _S1)], _S1)
    c.uses([("\ud800id", _S1), ("café", _S2)], _S1 + " " + _S2)
    c.uses(
        [("big", ("r", [(_S2 + " ", 200)]))],
        ("r", [("filler words here ", 500), (_S2, 1)]),
    )
    bodies = _random_texts(rng, 60)
    replies = _random_texts(rng, 30)
    for i, r in enumerate(replies):
        mems = [(f"r{j}", bodies[(2 * i + j) % len(bodies)]) for j in range(3)]
        c.uses(mems, r)
    return c


def _header(cases: list[dict[str, Any]], full: bool) -> dict[str, Any]:
    header: dict[str, Any] = {"k": "header", "count": len(cases)}
    if full:
        header["stopwords"] = sorted(at._STOPWORDS)
        header["limits"] = {
            "MIN_TOKEN_COUNT": at._MIN_TOKEN_COUNT,
            "MIN_CHAR_COUNT": at._MIN_CHAR_COUNT,
            "MAX_EXCERPT_CHARS": at._MAX_EXCERPT_CHARS,
            "MIN_CONTAINMENT": at._MIN_CONTAINMENT,
            "MIN_CONTAINMENT_TOKENS": at._MIN_CONTAINMENT_TOKENS,
            "STOPWORD_RATIO_MAX": at._STOPWORD_RATIO_MAX,
        }
    return header


def generate() -> dict[str, bytes]:
    """``attribution.jsonl``: not gated."""
    cases = _cases().plain
    return {"attribution.jsonl": jsonl([_header(cases, True), *cases])}


def generate_unicode() -> dict[str, bytes]:
    """``attribution_unicode.jsonl``: gated on the Unicode version."""
    cases = _cases().unicode
    return {"attribution_unicode.jsonl": jsonl([_header(cases, False), *cases])}

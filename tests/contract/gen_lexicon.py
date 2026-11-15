"""Fixture 1: the lexicon (`bettermemory.search_lexicon`).

For each corpus string: `tokenize`, `_tokenize_unstemmed`, the stopword
strip of the tokens, `fts_index_text`, `fts_match_query`, the diacritic
fold of the lowered text, and per distinct token its kebab parts. The
header carries `tokenizer_fingerprint` and the canonical spelling-table
text it hashes, so a native build can reproduce the digest without Python.
"""

from __future__ import annotations

import hashlib
from typing import Any

from bettermemory import search_lexicon as lx

from ._common import jsonl, text_field

_STEM_WORDS = (
    "cookies cookie policies policy caches cache branches branch classes class "
    "status statuses analysis analyses basis redis k8s aws dns yes guys guy "
    "ties tie lies alias aliases atlas atlases bias biases canvas canvases lens "
    "lenses news apis api clis cpus gpus guis skus uris tree trees free note "
    "notes here does these ares movies movie rookies hoodie standups standup "
    "boxes buses gases heroes potatoes studies study days keys toys plays "
    "address addresses process processes series species ss sss s es ies "
    "abcs a1s 3ds 10s x-rays docker-containers pre- -post --flag a--b"
).split()

_IDENTIFIERS = (
    "python-frontmatter",
    "docker_compose",
    "docker-compose",
    "__init__",
    "_private",
    "trailing_",
    "camelCaseIdentifier",
    "PascalCaseName",
    "HTTPServerError",
    "snake_case_with_digits_2",
    "kebab-case-with-digits-2026",
    "mixed_kebab-snake_name",
    "a-b-c-d-e-f",
    "x--y__z",
    "zephyr-quartz-9417",
    "src/bettermemory/search_lexicon.py",
    "/usr/local/bin/tool",
    "C:\\Users\\example\\file.txt",
    "~/notes/2026-01-02.md",
    "./relative/path-to/file_name.rs",
    "https://example.com/a/b?c=d&e=f#frag",
    "user@example.com",
    "v1.2.3",
    "3.12.1",
    "16.3",
    "1.2.3.4.5",
    "10.0.0.1:8080",
    "0.1",
    ".5",
    "5.",
    "1..2",
    "2026-11-15T08:09:10.123Z",
    "sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
    "deadbeefcafebabe0123456789abcdef",
    "4dfce9aa",
    "0x7fffffff",
    "1e10",
    "-42",
    "+7",
    "C++ C++20 c++17 c# C# f# F# .NET .net asp.net dotnet",
    "what's don't I'm we'll they're you've she'd users' o'clock it’s",
    "rock'n'roll",
    "'quoted' \"double\" `backtick`",
)

_SCRIPTS = (
    "Zürich café naïve résumé façade Ångström",
    "Tjörn",
    "Tjo\u0308rn",
    "e\u0301",
    "\u212b",
    "ß straße STRASSE ẞ",
    "øresund Æble ĳssel Œuvre",
    "İstanbul ıi",
    "ǅemal ǈubljana",
    "Ελληνικά αλφάβητο ΣΟΦΟΣ σοφός",
    "Русский язык Ёлка",
    "עברית שלום",
    "العربية مرحبا",
    "हिन्दी नमस्ते",
    "தமிழ்",
    "ภาษาไทยสวัสดีครับ",
    "東京オフィスは移転する予定",
    "猫",
    "2026年に移転",
    "docker-東京",
    "東京-docker",
    "ｻｰﾊﾞｰ ﾊﾟｰﾃｨｰ",
    "ＧＰＵ２０２６",
    "한국어 텍스트입니다",
    "ㄱㄴㄷ",
    "中文文本处理",
    "𠀀𠀁𠀂 𪚥",
    "ひらがな カタカナ ㇰㇱ",
    "ꥠꥡ",
    "豈更車",
    "² ³ ¹ ½ ﬁ ﬂ ㎞ ™ ℃ Ⅻ ①",
    "😀 🎉🎉 👍🏽 👨‍👩‍👧 🇸🇪",
    "text😀text emoji_😀_name",
    "\u200b zero\u200bwidth \u00a0nbsp \u2003emspace",
    "tab\tnew\nline\r\nreturn",
    "\x00 nul \x7f del",
    "mixed Ελλη-nika ру-ss",
    "١٢٣ ٤٥٦ ०१२ ௧௨",
    "ⅰⅱⅲ",
    "\U0001d400\U0001d401 math bold",
    "a\u0300\u0301\u0302 stacked",
    "\u0301 leading combining",
    "\ufeffbom",
    "\ufffd replacement",
)

_PROSE = (
    "How do I configure the docker compose file for the staging cluster?",
    "The store keeps an HMAC-chained log of every write.",
    "Anything stored about the deployment process?",
    "Och det är en bra dag för att skriva kod",
    "Der Hund und die Katze sind im Garten",
    "Le chat et le chien sont dans la maison",
    "El perro y el gato están en la casa todos los días",
    "We'll migrate the users' tables before the 3.12.1 release",
    "Postgres 16.3 replaced 15.4 in the 2026-Q1 upgrade",
    "the a an of to in",
    "",
    " ",
    "   \t\n  ",
    "-",
    "_",
    "--",
    "...",
    "!!!",
    "a",
    "I",
)

_SURROGATES = (
    "lone \ud800 high",
    "lone \udfff low",
    "file\udcff name",
    "\udc80\udc81",
    "pair-as-halves \ud83d\ude00",
)


def _stopword_entries() -> list[str]:
    out: list[str] = []
    for table in (
        lx._STOPWORDS_EN,
        lx._STOPWORDS_SV,
        lx._STOPWORDS_DE,
        lx._STOPWORDS_FR,
        lx._STOPWORDS_ES,
    ):
        words = sorted(table)
        out.append(" ".join(words))
        for word in words:
            out.append(word)
            out.append(word + "s")
            out.append(word + "es")
            out.append(word.upper())
    return out


def _combinations() -> list[str]:
    pieces = ("docker", "Café", "東京", "16.3", "api", "k8s", "C++", "what's", "😀")
    joins = (" ", "-", "_", ".", "/", "", "—")
    out = []
    for a in pieces:
        for b in pieces:
            for j in joins:
                out.append(f"{a}{j}{b}")
    return out


def corpus() -> list[str]:
    items: list[str] = []
    items.extend(_STEM_WORDS)
    items.extend(w.capitalize() for w in _STEM_WORDS)
    items.extend(w.upper() for w in _STEM_WORDS)
    items.extend(_IDENTIFIERS)
    items.extend(_SCRIPTS)
    items.extend(_PROSE)
    items.extend(_SURROGATES)
    items.extend(_stopword_entries())
    items.extend(_combinations())
    items.extend(lx._FINGERPRINT_PROBES)
    items.append("word " * 2000)
    items.append("東京" * 1500)
    items.append("x" * 5000)
    items.append("-".join(["seg"] * 600))
    items.append(" ".join(f"tok{i}-part{i}" for i in range(400)))
    return list(dict.fromkeys(items))


def _entry(text: str) -> dict[str, Any]:
    tokens = lx.tokenize(text)
    entry: dict[str, Any] = {
        **text_field("input", text),
        "tokens": tokens,
        "unstemmed": lx._tokenize_unstemmed(text),
        "content_tokens": lx._strip_stopwords(tokens),
        "index_text": lx.fts_index_text(text),
        "match_query": lx.fts_match_query(text),
        **text_field("folded", lx._fold_diacritics(text.lower())),
        "kebab_parts": {t: lx._kebab_parts(t) for t in dict.fromkeys(tokens)},
    }
    return entry


def generate() -> dict[str, bytes]:
    tables = lx._spelling_tables()
    probes = "\n".join(lx.fts_index_text(p) for p in lx._FINGERPRINT_PROBES)
    header = {
        "fixture": "lexicon",
        "tokenizer_fingerprint": lx.tokenizer_fingerprint(),
        "fingerprint_recipe": (
            "sha256(utf8(join('\\n', fts_index_text(p) for p in probes)) "
            "+ b'\\n\\x00\\n' + utf8(spelling_tables))"
        ),
        "probes": list(lx._FINGERPRINT_PROBES),
        "probe_index_text": probes,
        "spelling_tables_sha256": hashlib.sha256(tables.encode("utf-8")).hexdigest(),
    }
    records: list[Any] = [header]
    records.extend(_entry(text) for text in corpus())
    return {
        "lexicon.jsonl": jsonl(records),
        "lexicon_spelling_tables.json": tables.encode("utf-8"),
    }

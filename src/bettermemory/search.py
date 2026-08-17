"""Ranking memories against a query.

Three selectable rankers, dispatched by `search(mode=...)` — all of
them deterministic code; nothing here loads a model, and by project
direction nothing may (the code is the model):

- ``hybrid`` (default since 2.6.8): reciprocal rank fusion (Cormack
  et al., SIGIR 2009) over keyword + BM25. The fused score lives in a
  different (much smaller) scale than the single-ranker scores, so raw
  `score` is not comparable across modes — read `relevance` together
  with `matched_leg` instead.
- ``keyword`` (legacy default in 1.6.0): the original TF +
  scope-weighted + coverage + recency scorer. Cheap, deterministic,
  good on identifier-heavy queries but lacks IDF — underperforms on
  rare-term queries vs. BM25/hybrid.
- ``bm25``: Okapi BM25 with IDF weighting, TF saturation, length
  normalisation, plus the same scope-bonus and recency multiplier as
  the keyword scorer.

`compute_idf` and `reciprocal_rank_fusion` are exported alongside
their per-mode scorers so callers can wire the rankers directly
without going through `search()`. The dedup path (`find_similar`)
uses Jaccard over `_raw_content_token_set` token sets with
pairwise-aware kebab expansion (`_pairwise_content_jaccard`).
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import unicodedata
from bisect import bisect_left
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Callable, Literal, NamedTuple

from .expansion import (
    ExpansionTables,
    build_tables as _build_expansion_tables,
    expansion_terms as _expansion_terms_impl,
)
from .models import (
    Memory,
    MemoryHit,
    SimilarHit,
    TombstonedMemory,
    snippet_for,
    snippet_window,
)
from .origin import Origin, should_include_for_caller
from .verify import detect_path_drift

log = logging.getLogger("bettermemory.search")

# Search modes exposed via `search(mode=...)`. Default is `hybrid` since
# 2.6.8 — the keyword scorer lacks IDF weighting and underperforms on
# rare-term queries, and hybrid degrades gracefully to keyword+BM25
# fusion when no embedding extra is installed (so flipping the default
# doesn't add a dep requirement).
SearchMode = Literal["keyword", "bm25", "hybrid"]


# Strip punctuation, keep word characters (incl. unicode letters) and dashes
# inside tokens. NFKC-fold, lowercase, and diacritic-fold (see
# `_tokenize_impl` / `_fold_diacritics`) before tokenizing.
#
# `\w` (with `re.UNICODE`, which is the default in Python 3) is the right
# character class here: it covers ASCII alphanumerics plus the rest of
# Unicode's letters and digits, so a body like "Niño café Mañana" tokenizes
# correctly instead of fragmenting on each accented letter. The naive
# `[a-z0-9]` alternative — what this regex used to be — silently dropped
# every non-ASCII run after `.lower()` reduced the casing, which made
# non-English memories effectively unsearchable.
#
# `\w` also matches `_`, but `tokenize` canonicalizes `_` to `-` before this
# regex runs so snake_case and kebab-case spell the same token; the `[\w\-]`
# body keeps the hyphen token-internal so kebab-case stays whole.
#
# Two shape constraints beyond the original `\w[\w\-]*`:
# - the first alternative keeps dotted numeric literals whole ('16.3',
#   '3.12.1'), so a version pin survives as one token instead of
#   fragmenting into bare digits that match any enumeration digit;
# - a token must END on a word character, so suspended hyphenation
#   ("pre- and post-deploy") yields the matchable 'pre', not the dead
#   query token 'pre-'.
_TOKEN_RE = re.compile(r"\d+(?:\.\d+)+|\w(?:[\w\-]*\w)?", re.UNICODE)

# Used by `_expand_kebab` to peel off sub-tokens from a kebab/snake compound.
_KEBAB_SPLIT_RE = re.compile(r"[-_]+")

# Possessive/contraction suffixes ("what's", "don't", "I'm" — straight or
# curly apostrophe) are stripped before tokenization. Without this the
# orphan fragment ('s', 't', 'm', ...) survives stopword stripping, deflates
# the relevance-coverage denominator, and gets reported in `match_terms`
# whenever a body happens to contain any possessive. The pattern is anchored
# to the apostrophe, so legitimate standalone tokens ("re", "d") and
# non-contraction apostrophes ("o'clock", trailing "users'") are untouched.
_CONTRACTION_RE = re.compile(r"(?<=\w)['’](?:s|t|d|m|ll|re|ve)\b")

# Fixed alias allowlist for the handful of symbol-bearing tech names that
# `_TOKEN_RE` would otherwise collapse to a bare letter ('C++' -> 'c',
# indistinguishable from a list-enumeration 'c.'). Applied symmetrically —
# `tokenize` serves query and indexed text alike — with word-ish boundaries
# so arithmetic, markdown headers, and substrings like 'asp.net' don't
# fire. Deliberately a tiny hard-coded list rather than widening _TOKEN_RE
# to accept '+'/'#', which would change tokens for every body. 'c++' maps
# to 'cpp ' (trailing space) so 'C++20' tokenizes as ['cpp', '20'] and a
# bare 'C++' query still hits it.
#
# Each entry carries the raw surface spelling alongside the pattern (the
# patterns themselves are NOT mechanically derivable from it — e.g. 'c++'
# deliberately drops the trailing `(?!\w)` guard so 'C++20' still aliases).
# Since index schema v4 the raw spelling is documentation only: the FTS
# table indexes `fts_index_text` output — the SAME normalised tokens the
# rankers see — so a query token 'cpp' matches the indexed 'cpp' directly
# and the old reverse map ('cpp' -> also try '"c++"', because raw bodies
# indexed 'C++' as the bare unicode61 token 'c') has nothing left to widen.
_SYMBOL_ALIASES: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(r"(?<!\w)c\+\+"), "cpp ", "c++"),
    (re.compile(r"(?<!\w)c#(?!\w)"), "csharp", "c#"),
    (re.compile(r"(?<!\w)f#(?!\w)"), "fsharp", "f#"),
    (re.compile(r"(?<!\w)\.net(?!\w)"), "dotnet", ".net"),
)

# Reverse of `_SYMBOL_ALIASES`, for locating an aliased term back in a RAW
# body — `_query_biased_snippet`'s anchor scan. The alias fires on the TEXT
# before `_TOKEN_RE` runs, and its source characters ('+', '#', a leading
# '.') are not `\w`, so a scan over raw body tokens only ever sees the bare
# 'C' / 'NET' that no per-token normalisation can turn back into
# 'cpp' / 'dotnet'. The only way to find those anchors is to re-run the
# alias patterns over the raw text and take their spans directly.
# Case-insensitive because the forward patterns are written against
# already-lowercased text, and lowercasing a raw body is not
# length-preserving (U+0130 -> 'i' + U+0307), which would break the offsets
# the whole scan rests on.
_ALIAS_ANCHOR_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern.pattern, re.IGNORECASE), replacement.strip())
    for pattern, replacement, _ in _SYMBOL_ALIASES
)


# Short English stopword list. Stripped from the *query* only — bodies stay
# unfiltered so we don't lose information at index time. The point isn't NLP
# accuracy; it's stopping queries like "how to bake sourdough bread" from
# matching every memory on shared filler tokens ("how", "to"). We keep the
# list short and conservative — domain words ("get", "set", "run") stay in
# because they often *are* what the user is searching for. 'about' and the
# indefinite pronouns ('anything', 'something', 'everything') are pure
# grammatical filler within word classes the list already covers; without
# them, natural retrieval phrasings ("anything stored about X") deflate the
# relevance-coverage denominator and label exact-topic hits 'low'. 'am' and
# the third-person pronouns (he/she/him/his/her/hers) complete the pronoun
# rows that were already here; 'her' doubles as the guard that keeps the
# final-e normalisation in `_stem_segment` from folding 'here' into a
# content token.
_STOPWORDS_EN = frozenset(
    {
        "a",
        "about",
        "am",
        "an",
        "and",
        "anything",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "can",
        "do",
        "does",
        "everything",
        "for",
        "from",
        "had",
        "has",
        "have",
        "he",
        "her",
        "hers",
        "him",
        "his",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "me",
        "my",
        "no",
        "not",
        "of",
        "on",
        "or",
        "she",
        "so",
        "something",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "to",
        "too",
        "us",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
    }
)

# Non-English function-word lists (tokenizer v2). Same philosophy as the
# English list — short, high-frequency grammatical filler only — covering
# the languages that show up in real stores (this user's sessions are
# partly Swedish; the groundedness gate's stopword defence was previously
# English-only, so a hallucinated non-English claim could anchor on pure
# filler like 'vill'/'att'/'på'). Two ground rules:
#
# - Entries are spelled in their POST-`_fold_diacritics` form ('på' → 'pa',
#   'är' → 'ar', 'où' → 'ou'): membership tests run on folded tokens, so
#   an accented spelling here would never match anything.
# - Function words that collide with live tech vocabulary stay OUT, even
#   when high-frequency in their language — and the rule binds across ALL
#   four lists: 'sin' was curated out of the Spanish list for sin() and
#   then leaked back in as the Swedish possessive, zeroing single-word
#   recall until the cross-list re-curation. Out: 'vi'/'du'/'man'/'su'
#   (unix commands and man-pages), 'ni' (rare anyway), 'men' (English
#   plural of man), 'dom'/'sin'/'finns' (sv: the DOM, sin(), Finn the
#   name — 'finns' stems onto it), 'mit' (the license), 'war' (Java .war
#   artifacts), 'die'/'ist'/'hat'/'uber'/'fur' (de: dying processes —
#   the stemmer's own contract folds 'dies' onto 'die' — the IST
#   timezone, Red Hat, the company, the English noun), 'mon'/'son'/
#   'car'/'plus'/'meme'/'sans'/'ses'/'si' (fr: monitoring shorthand,
#   English nouns, memes, sans-serif, AWS SES, SI units), 'y'/'o'/
#   'con'/'son'/'para'/'hay'/'todo'/'todos' (es: math variables, big-O,
#   pros-and-cons, paragraph shorthand, the English noun, TODO markers).
#   Accepted borderline collisions, documented so nobody re-litigates
#   them one at a time: 'ar' (unix archiver), 'pa' (PA systems), 'av'
#   (audio/video), 'har' (.har dumps), 'nu' (nushell), 'sig' (.sig
#   files), 'es' (Elasticsearch shorthand), 'est' (the timezone), 'des'
#   (the cipher), 'en'/'de' (locale codes, particles in names), 'aux'
#   ('ps aux'), 'del' (Python's del), 'ha' (high-availability
#   shorthand), 'lo' (the loopback interface) — a query for those as
#   standalone terms is far rarer than the function word is in its
#   language, stripping them still leaves a mixed query's real content
#   tokens to match on, and the unstripped-token fallback in `search()`
#   keeps even the standalone query answerable.
_STOPWORDS_SV = frozenset(
    {
        "och",
        "att",
        "det",
        "den",
        "som",
        "en",
        "ett",
        "av",
        "ar",
        "pa",
        "med",
        "till",
        "inte",
        "har",
        "hade",
        "ska",
        "skulle",
        "kan",
        "kunde",
        "vill",
        "ville",
        "jag",
        "han",
        "hon",
        "sig",
        "sitt",
        "nar",
        "dar",
        "vad",
        "vem",
        "hur",
        "varfor",
        "om",
        "eller",
        "sa",
        "nu",
        "da",
        "sedan",
        "innan",
        "mycket",
        "mer",
        "mest",
        "alla",
        "allt",
        "bara",
        "ocksa",
        "aven",
        "utan",
        "vara",
        "blir",
        "blev",
    }
)

_STOPWORDS_DE = frozenset(
    {
        "der",
        "das",
        "und",
        "sind",
        "waren",
        "ein",
        "eine",
        "einen",
        "einem",
        "einer",
        "nicht",
        "von",
        "auf",
        "dem",
        "den",
        "des",
        "im",
        "am",
        "um",
        "zu",
        "zum",
        "zur",
        "sich",
        "auch",
        "aber",
        "oder",
        "wenn",
        "wie",
        "ich",
        "sie",
        "er",
        "es",
        "wird",
        "werden",
        "wurde",
        "kann",
        "muss",
        "haben",
        "hatte",
        "sein",
        "seine",
        "ihr",
        "ihre",
        "dass",
        "noch",
        "nur",
        "schon",
        "sehr",
        "aus",
        "bei",
        "nach",
        "vor",
        "durch",
        "als",
        "wir",
        "alle",
    }
)

_STOPWORDS_FR = frozenset(
    {
        "le",
        "la",
        "les",
        "un",
        "une",
        "des",
        "de",
        "et",
        "est",
        "sont",
        "dans",
        "pour",
        "par",
        "sur",
        "avec",
        "mais",
        "ou",
        "qui",
        "que",
        "quoi",
        "ne",
        "pas",
        "se",
        "ce",
        "cette",
        "ces",
        "leur",
        "nous",
        "vous",
        "ils",
        "elles",
        "il",
        "elle",
        "je",
        "tu",
        "au",
        "aux",
        "comme",
        "tout",
        "tous",
        "toute",
        "etre",
        "avoir",
        "fait",
        "tres",
        "bien",
        "aussi",
        "deja",
        "encore",
        "alors",
    }
)

_STOPWORDS_ES = frozenset(
    {
        "el",
        "los",
        "las",
        "unos",
        "unas",
        "pero",
        "porque",
        "como",
        "cuando",
        "donde",
        "quien",
        "por",
        "del",
        "al",
        "lo",
        "le",
        "sus",
        "mi",
        "mis",
        "yo",
        "ella",
        "ellos",
        "nosotros",
        "ha",
        "han",
        "ser",
        "estar",
        "muy",
        "mas",
        "tambien",
        "ya",
        "esta",
        "este",
        "esto",
        "estos",
        "estas",
    }
)

_STOPWORDS = (
    _STOPWORDS_EN | _STOPWORDS_SV | _STOPWORDS_DE | _STOPWORDS_FR | _STOPWORDS_ES
)


def _nfd_fold(text: str) -> str:
    """The reference diacritic fold: NFD-decompose, drop combining marks.
    Per-char Python-level cost (a genexp with a `unicodedata.combining`
    call per char) — `_fold_diacritics` reserves it for input outside
    `_LATIN_FOLD_TABLE`'s range and serves the common Latin ranges via
    `str.translate`."""
    return "".join(
        ch for ch in unicodedata.normalize("NFD", text) if not unicodedata.combining(ch)
    )


# `str.translate` fold table over ASCII + Latin-1 Supplement + Latin
# Extended-A (U+0000–U+017F), generated at import by running each code
# point through `_nfd_fold`. A dense list, not a dict: sequence tables
# index at C speed per char, where a dict table pays a caught KeyError
# per miss on the ASCII majority — measured as slow as the genexp it
# replaces. Applying the fold per-char via the table is exact, not an
# approximation, by two facts:
#
# - the fold is per-char decomposable: NFD concatenates each char's own
#   canonical decomposition, and canonical reordering only permutes
#   combining marks (ccc > 0) past each other, never past a starter —
#   and every combining char is dropped anyway, so the whole-string fold
#   IS the concatenation of the per-char folds;
# - the table covers every code point the gate below admits: foldable
#   entries carry their base letters ('é' → 'e', 'å' → 'a'), the rest
#   map to themselves ('ß', 'ø', 'æ', 'ĳ' have no canonical
#   decomposition; the NFD path never folded them either).
#
# Both facts are pinned byte-for-byte against the reference fold by
# test_fold_diacritics_fast_path_equals_nfd_reference.
_LATIN_FOLD_TABLE = [_nfd_fold(chr(cp)) for cp in range(0x180)]

# Any char beyond the table's range (combining sequences, Latin Ext-B and
# up, non-Latin scripts, precomposed singletons like U+212B) routes the
# whole string to the NFD fallback.
_FOLD_FALLBACK_RE = re.compile(r"[^\u0000-\u017f]")


def _fold_diacritics(text: str) -> str:
    """NFD-decompose and drop combining marks, so 'Zürich' and 'zurich'
    share one token form.

    This mirrors the accent-insensitive matching of the FTS5 ``unicode61``
    tokenizer in index.py (whose ``remove_diacritics`` defaults on),
    closing the prefilter/ranker disagreement where the index returned a
    candidate that every Python ranker then scored 0. The NFD pass also
    normalises combining-mark *input*: a body pasted from macOS in
    decomposed form ('Tjörn' as 'o' + U+0308) previously SPLIT at the mark
    — ``\\w`` excludes category Mn — yielding ['tjo', 'rn']; now both the
    precomposed and decomposed spellings fold to 'tjorn'.

    Latin-diacritic text — the dominant non-ASCII case in a partly-Swedish
    store, which `_tokenize_impl`'s `isascii` fast path never spares —
    folds via `_LATIN_FOLD_TABLE` at C speed instead of the per-char NFD
    genexp; byte-identical by construction (see the table comment).
    Anything the table doesn't cover takes `_nfd_fold`, the unchanged
    original path."""
    if not _FOLD_FALLBACK_RE.search(text):
        return text.translate(_LATIN_FOLD_TABLE)
    return _nfd_fold(text)


# Scripts written without inter-word whitespace (tokenizer v2). `\w` treats
# an entire CJK clause as one giant token — '東京オフィスは移転する' came out
# as a single 12-char "word" that only a byte-exact query could ever match,
# so CJK-language memories were written successfully, passed dedup, and were
# then unfindable in every ranker AND the FTS5 index. The standard
# dictionary-free fix (Lucene's CJKAnalyzer, among others) is overlapping
# character bigrams: '東京オフ' → 東京 / 京オ / オフ. Applied symmetrically
# (tokenize serves query and indexed text alike), a two-char query word is
# exactly one bigram and a longer query word is a bag of bigrams the body's
# own bigrams cover. A SINGLE-char query word is one unigram; matching it
# inside a body's runs is `_expand_kebab`'s job — the index side also
# emits each bigram's chars.
#
# Ranges: Han (+ Ext A, compatibility ideographs, Ext B–F astral planes),
# Hiragana, Katakana (+ phonetic extensions and halfwidth forms), Hangul —
# NOTE: `_fold_diacritics` runs NFD first, which canonically decomposes
# Hangul syllables into Jamo (U+1100–U+11FF), so the Jamo blocks matter
# even though typed Korean arrives as syllables — and Thai, whose prose is
# also unspaced (its combining vowel marks are already stripped by the NFD
# fold; the surviving Lo letters bigram consistently on both sides).
_UNSEGMENTED_RE = re.compile(
    "["
    "\u0e01-\u0e5b"  # Thai letters (post-fold survivors)
    "\u1100-\u11ff"  # Hangul Jamo (NFD form of syllables)
    "\u3041-\u30ff"  # Hiragana + Katakana
    "\u31f0-\u31ff"  # Katakana phonetic extensions
    "\u3400-\u4dbf"  # CJK Unified Ideographs Extension A
    "\u4e00-\u9fff"  # CJK Unified Ideographs
    "\ua960-\ua97f"  # Hangul Jamo Extended-A
    "\uac00-\ud7ff"  # Hangul syllables + Jamo Extended-B
    "\uf900-\ufaff"  # CJK Compatibility Ideographs
    "\uff66-\uff9d"  # Halfwidth Katakana (defensive: NFKC folds these out first)
    "\U00020000-\U0002ebef"  # CJK Unified Ideographs Extensions B-F
    "]+"
)


def _segment_unspaced(token: str) -> list[str]:
    """Split a raw `_TOKEN_RE` token into matchable units.

    Tokens without unsegmented-script runs pass through whole (the
    `isascii` fast path covers virtually every token in a Latin-script
    store). A token that mixes scripts — '2026年に移転' — yields its
    non-CJK chunks whole and each CJK run as overlapping bigrams; a
    single stranded CJK char is emitted as itself so it stays matchable.
    Symmetric — query and indexed text alike; the index-side unigram
    widening for chars INSIDE runs lives in `_expand_kebab`. Chunk edges
    are stripped of separator hyphens ('docker-東京' must not emit the
    dead token 'docker-')."""
    if token.isascii() or not _UNSEGMENTED_RE.search(token):
        return [token]
    out: list[str] = []
    pos = 0
    for m in _UNSEGMENTED_RE.finditer(token):
        if m.start() > pos:
            chunk = token[pos : m.start()].strip("-")
            if chunk:
                out.append(chunk)
        run = m.group()
        if len(run) == 1:
            out.append(run)
        else:
            out.extend(run[i : i + 2] for i in range(len(run) - 1))
        pos = m.end()
    if pos < len(token):
        chunk = token[pos:].strip("-")
        if chunk:
            out.append(chunk)
    return out


# Words the suffix rules below would fold into a misleading form. 'news'
# is not the plural of 'new' (Porter makes the same mistake); keeping it
# whole costs nothing because the bare singular spelling doesn't occur.
# The rest are singulars that END in 's' yet escape the 'ss'/'us'/'is'
# guard: their '-es' plural strips back to the singular's SURFACE form
# (aliases → aliase → alias) while the bare-s drop moved the singular
# itself elsewhere (alias → alia) — a guaranteed plural/singular miss.
# Pinning the singular whole puts both inflections on the surface key.
_STEM_EXCEPTIONS = frozenset({"alias", "atlas", "bias", "canvas", "lens", "news"})

# Irregular plural → singular surface map, consulted BEFORE the suffix
# rules: acronym plurals whose '-s' lands ON the 'ss'/'us'/'is' singular
# guard ('apis' ends in 'is', 'gpus' in 'us') while the 3-char singular
# early-returns whole — the mirror image of the `_STEM_EXCEPTIONS` class,
# and the same total plural/singular miss in every ranker and the FTS
# index. A tiny audited list of top-tier tech acronyms, not a widened
# guard: the guard's own members (redis, analysis, status, basis) are
# singulars that must stay pinned.
_ACRONYM_PLURALS = {
    "apis": "api",
    "clis": "cli",
    "cpus": "cpu",
    "gpus": "gpu",
    "guis": "gui",
    "skus": "sku",
    "uris": "uri",
}


def _stem_segment(seg: str) -> str:
    """Light inflectional stem of one hyphen-free segment (tokenizer v2).

    Matching is exact token equality, so without this the most ordinary
    morphological variance between a retrieval question and a stored fact
    — plural vs singular ('standups' vs 'standup') — was a total miss in
    every ranker and the FTS5 index. This is deliberately NOT Porter:
    relevance buckets and dedup Jaccard feed automation, so aggressive
    derivational conflation ('general'/'generous') is a worse failure
    mode here than an occasional unfolded plural. Plural inflection only:

    - acronym plurals the guards would strand ('apis' hits the 'is'
      ending, 'gpus' the 'us' ending, while 'api'/'gpu' early-return
      whole at 3 chars) fold via the `_ACRONYM_PLURALS` surface map
      before any suffix rule runs;
    - 'sses' → 'ss' (classes → class), 'ies' → 'y' (policies → policy —
      the final-y normalisation below carries both spellings to
      'polici'; 4-char forms keep 'ie', so ties meets tie whole);
    - final 's' dropped behind the usual guards ('ss'/'us'/'is' endings
      and digit-final acronyms like 'k8s' stay; 3-char tokens like
      'aws'/'dns'/'yes' stay whole; singulars that end in 's' OUTSIDE
      those guards — alias, canvas — are pinned in `_STEM_EXCEPTIONS`,
      because the drop moved them off the key their own '-es' plural
      strips back to);
    - final-e NORMALISATION on everything that survives, both plural and
      singular: dropping final 'e' collapses the '-es attachment'
      ambiguity that no dictionary-free rule can split — 'branches'
      (branch+es) and 'caches' (cache+s) both end in 'ches', but
      branches→branche→branch meets branch→branch and
      caches→cache→cach meets cache→cach. The result is an index KEY,
      not a word; symmetry is what matters. Guarded so it can't fold a
      token into a stopword ('note' would become 'not', 'here' would
      become 'her' — both stay whole) and so 'ee' endings keep their
      spelling ('tree', 'free');
    - final-y NORMALISATION mirroring it: a final 'y' rewrites to 'i'
      under the same guards (4+ char stems only, never onto a
      stopword). Without this the '-ies' rule and the final-e rule
      split the -ie noun class across two keys — cookies→cooky but
      cookie→'cooki' — so plural and singular could never meet; now
      cookies→cooky→'cooki' meets cookie→'cooki' (likewise movie,
      rookie, hoodie) and policy→'polici' meets policies. The length
      guard keeps 'guys'→'guy' on the surface form the 3-char early
      return already gives 'guy'.

    Stopwords are exempt BY SURFACE FORM before any rule runs —
    'does'→'doe' would otherwise leak a former stopword into content-token
    counts and quietly weaken the audit gate — and any rule chain whose
    RESULT lands on a stopword returns the original spelling for the same
    reason ('ares'→'are' stays 'ares'). CJK bigrams and digit-bearing
    tokens fall out naturally: no rule fires on them.
    """
    if len(seg) < 4 or seg in _STOPWORDS or seg in _STEM_EXCEPTIONS:
        return seg
    mapped = _ACRONYM_PLURALS.get(seg)
    if mapped is not None:
        return mapped
    stem = seg
    if stem.endswith("sses"):
        stem = stem[:-2]
    elif stem.endswith("ies"):
        stem = stem[:-3] + "y" if len(stem) > 4 else stem[:-1]
    elif stem.endswith(("ss", "us", "is")):
        pass
    elif stem.endswith("s") and stem[-2].isalpha():
        stem = stem[:-1]
    if (
        len(stem) >= 4
        and stem.endswith("e")
        and not stem.endswith("ee")
        and stem[:-1] not in _STOPWORDS
    ):
        stem = stem[:-1]
    if len(stem) >= 4 and stem.endswith("y") and stem[:-1] + "i" not in _STOPWORDS:
        stem = stem[:-1] + "i"
    return seg if stem in _STOPWORDS else stem


def _stem_token(tok: str) -> str:
    """Apply `_stem_segment` per hyphen-separated segment so compounds
    fold the same way their expanded parts do: 'docker-containers' →
    'docker-container', whose `_expand_kebab` / `_kebab_parts` components
    are exactly the stemmed singles. Hyphen structure (including
    consecutive hyphens `_TOKEN_RE` can admit) is preserved verbatim."""
    if "-" in tok:
        return "-".join(_stem_segment(p) for p in tok.split("-"))
    return _stem_segment(tok)


def tokenize(text: str) -> list[str]:
    """Regex tokenization behind a small symmetric normalisation pipeline.

    Whitespace and punctuation split; hyphens stay token-internal (so
    `python-frontmatter` is one token). Every normalisation applies to
    query and indexed text alike (tokenize serves both sides):

    - NFKC compatibility fold, so width variants meet: fullwidth
      Latin/digits ('ＧＰＵ', '２０２６') become ASCII and halfwidth
      katakana ('ｻｰﾊﾞｰ') becomes fullwidth ahead of bigram
      segmentation ('²' -> '2' and 'ﬁ' -> 'fi' ride along);
    - lowercase, then fold diacritics (see `_fold_diacritics`);
    - strip possessive/contraction suffixes ("what's" -> "what");
    - alias symbol-bearing tech names ("C++" -> "cpp", see
      `_SYMBOL_ALIASES`);
    - canonicalize '_' to '-' so `docker_compose` and `docker-compose`
      spell the same token;
    - keep dotted numerics whole ('16.3') and end tokens on a word
      character ('pre-' -> 'pre'), per `_TOKEN_RE`;
    - segment unspaced scripts into overlapping CJK bigrams (see
      `_UNSEGMENTED_RE`) so CJK-language text is matchable at all;
    - fold plural inflection with the light stemmer (see
      `_stem_segment`) so 'standups' meets 'standup'.

    Pair with `_expand_kebab` on indexed text if you also want to match by
    component.
    """
    return _tokenize_impl(text, stem=True)


def _tokenize_unstemmed(text: str) -> list[str]:
    """`tokenize` minus the plural stemmer — same folds, contraction
    strips, symbol aliases, and CJK bigrams, but tokens keep their
    surface spelling. Consumed by groundedness's alias-anchor rescue,
    which reasons about SPELLING relations (substring / subsequence):
    stems shorten words below that rescue's particle length gate
    ('code' → 'cod' falls under `_ALIAS_MIN_TOKEN_LEN`), so that one
    path compares surface forms. Everything that SCORES uses
    `tokenize`."""
    return _tokenize_impl(text, stem=False)


def _fold_ascii_safe(text: str) -> str:
    """The pipeline folds that map ASCII to ASCII: contraction strip,
    symbol aliases, '_' → '-' canonicalisation. Shared by both
    `_tokenize_impl` paths — the ASCII fast path's stream identity
    depends on every fold here preserving `isascii()`."""
    text = _CONTRACTION_RE.sub("", text)
    for pattern, replacement, _ in _SYMBOL_ALIASES:
        text = pattern.sub(replacement, text)
    return text.replace("_", "-")


def _tokenize_impl(text: str, *, stem: bool) -> list[str]:
    # ASCII fast path (the overwhelming case in Latin-script stores):
    # NFKC and NFD are identity on pure ASCII — no ASCII char has a
    # decomposition, and composition needs a combining mark, all of which
    # are non-ASCII — so the NFKC pass and `_fold_diacritics` are no-ops,
    # and since `.lower()` and `_fold_ascii_safe` keep ASCII ASCII, every
    # raw token would pass `_segment_unspaced` through whole. Stream
    # identity is pinned by test_ascii_normalization_invariance and the
    # golden-stream test in test_search.py.
    if text.isascii():
        raws = _TOKEN_RE.findall(_fold_ascii_safe(text.lower()))
        return [_stem_token(r) for r in raws] if stem else raws
    # NFKC first — the NFD fold below is canonical-only, so width variants
    # never met it: fullwidth Latin/digits ('ＧＰＵ', '２０２６' — standard
    # Japanese IME output) missed 'gpu'/'2026' in both directions, and
    # halfwidth katakana ('ｻｰﾊﾞｰ') missed fullwidth. NFKC folds fullwidth
    # to ASCII and halfwidth kana to fullwidth BEFORE `_segment_unspaced`
    # bigrams the run, composing the halfwidth voiced-sound marks
    # U+FF9E/U+FF9F (outside `_UNSEGMENTED_RE`'s ranges — they stranded as
    # junk tokens) into base kana whose dakuten the NFD fold then strips
    # exactly like the fullwidth spelling's. The remaining compatibility
    # folds ride along, symmetric on query and index side alike ('²'→'2',
    # 'ﬁ'→'fi', '㎞'→'km').
    text = unicodedata.normalize("NFKC", text)
    text = _fold_diacritics(text.lower())
    out: list[str] = []
    for raw in _TOKEN_RE.findall(_fold_ascii_safe(text)):
        for seg in _segment_unspaced(raw):
            out.append(_stem_token(seg) if stem else seg)
    return out


def _expand_kebab(tokens: list[str]) -> list[str]:
    """Append the parts of any hyphen/underscore-joined token after the whole.

    `python-frontmatter` -> ['python-frontmatter', 'python', 'frontmatter'].

    Applied to indexed text (body, scope) only — never the query. The
    asymmetry is deliberate: a body containing `zephyr-quartz-9417` is
    *also* about `zephyr` and `quartz`, so a one-word query should hit it.
    But a query for `python-frontmatter` is a specific intent — we don't
    want it dragging in every body that happens to mention plain `python`.
    Index side widens; query side stays narrow.

    Dotted numeric tokens get the same index-side treatment: '16.3' also
    emits '16' and '3', so a query for 'postgres 16' still hits a body
    that says 'Postgres 16.3' — while the query token '16.3' can no
    longer be satisfied by a stray enumeration digit.

    CJK bigrams (see `_segment_unspaced`) too: each bigram also emits its
    two chars, so a single-char query ('猫') hits a body whose 猫 lives
    inside a run and therefore only exists in bigrams ('い猫', '猫を').
    Without this the char matched only where it stranded ALONE ('年' hit
    '2026年' but not '2026年に移転'), and — matching being exact token
    equality — single-char queries scored zero in every ranker and the
    FTS index. Since the query side never widens, multi-char queries
    keep bigram semantics instead of decaying into shared-char recall.
    A char interior to a run rides two bigrams and is emitted twice —
    the same per-containing-compound TF the kebab parts already carry.
    """
    out: list[str] = []
    for t in tokens:
        out.append(t)
        if "-" in t or "_" in t:
            for sub in _KEBAB_SPLIT_RE.split(t):
                if sub:
                    out.append(sub)
        elif "." in t:
            # Only dotted numerics ('16.3') survive _TOKEN_RE with a '.'.
            for sub in t.split("."):
                if sub:
                    out.append(sub)
        elif len(t) == 2 and not t.isascii() and _UNSEGMENTED_RE.fullmatch(t):
            # Post-tokenize, a two-char unsegmented-script token can only
            # be a `_segment_unspaced` bigram — runs never share a token
            # with other characters.
            out.append(t[0])
            out.append(t[1])
    return out


def _kebab_parts(tok: str) -> list[str]:
    """Components of a hyphen/underscore-joined or dotted-numeric token,
    or [] when the token isn't joined. Used by the conjunctive fallback in
    the scorers — a joined query token with no direct hit ('claude-code'
    against a body spelling it 'Claude Code'; '3.12' against a body whose
    '3.12.1' expanded to bare components) counts as matched iff ALL its
    components hit — and by `_pairwise_content_jaccard`'s one-sided dedup
    expansion. The split mirrors `_expand_kebab` branch for branch so the
    query-side fallback and the index-side expansion can't disagree about
    what counts as a component. Known accepted imprecision: the fallback
    is order-insensitive, so '3.12' also matches a body carrying '12.3' —
    the same class of looseness the kebab fallback already carries."""
    if "-" in tok or "_" in tok:
        parts = [p for p in _KEBAB_SPLIT_RE.split(tok) if p]
    elif "." in tok:
        # Only dotted numerics ('16.3') survive `_TOKEN_RE` with a '.'.
        parts = [p for p in tok.split(".") if p]
    else:
        return []
    return parts if len(parts) >= 2 else []


def _fts_phrase(term: str) -> str:
    """Quote a term as an FTS5 phrase literal. Doubling embedded quotes is
    the FTS5 escape; wrapping in quotes keeps special characters (`:`,
    `*`, `+`, `.`) as literal phrase content instead of MATCH syntax.
    `tokenize` output can't contain '"', but the escape stays
    unconditional so the helper never depends on its input's shape."""
    escaped = term.replace('"', '""')
    return f'"{escaped}"'


def fts_index_text(text: str) -> str:
    """The text the FTS5 index stores for a memory (schema v4+).

    `' '.join(_expand_kebab(tokenize(text)))` — the exact index-side token
    stream the Python rankers score, joined so unicode61 re-derives it
    losslessly (tokens are space-free; a compound like 'claude-code'
    re-splits into the adjacent pair the quoted phrase query matches).
    This is what makes prefilter/ranker parity hold BY CONSTRUCTION:
    before v4 the index tokenized the RAW body under unicode61, so every
    normalisation `tokenize` grew (diacritic folds, symbol aliases,
    contraction strips — and now stems and CJK bigrams) had to be
    hand-mirrored in `fts_match_query`'s OR-variants or indexed stores
    silently dropped candidates the rankers rate 'high'. Indexing the
    pipeline's own output removes the second spelling authority: both
    sides of a MATCH are `tokenize` tokens.

    Used by `index._upsert_memory` / `rebuild` for the
    `body_fts` column (and for `scopes_fts` over the space-joined scope
    list). Raw bodies stay canonical on disk and in `memories.body`; this
    is derived-index content only.
    """
    return " ".join(_expand_kebab(tokenize(text)))


def fts_match_query(query: str) -> str:
    """Build the FTS5 MATCH expression `index.query` prefilters with.

    Prefilter/ranker parity is a hard invariant (see `_fold_diacritics`):
    the FTS5 index serves the CANDIDATE set on large stores, so the
    candidate set must be a superset of what the rankers would score.
    Since schema v4 the index stores `fts_index_text` output — the same
    `tokenize` stream this builder consumes — so parity holds by
    construction and the expression stays simple: each query token is a
    quoted phrase of itself. Living next to `tokenize` (and consuming it)
    is still the point: the two sides can't drift apart without touching
    the same module.

    Per token (deduplicated, insertion-ordered) one OR-variant remains:
    joined tokens (kebab/snake/dotted, per `_kebab_parts`) also emit
    their conjunctive form: 'claude-code' ->
    '("claude-code" OR ("claude" AND "code"))'. The quoted compound is
    an FTS *phrase* (adjacent tokens only) — it matches the compound's
    own indexed split — but the rankers' conjunctive fallback also
    matches the parts anywhere in the body; AND-of-components mirrors
    that anywhere-in-body semantics. (The pre-v4 raw-symbol variant —
    'cpp' also trying '"c++"' — is gone: the indexed text already says
    'cpp'.)

    Stopwords are kept: the rankers strip them from the query, so
    stopword terms only ever ADD candidates ahead of authoritative
    scoring — dropping them here could not widen recall, and keeping
    them matches the raw-split builder's historical candidate sets.
    Returns '' when the query yields no tokens; callers treat that as
    "nothing to match"."""
    groups: list[str] = []
    for tok in dict.fromkeys(tokenize(query)):
        variants = [_fts_phrase(tok)]
        parts = _kebab_parts(tok)
        if parts:
            variants.append("(" + " AND ".join(_fts_phrase(p) for p in parts) + ")")
        groups.append(
            variants[0] if len(variants) == 1 else "(" + " OR ".join(variants) + ")"
        )
    return " OR ".join(groups)


# Fixed probe corpus for `tokenizer_fingerprint`. One entry per
# normalisation family in the pipeline, so any behavioural change to the
# index-side token stream — tokenize()'s folds, stopword lists, stemmer
# rules, CJK segmentation, or `_expand_kebab`'s widening — lands in the
# digest. The corpus is part of the fingerprint's identity: editing it
# changes the digest exactly like a tokenizer change does and requires
# the same `index.SCHEMA_VERSION` bump ceremony.
_FINGERPRINT_PROBES: tuple[str, ...] = (
    # Plural stemmer: -ies/-es/-s pairs plus the final-e/-y
    # normalisations that put both inflections on one key.
    "cookies cookie policies policy caches cache branches branch",
    # Singular guards: -ss/-us/-is endings, digit-final acronyms, the
    # 3-char early return, the `_STEM_EXCEPTIONS` pins, and the
    # `_ACRONYM_PLURALS` fold ('apis' must land on 'api').
    "class status analysis k8s aws alias aliases news apis api",
    # Stopword-list sensitivity. `_stem_segment` exempts stopwords by
    # surface form, so membership edits respell the stream: one
    # stemmable member per list (en/sv/de/fr/es), 'todos' (an es
    # curation already flipped it 'todos'→'todo' once), and
    # 'notes here' (the final-e guard reads OTHER entries: 'not', 'her').
    "does these skulle eine cette estas todos notes here",
    # CJK run (overlapping bigrams + index-side unigrams) and a
    # stranded single char.
    "東京オフィスは移転する予定 猫",
    # NFKC compatibility fold: halfwidth katakana with a dakuten,
    # fullwidth Latin/digits.
    "ｻｰﾊﾞｰ ＧＰＵ２０２６",
    # Diacritic fold (the Latin fast-path table).
    "Zürich café naïve",
    # Kebab compound (per-segment stems + part expansion), contraction
    # strip, symbol aliases, dotted numeric.
    "docker-containers what's C++ .NET 3.12.1",
)


@lru_cache(maxsize=1)
def tokenizer_fingerprint() -> str:
    """sha256 over `fts_index_text` applied to `_FINGERPRINT_PROBES` —
    a stable identity for the persisted index-side token stream.

    Schema v4+ stores tokenize() output on disk (`body_fts` /
    `scopes_fts`), so query/index parity holds only while the persisted
    stream matches the live tokenizer — four separate post-3.12.0 fixes
    (stopword curation, final-y normalisation, CJK index-side unigrams,
    the NFKC fold) respelled the stream with no schema bump, leaving
    every 3.12.0-built index stale-spelled against live queries.
    `index._ensure_schema` stamps this digest into the index meta next
    to `schema_version` and treats a mismatch exactly like an older
    version (atomic wipe + rebuild-pending flag → auto-rebuild), so
    tokenizer drift heals through the standard migration path instead
    of silently losing recall. The pinned value lives at
    `index.TOKENIZER_FINGERPRINT`; the regression test asserting it
    equals this function is the ratchet that forces a deliberate
    `SCHEMA_VERSION` bump on any stream change.

    Cached: the probe corpus is a module-level constant, so the digest
    is process-stable, and `_ensure_schema` reads it on every index
    connect."""
    joined = "\n".join(fts_index_text(probe) for probe in _FINGERPRINT_PROBES)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _strip_stopwords(tokens: list[str]) -> list[str]:
    return [t for t in tokens if t not in _STOPWORDS]


# ---------------------------------------------------------------------------
# Query-time rescue expansion (retrieval campaign, Phase 1)
# ---------------------------------------------------------------------------
#
# The measured enemy is vocabulary: the query says "toggles", the memory
# says "feature flags", and on bench/retrieval that class of miss is the
# whole 25-point recall@1 gap the removed embedding arm used to cover.
# The rescue is three committed word tables (expansion.py) feeding one
# extra, down-weighted BM25 leg — engaged only when the base ranking is
# not confident. Every constant below is measured on the gold set
# (bench/retrieval/README.md, 2026-08-09 grid), not chosen by taste.

# Tables built once through the live stemmer so lookups and emitted
# terms share the rankers' post-stem token space. A stemmer rule change
# re-stems the tables automatically at import — stemmed literals in the
# tables would drift silently instead.
_EXPANSION_TABLES: ExpansionTables = _build_expansion_tables(_stem_token)

# The rescue leg engages only when the fused base ranking is NOT
# confident: top-hit coverage below this bar. Measured, not derived: on
# the gold set every requery-probe top hit covers >= 0.60 of its query
# (the leg never engages; requery stays byte-stable at 80%/100%) while
# every rescued asked-probe case covers below it. At 0.65 requery gives
# back a question; at 0.75 two. The value sits between the relevance
# bands' 0.40/0.75 because it is the same signal those bands read —
# coverage as ranking confidence.
_RESCUE_COVERAGE_GATE = 0.60

# Weight of the expansion leg in weighted RRF; base legs stay at 1.0.
# Expansion is a rescue, never a peer: at weight 1.0 the leg overrules
# confident base-leg agreement (measured: two rank-0 hits lost to
# variant noise), at 0.5 two rescued cases stall short of the top-5. At
# 0.7 every measured rescue lands and no base-leg agreement is
# overturned.
_RESCUE_LEG_WEIGHT = 0.7

# How many synthesized terms the rescue leg's top candidate must match
# before the leg's vote counts. Below this the leg is withheld
# entirely, leaving the base fusion exactly as it stood. (The lane's
# other mechanism, the filler df-floor, is keyed on `rescue_expansion`
# rather than on the leg, so it still applies — a withheld leg
# reproduces a lane-on query whose leg found nothing, not a
# `rescue_expansion=False` query.)
#
# Why the leg needs a confidence test at all: `_hybrid_fuse` fuses by
# RANK, so a leg contributes `_RESCUE_LEG_WEIGHT / (rrf_k + rank)`
# whether its rank-1 was found by a discriminating synonym or by a
# single coincidental token. IDF only reorders WITHIN the leg; it
# cannot reduce the leg's influence.
#
# Why a COUNT and not a threshold on a score distribution. Two earlier
# rules were measured and retired: a fixed margin level (round 3) and a
# self-calibrating gap ratio (round 4). Both separated on the dev set,
# both caught the same three harmful legs, and both also withheld nine
# and seven HELPFUL legs — because both were fitted to a proxy ("the
# leg's rank-1 is the gold document") rather than to whether the leg's
# vote actually moved the gold. Labelling that directly
# (bench/leg_labels.py) reframes the problem: of 39 engaged dev legs,
# 21 help, 3 hurt, 15 are neutral, and every harmful one had a rank-1
# matching exactly ONE synthesized term while no helpful one did.
#
# One matched term is a coincidence; two independent synthesized terms
# agreeing on the same document is evidence. 2 is the minimum
# non-trivial count — a bar stated independently of any measurement,
# which the labels confirm rather than select. It is also the first
# rule here that is not a threshold on a distribution, so there is no
# spread to shift between corpora: rounds 3 and 4 failed on exactly
# that, and a count of agreeing terms has no such spread.
#
# Preregistered in bench/longmemeval/PREREGISTRATION.md addendum 7
# before this code existed.
_RESCUE_LEG_MIN_EVIDENCE = 2

# The evidence count at which the rescue leg earns its FULL weight.
# Below it the leg still votes, but at the fraction of that bar its own
# evidence reaches:
#
#     scale(m) = min(1.0, m / _EVIDENCE_FULL_AT)
#
# so a leg at the floor (2 matched terms) votes at two thirds strength
# and a leg with 3 or more votes in full.
#
# Why the vote should scale at all: `_hybrid_fuse` fuses by RANK, so
# before this the leg contributed `_RESCUE_LEG_WEIGHT / (rrf_k + rank)`
# whether its rank-1 rested on a discriminating synonym or on a single
# coincidental token. Three rounds of choosing WHICH legs vote plateaued
# within 0.004 of each other, and one round of choosing WHAT they
# contain could not reach the incumbent's precision; the vote itself was
# the one variable that had never moved.
#
# 3 is read off the dev labels by a stated rule, not tuned: legs
# stratify monotonically by evidence — 0% helpful at one matched term,
# 68.2% at two, 100% at three or more — and the leg earns its full
# weight at the count where the labels first read 100%.
# `bench/leg_labels.py` is the instrument; addendum 9 is the
# preregistration, committed before this constant existed.
#
# The scale is bounded in [0, 1], so this can only ever REDUCE the
# leg's influence relative to the flat weight, never amplify it. An
# amplifying change would need a different safety argument.
_EVIDENCE_FULL_AT = 3

# Whether the leg's weight is SCALED by its evidence, or flat above the
# floor. **Default off: the shipped lane uses the flat weight.**
#
# Both forms were preregistered, implemented and measured (addenda 9 and
# 10). The scaled forms win on conversational stores and lose on
# technical ones, and the decision follows from who actually turns this
# lane on. Measured on the gold set, by the preregistered dev gates'
# own verdicts:
#
#     form                        asked r@1/r@5   control r@1/r@5
#     flat (this default)         0.55 / 0.90     0.50 / 0.85
#     scaled (m-1)/(F-1)          0.55 / 0.80     0.50 / 0.80
#     scaled m/F                  0.55 / 0.80     0.50 / 0.85
#
# The scaled forms' compensating gains land on LongMemEval's
# conversational stores (macro@5 0.8823 flat -> 0.8926 / 0.8901
# scaled) — and every one of those figures is BELOW that corpus's
# lane-off baseline of 0.8935, so a conversational store is better
# served leaving `rescue_expansion` off entirely than by any form of it.
# The audience that turns the lane on is a store whose owner knows it
# holds technical prose, and for that audience flat is strictly better.
#
# Round 8 is why this is a knob rather than an inference: no cheap store
# statistic separates the two corpora by anything near the twofold the
# weight would have to move (best 1.70x, and filler-token share — the
# quantity the lane's own premise names — only 1.13x). The engine cannot
# tell which corpus it is holding, so the owner does.
#
# The scaled form stays committed and exercisable rather than deleted:
# it is the campaign's best held-out configuration and the record for
# it is published. `bench/*/run.py --evidence-scaling on` drives it.
_RESCUE_LEG_EVIDENCE_SCALING = False

# Whether a BASE leg whose rank-1 evidence trails its peer's is withheld
# from the hybrid fusion. **Default off until round 9's gates pass: the
# shipped base fusion is unchanged.**
#
# Why the base pair should be weighted at all: `_hybrid_fuse` reads
# rank, not evidence, so a keyword or BM25 leg whose rank-1 rests on one
# coincidental token votes exactly as hard as one whose top candidate
# matched four query terms. That is the same defect the rescue leg's
# machinery above corrects — addendum 9 named the generalisation as a
# future hypothesis, and the base-leg census measured it
# (bench/base_leg_census.py, artifact
# bench/retrieval/results/base-leg-labels-2026-08-12.json).
#
# Why RELATIVE evidence and not the rescue leg's absolute count: at a
# base leg's rank-1 the matched-term count mostly measures query length,
# and the census found it does not stratify helpfulness (BM25 runs
# backwards under it). The leg's evidence relative to its peer's does
# stratify, sharply: leading legs helped 20/29 labelled cases, trailing
# legs 2/29, zero of twelve at a deficit of two or more. With exactly
# two base legs only the weight RATIO matters, so the whole design
# space is what happens to the trailing leg.
#
# Why withholding rather than a graded weight: rounds 6-7 measured that
# a graded constant between 0 and 1 has monotone OPPOSITE optima on a
# technical and a conversational corpus, and round 8 measured that the
# store cannot supply the constant. This rule declines to own such a
# scalar. Withholding is already the lane's grammar for thin evidence
# ("a leg with one word of evidence does not get to vote"); this is the
# same sentence with "fewer words than its peer" as the predicate. Ties
# — 80% of dev probes — return None and fuse byte-identically.
#
# Preregistered in bench/longmemeval/PREREGISTRATION.md addendum 12
# (round 9) before this code existed. `bench/*/run.py --base-withhold
# on` drives the mechanism arm.
_BASE_LEG_TRAILING_WITHHOLD = False

# Document-frequency floor for the QUERY_FILLER_WORDS list, as a
# fraction of the priced collection. Memory bodies are technical prose,
# so conversational filler is corpus-RARE, and Okapi IDF prices it like
# a discriminating term — a distractor matching "supposed" + "remember"
# outranked the right memory matching "paged" + "wake". The floor says:
# a word this common in QUESTIONS is common, full stop. df >= half the
# collection puts its IDF at ~log(2), ~14% of a genuinely-rare term's
# weight — deflated, never deleted (hard-stripping was measured to
# delete the only hooks some queries have). A floor (max with the real
# df), never a ceiling: filler genuinely common in a store keeps its
# honest pricing.
_FILLER_DF_FLOOR_RATIO = 0.5


def _filler_floor_stats(
    base: CorpusStats | None, terms: list[str], pool_n: int
) -> CorpusStats | None:
    """CorpusStats with the filler df-floor applied for `terms`.

    Returns `base` untouched when no term is on the filler list — the
    common path stays allocation-free and byte-stable. Otherwise the
    floor entries OVERRIDE per term through `compute_idf`'s existing
    corpus-stats mechanism, which only ever re-prices terms the pool
    actually carries — filler absent from every candidate body matches
    nothing and needs no cap. `max(real df, floor)` keeps the honest
    direction: the floor can only make filler look common, never make
    a genuinely common word look rare.
    """
    filler_present = [t for t in terms if t in _EXPANSION_TABLES.filler_stems]
    if not filler_present:
        return base
    size = base.size if base is not None else pool_n
    if size <= 0:
        return base
    floor = max(1, int(size * _FILLER_DF_FLOOR_RATIO))
    body_df = dict(base.body_df) if base is not None else {}
    scope_df = dict(base.scope_df) if base is not None else {}
    for t in filler_present:
        body_df[t] = max(body_df.get(t, 0), floor)
        scope_df[t] = max(scope_df.get(t, 0), floor)
    return CorpusStats(size=size, body_df=body_df, scope_df=scope_df)


def _merge_corpus_stats(
    a: CorpusStats | None, b: CorpusStats | None
) -> CorpusStats | None:
    """Union of two stats over the SAME collection; `b` wins per term.

    Used to fold a second provider fetch (expansion terms) into the
    already-floored query-term stats. Sizes describe the same admitted
    collection by construction; `b.size` is preferred with `a.size` as
    the fallback so a defensive zero can't zero the Okapi denominator.
    """
    if a is None:
        return b
    if b is None:
        return a
    body_df = dict(a.body_df)
    body_df.update(b.body_df)
    scope_df = dict(a.scope_df)
    scope_df.update(b.scope_df)
    return CorpusStats(
        size=b.size if b.size > 0 else a.size, body_df=body_df, scope_df=scope_df
    )


# ---------------------------------------------------------------------------
# The conversational lane (Lane L unit 1, bench/l/L1_DECLARATION.md)
# ---------------------------------------------------------------------------
#
# Two deterministic repairs for conversation-shaped stores, behind
# `search(conversational=...)`, default OFF, hybrid-mode only — the same
# opt-in shape as `rescue_expansion` and byte-stable for every caller
# that does not pass the flag:
#
# - L1-S: when the query has a temporal reading, its temporal-SCAFFOLD
#   tokens (day/week/ago/last/many and kin — the question's syntax, not
#   its content) get a document-frequency floor in the BM25 legs so they
#   cannot outprice content terms. The L1 miss anatomy found this the
#   dominant LongMemEval failure: for "how many days ago did I buy a
#   smoker?" the sessions outranking the gold matched `ago`/`day`/`many`
#   while the gold matched `smoker`. Exactly the filler df-floor's
#   pathology in temporal dress, repaired through the same stats seam.
# - L1-T: within the near-tie band of the fused ranking, date anchors
#   break lookalike ties — an explicit query window (a named month, a
#   date, "last month") boosts in-window anchors and demotes out-of-
#   window ones; an elapsed/order-shaped ask ("how many weeks ago did
#   I…", "which happened first…") boosts the FIRST narration (earliest
#   anchor), or the latest when the ask says "the last time I".
#
# Constants below are the declaration's declared defaults; the caps and
# the tuning protocol live in the declaration, and the finals a gate
# read used are recorded in its artifact.

# Closed class of temporal-scaffold SURFACE forms, stemmed through the
# live stemmer at import so membership tests run in ranker token space.
# Generic English temporal syntax plus small numerals — no topical
# vocabulary is admitted (declaration §3 caps the class at 40 stems).
_CONV_SCAFFOLD_SURFACE = (
    "day",
    "days",
    "week",
    "weeks",
    "weekend",
    "month",
    "months",
    "year",
    "years",
    "ago",
    "yesterday",
    "today",
    "last",
    "latest",
    "first",
    "earliest",
    "past",
    "recent",
    "recently",
    "current",
    "currently",
    "many",
    "much",
    "long",
    "total",
    "time",
    "times",
    "since",
    "between",
    "order",
    "passed",
    "once",
    "twice",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
)
_CONV_SCAFFOLD_STEMS = frozenset(_stem_token(w) for w in _CONV_SCAFFOLD_SURFACE)

# df floor for scaffold terms, as a fraction of the ranked collection.
# A floored term still matches and still scores. Tuning read 3 raised
# this from the filler floor's 0.5 to the declared cap: at 1.0 a
# scaffold term prices as a word every document carries — near-zero
# Okapi IDF — so the BM25 legs rank temporal-shaped queries on their
# content terms alone while the keyword leg still counts everything.
_CONV_SCAFFOLD_FLOOR_RATIO = 1.0

# Near-tie band for L1-T: items whose fused score is at least this
# fraction of the top hit's participate in anchor selection. RRF scores
# decay slowly (1/(k+rank)), so 0.5 reaches roughly the top fifty of a
# two-leg ranking — the L1 anatomy put 81% of missed evidence at
# distinct-session ranks 5-19.
_CONV_BAND_TAU = 0.50

# L1-T adjustment magnitudes, multiplicative on the fused score.
#
# The tuning frontier's path (artifacts in bench/l/results/): read 1
# measured the declared defaults net-negative at @1 (0.5301 -> 0.5038
# on the tuning half), the damage concentrated in earliest-selector
# displacement of already-correct rank-1 golds — the anatomy's
# gold-is-earliest evidence holds among MISSES and inverts among
# tops, so the SELECTOR IS DROPPED (declaration §5's drop rule;
# 0.0 is the drop, not a tuned value). Read 2 isolated the floor
# (clean +0.54 @5, @1 exactly preserved). Read 4 re-enters the
# window arm boost-only: read 1's window losses came through the
# demote side, its gains through the boost, so the demote stays 0.
_CONV_WINDOW_BOOST = 0.30
_CONV_WINDOW_DEMOTE = 0.0
_CONV_SELECTOR_BOOST = 0.0
_CONV_SELECTOR_DECAY = 0.7

# L2 (declared in bench/l/L2_DECLARATION.md): the pricing gate's
# widening and the keyword leg's scaffold weight. Both ship None —
# dark: with both None the engine is behaviorally identical to 6.1.0,
# and only a tuning-read config commit under the declaration's
# protocol may set an arm.
#
# _CONV_SCAFFOLD_MIN_STEMS — a query with NO temporal reading still
# enters the PRICING gate (the BM25 df-floor and the keyword
# repricing, never the window rerank) when it carries at least this
# many distinct scaffold tokens plus at least one content token. The
# count asks that dominate the multi-session residual ("how many
# projects have I led…") carry the scaffold class densely but parse
# no window and no selector; co-occurrence plus content is the
# structural signal. None removes the widening — the pricing gate is
# L1's temporal reading alone.
#
# Tuning read 1 (L2 §5): the declared defaults, armed whole — the
# widening at two stems, the keyword leg at weight zero. Net -0.68 @5
# on the half; the paired movers decompose 7-down/2-up temporal
# against 3-down/2-up scaffold-gated, so the α=0 keyword edge is the
# toxin: zeroed scaffold collapses a temporal gold's separation over
# generic content matchers into a raw/coverage tie, and the recency
# tiebreak hands the rank to the newer wrong doc — the inverse of the
# gold-is-earliest evidence. Read 2 priced the interior weight: the
# temporal edge went positive and the widening's three multi-session
# downs persisted at both weights — in count asks the bodies' own
# scaffold (amounts, "total", "this year") is evidence, and the
# widened floor strips the gold's edge over content lookalikes. Read
# 3 drops the widening and prices the keyword edge clean.
_CONV_SCAFFOLD_MIN_STEMS: int | None = None
# _CONV_KEYWORD_SCAFFOLD_WEIGHT — a priced query's scaffold terms
# contribute this multiple of their standard keyword-scorer
# contribution, and the coverage multiplier is computed over content
# terms alone (see score_memory). None leaves the keyword leg stock:
# L1 repriced only the BM25 half of the fusion's vote, and this
# constant is the other half. Read 2: the interior weight — scaffold
# priced down fourfold but not erased, keeping the separation the
# zero arm collapsed into recency-tiebreak losses. Read 4: the cap —
# the curve read -0.89, +0.25 type points at weights 0 and 0.25 on
# the temporal half's own type; the half weight prices the top of
# the declared range, where the two remaining partial losses stand
# to recover and the full rescue stands to survive.
_CONV_KEYWORD_SCAFFOLD_WEIGHT: float | None = 0.5

_MONTH_NAMES = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)
_MONTH_NO = {name: i + 1 for i, name in enumerate(_MONTH_NAMES)}

# Query-side temporal shapes. Matched against the RAW lowercased query,
# not the token stream — these are phrase shapes, and stemming would
# destroy them.
_CONV_DATE_RE = re.compile(r"\b(\d{4})[/-](\d{2})[/-](\d{2})\b")
_CONV_MONTH_RE = re.compile(r"\b(" + "|".join(_MONTH_NAMES) + r")\b(?:\s+(\d{4}))?")
_CONV_LAST_PERIOD_RE = re.compile(r"\blast\s+(week|month|year)\b")
_CONV_THIS_PERIOD_RE = re.compile(r"\bthis\s+(week|month|year)\b")
_CONV_YESTERDAY_RE = re.compile(r"\byesterday\b")
# "N days/weeks/months/years ago" as a STATEMENT of when — a window one
# unit wide centred on the rollback. The elapsed-ASK shape ("how many
# days ago did I…") is deliberately excluded: there the count is the
# question, not a constraint, and `_CONV_ELAPSED_RE` owns that reading.
_CONV_AGO_RE = re.compile(
    r"\b(?<!how many )(?<!how much )"
    r"(a|an|one|two|three|four|five|six|seven|eight|nine|ten|\d{1,2})\s+"
    r"(day|week|month|year)s?\s+ago\b"
)
_CONV_AGO_NUMBERS = {
    "a": 1,
    "an": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}
_CONV_AGO_UNIT_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}
_CONV_ELAPSED_RE = re.compile(
    r"\bhow\s+(?:many|much)\s+(?:days?|weeks?|months?|years?|time)\b"
    r"|\bhow\s+long\b"
)
_CONV_ORDER_RE = re.compile(
    r"\b(?:the\s+)?order\s+(?:of|from)\b|\bearliest\s+to\s+latest\b"
    r"|\bfirst\s+to\s+last\b|\bchronolog"
    r"|\bwhich\s+.{0,80}?(?:first|last)\b"
)
_CONV_WHEN_RE = re.compile(r"^when\b|\bwhen\s+did\b|\bwhat\s+(?:day|date|month)\b")
# 'last' used adverbially about the user's own action selects the LATEST
# narration ("since I last visited", "the last time I…"); 'last' as a
# period word ("last month") is a window, and 'last' inside an order ask
# ("first to last") is neither — the window and order regexes above
# consume those readings first.
_CONV_LATEST_RE = re.compile(
    r"\b(?:i|we)\s+last\b|\blast\s+time\b|\bmost\s+recent(?:ly)?\b"
)

# Memory-side anchors: the leading bracketed date line conversational
# ingest writes, else the first ISO-ish date early in the body.
_CONV_ANCHOR_HEAD_RE = re.compile(r"^\[(\d{4})[/-](\d{2})[/-](\d{2})")
_CONV_ANCHOR_SCAN_CHARS = 200


class _TemporalReading(NamedTuple):
    """A query's parsed temporal shape — `None`-free sentinel via fields.

    `window` is a closed [start, end] day range when the query names one
    (a month, a date, "last month"); `selector` is `"earliest"` /
    `"latest"` when the query's shape picks a narration by date order.
    Both empty means the query is not temporal and the lane must not
    touch anything.
    """

    window: tuple[date, date] | None
    selector: str | None

    @property
    def is_temporal(self) -> bool:
        return self.window is not None or self.selector is not None


def _month_window(month: int, year: int) -> tuple[date, date]:
    start = date(year, month, 1)
    end = (
        date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    ) - timedelta(days=1)
    return start, end


def _temporal_reading(query: str, now: datetime) -> _TemporalReading:
    """Parse the query's temporal shape against the caller's clock.

    Windows collected from every explicit form are merged to their
    envelope (min start, max end), which is what makes "between March
    and May" one range instead of two competing ones. Relative periods
    resolve calendar-correct against `now` — "last month" is the
    previous calendar month, not a 30-day rollback. Selector precedence
    per the declaration: an explicit window wins over a selector; the
    latest-selector's adverbial-'last' reading wins over the earliest
    default when both shapes appear.
    """
    lower = query.lower()
    today = now.date()
    windows: list[tuple[date, date]] = []

    for m in _CONV_DATE_RE.finditer(lower):
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            try:
                windows.append((date(y, mo, d), date(y, mo, d)))
            except ValueError:
                pass
    for m in _CONV_MONTH_RE.finditer(lower):
        month = _MONTH_NO[m.group(1)]
        if m.group(2):
            year = int(m.group(2))
        else:
            # The most recent occurrence of that month not after now.
            year = today.year if month <= today.month else today.year - 1
        windows.append(_month_window(month, year))
    for m in _CONV_LAST_PERIOD_RE.finditer(lower):
        unit = m.group(1)
        if unit == "week":
            start_this = today - timedelta(days=today.weekday())
            windows.append(
                (start_this - timedelta(days=7), start_this - timedelta(days=1))
            )
        elif unit == "month":
            prev_last = date(today.year, today.month, 1) - timedelta(days=1)
            windows.append(_month_window(prev_last.month, prev_last.year))
        else:
            windows.append((date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)))
    for m in _CONV_THIS_PERIOD_RE.finditer(lower):
        unit = m.group(1)
        if unit == "week":
            start_this = today - timedelta(days=today.weekday())
            windows.append((start_this, start_this + timedelta(days=6)))
        elif unit == "month":
            windows.append(_month_window(today.month, today.year))
        else:
            windows.append((date(today.year, 1, 1), date(today.year, 12, 31)))
    if _CONV_YESTERDAY_RE.search(lower):
        yday = today - timedelta(days=1)
        windows.append((yday, yday))
    for m in _CONV_AGO_RE.finditer(lower):
        count = _CONV_AGO_NUMBERS.get(m.group(1)) or int(m.group(1))
        unit_days = _CONV_AGO_UNIT_DAYS[m.group(2)]
        target = today - timedelta(days=count * unit_days)
        half = max(1, unit_days // 2)
        windows.append((target - timedelta(days=half), target + timedelta(days=half)))

    window: tuple[date, date] | None = None
    if windows:
        window = (min(w[0] for w in windows), max(w[1] for w in windows))

    selector: str | None = None
    if _CONV_ORDER_RE.search(lower):
        selector = "earliest"
    elif _CONV_ELAPSED_RE.search(lower) or _CONV_WHEN_RE.search(lower):
        selector = "latest" if _CONV_LATEST_RE.search(lower) else "earliest"
    elif _CONV_LATEST_RE.search(lower):
        selector = "latest"

    return _TemporalReading(window=window, selector=selector)


def _conv_scaffold_terms(tokens: list[str]) -> list[str]:
    """The query tokens the scaffold floor reprices: the closed class
    plus bare small numerals (one- and two-digit tokens — '3' in "3
    months ago"; four-digit years are a window constraint, never
    scaffold, and dotted version literals never match `isdigit`)."""
    return [
        t for t in tokens if t in _CONV_SCAFFOLD_STEMS or (t.isdigit() and len(t) <= 2)
    ]


def _conv_scaffold_shaped(query_tokens: list[str]) -> bool:
    """The widened pricing-gate predicate (L2): at least
    `_CONV_SCAFFOLD_MIN_STEMS` distinct scaffold tokens AND at least
    one content token. Count asks carry the scaffold class without
    parsing a window or a selector; requiring co-occurrence plus
    content is what keeps single common stems ("long", "time") from
    firing on non-count discourse — the declaration's rejected head
    trigger. None disables the widening entirely.
    """
    if _CONV_SCAFFOLD_MIN_STEMS is None:
        return False
    distinct = list(dict.fromkeys(query_tokens))
    scaffold = _conv_scaffold_terms(distinct)
    return len(scaffold) >= _CONV_SCAFFOLD_MIN_STEMS and len(scaffold) < len(distinct)


def _scaffold_floor_stats(
    base: CorpusStats | None, terms: list[str], pool_n: int
) -> CorpusStats | None:
    """CorpusStats with the scaffold df-floor applied for `terms`.

    The filler floor's exact mechanics (`_filler_floor_stats`, which
    see) pointed at the temporal-scaffold class instead: floor entries
    override per term through `compute_idf`, `max(real df, floor)`
    keeps the honest direction, and a query with no scaffold terms
    returns `base` untouched, allocation-free.
    """
    present = _conv_scaffold_terms(terms)
    if not present:
        return base
    size = base.size if base is not None else pool_n
    if size <= 0:
        return base
    floor = max(1, int(size * _CONV_SCAFFOLD_FLOOR_RATIO))
    body_df = dict(base.body_df) if base is not None else {}
    scope_df = dict(base.scope_df) if base is not None else {}
    for t in present:
        body_df[t] = max(body_df.get(t, 0), floor)
        scope_df[t] = max(scope_df.get(t, 0), floor)
    return CorpusStats(size=size, body_df=body_df, scope_df=scope_df)


def _memory_anchor_day(memory: Memory) -> date:
    """The day a memory's content is anchored to.

    In order: the leading bracketed date line conversational ingest
    writes, else the first ISO-ish date within the body's first
    `_CONV_ANCHOR_SCAN_CHARS` characters, else the day the memory was
    created — the product fallback, where `created` IS the best-known
    event time.
    """
    m = _CONV_ANCHOR_HEAD_RE.match(memory.body)
    if m is None:
        m = _CONV_DATE_RE.search(memory.body[:_CONV_ANCHOR_SCAN_CHARS])
    if m is not None:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            try:
                return date(y, mo, d)
            except ValueError:
                pass
    created = memory.created
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return created.date()


def _conversational_rerank(
    scored: list[tuple[Memory, float, list[str]]],
    *,
    reading: _TemporalReading,
) -> list[tuple[Memory, float, list[str]]]:
    """L1-T: date-anchor selection inside the near-tie band.

    Multiplicative, bounded adjustments on the fused RRF score, then a
    re-sort under the standard `(score, created, id)` key — determinism
    is structural. Items below the band, and every item when the query
    has no temporal reading, keep their score bit-for-bit.
    """
    if not scored:
        return scored
    top_score = scored[0][1]
    if top_score <= 0.0:
        return scored
    threshold = _CONV_BAND_TAU * top_score
    banded = [i for i, (_, s, _m) in enumerate(scored) if s >= threshold]
    if len(banded) <= 1:
        return scored
    anchors = {i: _memory_anchor_day(scored[i][0]) for i in banded}

    factors: dict[int, float] = {}
    if reading.window is not None:
        lo, hi = reading.window
        for i in banded:
            in_window = lo <= anchors[i] <= hi
            factors[i] = (
                1.0 + _CONV_WINDOW_BOOST if in_window else 1.0 - _CONV_WINDOW_DEMOTE
            )
    elif reading.selector is not None:
        distinct = sorted(set(anchors.values()), reverse=(reading.selector == "latest"))
        ordinal = {a: k for k, a in enumerate(distinct)}
        for i in banded:
            factors[i] = 1.0 + _CONV_SELECTOR_BOOST * (
                _CONV_SELECTOR_DECAY ** ordinal[anchors[i]]
            )
    else:
        return scored

    adjusted = [
        (memory, score * factors.get(i, 1.0), matched)
        for i, (memory, score, matched) in enumerate(scored)
    ]
    adjusted.sort(key=lambda x: (x[1], x[0].created, x[0].id), reverse=True)
    return adjusted


# `matched_leg` vocabulary: WHICH RANKER surfaced a hit. Reported per hit
# alongside `relevance` so the caller can read the label in the light of
# how the hit was found — evidence about the retrieval, not a second
# verdict about the memory.
#
# Since 4.0.0 every ranker is lexical (keyword, BM25, their fusion), so
# the base label's live value is `lexical`; the field survives so
# callers keyed on it keep parsing, and so the vocabulary has somewhere
# to grow if a future CODE ranker earns a leg of its own. The rescue-
# expansion leg (still deterministic lexical code, but matching
# SYNTHESIZED vocabulary rather than the caller's words) is that
# growth: a hit surfaced ONLY by it reports `expansion`, which is what
# keeps `relevance` readable — coverage of the caller's own tokens is
# legitimately "low" for a hit found through a synonym, and the leg
# says so instead of leaving the label looking broken.
LEG_LEXICAL = "lexical"
LEG_EXPANSION = "expansion"


def _matched_leg(*, lexical: bool, expansion: bool = False) -> str:
    """Leg label for one hit; `""` when no ranker ran at all.

    `lexical` wins when both are set — the base legs matched the
    caller's own words, which is the stronger statement; `expansion`
    is reported only for hits NO base leg scored. The empty case is
    browse mode (`allow_empty_query` with no query tokens): candidates
    are filtered and date-sorted, never ranked, so there is no leg to
    report and the field is omitted rather than guessed. The leg
    reports what RAN, not what was requested.
    """
    if lexical:
        return LEG_LEXICAL
    if expansion:
        return LEG_EXPANSION
    return ""


def _relevance_label(matched_unique: int, query_unique: int) -> str:
    """Map coverage (fraction of distinct query terms that hit) to a label.

    Calibrated for short queries: matching 1/1 or 2/2 is "high"; matching 1/3
    is "low". The thresholds are deliberately generous on the high side
    because a 1-word query with a strong match shouldn't be downgraded.

    NEGATIVE RESULT, 2026-07-30 — the recut that was NOT shipped. The
    plan for this label was to stop scoring pure-paraphrase hits by lexical
    coverage and label them from calibrated cosine bands instead, gated
    on the telemetry_v2 shadow replay: no user-message-length bucket at a
    0% high-rate, and a max/min bucket spread under 3x. Replaying the
    dogfood store's `turn_audited` log (317 turns, 274 carrying hits;
    the harness reproduces both logged labels exactly, so it is measuring
    the shipped rules and not a paraphrase of them) returns:

        user message chars     0-40   40-80   80-150   150+
        turns                    65      79       63      67
        v1 "high" rate          43%     25%       5%      4%

    and — the finding that closed the item — **0 of those 274 top hits
    carried `matched_unique == 0`**. The store runs the default install,
    so no semantic leg ever ran (that leg was removed outright in 4.0.0), so the population a cosine-band rule
    changes has ZERO representation in the instrument the bar is measured
    on. A semantic-only rule is provably byte-identical on that corpus:
    it cannot clear the 3x bar, and it cannot fail it either. Shipping
    bands anyway would have been calibrating a verdict on taste, which is
    the one thing this label's history says not to do (see
    `_relevance_label_v2`). So v1 stands, `matched_leg` ships instead of
    a new number, and `search` events now carry the leg — that is the
    instrument whose absence made the question unanswerable.

    Worth recording for whoever re-opens this: on the same 274 turns the
    v2 rule now profiles 46/62/86/100, i.e. no zero bucket and a 2.17x
    spread, so it PASSES the stated bar while remaining the
    length-credulous rule this project measured and rejected. The bar is
    a necessary condition, not a sufficient one — exactly what
    `_relevance_label_v2` says about the screen it comes from. Do not use
    it alone as a gate.
    """
    if query_unique <= 0:
        return "low"
    coverage = matched_unique / query_unique
    if coverage >= 0.75:
        return "high"
    if coverage >= 0.40:
        return "medium"
    return "low"


# Absolute matched-token floor for the v2 relevance label's "high" arm.
# Four distinct content tokens landing in one memory is strong evidence
# regardless of query length; the value deliberately mirrors
# `attribution._MIN_CONTAINMENT_TOKENS` — both gates answer "how many
# distinct content-token overlaps constitute a deliberate connection
# rather than coincidence" (cross-pinned in tests).
_V2_HIGH_MATCHED_FLOOR = 4


def _relevance_label_v2(matched_unique: int, query_unique: int) -> str:
    """Candidate successor to `_relevance_label` — SHADOW-ONLY.

    Same coverage mapping, plus an absolute matched-count floor on the
    "high" arm: a long natural-language query that matches >=
    `_V2_HIGH_MATCHED_FLOOR` distinct content tokens labels "high" even
    when the coverage FRACTION dips below 0.75 — the denominator grows
    with query length, the evidence doesn't shrink. This is the
    structural candidate fix for audit.py's documented blind spot
    ("multi-token natural language with stopwords often lands at
    2/3 = medium and does not fire").

    Shadow contract: the v2 label is logged on `search` /
    `turn_audited` / `search_miss` events for calibration and NEVER
    surfaced in an MCP response — live behavior (expand_top, the miss
    threshold rule) stays on v1 until the logged v1/v2 disagreement
    data justifies a flip. v1-high implies v2-high (the fraction arm is
    unchanged), so v2 is a strict widening.

    THAT DATA NOW EXISTS, AND IT SAYS DO NOT FLIP. Measured 2026-07-25
    over 195 `turn_audited` events carrying both labels, from a
    185-memory dogfood store. Bucketing the SAME turns by the length of
    the user message that produced them:

        user message chars     0-40   40-80   80-150   150+
        v1 "high" rate          45%     32%       0%     3%
        v2 "high" rate          47%     63%      83%   100%

    Neither label is measuring relevance; both are measuring LENGTH, in
    opposite directions. v1's coverage fraction has a denominator that
    grows with the query, so a long message can no longer clear 0.75 and
    the rate collapses to zero — the documented blind spot this function
    was written to close. But the floor that closes it replaces a
    length-BLIND rule with a length-CREDULOUS one: four distinct content
    tokens landing somewhere in a 185-document store is not evidence
    about a specific memory, it is a near-certainty for any message long
    enough, which is why the v2 arm reaches 100% and stays there.

    Flipping the miss rule to v2 would have taken this store from 11
    flagged misses to 105 of 195 turns. Reading the 95 newly-flagged
    previews is the fastest way to see it: they are dominated by bare
    continuations ("sorry continue, 5 hour limit cap" x6, "sorry about
    that, i restarted the server" x8) and ordinary work turns ("fix the
    readme" x4, "improve the slogans", "dude this svg sucks") — turns
    with no memory to miss.

    A rule's flag rate not tracking message length is a necessary
    condition, not a sufficient one, and it is checkable without ground
    truth, which is why it is the screen used here. Same 195 turns,
    max-minus-min flag rate across those four buckets: v1 45%, v2 53%
    (worse than what it replaces, and the only candidate that climbs
    monotonically to 100%), `matched>=4` alone 92%, and the CONJUNCTIVE
    form `coverage>=0.75 AND matched>=4` 29% at a 11.3% flag rate. So
    the matched-token count is worth keeping and the `or` is the defect:
    it lets the floor overrule the fraction instead of corroborating it.
    That conjunction is the candidate a v5 should be calibrated from —
    against labelled turns, since this screen can only rank candidates,
    never confirm one. Caveat that travels with every number here: one
    store, one user, n=195.
    """
    if query_unique <= 0:
        return "low"
    coverage = matched_unique / query_unique
    if coverage >= 0.75 or matched_unique >= _V2_HIGH_MATCHED_FLOOR:
        return "high"
    if coverage >= 0.40:
        return "medium"
    return "low"


def _scope_tokens(scope: str) -> list[str]:
    """Break `projects:foo-bar` into ['projects', 'foo-bar', 'foo', 'bar']
    for matching — both the joined form and its components are emitted.
    """
    return _expand_kebab(tokenize(scope))


class _MemoryTokens(NamedTuple):
    """Per-memory token streams, computed once per `search()` call.

    `tokenize()` is the hot spot of a search (NFKC + diacritic fold +
    stemmer + CJK segmentation), and before this existed each candidate's
    body and scopes were re-tokenized by every consumer — the keyword
    scorer, `compute_idf`, and `score_memory_bm25` — 6 tokenize calls per
    memory per hybrid search, ~88% of cumulative search time on a
    ~500-memory store. The scorers accept this as an optional `tokens`
    argument; passing it must be a pure perf change (the fields are
    exactly the expressions the recompute path evaluates), pinned by the
    precompute-equality test in test_search.py. Consumers only read —
    never mutate — the shared lists/sets.
    """

    body: list[str]
    """`_expand_kebab(tokenize(memory.body))` — stopwords kept (the
    keyword scorer's stream; `set()` of it was the literal-match
    stream)."""

    content: list[str]
    """`_strip_stopwords(body)` — the BM25/IDF stream."""

    scope_set: set[str]
    """Union of `_scope_tokens(scope)` across `memory.scopes` — every
    consumer builds a set from the per-scope lists, so only the union is
    kept."""


def _memory_tokens(memory: Memory) -> _MemoryTokens:
    """Build the `_MemoryTokens` for one candidate. Field expressions
    mirror the scorers' recompute paths token for token — see
    `_MemoryTokens` for why equality is load-bearing."""
    body = _expand_kebab(tokenize(memory.body))
    scope_set: set[str] = set()
    for scope in memory.scopes:
        scope_set.update(_scope_tokens(scope))
    return _MemoryTokens(body=body, content=_strip_stopwords(body), scope_set=scope_set)


def _recency_factor(created: datetime, now: datetime, half_life_days: float) -> float:
    """1 + 0.1 * exp(-days_old / half_life). Mild bump, not a takeover."""
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    age_seconds = max(0.0, (now - created).total_seconds())
    age_days = age_seconds / 86400.0
    return 1.0 + 0.1 * math.exp(-age_days / max(half_life_days, 0.001))


def _endorsement_factor(applied_count: int) -> float:
    """1 + 0.1 * (1 - exp(-applied_count / 3)). Mild usage bump, bounded to
    [1.0, 1.1) — exactly the ceiling `_recency_factor` uses.

    A memory the model has DELIBERATELY applied (an explicit
    `memory_record_use(applied)`, not the auto-fallback) climbs slightly, so
    a load-bearing fact wins a near-tie over a never-endorsed peer. The cap
    is the whole point: like recency, it can only break near-ties, never
    override the relevance signal — which keeps it from a rich-get-richer
    runaway. `applied_count == 0` returns exactly 1.0 (neutral), so the
    factor is a no-op unless real endorsement counts are supplied."""
    if applied_count <= 0:
        return 1.0
    return 1.0 + 0.1 * (1.0 - math.exp(-applied_count / 3.0))


def _demotion_factor(ignored_count: int, contradicted_count: int) -> float:
    """1 - 0.15 * (1 - exp(-(ignored + 2*contradicted) / 3)). Bounded to
    (0.85, 1.0] — the negative mirror of `_endorsement_factor`.

    A memory the model has recently rejected (`memory_record_use(ignored)`
    — retrieved but off-topic) or, worse, flagged as wrong
    (`contradicted`) slides down slightly. Contradicted weighs double:
    an off-topic surfacing is often the QUERY's fault, but a stored
    claim that disagreed with reality is the memory's. The cap is
    slightly deeper than endorsement's +10% on purpose — retrieving a
    falsehood costs more than missing a bonus tie-break — but still a
    near-tie signal, never a relevance override.

    Three guards keep this from a rich-get-poorer death spiral (demoted →
    less surfaced → never earns the clearing `applied`):

    - counts come from a bounded window (the caller tallies over
      `NEGATIVE_OUTCOME_WINDOW_DAYS`), so evidence expires;
    - a later NON-AUTO `applied` supersedes earlier negatives (the same
      rule `recent_negative_outcomes` uses — the model re-validated it);
    - a `memory_update` or `memory_verify` newer than the negative event
      clears it (the same resolution semantics as
      `health._has_unresolved_contradiction` — the claim the event judged
      no longer exists, or was re-attested true).

    Both counts at 0 return exactly 1.0 (neutral), so the factor is a
    no-op unless real negative counts are supplied."""
    weighted = max(0, ignored_count) + 2 * max(0, contradicted_count)
    if weighted <= 0:
        return 1.0
    return 1.0 - 0.15 * (1.0 - math.exp(-weighted / 3.0))


def _corroboration_factor(corroborations: int) -> float:
    """1 + 0.1 * (1 - exp(-corroborations / 3)). Same shape and +10% cap
    as `_endorsement_factor`, fed by a different signal: endorsement is
    "the model deliberately APPLIED this in a reply"; corroboration is
    "the claim independently RE-ENTERED a conversation and dedup caught
    it" (`Store.record_corroboration`, once per session per memory). A
    claim that keeps coming up wins a near-tie over a one-off remark —
    recurrence accumulating into retrieval weight, capped so it can
    never override relevance. Reads the persisted rollup on the Memory
    record, so unlike the event-fed factors it costs no event-log walk."""
    if corroborations <= 0:
        return 1.0
    return 1.0 + 0.1 * (1.0 - math.exp(-corroborations / 3.0))


# ---------------------------------------------------------------------------
# BM25 scorer (Okapi variant)
# ---------------------------------------------------------------------------
#
# The Jaccard / TF-coverage scorer below (score_memory) treats every term
# equally and adds a coverage multiplier. It works well for short, content-
# rich queries but undervalues rare terms and overvalues repeated common
# ones. BM25 corrects both: IDF weights rare terms higher, TF saturation
# clips diminishing returns on repeats, and length normalisation gives a
# small edge to focused short bodies over long ones with the same hit
# count. We keep the scope-match bonus and recency factor on top so the
# bettermemory-specific signals still apply — BM25 isn't a religion, it's
# one of several signals fused by RRF in hybrid mode.
#
# `compute_idf` is a one-pass corpus walk run once per search() call; it's
# O(total_tokens) and shows up nowhere on profiles for corpora under ~50K
# memories. When we add an inverted index (T3.1), the same shape returns
# directly from the index.


_BM25_K1_DEFAULT = 1.2
_BM25_B_DEFAULT = 0.75


class CorpusStats(NamedTuple):
    """Document frequencies over the collection a search ranks, for when
    `memories` is a subset of it.

    `size` is the ranked collection's document count; `body_df` / `scope_df`
    map a term to how many documents in that collection carry it, in the
    body and in body-or-scopes respectively — the two denominators
    `compute_idf` otherwise derives from the list it was handed.

    "The collection" is deliberately NOT the whole store:
    `index.corpus_document_frequencies` builds these over the admitted set
    — scopes, excluded scopes, repo and worktree, the same rule
    `_filter_candidates` applies — so the denominator describes documents
    the caller could actually retrieve. A provider that fed whole-store
    frequencies here would re-create the auto-scope mispricing c58c836
    removed. Which see for why the subset case needs this at all.
    """

    size: int
    body_df: dict[str, int]
    scope_df: dict[str, int]


def compute_idf(
    memories: list[Memory],
    *,
    tokens: list[_MemoryTokens] | None = None,
    corpus_stats: CorpusStats | None = None,
) -> tuple[dict[str, float], dict[str, float], float]:
    """Build the per-term IDF maps and the average doc length for BM25.

    Returns ``(body_idf_map, scope_idf_map, avgdl)``. Both maps carry
    `term -> log((N - df + 0.5) / (df + 0.5) + 1.0)`, the Okapi BM25 IDF
    variant that stays non-negative (so terms appearing in >half the
    corpus still contribute a tiny positive signal rather than pushing
    scores down) — they differ only in what counts toward df:

    - ``body_idf_map``: df over BODY tokens only. Prices the tf > 0
      branch in `score_memory_bm25` off body rarity. Scope tokens are
      deliberately excluded: a single shared map fed body scoring too,
      which crushed the body-match weight of any term that rides every
      candidate's scope — under auto-scoping that is the project's own
      name, the most-queried term of all, so a body literally answering
      'bettermemory crash' lost to a fresher body that never mentioned
      the project (the term's df hit N via scopes and its IDF ~0).
    - ``scope_idf_map``: df over body AND scope tokens (each term once
      per memory). Prices the `2.0 * idf` scope bonus, which
      self-deflates for ubiquitous namespace tokens: 'projects' sits on
      every project-scoped memory, so its df approaches N and its Okapi
      IDF approaches 0, while a discriminating scope token ('homelab')
      keeps a high IDF. Body-only IDF priced that bonus off body rarity
      alone, letting the bare namespace prefix outrank genuine
      full-coverage body matches.

    ``avgdl``: average kebab-expanded stopword-stripped doc length
    across the corpus. Length normalisation in BM25 reads from this.
    Scope tokens do NOT count toward `avgdl` — it is a body-length
    statistic.

    Tokenisation here matches `_content_token_set` (the dedup path) on
    the body side — kebab expansion symmetric, stopwords stripped. The
    search-time query side strips stopwords too. Empty corpus returns
    `({}, {}, 0.0)` so callers can short-circuit.

    ``tokens``: optional precomputed `_MemoryTokens`, index-aligned with
    `memories` — `search()` tokenizes each candidate once and threads the
    streams here. None recomputes them; identical output either way.

    ``corpus_stats``: optional ranked-collection document frequencies —
    counted over the ADMITTED set, not the whole store; see `CorpusStats`.
    Supply it whenever `memories` is a query-biased SUBSET of that
    collection — which is what the FTS prefilter produces above
    `_INDEX_THRESHOLD_DEFAULT`. Without it, df is counted over candidates
    that are present precisely because they matched, so a discriminative
    query term's df approaches N and its Okapi IDF collapses toward zero:
    BM25 degenerates into length normalisation plus recency for exactly the
    term that should dominate. Measured at 74x on a 608-memory store.

    Supplied stats OVERRIDE per term rather than replace the maps
    wholesale: any term the corpus lookup does not carry (a stale index, a
    tokenizer edge) keeps its pool-derived value, so the degraded path is
    never worse than not passing stats at all.

    ``avgdl`` stays pool-derived even with `corpus_stats`. It is a scalar
    length normaliser, so a biased sample shifts scores by a roughly common
    factor instead of inverting their order the way collapsed IDF does, and
    a corpus-wide average would cost a full vocabulary scan per search.
    """
    n = len(memories)
    if n == 0:
        return {}, {}, 0.0

    body_df: dict[str, int] = {}
    scope_df: dict[str, int] = {}
    total_len = 0
    for i, memory in enumerate(memories):
        pre = tokens[i] if tokens is not None else None
        toks = (
            pre.content
            if pre is not None
            else _strip_stopwords(_expand_kebab(tokenize(memory.body)))
        )
        total_len += len(toks)
        # Count each term once per doc — that's document-frequency, not
        # term-frequency. set() collapses repeats; scope tokens join the
        # scope-side per-doc set only (see docstring) and avgdl stays
        # body-only.
        body_terms = set(toks)
        for term in body_terms:
            body_df[term] = body_df.get(term, 0) + 1
        doc_terms = set(body_terms)
        if pre is not None:
            doc_terms.update(pre.scope_set)
        else:
            for scope in memory.scopes:
                doc_terms.update(_scope_tokens(scope))
        for term in doc_terms:
            scope_df[term] = scope_df.get(term, 0) + 1

    avgdl = total_len / n if n else 0.0

    def _okapi(df: dict[str, int], total: int) -> dict[str, float]:
        return {
            term: math.log((total - dfi + 0.5) / (dfi + 0.5) + 1.0)
            for term, dfi in df.items()
        }

    body_idf = _okapi(body_df, n)
    scope_idf = _okapi(scope_df, n)

    if corpus_stats is not None and corpus_stats.size > 0:
        # Per-term override, not a wholesale swap — see the docstring. A
        # corpus df of 0 is dropped rather than trusted: it means the index
        # disagrees with a body we can see in front of us (mid-write, or a
        # stale row), and the pool-derived value is the better guess there.
        corpus_n = corpus_stats.size
        for term, dfi in corpus_stats.body_df.items():
            if term in body_idf and dfi > 0:
                body_idf[term] = math.log((corpus_n - dfi + 0.5) / (dfi + 0.5) + 1.0)
        for term, dfi in corpus_stats.scope_df.items():
            if term in scope_idf and dfi > 0:
                scope_idf[term] = math.log((corpus_n - dfi + 0.5) / (dfi + 0.5) + 1.0)

    return body_idf, scope_idf, avgdl


def score_memory_bm25(
    memory: Memory,
    query_tokens: list[str],
    *,
    body_idf_map: dict[str, float],
    scope_idf_map: dict[str, float],
    avgdl: float,
    now: datetime,
    half_life_days: float = 30.0,
    k1: float = _BM25_K1_DEFAULT,
    b: float = _BM25_B_DEFAULT,
    tokens: _MemoryTokens | None = None,
    stopword_fallback: bool = False,
) -> tuple[float, list[str]]:
    """BM25 score for one memory against a tokenized query.

    Body terms scored via standard Okapi BM25: `idf * tf * (k1+1) /
    (tf + k1 * (1 - b + b*dl/avgdl))`, priced off `body_idf_map`. Scope
    matches add `2.0 * idf` as a fixed bonus priced off the
    scope-inclusive `scope_idf_map`, matching the keyword scorer's 2x
    scope weight so fusing the two rankers doesn't reweight scopes
    accidentally — see `compute_idf` for why the two signals read
    different df statistics. The recency multiplier (`_recency_factor`)
    is applied at the end so a recently-edited memory climbs the same
    way it does in the keyword scorer.

    Returns `(score, matched_terms)`. `matched_terms` is the unique
    subset of `query_tokens` that hit body or scopes — used for the
    `match_terms` field on `MemoryHit` so the consumer sees which
    query words actually pulled the result up.

    Empty `query_tokens` or `avgdl <= 0` (empty corpus) returns
    `(0.0, [])`. Unknown terms (not in `body_idf_map`) contribute zero
    from the body but can still match a scope; scope-only matches
    default to `idf=1.0` since the term has no corpus statistics yet.

    ``tokens``: optional precomputed `_MemoryTokens` for this memory —
    `search()` tokenizes each candidate once and threads the streams
    here. None recomputes them; identical output either way.

    ``stopword_fallback``: set by `search()` when stopword stripping
    emptied the query and the unstripped-token fallback fired (every
    query token is a stopword). Body TF is then counted against the
    UNSTRIPPED body stream — the keyword scorer's stream — instead of
    the stripped content stream, with body IDF floored at 1.0 (fallback
    tokens are stopwords, absent from the content-stream `body_idf_map`
    by construction; the same no-corpus-statistics floor scope-only
    matches use). `dl` and the caller's `avgdl` stay on the content
    stream so length normalisation prices one consistent statistic in
    both calls. False (the default) is byte-identical to the scoring
    behaviour before the flag existed; without it a fallen-back token
    could match scopes but never a body — silent zero recall in
    mode="bm25" for exactly the stopword-collision queries the
    `search()` fallback guarantees answerable.
    """
    if not query_tokens or avgdl <= 0:
        return 0.0, []

    # Fallback calls count TF against the unstripped stream (stopwords
    # kept) so the fallen-back tokens are matchable at all; `dl` stays
    # on the content stream in BOTH calls — avgdl is a content-stream
    # statistic, and length normalisation must keep pricing the same
    # ratio whether or not the fallback fired.
    if tokens is not None:
        content_tokens = tokens.content
        body_stream = tokens.body
    else:
        body_stream = _expand_kebab(tokenize(memory.body))
        content_tokens = _strip_stopwords(body_stream)
    count_tokens = body_stream if stopword_fallback else content_tokens
    body_count: dict[str, int] = {}
    for tok in count_tokens:
        body_count[tok] = body_count.get(tok, 0) + 1
    dl = len(content_tokens)
    # Component counter for the conjunctive kebab fallback below, built
    # lazily (only when a compound query token misses directly). It reads
    # the UNSTRIPPED `body_stream` so a stopword component ('to' in
    # 'end-to-end', both parts of 'to-do') is countable at all: in
    # non-fallback mode `body_count` above is built from the stopword-
    # STRIPPED content stream, where such a component has count 0 and would
    # zero the min() — silent zero recall in mode='bm25' for the whole
    # X-to-X / X-by-X compound family. Mirrors the keyword scorer, whose
    # `body_count` is unstripped by construction.
    comp_count: dict[str, int] | None = None

    if tokens is not None:
        scope_set = tokens.scope_set
    else:
        scope_set = set()
        for scope in memory.scopes:
            scope_set.update(_scope_tokens(scope))

    score = 0.0
    matched: list[str] = []
    length_norm = 1 - b + b * (dl / avgdl) if avgdl > 0 else 1.0
    # De-duplicate query tokens (insertion-ordered) before accumulating:
    # `matched` always used set semantics, but the raw loop re-added the
    # full saturated-TF contribution (and the scope bonus) per duplicate,
    # so a reduplicated phrase query ("end to end") silently doubled the
    # repeated word's weight. Byte-identical for non-duplicated queries.
    for tok in dict.fromkeys(query_tokens):
        contrib = 0.0

        tf = body_count.get(tok, 0)
        # Fallback tokens are stopwords — absent from the content-stream
        # `body_idf_map` by construction — so floor their body IDF at 1.0
        # (the scope bonus's no-corpus-statistics floor below), or the
        # occurrence just counted would be zeroed right back by a 0.0 IDF.
        body_idf = body_idf_map.get(tok, 1.0 if stopword_fallback else 0.0)
        scope_hit = tok in scope_set
        # Floor IDF at 1.0 for scope-only hits so a brand-new scope
        # term (absent from every body AND scope in the idf corpus)
        # still contributes; the 2x factor keeps it aligned with the
        # keyword scorer. compute_idf counts scope tokens into the
        # scope-inclusive map's df, so known scope terms price off real
        # corpus statistics instead of this floor.
        scope_idf = scope_idf_map.get(tok, 1.0)
        if tf == 0 and not scope_hit:
            # Conjunctive fallback for a joined query token with no
            # direct hit — see `_kebab_parts`. ALL components must hit
            # (preserving the 'python-frontmatter' must-not-match-plain-
            # 'python' precision guard); tf is the min component count
            # (the joined phrase can occur at most that often) and IDF
            # is the min across components — the weakest component
            # bounds how discriminating the joined phrase can be.
            #
            # Range the conjunction over the UNFILTERED parts and count
            # them against the unstripped `comp_count`. Filtering the
            # stopword parts out instead collapsed the conjunction: an
            # 'X-to-X' compound reduced to a single-word OR ('end-to-end'
            # -> match any 'end'), and a compound whose parts are ALL
            # stopwords ('to-do' -> ['to','do']) emptied `parts` and
            # skipped the fallback entirely — and 'to-do' survives
            # `_strip_stopwords` too, so it never reached the stopword
            # fallback either: silent zero recall in mode='bm25' for the
            # whole X-to-X / X-by-X family. This mirrors the keyword
            # scorer, whose fallback already ranges over unfiltered parts
            # on an unstripped body_count.
            parts = _kebab_parts(tok)
            if parts:
                if comp_count is None:
                    comp_count = {}
                    for t in body_stream:
                        comp_count[t] = comp_count.get(t, 0) + 1
                component_hits = [comp_count.get(p, 0) for p in parts]
                if min(component_hits) > 0:
                    tf = min(component_hits)
                    # Price body IDF off the parts that carry content-
                    # stream corpus statistics — the non-stopword ones.
                    # When EVERY part is a stopword ('to-do') there is no
                    # body IDF to read; floor at 1.0 (the same no-corpus-
                    # statistics floor scope-only and fallback matches use)
                    # rather than letting a defaulted 0.0 IDF zero the
                    # occurrence just counted.
                    content_parts = [p for p in parts if p not in _STOPWORDS]
                    body_idf = (
                        min(body_idf_map.get(p, 0.0) for p in content_parts)
                        if content_parts
                        else 1.0
                    )
                if all(p in scope_set for p in parts):
                    scope_hit = True
                    scope_idf = min(scope_idf_map.get(p, 1.0) for p in parts)

        if tf > 0:
            denom = tf + k1 * length_norm
            contrib += body_idf * tf * (k1 + 1) / denom if denom > 0 else 0.0

        if scope_hit:
            contrib += 2.0 * scope_idf

        if contrib > 0:
            matched.append(tok)
        score += contrib

    if score <= 0:
        return 0.0, []

    freshness = max(memory.created, memory.updated)
    return score * _recency_factor(freshness, now, half_life_days), matched


def score_memory(
    memory: Memory,
    query_tokens: list[str],
    *,
    now: datetime,
    half_life_days: float = 30.0,
    tokens: _MemoryTokens | None = None,
    scaffold_terms: frozenset[str] | None = None,
    scaffold_weight: float = 0.0,
) -> tuple[float, list[str]]:
    """Score a memory against a query. Return `(score, matched_terms)`.

    `matched_terms` is the de-duplicated subset of `query_tokens` that hit
    the body or scopes — surfaced in the result so the consumer can tell
    whether a partial match is meaningful or stopword-driven noise.

    ``tokens``: optional precomputed `_MemoryTokens` for this memory —
    `search()` tokenizes each candidate once and threads the streams
    here. None recomputes them; identical output either way.

    ``scaffold_terms`` / ``scaffold_weight`` (L2, lane-internal): when
    `scaffold_terms` is given — search() passes it only for a priced
    conversational query with at least one content term — each scaffold
    term's contribution is multiplied by `scaffold_weight`, and the
    coverage multiplier is computed over content terms alone, so
    scaffold can neither pay for rank nor dilute coverage. A scaffold
    hit that coexists with content still appends to `matched_terms`
    (display and evidence read as before); a candidate whose every
    match is scaffold scores `raw == 0` at weight zero and drops from
    the leg. None (the default, and every non-lane caller) is the
    stock scorer, byte-identical to before the parameters existed.
    """
    if not query_tokens:
        return 0.0, []

    body_tokens = (
        tokens.body if tokens is not None else _expand_kebab(tokenize(memory.body))
    )
    body_count: dict[str, int] = {}
    for tok in body_tokens:
        body_count[tok] = body_count.get(tok, 0) + 1

    if tokens is not None:
        scope_set = tokens.scope_set
    else:
        scope_set = set()
        for scope in memory.scopes:
            scope_set.update(_scope_tokens(scope))

    raw = 0.0
    matched: list[str] = []
    content_matched = 0
    query_unique = len(set(query_tokens))
    # De-duplicate query tokens (insertion-ordered) before accumulating:
    # coverage and `matched` always used set semantics, but the raw loop
    # re-added the full contribution per duplicate, so a reduplicated
    # phrase query ("end to end") silently doubled the repeated word's
    # weight. Byte-identical for non-duplicated queries.
    for tok in dict.fromkeys(query_tokens):
        body_hits = body_count.get(tok, 0)
        scope_hit = 1 if tok in scope_set else 0
        if body_hits == 0 and scope_hit == 0:
            # Conjunctive fallback for a joined query token with no
            # direct hit: 'claude-code' should match a body that spells
            # it 'Claude Code'. ALL components must hit (preserving the
            # 'python-frontmatter' must-not-match-plain-'python'
            # precision guard); the contribution is the min component
            # count — the joined phrase can occur at most that often.
            parts = _kebab_parts(tok)
            if parts:
                component_hits = [body_count.get(p, 0) for p in parts]
                if min(component_hits) > 0:
                    body_hits = min(component_hits)
                if all(p in scope_set for p in parts):
                    scope_hit = 1
        # Per-term body TF saturates at 2 (scopes stay weighted 2x). The
        # coverage multiplier below spans only 2x, so an unbounded TF sum
        # would overrun it: a single-term spam body capped at 2 tops out
        # at 2 * (0.5 + 0.5/n) <= 1.5, strictly below any full-coverage
        # match (raw >= n, multiplier 1.0) for every query length n >= 2.
        #
        # n == 1 is a deliberate carve-out: the proof above only covers
        # n >= 2, and at n == 1 the coverage multiplier is CONSTANT
        # (every matching doc has coverage 1/1), so there is no
        # cross-term coverage race for repeated TF to overrun. The hard
        # cap there erased ALL term-frequency discrimination instead —
        # a focal six-mention body and an incidental two-mention body
        # tied at raw 2, recency decided, and the mirrored hybrid RRF
        # tie's created-desc tiebreaker handed rank 1 (and expand_top's
        # inlined body) to the newer wrong doc. A log1p residual above
        # the cap restores monotone TF ordering while staying
        # sub-linear: 1000 mentions earn ~2 + log1p(998) ≈ 8.9 raw, not
        # 1000, so repetition has steeply diminishing returns rather
        # than a linear takeover.
        contrib: float = min(body_hits, 2) + 2 * scope_hit
        if query_unique == 1 and body_hits > 2:
            contrib += math.log1p(body_hits - 2)
        if contrib > 0:
            matched.append(tok)
        if scaffold_terms is not None and tok in scaffold_terms:
            raw += contrib * scaffold_weight
        else:
            raw += contrib
            if contrib > 0:
                content_matched += 1

    if raw == 0.0:
        return 0.0, []

    # Mild boost for matching multiple distinct query terms — together with
    # the per-term TF cap above, this is what actually keeps "foo bar"
    # ranked above "foo foo foo" when the latter is just keyword spam.
    if scaffold_terms is None:
        coverage = len(matched) / query_unique
    else:
        # L2: content coverage. The caller guarantees at least one
        # content term in the query, so the denominator is never zero.
        content_unique = sum(
            1 for t in dict.fromkeys(query_tokens) if t not in scaffold_terms
        )
        coverage = content_matched / content_unique
    base = raw * (0.5 + 0.5 * coverage)

    # Recency boost reads from the freshness timestamp — `max(created, updated)`
    # — so an edited memory ranks like a new one. Without this, calling
    # memory_update on a year-old fact gives it the score of a year-old fact;
    # with it, refining a fact moves it up the list as you'd expect.
    freshness = max(memory.created, memory.updated)
    return base * _recency_factor(freshness, now, half_life_days), matched


# ---------------------------------------------------------------------------
# Reciprocal Rank Fusion
# ---------------------------------------------------------------------------
#
# RRF (Cormack, Clarke, Büttcher, SIGIR 2009) fuses multiple ranked lists
# into one without needing the underlying scores to be on the same scale.
# Each doc's fused score is the sum, over rankers, of `1 / (k + rank)`.
# Docs absent from a ranker contribute nothing from that ranker. k=60 is
# the original paper's recommendation and is the de-facto default across
# implementations.
#
# Why RRF and not weighted score fusion: BM25 scores, Jaccard-style
# coverage scores, and cosine scores live on different scales (BM25 is
# unbounded, cosine is 0..1, the keyword scorer here mixes raw counts
# with multiplicative coefficients). Adding them directly biases the
# fused result toward whichever scale happens to be largest. Rank-only
# fusion sidesteps the calibration problem entirely — only positions
# matter, so a ranker can swap its scoring function without changing
# the fused output as long as the order stays the same.
#
# Practical note: when only one ranker is provided, RRF degenerates to
# `1 / (k + rank)` over that ranker — order is preserved, scores are
# rescaled. Callers can use that as a sanity check.


_RRF_K_DEFAULT = 60

# Relative score lead the top hit must hold over the runner-up before a
# caller's `expand_top` inlines its body (`handlers/search.py`). Lives
# here because it is DERIVED from RRF's spacing rather than chosen: a
# fused score is a sum of `1/(k + rank)` terms, so a top hit that leads
# by exactly ONE rank slot in every ranker scores `(k+2)/(k+1)` of the
# runner-up — 1.6% at the canonical k — which is the smallest possible
# non-tie and carries no information beyond "not tied". A TWO-slot lead,
# `(k+3)/(k+1)`, is the first margin a single position disagreement
# cannot manufacture, so that is the bar. Measured on the dogfood store's
# logged searches, the median top/runner-up ratio is 1.016 — the one-slot
# case, i.e. most hybrid results genuinely are near-ties and the
# derivation is sitting right on top of the mass of the distribution.
#
# Raw-score modes (keyword / bm25) separate far more widely
# than fused ones, so the same constant is loose there by construction.
# That is the correct direction: it is calibrated on the tightest scale
# in use, and a mode whose scores actually spread has already made the
# caller's case for it.
EXPAND_TOP_SCORE_MARGIN = (_RRF_K_DEFAULT + 3) / (_RRF_K_DEFAULT + 1) - 1.0


def top_hit_leads_runner_up(top_score: float, next_score: float) -> bool:
    """Does the top hit lead the runner-up by more than a rank slot?

    The leg-agnostic half of the `expand_top` gate. The lexical half —
    a "high" coverage label — could not fire for a pure-paraphrase hit
    (`match_terms` is empty by construction), which is how the 4x-cost
    semantic capability (removed in 4.0.0) ended up unable to trigger the one affordance
    that shows the caller a full body.

    Zero or negative runner-up scores mean nothing to compare against on
    a ratio, so any positive top score leads. An all-zero result set
    (browse mode) leads nothing.
    """
    if next_score <= 0.0:
        return top_score > 0.0
    return top_score >= next_score * (1.0 + EXPAND_TOP_SCORE_MARGIN)


def reciprocal_rank_fusion(
    ranking_lists: list[list[str]],
    *,
    k: int = _RRF_K_DEFAULT,
    weights: list[float] | None = None,
) -> dict[str, float]:
    """Fuse multiple ranked id-lists into one score-per-id map.

    Each `ranking_lists[i]` is a list of memory ids in best-first order
    for ranker i. The returned dict maps memory_id -> RRF score; sort
    descending to get the fused ranking. Ids that appear in no list are
    not present in the output. Duplicate ids within a single ranker's
    list are unusual but tolerated — the first (best-ranked) position
    wins for that ranker; later duplicates are ignored, matching the
    "one rank per (ranker, doc)" reading of the original paper.

    `weights` (optional) is index-aligned with `ranking_lists`: ranker
    i contributes `weights[i] / (k + rank)` instead of `1 / (k + rank)`.
    None means all-1.0 and is byte-identical to the pre-weights output
    — the rescue-expansion leg is the reason this exists (a rescue
    contributes at reduced strength; base legs stay at 1.0). Length
    mismatch raises: a silently-recycled weight would misweight a leg
    without anything looking wrong.

    Empty `ranking_lists` returns an empty dict. `k` must be positive;
    the default (60) matches the Cormack et al. paper.
    """
    if k <= 0:
        raise ValueError(f"RRF k must be positive, got {k}")
    if weights is not None and len(weights) != len(ranking_lists):
        raise ValueError(
            f"weights length {len(weights)} != rankings length {len(ranking_lists)}"
        )
    if not ranking_lists:
        return {}

    fused: dict[str, float] = {}
    for i, ranking in enumerate(ranking_lists):
        w = weights[i] if weights is not None else 1.0
        # Iterate with 1-indexed rank — the original formula assumes
        # rank starts at 1. `seen` guards the dedup contract above.
        seen: set[str] = set()
        for rank, memory_id in enumerate(ranking, start=1):
            if memory_id in seen:
                continue
            seen.add(memory_id)
            fused[memory_id] = fused.get(memory_id, 0.0) + w / (k + rank)
    return fused


def _filter_candidates(
    memories: list[Memory],
    *,
    scopes: list[str] | None,
    excluded_scopes: set[str] | None,
    repo_filter: str | None,
    worktree_filter: str | None,
) -> list[Memory]:
    """Apply scope / excluded-scope / repo / worktree filters.

    Extracted from `search()` so each search mode walks the same
    pre-filtered candidate list — fairness across rankers requires it,
    and it makes the per-mode scorers obviously equivalent on the
    filtering side. Order of `memories` is preserved.
    """
    scope_filter = set(scopes) if scopes else None
    excluded = excluded_scopes or set()
    return [
        memory
        for memory in memories
        if candidate_admitted(
            memory.scopes,
            memory.origin,
            scope_filter=scope_filter,
            excluded=excluded,
            repo_filter=repo_filter,
            worktree_filter=worktree_filter,
        )
    ]


def candidate_admitted(
    memory_scopes: list[str],
    memory_origin: Origin | None,
    *,
    scope_filter: set[str] | None,
    excluded: set[str],
    repo_filter: str | None,
    worktree_filter: str | None,
) -> bool:
    """Does one memory survive the search filters?

    Split out of `_filter_candidates` so the BM25 corpus-statistics path
    can decide admission from an INDEX row — id, scopes, origin — without
    parsing a body. Both callers therefore run the identical predicate:
    the IDF denominator is provably the same collection that gets ranked,
    rather than a SQL approximation of it kept in sync by hand.

    That parity is not optional here. `repos_match` compares on
    `(host, owner, name)` and additionally consults per-process alternate
    spellings registered by `origin.capture`, so the admission rule is not
    expressible in SQL at all — any index-side filter would silently
    disagree with the ranked set for exactly the multi-remote stores the
    alternates mechanism exists to serve.
    """
    memory_scope_set = set(memory_scopes)
    if excluded and (memory_scope_set & excluded):
        return False
    if scope_filter is not None and not (memory_scope_set & scope_filter):
        return False
    if repo_filter is not None and not should_include_for_caller(
        memory_origin,
        repo_filter,
        caller_worktree_root=worktree_filter,
    ):
        return False
    return True


# How much left context a mid-body snippet window keeps in front of its
# anchor, and — the same number, deliberately — the offset at or under
# which we serve the plain head instead. A match that close to the top is
# already inside the head window, so a leading "..." over the first few
# words would be pure noise.
_SNIPPET_LEAD_CHARS = 40

# Hard bound on how far into a body the anchor scan walks. The scan is one
# `_TOKEN_RE` pass plus one uncached `tokenize` per raw token — cheap per
# token, but linear in body length and paid once per RETURNED hit, so a
# pathological body degrades to head-of-body past this point rather than
# to a stall.
_SNIPPET_SCAN_CHARS = 8000


def _query_biased_snippet(body: str, matched: list[str], max_chars: int = 200) -> str:
    """`snippet_for`, but windowed on where the query actually hit.

    Head-of-body truncation answers "what does this memory start with",
    not "why did this memory come back": on a long body the matched terms
    routinely sit past character 200 and the caller is shown an unrelated
    opening paragraph. This walks the RAW body with `_TOKEN_RE` and
    normalises each raw token INDIVIDUALLY — `_expand_kebab(tokenize(tok))`,
    exactly the per-token slice of the `_MemoryTokens.body` stream the
    scorers count against, so membership in it is the same predicate that
    put the term in `matched`. Per-token and not whole-body because the
    normalised stream's offsets do not map back: NFKC, `.lower()` and
    `_fold_diacritics` are all length-changing, which is why the folded
    text cannot be used to locate anything in the raw one.

    Only `_build_hit` calls this. `snippet_for` keeps its head-of-body
    contract for every other consumer — write-time dedup's `SimilarHit`,
    consolidate's summaries — none of which have a query to bias toward.

    Falls back to `snippet_for` by DELEGATING to it, never by re-deriving
    it, whenever biasing is impossible or pointless: short body, no
    matched terms, no anchor found in the body, or an anchor already
    inside the head window.
    """
    text = body.strip()
    # This must stay the FIRST statement. `tokenize` is uncached, so the
    # scan below costs one `_tokenize_impl` call per raw body token, and
    # `test_search_tokenizes_each_candidate_once` pins the per-search
    # call count. Its fixtures are 46-char bodies, so they exit here and
    # the count is unchanged; hoisting the scan above this line breaks
    # that test — and, more to the point, would spend the calls on every
    # hit whose body already fits whole.
    #
    # The `not matched` half covers the two populations that legitimately
    # carry no literal terms: browse mode (`_build_hit(..., matched=[])`)
    # and (historically) paraphrase-only semantic hits, whose `literal_matched` is empty
    # by design.
    if len(text) <= max_chars or not matched:
        return snippet_for(text, max_chars)

    # Tier 1 is the matched term itself. Tier 2 is its kebab components:
    # the scorers' conjunctive fallback marks 'claude-code' matched when
    # the body spells it 'Claude Code', and no raw token there normalises
    # to the compound. Tier 2 is consulted only when tier 1 found nothing,
    # so a compound that DID hit literally is never dragged off to a bare
    # 'python' mention elsewhere — the precision `_expand_kebab`'s
    # one-directional widening exists to protect.
    primary_terms = set(matched)
    part_terms = {p for tok in matched for p in _kebab_parts(tok)} - primary_terms

    scan = text[:_SNIPPET_SCAN_CHARS]
    starts: list[int] = []
    primary: list[int] = []
    secondary: list[int] = []
    for m in _TOKEN_RE.finditer(scan):
        starts.append(m.start())
        surfaces = set(_expand_kebab(tokenize(m.group())))
        if surfaces & primary_terms:
            primary.append(m.start())
        elif surfaces & part_terms:
            secondary.append(m.start())

    # Symbol-aliased terms are invisible to the token scan above and can
    # only be found by re-running their own patterns — see
    # `_ALIAS_ANCHOR_PATTERNS`.
    for pattern, alias in _ALIAS_ANCHOR_PATTERNS:
        if alias in primary_terms:
            bucket = primary
        elif alias in part_terms:
            bucket = secondary
        else:
            continue
        for m in pattern.finditer(scan):
            bucket.append(m.start())
            starts.append(m.start())

    anchors = sorted(primary) or sorted(secondary)
    if not anchors:
        # Every matched term hit a SCOPE rather than the body (the
        # scorers' `scope_hit` term), or the body carries decomposed
        # combining marks that split a raw token where the folded stream
        # does not, or every occurrence lies past `_SNIPPET_SCAN_CHARS`.
        return snippet_for(text, max_chars)

    # Densest window wins. The rendered window opens `_SNIPPET_LEAD_CHARS`
    # BEFORE its anchor, so counting against `a + budget` would promise
    # anchors the window never reaches — count against
    # `a + budget - lead`, the span actually shown. `>` (not `>=`) keeps
    # the EARLIEST window on a tie, so a body of uniform density reads
    # exactly as it did before this existed.
    budget = max_chars - 3
    reach = budget - _SNIPPET_LEAD_CHARS
    starts = sorted(set(starts))
    best_at, best_cover = anchors[0], -1
    for i, anchor in enumerate(anchors):
        cover = bisect_left(anchors, anchor + reach) - i
        if cover > best_cover:
            best_cover, best_at = cover, anchor

    if best_at <= _SNIPPET_LEAD_CHARS:
        # Already inside the head window — serve it unchanged, with no
        # leading ellipsis. This is what keeps every head-anchored hit
        # byte-identical to what it was before.
        return snippet_for(text, max_chars)

    # Snap the window start to a token start so it never opens mid-word;
    # `_truncate_at_word` only ever guarded the trailing edge. The lookup
    # always lands, so there is no "nothing to snap to" case to guard:
    # `best_at` is itself in `starts` — the token loop and the alias loop
    # both feed every anchor position into it — and `lead <= best_at`, so
    # the first start at or after `lead` is at or before `best_at`.
    lead = best_at - _SNIPPET_LEAD_CHARS
    start = starts[bisect_left(starts, lead)]
    return snippet_window(text, start, max_chars)


def _build_hit(
    memory: Memory,
    score: float,
    matched: list[str],
    *,
    query_unique: int,
) -> MemoryHit:
    """Construct a MemoryHit from a scored memory.

    `detect_path_drift` is the only call here that touches the
    filesystem — one regex pass + up to 8 stat() calls per hit. The
    body's already in memory at this point (load_all already ran), so
    the marginal cost is bounded by the cap inside `detect_path_drift`
    rather than corpus size.

    `_query_biased_snippet` adds a second regex pass over the body plus
    one uncached `tokenize` per raw token — but only for bodies longer
    than the snippet budget, and bounded by `_SNIPPET_SCAN_CHARS`. Both
    passes are per-HIT, not per-candidate: `_build_hit` runs at most
    `max_results` times (default 5), after the ranking has been trimmed.
    """
    drift = detect_path_drift(
        memory.body,
        verified_paths=memory.verified_paths,
        absent_paths=memory.verified_absent_paths,
        worktree_root=memory.origin.worktree_root if memory.origin else None,
    )
    return MemoryHit(
        id=memory.id,
        scopes=memory.scopes,
        confidence=memory.confidence,
        category=memory.category,
        snippet=_query_biased_snippet(memory.body, matched),
        score=round(score, 4),
        relevance=_relevance_label(len(matched), query_unique),
        match_terms=matched,
        query_unique=query_unique,
        created=memory.created,
        updated=memory.updated,
        last_verified_at=memory.last_verified_at,
        path_drift_checked=len(drift.checked),
        path_drift_missing=len(drift.missing),
        path_drift_checked_paths=list(drift.checked),
        path_drift_missing_paths=list(drift.missing),
        path_drift_verified_paths=list(drift.verified),
        path_drift_expected_absent_paths=list(drift.expected_absent),
        path_drift_dropped_as_route_paths=list(drift.dropped_as_route),
        # The counts and the full path list stay full-set — every
        # absence the caller could see before is still on the hit. This
        # is the provenance subset the verdict escalates on, and it has
        # to ride the hit because the response builder is where the
        # verdict is computed and a bare path string carries no trace of
        # where it came from.
        path_drift_claim_anchored_missing_paths=list(drift.claim_anchored_missing),
    )


def _score_keyword(
    candidates: list[Memory],
    query_tokens: list[str],
    *,
    now: datetime,
    half_life_days: float,
    applied_by_id: dict[str, int] | None = None,
    negative_by_id: dict[str, tuple[int, int]] | None = None,
    corroboration_boost: bool = False,
    candidate_tokens: list[_MemoryTokens] | None = None,
    scaffold_terms: frozenset[str] | None = None,
    scaffold_weight: float = 0.0,
) -> list[tuple[Memory, float, list[str]]]:
    """Run the original keyword scorer across all candidates. Returns
    `(memory, score, matched)` tuples for every candidate with `score > 0`.
    Order preserved from the input — sorting happens at the caller.

    `scaffold_terms` / `scaffold_weight` thread through to
    `score_memory` (L2, lane-internal — see there). None is the stock
    scorer.

    `applied_by_id` (optional) maps memory id → explicit-applied count; when
    given, a bounded `_endorsement_factor` nudges endorsed memories. None
    (the default) leaves scores untouched.

    `negative_by_id` (optional) maps memory id → (active-ignored,
    active-contradicted) counts; when given, a bounded `_demotion_factor`
    slides recently-rejected memories down. None (the default) leaves
    scores untouched.

    `candidate_tokens` (optional): precomputed `_MemoryTokens`,
    index-aligned with `candidates` — see `search()`."""
    out: list[tuple[Memory, float, list[str]]] = []
    for i, memory in enumerate(candidates):
        score, matched = score_memory(
            memory,
            query_tokens,
            now=now,
            half_life_days=half_life_days,
            tokens=candidate_tokens[i] if candidate_tokens is not None else None,
            scaffold_terms=scaffold_terms,
            scaffold_weight=scaffold_weight,
        )
        if score > 0:
            if applied_by_id:
                score *= _endorsement_factor(applied_by_id.get(memory.id, 0))
            if negative_by_id:
                ig, ct = negative_by_id.get(memory.id, (0, 0))
                score *= _demotion_factor(ig, ct)
            if corroboration_boost and memory.corroborations:
                score *= _corroboration_factor(memory.corroborations)
            out.append((memory, score, matched))
    return out


def _score_bm25(
    candidates: list[Memory],
    query_tokens: list[str],
    *,
    now: datetime,
    half_life_days: float,
    applied_by_id: dict[str, int] | None = None,
    negative_by_id: dict[str, tuple[int, int]] | None = None,
    corroboration_boost: bool = False,
    candidate_tokens: list[_MemoryTokens] | None = None,
    stopword_fallback: bool = False,
    corpus_stats: CorpusStats | None = None,
) -> list[tuple[Memory, float, list[str]]]:
    """Run the BM25 scorer across all candidates. Returns
    `(memory, score, matched)` tuples for candidates with `score > 0`.
    `applied_by_id` / `negative_by_id` / `candidate_tokens`: see
    `_score_keyword`; `stopword_fallback`: see `score_memory_bm25`;
    `corpus_stats`: see `compute_idf` — required for a correct ranking
    whenever `candidates` is a query-filtered subset rather than the
    whole corpus."""
    body_idf_map, scope_idf_map, avgdl = compute_idf(
        candidates, tokens=candidate_tokens, corpus_stats=corpus_stats
    )
    if avgdl <= 0:
        return []
    out: list[tuple[Memory, float, list[str]]] = []
    for i, memory in enumerate(candidates):
        score, matched = score_memory_bm25(
            memory,
            query_tokens,
            body_idf_map=body_idf_map,
            scope_idf_map=scope_idf_map,
            avgdl=avgdl,
            now=now,
            half_life_days=half_life_days,
            tokens=candidate_tokens[i] if candidate_tokens is not None else None,
            stopword_fallback=stopword_fallback,
        )
        if score > 0:
            if applied_by_id:
                score *= _endorsement_factor(applied_by_id.get(memory.id, 0))
            if negative_by_id:
                ig, ct = negative_by_id.get(memory.id, (0, 0))
                score *= _demotion_factor(ig, ct)
            if corroboration_boost and memory.corroborations:
                score *= _corroboration_factor(memory.corroborations)
            out.append((memory, score, matched))
    return out


def _id_order(
    scored: list[tuple[Memory, float, list[str]]],
) -> list[str]:
    """Return memory ids sorted desc by score, with (created, id) tiebreakers.
    Matches the existing search() sort key so single-mode RRF degenerates
    to the same order as direct scoring would produce."""
    scored_sorted = sorted(
        scored,
        key=lambda x: (x[1], x[0].created, x[0].id),
        reverse=True,
    )
    return [memory.id for memory, _, _ in scored_sorted]


def _leg_top_evidence(scored: list[tuple[Memory, float, list[str]]]) -> int:
    """How many synthesized terms the leg's rank-1 candidate matched.

    Read off the leg's own fusion ordering — the same
    `(score, created, id)` ordering `_id_order` applies — so the
    candidate judged is the one that would have voted.
    """
    if not scored:
        return 0
    top = max(scored, key=lambda x: (x[1], x[0].created, x[0].id))
    return len(top[2])


def _leg_evidence_weight(evidence: int) -> float:
    """The rescue leg's RRF weight, scaled by its own evidence.

    Returns 0.0 below `_RESCUE_LEG_MIN_EVIDENCE` — the round-5 rule,
    unchanged: a leg resting on one matched term is a coincidence and
    votes nothing.

    Above the floor the SHIPPED form is flat, the full
    `_RESCUE_LEG_WEIGHT`, because that is what measures best for the
    audience that turns this lane on — see `_RESCUE_LEG_EVIDENCE_SCALING`
    for the arms and the reasoning. With scaling enabled the weight is
    instead the fraction of the full-evidence bar the leg's own evidence
    reaches, `evidence / _EVIDENCE_FULL_AT`, capped at 1. Either way the
    result is within [0, `_RESCUE_LEG_WEIGHT`].

    Round 6 used `(evidence - 1) / (_EVIDENCE_FULL_AT - 1)` instead. The
    offset existed only to map the floor to exactly half weight, which
    was a choice rather than a derivation, and it is the choice that
    cost the dev set two questions at recall@5 while the mechanism
    itself was working. The plain ratio has no offset to justify and
    introduces no constant.

    The forms differ only at the floor: 2/3 of full weight here against
    1/2 there. The round-5 labels independently measure that stratum at
    68.2% helpful, which the structural 66.7% corroborates to 1.5
    points — two numbers from different places, neither derived from
    the other. See addendum 10.
    """
    if evidence < _RESCUE_LEG_MIN_EVIDENCE:
        return 0.0
    if not _RESCUE_LEG_EVIDENCE_SCALING:
        return _RESCUE_LEG_WEIGHT
    scale = min(1.0, evidence / max(1, _EVIDENCE_FULL_AT))
    return _RESCUE_LEG_WEIGHT * scale


def _base_leg_weights(
    rankings: list[list[tuple[Memory, float, list[str]]]],
) -> list[float] | None:
    """Fusion weights for the base pair — None for the shipped flat 1.0s.

    With `_BASE_LEG_TRAILING_WITHHOLD` on and one base leg's rank-1
    matching strictly fewer query terms than the other's, the trailing
    leg is withheld (weight 0.0) and the leading leg keeps its full
    vote. Ties — the overwhelmingly common case — return None, so the
    fusion is byte-identical to the pre-round-9 engine.

    The withheld leg's ranking stays IN the fusion at weight zero (its
    unique candidates keep tail positions rather than vanishing) — the
    exact counterfactual the base-leg census labelled, so its labels
    transfer 1:1. See `_BASE_LEG_TRAILING_WITHHOLD` for the derivation
    and addendum 12 for the preregistration.
    """
    if not _BASE_LEG_TRAILING_WITHHOLD or len(rankings) != 2:
        return None
    m_a = _leg_top_evidence(rankings[0])
    m_b = _leg_top_evidence(rankings[1])
    if m_a == m_b:
        return None
    return [1.0, 0.0] if m_a > m_b else [0.0, 1.0]


def _hybrid_fuse(
    rankings: list[list[tuple[Memory, float, list[str]]]],
    *,
    rrf_k: int,
    weights: list[float] | None = None,
) -> list[tuple[Memory, float, list[str]]]:
    """Fuse multiple ranker outputs into one ranked list via RRF.

    Each input is a per-ranker `[(memory, score, matched), ...]` list.
    Output is `[(memory, rrf_score, matched_union), ...]` ordered desc
    by RRF score. `matched_union` is the union of matched terms across
    rankers that surfaced the memory, sorted for stability. `weights`
    (optional, index-aligned) passes through to
    `reciprocal_rank_fusion`; None is byte-identical to before the
    parameter existed.
    """
    if not rankings:
        return []

    by_id: dict[str, Memory] = {}
    matched_by_id: dict[str, set[str]] = {}
    ranking_id_lists: list[list[str]] = []
    for scored in rankings:
        ranking_id_lists.append(_id_order(scored))
        for memory, _, matched in scored:
            by_id.setdefault(memory.id, memory)
            matched_by_id.setdefault(memory.id, set()).update(matched)

    fused = reciprocal_rank_fusion(ranking_id_lists, k=rrf_k, weights=weights)
    if not fused:
        return []

    # Tiebreaker: equal RRF scores fall back to (created, id) desc, same
    # as single-mode search — preserves deterministic ordering under
    # microsecond-tied writes / mocked clocks.
    ordered_ids = sorted(
        fused.keys(),
        key=lambda mid: (fused[mid], by_id[mid].created, mid),
        reverse=True,
    )
    return [(by_id[mid], fused[mid], sorted(matched_by_id[mid])) for mid in ordered_ids]


def search(
    memories: list[Memory],
    query: str,
    *,
    scopes: list[str] | None = None,
    excluded_scopes: set[str] | None = None,
    repo_filter: str | None = None,
    worktree_filter: str | None = None,
    max_results: int = 5,
    now: datetime | None = None,
    half_life_days: float = 30.0,
    mode: SearchMode = "hybrid",
    rrf_k: int = _RRF_K_DEFAULT,
    applied_by_id: dict[str, int] | None = None,
    negative_by_id: dict[str, tuple[int, int]] | None = None,
    corroboration_boost: bool = False,
    allow_empty_query: bool = False,
    corpus_stats_provider: Callable[[list[str]], CorpusStats | None] | None = None,
    matched_leg_out: dict[str, str] | None = None,
    rescue_expansion: bool = False,
    conversational: bool = True,
) -> list[MemoryHit]:
    """Rank `memories` against `query` and return up to `max_results` hits.

    - `scopes`: if given, only consider memories tagged with at least one.
    - `excluded_scopes`: any memory tagged with one of these is dropped.
      (Used for session-disabled scopes.)
    - `repo_filter`: a remote URL. When provided, memories whose
      `origin.repo` doesn't match (compared via `origin.repos_match`) are
      dropped. Memories with no `origin.repo` (legacy or non-repo writes)
      pass through — they're treated as global.
    - `worktree_filter`: the caller's `git rev-parse --show-toplevel`
      path. Layered on top of `repo_filter` to catch worktree leakage:
      a memory written from one worktree of a repo shouldn't surface
      in a search run from a sibling worktree of the same repo.
      Memories with no `worktree_root` (legacy or non-repo writes)
      pass through. No-op without `repo_filter` — a worktree path
      without a repo identifier doesn't carry enough context to
      filter on.
    - `mode`: ranker selection. `"hybrid"` (default since 2.6.8: RRF
      fusion of keyword + BM25); `"keyword"` (legacy TF + coverage +
      recency scorer with no IDF weighting); `"bm25"` (Okapi BM25 with
      the same scope-bonus + recency boost).
    - `rrf_k`: smoothing constant for hybrid fusion. Larger spreads
      weight further down the list; smaller makes top ranks dominate.
      60 is the canonical default and almost always correct.
    - `applied_by_id`: optional map of memory id → explicit-applied count.
      When given, a bounded `_endorsement_factor` (≤ +10%, same ceiling as
      recency) nudges endorsed memories up — a near-tie breaker, never a
      relevance override. `None` (the default) leaves scores untouched, so
      every existing caller and the package default are byte-stable.
    - `negative_by_id`: optional map of memory id → (active-ignored,
      active-contradicted) counts. When given, a bounded
      `_demotion_factor` (≥ 0.85x) slides recently-rejected memories
      down — the negative mirror of `applied_by_id`, with the same
      near-tie-only ceiling. The caller owns the "active" semantics
      (windowing, applied-supersedes, resolution clearing — see
      `handlers.search._active_negative_counts`); this layer just
      applies the factor. `None` (the default) is byte-stable.
    - `corroboration_boost`: when True, a bounded `_corroboration_factor`
      (≤ +10%) nudges memories whose persisted `corroborations` rollup is
      non-zero — claims that keep independently re-entering conversations
      win near-ties over one-off remarks. Reads the Memory record
      directly (no event walk). False (the default) is byte-stable.
    - `allow_empty_query`: when True, an empty or stopword-only query
      no longer short-circuits to `[]`. Instead the function runs the
      `_filter_candidates` pass (scope / repo / worktree / excluded)
      and returns the survivors sorted by `updated` desc — a browse
      mode. Hits get `score=0.0`, no `match_terms`, and the default
      "low" relevance label. Used by callers that already narrowed
      the candidate pool externally (e.g. `since_prior_session=True`)
      and want recency ordering rather than relevance ranking. When
      False (the default), only a truly EMPTY query returns `[]`; a
      stopword-only query is ranked on its unstripped tokens instead
      (see the fallback note in the body), so stopword curation can
      never make a non-empty query unanswerable.
    - `matched_leg_out`: optional dict the function fills with
      `{memory_id: "lexical" | "expansion"}` for the hits it
      returns — WHICH RANKER surfaced each one. An out-parameter rather
      than a `MemoryHit` field because the leg is a property of THIS
      CALL's ranker configuration, not of the memory, and every other
      consumer of `MemoryHit` (memory_show, memory_list, the detail
      surfaces) has no legs to report; the MCP search handler is the one
      surface where it is actionable. `None` (the default) skips the
      bookkeeping entirely, so every existing caller is byte-stable in
      both output and cost. Browse-mode hits get no entry — nothing
      ranked them.
    - `rescue_expansion`: hybrid-mode only, DEFAULT OFF. When True,
      two query-time repairs from the retrieval campaign run:
      (a) listed discourse-filler words (`expansion.QUERY_FILLER_WORDS`)
      get a document-frequency FLOOR in the BM25 legs, so corpus-rare
      conversational filler can't outprice real content terms; and
      (b) when the fused base ranking's top hit covers less than
      `_RESCUE_COVERAGE_GATE` of the query's unique tokens, one extra
      BM25 leg over SYNTHESIZED vocabulary (inflection variants,
      clipping full-forms, synonym group mates — `expansion.py`) joins
      the fusion at `_RESCUE_LEG_WEIGHT`. Hits surfaced only by that
      leg report `matched_leg="expansion"` with `match_terms` still an
      honest subset of the caller's own tokens (possibly empty — the
      same shape pure-paraphrase hits always had). False (the default)
      is the pre-5.1 two-leg behavior byte for byte.

      Why the default is off: the lane's own preregistered held-out
      check killed default-on. On the technical-prose gold set the
      repairs are worth +15/+30 recall@1/@5 as-asked
      (bench/retrieval, 2026-08-09 artifacts); on LongMemEval's
      conversational stores the same mechanisms cost 1.65 macro@5
      points — inflection variants of common chat verbs are
      promiscuous matchers there, the inverse of the technical corpus
      where expansion vocabulary is rare and discriminating. Kill
      criterion and ablations: bench/longmemeval/PREREGISTRATION.md
      addendum 3 and its README. Flipping the default back on is
      earned by a fresh preregistration on both instruments, not by an
      operator's hunch — but `[behavior] rescue_expansion = true` is a
      supported, documented choice for stores that look like the gold
      set (technical prose, casual queries).

      `keyword` and `bm25` modes are explicit instrument choices and
      are never touched. Above the index threshold the FTS prefilter
      nominates the pool from the CALLER's tokens, so there the rescue
      re-ranks the nominated pool rather than widening it — a
      documented limit, not a silent one.
    - `conversational`: hybrid-mode only, DEFAULT ON since 6.1.0 (the
      L1 ship, owner decision 2026-08-16). The Lane L repairs for
      conversation-shaped stores (`bench/l/L1_DECLARATION.md`): when
      the query has a temporal reading, (a) temporal-SCAFFOLD tokens
      (day/week/ago/last/many and kin — the question's syntax, not
      its content) get a document-frequency floor in the BM25 legs
      through the same stats seam as the filler floor, so they cannot
      outprice content terms; and (b) date anchors break lookalike
      ties inside the fused ranking's near-tie band — an explicit
      window (a named month, a date, "last month" resolved against
      `now`) boosts in-window anchors, and boosts only: the demote
      and the earliest/latest selector were measured net-negative at
      tuning and are dead (the record has the join). A query with no
      temporal reading is untouched byte for byte, as is every call
      with the flag off, so `conversational=False` reproduces the
      pre-6.1.0 ranking exactly.

      Why the default is on: the unit's gate read
      (`bench/l/L1_RECORD.md`, artifacts beside it) measured +1.27
      LongMemEval macro-recall@5 points and +0.93 at @1 on the full
      500 against the paired off arm, with the dev instrument
      byte-identical, no question type regressed, and the untouched
      holdout half generalizing stronger than the tuning half. The
      reference line (0.916) is NOT met — the shipped reading is
      0.9062, stated plainly in the record. `[behavior]
      conversational = false` is the supported opt-out; the module
      constants carry the tuned finals the gate ranked with.

    Score semantics vary by mode: keyword/BM25 scores live on
    different scales and are not comparable across modes. Hybrid scores
    are RRF outputs (~0.01-0.05 range, summed `1/(k+rank)` over rankers).
    Comparing hits across modes on the raw score is meaningless; compare
    on `relevance` plus `matched_leg`, which together say how much of the
    query the hit literally contains AND which ranker found it —
    `relevance` alone answers only the first, and answers it "low" for
    every pure-paraphrase hit.
    """
    # Runtime guard against unknown modes. The `SearchMode` Literal pins
    # this at the type-checker layer, but the handler accepts an opaque
    # string from MCP and Python doesn't enforce Literals at call time;
    # without this check, a typo like `mode="emantic"` would fall through
    # the if/elif chain into the `else` branch and silently run hybrid.
    # Raising here makes the failure mode loud at the dispatch boundary
    # regardless of where the bad string came from (handler, CLI, future
    # programmatic client).
    if mode not in ("keyword", "bm25", "hybrid"):
        raise ValueError(
            f"unknown search mode {mode!r}; must be one of: keyword, bm25, hybrid"
        )

    now = now or datetime.now(timezone.utc)
    raw_tokens = tokenize(query)
    # Strip stopwords from the query — bodies stay unfiltered. If stripping
    # EMPTIES a non-empty token list ("what is the" — or a lone term some
    # future stopword addition absorbs), fall back to the unstripped
    # tokens: stopword curation must never make a real query unanswerable,
    # so the worst case is filler-grade ranking, not silent zero recall.
    # The fallback flag threads into the BM25 legs below: BM25 counts TF
    # against the stopword-STRIPPED body stream, so without the signal a
    # fallen-back token could match scopes but never a body — silent zero
    # recall in mode="bm25", exactly what this fallback exists to rule
    # out (the keyword scorer's stream keeps stopwords and needs no flag).
    # A truly empty query still returns empty rather than serving every
    # memory at score 0, and browse mode keeps its recency-ordered
    # semantics for both empty and stopword-only queries — see
    # `allow_empty_query` above.
    query_tokens = _strip_stopwords(raw_tokens)
    stopword_fallback = False
    if not query_tokens and raw_tokens and not allow_empty_query:
        query_tokens = raw_tokens
        stopword_fallback = True
    if not query_tokens:
        if not allow_empty_query:
            return []
        # Browse mode: apply the same candidate filter the scored
        # path would, then sort by `updated` desc and emit zero-score
        # hits with no match terms. Mirrors the post-rank trim the
        # scored branches do at the end of `search()`.
        browse_candidates = _filter_candidates(
            memories,
            scopes=scopes,
            excluded_scopes=excluded_scopes,
            repo_filter=repo_filter,
            worktree_filter=worktree_filter,
        )
        browse_candidates.sort(key=lambda m: (m.updated, m.id), reverse=True)
        return [
            _build_hit(memory, score=0.0, matched=[], query_unique=0)
            for memory in browse_candidates[:max_results]
        ]

    query_unique = len(set(query_tokens))
    candidates = _filter_candidates(
        memories,
        scopes=scopes,
        excluded_scopes=excluded_scopes,
        repo_filter=repo_filter,
        worktree_filter=worktree_filter,
    )
    if not candidates:
        return []

    # `matched_leg` bookkeeping. Populated only when the caller asked for
    # it, so the default path pays nothing; the sets are the ids each leg
    # actually SCORED, which is what makes the reported leg a statement
    # about the run rather than about `mode`. `expansion_ids` is the one
    # exception to the only-when-asked rule: the rescue block fills it
    # while it has the leg in hand (a set build over an already-scored
    # list), because re-deriving it at the trim would mean re-running
    # the leg.
    lexical_ids: set[str] = set()
    expansion_ids: set[str] = set()

    # Tokenize each candidate exactly once per call and thread the streams
    # through every consumer below — the keyword scorer, compute_idf, BM25,
    # blocks otherwise re-tokenize the same
    # bodies and scopes (6 tokenize calls per memory per hybrid search,
    # ~88% of cumulative search time). Pure perf: see `_MemoryTokens`.
    candidate_tokens = [_memory_tokens(m) for m in candidates]

    # Corpus-wide document frequencies for the BM25 rankers, resolved from
    # the SAME terms the scorer will look up: every query token plus the
    # `_kebab_parts` components of each joined one. The conjunctive
    # fallback prices a joined token with no direct hit off its parts (min
    # component IDF), and `compute_idf`'s override only re-prices fetched
    # terms — a whole-tokens-only fetch would leave those parts at the
    # pool-collapsed IDF the provider exists to correct. That parity is
    # the whole reason this is a provider rather than a precomputed value:
    # the caller knowing which terms to fetch would mean re-deriving this
    # tokenisation outside `search()`, and a hand-mirrored token pipeline is
    # exactly the drift schema v4 removed. Only the BM25 branches consume
    # it, so a keyword-only search never pays the lookup.
    # Lane L: parse the query's temporal shape once. Hybrid-only, like
    # the rescue lane, and skipped on the stopword fallback for the same
    # reason (the fallback's TF stream has different matched semantics).
    # A non-temporal query leaves `conv_reading` None and every path
    # below byte-identical to the flag being off.
    conv_reading: _TemporalReading | None = None
    if conversational and mode == "hybrid" and not stopword_fallback:
        reading = _temporal_reading(query, now)
        if reading.is_temporal:
            conv_reading = reading
    # L2: the PRICING gate — the repricing mechanisms' key, wider than
    # the temporal reading when the widening constant is set. The
    # window/selector rerank below stays keyed on `conv_reading` alone:
    # a scaffold-shaped query has no window to boost. With
    # `_CONV_SCAFFOLD_MIN_STEMS` None the predicate is constant-False
    # and `conv_pricing == (conv_reading is not None)` — L1's key.
    conv_pricing = conv_reading is not None
    if (
        not conv_pricing
        and conversational
        and mode == "hybrid"
        and not stopword_fallback
    ):
        conv_pricing = _conv_scaffold_shaped(query_tokens)

    corpus_stats: CorpusStats | None = None
    if corpus_stats_provider is not None and mode in ("bm25", "hybrid"):
        fetch_terms = list(query_tokens)
        for tok in query_tokens:
            fetch_terms.extend(_kebab_parts(tok))
        corpus_stats = corpus_stats_provider(list(dict.fromkeys(fetch_terms)))

    if mode == "keyword":
        scored = _score_keyword(
            candidates,
            query_tokens,
            now=now,
            half_life_days=half_life_days,
            applied_by_id=applied_by_id,
            negative_by_id=negative_by_id,
            corroboration_boost=corroboration_boost,
            candidate_tokens=candidate_tokens,
        )
        # Sort by score, then created (newer wins on tie), then id as the
        # final discriminator. Without `id` the tiebreaker is undefined for
        # two memories that share both score and created timestamp — a real
        # case under microsecond-tied writes or under tests that mock the
        # clock. ULID-shaped ids are lexically time-ordered, so the final
        # tiebreaker also gives "newer wins" semantics.
        scored.sort(key=lambda x: (x[1], x[0].created, x[0].id), reverse=True)
        if matched_leg_out is not None:
            lexical_ids = {memory.id for memory, _, _ in scored}
    elif mode == "bm25":
        scored = _score_bm25(
            candidates,
            query_tokens,
            now=now,
            half_life_days=half_life_days,
            applied_by_id=applied_by_id,
            negative_by_id=negative_by_id,
            corroboration_boost=corroboration_boost,
            candidate_tokens=candidate_tokens,
            stopword_fallback=stopword_fallback,
            corpus_stats=corpus_stats,
        )
        scored.sort(key=lambda x: (x[1], x[0].created, x[0].id), reverse=True)
        if matched_leg_out is not None:
            lexical_ids = {memory.id for memory, _, _ in scored}
    else:  # mode == "hybrid"
        # The filler df-floor applies to the BM25 legs of the default
        # hybrid path only — `bm25` and `keyword` modes are explicit
        # instrument choices and stay pure. With `rescue_expansion`
        # off, the raw stats pass through and the branch is
        # byte-identical to pre-5.1.
        hybrid_stats = (
            _filler_floor_stats(corpus_stats, query_tokens, len(candidates))
            if rescue_expansion
            else corpus_stats
        )
        # Lane L's scaffold floor composes AFTER the filler floor: both
        # only ever raise a term's df, they touch disjoint classes, and
        # the pricing gate is required — no priced reading, no
        # repricing. (L1 keyed this on the temporal reading; L2's gate
        # subsumes it and is identical while the widening ships dark.)
        if conv_pricing:
            hybrid_stats = _scaffold_floor_stats(
                hybrid_stats, query_tokens, len(candidates)
            )
        # L2: the keyword leg's scaffold repricing, the fusion's other
        # half. Engaged only for a priced query with at least one
        # content term (an all-scaffold temporal query leaves the leg
        # stock — the shipped behavior), and only when the weight
        # constant is set; None is byte-identical to 6.1.0.
        conv_scaffold: frozenset[str] | None = None
        if conv_pricing and _CONV_KEYWORD_SCAFFOLD_WEIGHT is not None:
            distinct_q = list(dict.fromkeys(query_tokens))
            scaffold_q = _conv_scaffold_terms(distinct_q)
            if scaffold_q and len(scaffold_q) < len(distinct_q):
                conv_scaffold = frozenset(scaffold_q)
        rankings: list[list[tuple[Memory, float, list[str]]]] = [
            _score_keyword(
                candidates,
                query_tokens,
                now=now,
                half_life_days=half_life_days,
                applied_by_id=applied_by_id,
                negative_by_id=negative_by_id,
                corroboration_boost=corroboration_boost,
                candidate_tokens=candidate_tokens,
                scaffold_terms=conv_scaffold,
                scaffold_weight=_CONV_KEYWORD_SCAFFOLD_WEIGHT or 0.0,
            ),
            _score_bm25(
                candidates,
                query_tokens,
                now=now,
                half_life_days=half_life_days,
                applied_by_id=applied_by_id,
                negative_by_id=negative_by_id,
                corroboration_boost=corroboration_boost,
                candidate_tokens=candidate_tokens,
                stopword_fallback=stopword_fallback,
                corpus_stats=hybrid_stats,
            ),
        ]
        # Snapshot the leg membership from the working list by name,
        # not position, so a future ranker joining `rankings` can't
        # silently change what "lexical" means here — the rescue leg
        # below joins the FUSION but deliberately never this snapshot.
        if matched_leg_out is not None:
            lexical_ids = {
                memory.id for ranking in rankings for memory, _, _ in ranking
            }
        # Round 9: a base leg whose rank-1 evidence trails its peer's is
        # withheld. Skipped on the stopword fallback exactly as the
        # rescue leg is — the fallback's TF stream has different matched
        # semantics. None (every tie, and the shipped default) fuses
        # byte-identically.
        base_weights = None if stopword_fallback else _base_leg_weights(rankings)
        scored = _hybrid_fuse(rankings, rrf_k=rrf_k, weights=base_weights)

        # Rescue expansion: one extra, down-weighted BM25 leg over
        # synthesized vocabulary, engaged only when the base fusion is
        # not confident about its own top hit. Skipped on the stopword
        # fallback (an all-filler query has no content tokens worth
        # expanding, and the fallback already runs a special TF stream
        # the expansion leg does not share).
        if rescue_expansion and not stopword_fallback:
            if scored:
                top_matched = set(scored[0][2]) & set(query_tokens)
                coverage = len(top_matched) / query_unique if query_unique else 0.0
            else:
                coverage = 0.0
            if coverage < _RESCUE_COVERAGE_GATE:
                exp_terms = _expansion_terms_impl(
                    list(dict.fromkeys(query_tokens)),
                    _EXPANSION_TABLES,
                    _stem_token,
                )
                if exp_terms:
                    exp_stats = hybrid_stats
                    if corpus_stats_provider is not None:
                        # Above the threshold the floored query-term
                        # stats say nothing about the synthesized
                        # terms; fetch those too so a store-rare
                        # synonym prices off the store, not the slice.
                        #
                        # Same terms-to-fetch rule as the base fetch
                        # above, and for the same reason: a synthesized
                        # term can itself be joined — `morph_variants`
                        # rewrites the tail of a kebab token, so
                        # "split-testing" emits "split-test" — and
                        # `_score_bm25`'s conjunctive fallback prices a
                        # joined term with no direct hit off its parts.
                        # Fetching whole terms only would leave those
                        # parts at the pool-collapsed IDF the provider
                        # exists to correct.
                        exp_fetch = list(exp_terms)
                        for term in exp_terms:
                            exp_fetch.extend(_kebab_parts(term))
                        exp_stats = _merge_corpus_stats(
                            hybrid_stats,
                            corpus_stats_provider(list(dict.fromkeys(exp_fetch))),
                        )
                    exp_leg = _score_bm25(
                        candidates,
                        exp_terms,
                        now=now,
                        half_life_days=half_life_days,
                        applied_by_id=applied_by_id,
                        negative_by_id=negative_by_id,
                        corroboration_boost=corroboration_boost,
                        candidate_tokens=candidate_tokens,
                        corpus_stats=exp_stats,
                    )
                    # A leg with one word of evidence does not get to
                    # vote. RRF reads rank, not score, so a leg resting
                    # on a single coincidental match contributes exactly
                    # as much as one three synonyms agree on;
                    # withholding it is the only way the fusion can tell
                    # them apart. A withheld leg leaves `scored` as
                    # the base fusion, so the result reproduces
                    # a lane-on query whose leg found nothing — the
                    # same shape the lane already has when `exp_terms`
                    # comes back empty. The filler floor above is keyed
                    # on `rescue_expansion`, not on the leg, so it
                    # stays. See `_RESCUE_LEG_MIN_EVIDENCE`.
                    leg_weight = (
                        _leg_evidence_weight(_leg_top_evidence(exp_leg))
                        if exp_leg
                        else 0.0
                    )
                    if leg_weight <= 0.0:
                        exp_leg = []
                    if exp_leg:
                        expansion_ids = {m.id for m, _, _ in exp_leg}
                        # The base pair keeps the SAME weights it fused
                        # with above — one code path, no fork (addendum
                        # 12's scope): a lane-on query must not restore
                        # a vote the base fusion just withheld.
                        scored = _hybrid_fuse(
                            rankings + [exp_leg],
                            rrf_k=rrf_k,
                            weights=[*(base_weights or (1.0, 1.0)), leg_weight],
                        )
                        # `match_terms` stays a subset of the CALLER's
                        # tokens — synthesized terms explain the leg,
                        # not the caller's query, and letting them into
                        # the matched list would inflate the coverage
                        # `relevance` is computed from. An
                        # expansion-only hit therefore reads
                        # relevance="low", match_terms=[] with
                        # matched_leg="expansion" — the exact shape
                        # pure-paraphrase hits have always had.
                        qset = set(query_tokens)
                        scored = [
                            (m, s, [t for t in matched if t in qset])
                            for m, s, matched in scored
                        ]

    # Lane L's anchor selection runs last — after the rescue leg has
    # joined or declined the fusion — so a lane-on query reorders the
    # ranking the caller would otherwise have received, before the trim.
    if conv_reading is not None:
        scored = _conversational_rerank(scored, reading=conv_reading)

    trimmed = scored[:max_results]
    if matched_leg_out is not None:
        for memory, _, _ in trimmed:
            leg = _matched_leg(
                lexical=memory.id in lexical_ids,
                expansion=memory.id in expansion_ids,
            )
            if leg:
                matched_leg_out[memory.id] = leg
    return [
        _build_hit(memory, score, matched, query_unique=query_unique)
        for memory, score, matched in trimmed
    ]


# ---------------------------------------------------------------------------
# Dedup at write time
# ---------------------------------------------------------------------------


# Thresholds for find_similar. Calibrated against jaccard on stopword-stripped
# token sets with pairwise-aware kebab expansion:
# - >= HIGH: block the write unless force=True. Two memories with this much
#   token overlap are very likely about the same fact; the right move is
#   memory_update on the existing entry.
# - >= MEDIUM: surface as `related` but do not block. The new memory may add
#   nuance worth keeping separate, but the writer should at least know the
#   adjacent memory exists.
# - <  MEDIUM: ignore.
HIGH_SIMILARITY = 0.75
MEDIUM_SIMILARITY = 0.40

# Minimum size of the SMALLER token set before the containment score in
# `_pairwise_content_jaccard` is allowed to fire. A 3-token fact whose
# handful of words all happen to appear somewhere in a long, topically
# unrelated note otherwise reaches containment ~1.0; requiring a real
# overlap keeps containment aimed at its actual target — a multi-token
# near-verbatim restatement of one sentence of a long body — rather than
# incidental common-vocabulary reuse.
_CONTAINMENT_MIN_TOKENS = 8
# Containment is a SOFT signal: it may raise a pair to 'related' but never
# to the 'high'/block bar. The ingest dedup gate (ingest.py) and the write
# gate skip/block only on a 'high' active hit, so an unbounded containment
# score would let a short vocabulary overlap SILENTLY drop a legitimately
# distinct write. Pin the containment contribution into the middle of the
# 'related' band so it always SURFACES (never silently blocks); a genuine
# verbatim duplicate still carries enough raw Jaccard to reach 'high' on
# its own merits.
_CONTAINMENT_CEILING = (HIGH_SIMILARITY + MEDIUM_SIMILARITY) / 2


def _content_token_set(text: str) -> set[str]:
    """Stopword-stripped token set with UNCONDITIONAL symmetric kebab/snake
    expansion. Retained as the reference for `compute_idf` (the BM25 corpus
    stats build the same shape inline); the Jaccard dedup scorers no longer
    call it.

    The dedup paths moved to `_raw_content_token_set` +
    `_pairwise_content_jaccard`: this function's old docstring claimed the
    union "grows in proportion and Jaccard stays well-behaved", which is
    mathematically false for a compound BOTH sides share — expanding it adds
    the same k part-tokens to intersection and union, and (i+k)/(u+k) > i/u
    whenever i < u, so expansion strictly inflates Jaccard for any
    non-identical pair. Two distinct per-environment facts sharing one
    compound identifier ("docker-compose ... prod" vs "docker-compose ...
    dev") crossed the 0.75 manual-apply threshold purely from that inflation.
    """
    return set(_strip_stopwords(_expand_kebab(tokenize(text))))


def _raw_content_token_set(text: str) -> set[str]:
    """Stopword-stripped tokens WITHOUT kebab expansion — the per-memory
    half of the pairwise dedup tokenisation. Compounds stay whole here;
    `_pairwise_content_jaccard` expands them per PAIR, only when the other
    side lacks the compound."""
    return set(_strip_stopwords(tokenize(text)))


def _pairwise_content_jaccard(raw_a: set[str], raw_b: set[str]) -> float:
    """Jaccard similarity with pairwise-aware kebab expansion.

    A kebab/snake compound is expanded into its parts (`_kebab_parts`,
    stopword parts stripped to match the old expand-then-strip order)
    only when the OTHER side's raw set lacks the compound. That keeps the
    cross-notation match (`python-frontmatter` vs `python frontmatter`
    still intersect on the parts) while a compound BOTH sides share
    contributes exactly one token to intersection and union — symmetric
    expansion of a shared compound added the same k part-tokens to both,
    which strictly inflates Jaccard whenever J < 1 (see
    `_content_token_set`) and pushed distinct per-environment facts over
    the dedup thresholds.
    """
    if not raw_a or not raw_b:
        return 0.0
    a = set(raw_a)
    for tok in raw_a:
        if tok not in raw_b:
            a.update(p for p in _kebab_parts(tok) if p not in _STOPWORDS)
    b = set(raw_b)
    for tok in raw_b:
        if tok not in raw_a:
            b.update(p for p in _kebab_parts(tok) if p not in _STOPWORDS)
    intersection = a & b
    if not intersection:
        return 0.0
    jaccard = len(intersection) / len(a | b)
    # Containment blind spot: a short near-verbatim restatement of one
    # sentence of a long body has |intersection| ~= the small set but a
    # union dominated by the long body, so Jaccard sinks below the
    # 'related' floor and the near-duplicate commits silently. Add a
    # containment score |intersection|/min — gated so it targets that case
    # and ONLY that case:
    #   - an absolute floor on the smaller set (`_CONTAINMENT_MIN_TOKENS`)
    #     so a 3-token fact sharing a few common words with a long
    #     unrelated note can't reach containment ~1.0;
    #   - it must itself clear MEDIUM_SIMILARITY (a weak overlap stays
    #     ignored); and
    #   - the result CAPPED into the 'related' band (`_CONTAINMENT_CEILING`,
    #     below HIGH_SIMILARITY) so containment can raise a pair to
    #     'related' but never to the 'high'/block bar — the ingest/write
    #     dedup gates skip only on a 'high' active hit, so an uncapped
    #     containment score could silently drop a legitimately distinct
    #     short write.
    #
    # There is NO size-ratio gate. The earlier `larger >= 3 * smaller`
    # cliff left a dead band: pure Jaccard covers full containment only up
    # to ratio 2.5 (1/r >= 0.40 = MEDIUM), so full containment at a ratio
    # in (2.5, 3.0) scored 0.333-0.400 — below MEDIUM AND below the gate,
    # hence silently ignored. `_CONTAINMENT_MIN_TOKENS` + the MEDIUM floor
    # + the ceiling already fence containment to its target case without
    # the discontinuity; `max(jaccard, containment)` makes containment a
    # no-op for near-identical pairs (jaccard already dominates there).
    # Honest scope note: dropping the gate DOES widen the firing set for
    # comparable-length pairs — two equal-size distinct writes sharing
    # 40-57% of the smaller side's tokens now land in the `related` band
    # via containment where raw Jaccard (intersection/union) kept them
    # below it. The widening is confined to the ADVISORY surface: the
    # ceiling below keeps containment under every high/block threshold,
    # so no write is ever refused by it — the accepted trade for closing
    # the (2.5, 3.0) full-containment dead band, pinned by
    # `test_find_similar_comparable_pair_widened_related_band_is_deliberate`.
    #
    # Ceiling guarantee: containment contributes at most _CONTAINMENT_CEILING
    # (< HIGH_SIMILARITY), so it can never raise a pair to the 'high'/block
    # band on its own — a genuine verbatim duplicate reaches 'high' via raw
    # Jaccard, which `max` leaves untouched. (Contested C3: this guarantee
    # is technically false only if a caller passes high_threshold <=
    # _CONTAINMENT_CEILING; no production caller does — the Jaccard-natural
    # high is HIGH_SIMILARITY.)
    smaller = a if len(a) <= len(b) else b
    if len(smaller) >= _CONTAINMENT_MIN_TOKENS:
        containment = len(intersection) / len(smaller)
        if containment >= MEDIUM_SIMILARITY:
            return max(jaccard, min(containment, _CONTAINMENT_CEILING))
    return jaccard


def find_similar(
    new_body: str,
    existing: list[Memory],
    *,
    high_threshold: float | None = None,
    medium_threshold: float | None = None,
) -> list[SimilarHit]:
    """Find memories whose content overlaps `new_body` enough to flag.

    Default mode: Jaccard similarity on stopword-stripped token sets with
    pairwise-aware kebab expansion (`_pairwise_content_jaccard`) — symmetric
    and recency-free, unlike `score_memory`. Fast, deterministic, no extra
    deps.

    Thresholds default to HIGH_SIMILARITY / MEDIUM_SIMILARITY (0.75 /
    0.40) when None. Pass explicit thresholds to tune.

    Returns hits with similarity >= medium_threshold, sorted descending
    by similarity. Hits below high_threshold are labeled `"medium"`; at
    or above, `"high"`. Empty when `new_body` has no content (or no
    tokens, in Jaccard mode).
    """
    return _find_similar_jaccard(
        new_body,
        existing,
        high_threshold=(
            high_threshold if high_threshold is not None else HIGH_SIMILARITY
        ),
        medium_threshold=(
            medium_threshold if medium_threshold is not None else MEDIUM_SIMILARITY
        ),
    )


# ---------------------------------------------------------------------------
# Generic dedup engine
# ---------------------------------------------------------------------------
#
# Pre-Round-2 the active and tombstone passes were separate functions
# whose loop bodies were near-clones — same threshold dispatch, same
# tokenisation, same hit-construction shape with only the relevance label
# and the optional `removed_at` / `removed_reason` fields differing
# between active and tombstone passes. The four-way duplication meant
# bug fixes had to land repeatedly. Consolidated below: one Jaccard
# scorer parameterised by a `build_hit`
# callable that knows how to construct a SimilarHit for the
# active-vs-tombstone variant. The two public entry points
# (`find_similar`, `find_similar_tombstones`) keep their existing
# signatures so the call sites in `_handlers.py` don't move.
#
# The shape: scorers are pure — given a similarity, a Memory-ish, and
# the relevance label, build the SimilarHit. They return None to drop
# the row, which lets the build-hit callable handle the rare case
# where a candidate fails downstream validation. In practice every
# adopter returns a hit; the Optional shape exists for symmetry with
# the threshold check above it.


def _score_similar_jaccard(
    new_body: str,
    existing: list[Any],
    *,
    high_threshold: float,
    medium_threshold: float,
    high_label: str,
    medium_label: str,
    build_hit: Callable[[Any, float, str], SimilarHit | None],
    sort_key: Callable[[SimilarHit], Any],
) -> list[SimilarHit]:
    """Jaccard-similarity dedup over `existing`, building hits via
    `build_hit`. See module commentary at the section header for the
    role this plays — extracted from the pre-Round-2 quartet of
    near-duplicate functions.

    Token sets are RAW (no kebab expansion); `_pairwise_content_jaccard`
    expands compounds per pair so a compound both sides share can't
    inflate the score."""
    new_tokens = _raw_content_token_set(new_body)
    if not new_tokens:
        return []

    hits: list[SimilarHit] = []
    for memory in existing:
        existing_tokens = _raw_content_token_set(memory.body)
        if not existing_tokens:
            continue

        similarity = _pairwise_content_jaccard(new_tokens, existing_tokens)
        if similarity <= 0.0:
            continue

        if similarity >= high_threshold:
            relevance = high_label
        elif similarity >= medium_threshold:
            relevance = medium_label
        else:
            continue

        hit = build_hit(memory, round(similarity, 4), relevance)
        if hit is not None:
            hits.append(hit)

    hits.sort(key=sort_key, reverse=True)
    return hits


def _build_active_hit(memory: Memory, similarity: float, relevance: str) -> SimilarHit:
    """Construct a SimilarHit for the active-memory dedup path."""
    return SimilarHit(
        id=memory.id,
        scopes=memory.scopes,
        confidence=memory.confidence,
        snippet=snippet_for(memory.body),
        similarity=similarity,
        relevance=relevance,
        created=memory.created,
        updated=memory.updated,
    )


def _build_tombstone_hit(
    memory: TombstonedMemory, similarity: float, relevance: str
) -> SimilarHit:
    """Construct a SimilarHit for the tombstone-aware dedup path. Carries
    the removal metadata the active variant doesn't have, so the
    write handler can render the `previously_removed` warning."""
    return SimilarHit(
        id=memory.id,
        scopes=memory.scopes,
        confidence=memory.confidence,
        snippet=snippet_for(memory.body),
        similarity=similarity,
        relevance=relevance,
        created=memory.created,
        updated=memory.updated,
        removed_at=memory.removed,
        removed_reason=memory.removed_reason,
    )


def _active_sort_key(h: SimilarHit) -> tuple[float, datetime]:
    return (h.similarity, h.updated)


def _tombstone_sort_key(h: SimilarHit) -> tuple[float, datetime]:
    # Fall back to `updated` when `removed_at` is missing — defensive
    # against any TombstonedMemory whose removal time didn't make the
    # round trip (legacy fixtures). The active path uses `updated`
    # straight, so the fallback keeps the orderings comparable.
    return (h.similarity, h.removed_at or h.updated)


def _find_similar_jaccard(
    new_body: str,
    existing: list[Memory],
    *,
    high_threshold: float,
    medium_threshold: float,
) -> list[SimilarHit]:
    return _score_similar_jaccard(
        new_body,
        existing,
        high_threshold=high_threshold,
        medium_threshold=medium_threshold,
        high_label="high",
        medium_label="medium",
        build_hit=_build_active_hit,
        sort_key=_active_sort_key,
    )


# ---------------------------------------------------------------------------
# Tombstone-aware dedup
# ---------------------------------------------------------------------------
#
# `find_similar` only walks the active set, which means the durability gate
# never fires when the writer is about to re-create a fact they previously
# removed. The lesson encoded in the tombstone's removal_reason is lost on
# the next write. `find_similar_tombstones` closes that loop: it scores
# the same body against tombstoned candidates and returns hits with
# `relevance="high-removed"` / `"medium-removed"` plus the removal
# metadata, so memory_write can warn ("you removed a 0.91-similar memory
# three weeks ago because 'turned out wrong'").
#
# Implementation note: we intentionally re-compute similarity here rather
# than calling find_similar(existing=tombstoned). The SimilarHit shape
# carries different metadata in each case (active hits have no removal
# fields; tombstone hits do), and TombstonedMemory is a distinct type
# from Memory so the type checker catches accidental mixing.


def find_similar_tombstones(
    new_body: str,
    tombstoned: list[TombstonedMemory],
    *,
    high_threshold: float | None = None,
    medium_threshold: float | None = None,
) -> list[SimilarHit]:
    """Like `find_similar`, but scored against tombstoned memories and
    returning hits labeled with the `-removed` relevance suffix.

    Threshold defaults match the active path: 0.75/0.40. Empty input
    or empty body returns []. Hits
    are sorted descending by similarity, like `find_similar`.
    """
    if not tombstoned:
        return []

    return _find_similar_tombstones_jaccard(
        new_body,
        tombstoned,
        high_threshold=(
            high_threshold if high_threshold is not None else HIGH_SIMILARITY
        ),
        medium_threshold=(
            medium_threshold if medium_threshold is not None else MEDIUM_SIMILARITY
        ),
    )


def _find_similar_tombstones_jaccard(
    new_body: str,
    tombstoned: list[TombstonedMemory],
    *,
    high_threshold: float,
    medium_threshold: float,
) -> list[SimilarHit]:
    return _score_similar_jaccard(
        new_body,
        tombstoned,
        high_threshold=high_threshold,
        medium_threshold=medium_threshold,
        high_label="high-removed",
        medium_label="medium-removed",
        build_hit=_build_tombstone_hit,
        sort_key=_tombstone_sort_key,
    )

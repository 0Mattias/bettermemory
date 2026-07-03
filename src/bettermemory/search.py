"""Ranking memories against a query.

Four selectable rankers, dispatched by `search(mode=...)`:

- ``hybrid`` (default since 2.6.8): reciprocal rank fusion (Cormack
  et al., SIGIR 2009) over keyword + BM25, plus semantic when a model
  is provided. Gracefully degrades to keyword+BM25 fusion when no
  model is available, so the flipped default doesn't add a dep
  requirement. The fused score lives in a different (much smaller)
  scale than the single-ranker scores — branch on `relevance`, not
  raw `score`, when comparing across modes.
- ``keyword`` (legacy default in 1.6.0): the original TF +
  scope-weighted + coverage + recency scorer. Cheap, deterministic,
  good on identifier-heavy queries but lacks IDF — underperforms on
  rare-term queries vs. BM25/hybrid.
- ``bm25``: Okapi BM25 with IDF weighting, TF saturation, length
  normalisation, plus the same scope-bonus and recency multiplier as
  the keyword scorer.
- ``semantic``: sentence-transformers cosine over per-memory cached
  embeddings (extras-gated; raises a clear error when the embeddings
  extra isn't installed).

`compute_idf` and `reciprocal_rank_fusion` are exported alongside
their per-mode scorers so callers can wire the rankers directly
without going through `search()`. The dedup path (`find_similar`)
uses Jaccard over `_raw_content_token_set` token sets with
pairwise-aware kebab expansion (`_pairwise_content_jaccard`), or
cosine when a model is supplied.
"""

from __future__ import annotations

import logging
import math
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Callable, Literal, NamedTuple

from .models import Memory, MemoryHit, SimilarHit, TombstonedMemory, snippet_for
from .origin import should_include_for_caller
from .verify import detect_path_drift

log = logging.getLogger("bettermemory.search")

# Search modes exposed via `search(mode=...)`. Default is `hybrid` since
# 2.6.8 — the keyword scorer lacks IDF weighting and underperforms on
# rare-term queries, and hybrid degrades gracefully to keyword+BM25
# fusion when no embedding extra is installed (so flipping the default
# doesn't add a dep requirement).
SearchMode = Literal["keyword", "bm25", "semantic", "hybrid"]


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
    precomposed and decomposed spellings fold to 'tjorn'."""
    return "".join(
        ch for ch in unicodedata.normalize("NFD", text) if not unicodedata.combining(ch)
    )


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


def _stem_segment(seg: str) -> str:
    """Light inflectional stem of one hyphen-free segment (tokenizer v2).

    Matching is exact token equality, so without this the most ordinary
    morphological variance between a retrieval question and a stored fact
    — plural vs singular ('standups' vs 'standup') — was a total miss in
    every ranker and the FTS5 index. This is deliberately NOT Porter:
    relevance buckets and dedup Jaccard feed automation, so aggressive
    derivational conflation ('general'/'generous') is a worse failure
    mode here than an occasional unfolded plural. Plural inflection only:

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

    Used by `index._insert_memory` / `_upsert_memory` / `rebuild` for the
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


def _strip_stopwords(tokens: list[str]) -> list[str]:
    return [t for t in tokens if t not in _STOPWORDS]


def _relevance_label(matched_unique: int, query_unique: int) -> str:
    """Map coverage (fraction of distinct query terms that hit) to a label.

    Calibrated for short queries: matching 1/1 or 2/2 is "high"; matching 1/3
    is "low". The thresholds are deliberately generous on the high side
    because a 1-word query with a strong match shouldn't be downgraded.
    """
    if query_unique <= 0:
        return "low"
    coverage = matched_unique / query_unique
    if coverage >= 0.75:
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
    keyword scorer's stream; `set()` of it is the semantic literal-match
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


def compute_idf(
    memories: list[Memory],
    *,
    tokens: list[_MemoryTokens] | None = None,
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

    def _okapi(df: dict[str, int]) -> dict[str, float]:
        return {
            term: math.log((n - dfi + 0.5) / (dfi + 0.5) + 1.0)
            for term, dfi in df.items()
        }

    return _okapi(body_df), _okapi(scope_df), avgdl


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
    """
    if not query_tokens or avgdl <= 0:
        return 0.0, []

    body_tokens = (
        tokens.content
        if tokens is not None
        else _strip_stopwords(_expand_kebab(tokenize(memory.body)))
    )
    body_count: dict[str, int] = {}
    for tok in body_tokens:
        body_count[tok] = body_count.get(tok, 0) + 1
    dl = len(body_tokens)

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
        body_idf = body_idf_map.get(tok, 0.0)
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
            parts = _kebab_parts(tok)
            if parts:
                component_hits = [body_count.get(p, 0) for p in parts]
                if min(component_hits) > 0:
                    tf = min(component_hits)
                    body_idf = min(body_idf_map.get(p, 0.0) for p in parts)
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
) -> tuple[float, list[str]]:
    """Score a memory against a query. Return `(score, matched_terms)`.

    `matched_terms` is the de-duplicated subset of `query_tokens` that hit
    the body or scopes — surfaced in the result so the consumer can tell
    whether a partial match is meaningful or stopword-driven noise.

    ``tokens``: optional precomputed `_MemoryTokens` for this memory —
    `search()` tokenizes each candidate once and threads the streams
    here. None recomputes them; identical output either way.
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
        raw += contrib

    if raw == 0.0:
        return 0.0, []

    # Mild boost for matching multiple distinct query terms — together with
    # the per-term TF cap above, this is what actually keeps "foo bar"
    # ranked above "foo foo foo" when the latter is just keyword spam.
    coverage = len(matched) / query_unique
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


def reciprocal_rank_fusion(
    ranking_lists: list[list[str]],
    *,
    k: int = _RRF_K_DEFAULT,
) -> dict[str, float]:
    """Fuse multiple ranked id-lists into one score-per-id map.

    Each `ranking_lists[i]` is a list of memory ids in best-first order
    for ranker i. The returned dict maps memory_id -> RRF score; sort
    descending to get the fused ranking. Ids that appear in no list are
    not present in the output. Duplicate ids within a single ranker's
    list are unusual but tolerated — the first (best-ranked) position
    wins for that ranker; later duplicates are ignored, matching the
    "one rank per (ranker, doc)" reading of the original paper.

    Empty `ranking_lists` returns an empty dict. `k` must be positive;
    the default (60) matches the Cormack et al. paper.
    """
    if k <= 0:
        raise ValueError(f"RRF k must be positive, got {k}")
    if not ranking_lists:
        return {}

    fused: dict[str, float] = {}
    for ranking in ranking_lists:
        # Iterate with 1-indexed rank — the original formula assumes
        # rank starts at 1. `seen` guards the dedup contract above.
        seen: set[str] = set()
        for rank, memory_id in enumerate(ranking, start=1):
            if memory_id in seen:
                continue
            seen.add(memory_id)
            fused[memory_id] = fused.get(memory_id, 0.0) + 1.0 / (k + rank)
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
    out: list[Memory] = []
    for memory in memories:
        memory_scope_set = set(memory.scopes)
        if excluded and (memory_scope_set & excluded):
            continue
        if scope_filter is not None and not (memory_scope_set & scope_filter):
            continue
        if repo_filter is not None:
            if not should_include_for_caller(
                memory.origin,
                repo_filter,
                caller_worktree_root=worktree_filter,
            ):
                continue
        out.append(memory)
    return out


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
    """
    drift = detect_path_drift(
        memory.body,
        verified_paths=memory.verified_paths,
        absent_paths=memory.verified_absent_paths,
    )
    return MemoryHit(
        id=memory.id,
        scopes=memory.scopes,
        confidence=memory.confidence,
        category=memory.category,
        snippet=snippet_for(memory.body),
        score=round(score, 4),
        relevance=_relevance_label(len(matched), query_unique),
        match_terms=matched,
        created=memory.created,
        updated=memory.updated,
        last_verified_at=memory.last_verified_at,
        path_drift_checked=len(drift.checked),
        path_drift_missing=len(drift.missing),
        path_drift_checked_paths=list(drift.checked),
        path_drift_missing_paths=list(drift.missing),
        path_drift_verified_paths=list(drift.verified),
        path_drift_expected_absent_paths=list(drift.expected_absent),
    )


def _score_keyword(
    candidates: list[Memory],
    query_tokens: list[str],
    *,
    now: datetime,
    half_life_days: float,
    applied_by_id: dict[str, int] | None = None,
    candidate_tokens: list[_MemoryTokens] | None = None,
) -> list[tuple[Memory, float, list[str]]]:
    """Run the original keyword scorer across all candidates. Returns
    `(memory, score, matched)` tuples for every candidate with `score > 0`.
    Order preserved from the input — sorting happens at the caller.

    `applied_by_id` (optional) maps memory id → explicit-applied count; when
    given, a bounded `_endorsement_factor` nudges endorsed memories. None
    (the default) leaves scores untouched.

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
        )
        if score > 0:
            if applied_by_id:
                score *= _endorsement_factor(applied_by_id.get(memory.id, 0))
            out.append((memory, score, matched))
    return out


def _score_bm25(
    candidates: list[Memory],
    query_tokens: list[str],
    *,
    now: datetime,
    half_life_days: float,
    applied_by_id: dict[str, int] | None = None,
    candidate_tokens: list[_MemoryTokens] | None = None,
) -> list[tuple[Memory, float, list[str]]]:
    """Run the BM25 scorer across all candidates. Returns
    `(memory, score, matched)` tuples for candidates with `score > 0`.
    `applied_by_id` / `candidate_tokens`: see `_score_keyword`."""
    body_idf_map, scope_idf_map, avgdl = compute_idf(
        candidates, tokens=candidate_tokens
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
        )
        if score > 0:
            if applied_by_id:
                score *= _endorsement_factor(applied_by_id.get(memory.id, 0))
            out.append((memory, score, matched))
    return out


def _score_semantic(
    candidates: list[Memory],
    query: str,
    semantic_model: Any,
    *,
    now: datetime,
    half_life_days: float,
    matched_terms_fallback: list[str],
    applied_by_id: dict[str, int] | None = None,
    candidate_tokens: list[_MemoryTokens] | None = None,
) -> list[tuple[Memory, float, list[str]]]:
    """Cosine-similarity scoring over sentence-transformers embeddings.

    Reuses the per-memory cache from `bettermemory.semantic` so a search
    that runs alongside dedup shares vectors.

    `matched_terms_fallback` is the stopword-stripped query token list. We
    do NOT blindly stamp it onto a semantic hit: that would report query
    words that appear nowhere in the memory as "matched" and drive the
    coverage-based relevance label to a fabricated "high" for a pure
    paraphrase hit, violating the MemoryHit contract (match_terms = the
    query tokens that actually hit the body or scopes; relevance = the
    fraction that matched). Instead we intersect the fallback with the
    memory's literal body/scope tokens — the exact overlap `score_memory`
    computes — and report that, possibly empty. A paraphrase-only hit then
    honestly carries `match_terms=[]` / low relevance while still surfacing
    by score.

    Threshold: hits with cosine < 0.3 are dropped. Below that, the
    similarity is noise — the model is matching style/structure rather
    than meaning. The threshold is conservative on purpose; we'd
    rather show fewer paraphrase hits than poison the result list
    with off-topic ones.
    """
    from .semantic import (
        _note_model_dimension,
        cached_embed,
        cosine_similarity_normalized,
        flush_persistent_cache,
    )

    query_clean = query.strip()
    if not query_clean:
        return []
    query_vec = semantic_model.encode(query_clean, normalize_embeddings=True)
    # The query encode is the first fresh embedding this run does —
    # feed its dimension to the cache reconcile so any stale-dimension
    # hydrated entries are purged before the `cached_embed` hits below.
    _note_model_dimension(len(query_vec))

    threshold = 0.3
    out: list[tuple[Memory, float, list[str]]] = []
    for i, memory in enumerate(candidates):
        body_clean = memory.body.strip()
        if not body_clean:
            continue
        body_vec = cached_embed(
            semantic_model,
            memory.id,
            memory.updated.isoformat(),
            body_clean,
        )
        sim = cosine_similarity_normalized(query_vec, body_vec)
        if sim < threshold:
            continue
        # Apply the same recency multiplier the other rankers use so a
        # stale paraphrase doesn't beat a fresh near-paraphrase.
        freshness = max(memory.created, memory.updated)
        score = sim * _recency_factor(freshness, now, half_life_days)
        if applied_by_id:
            score *= _endorsement_factor(applied_by_id.get(memory.id, 0))
        # Report only the query tokens that LITERALLY hit this memory's
        # body or scopes (same overlap `score_memory` computes), not the
        # whole query — so a paraphrase-only hit carries honest match_terms
        # and an honest (low) relevance label rather than a fabricated one.
        if candidate_tokens is not None:
            body_token_set = set(candidate_tokens[i].body)
            scope_token_set = candidate_tokens[i].scope_set
        else:
            body_token_set = set(_expand_kebab(tokenize(memory.body)))
            scope_token_set = set()
            for scope in memory.scopes:
                scope_token_set.update(_scope_tokens(scope))
        literal_matched = [
            tok
            for tok in matched_terms_fallback
            if tok in body_token_set or tok in scope_token_set
        ]
        out.append((memory, score, literal_matched))
    flush_persistent_cache()
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


def _hybrid_fuse(
    rankings: list[list[tuple[Memory, float, list[str]]]],
    *,
    rrf_k: int,
) -> list[tuple[Memory, float, list[str]]]:
    """Fuse multiple ranker outputs into one ranked list via RRF.

    Each input is a per-ranker `[(memory, score, matched), ...]` list.
    Output is `[(memory, rrf_score, matched_union), ...]` ordered desc
    by RRF score. `matched_union` is the union of matched terms across
    rankers that surfaced the memory, sorted for stability.
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

    fused = reciprocal_rank_fusion(ranking_id_lists, k=rrf_k)
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
    semantic_model: Any | None = None,
    rrf_k: int = _RRF_K_DEFAULT,
    applied_by_id: dict[str, int] | None = None,
    allow_empty_query: bool = False,
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
      fusion of keyword + BM25, plus semantic when a model is
      provided); `"keyword"` (legacy TF + coverage + recency scorer
      with no IDF weighting); `"bm25"` (Okapi BM25 with the same
      scope-bonus + recency boost); `"semantic"` (sentence-
      transformers cosine — requires `semantic_model`). The hybrid
      mode gracefully degrades when no `semantic_model` is given: it
      fuses keyword + BM25 only, so flipping the default doesn't
      require any embedding extra.
    - `semantic_model`: optional sentence-transformers model. Required
      for `mode="semantic"`; optional for `mode="hybrid"` (semantic is
      added to the fusion when present).
    - `rrf_k`: smoothing constant for hybrid fusion. Larger spreads
      weight further down the list; smaller makes top ranks dominate.
      60 is the canonical default and almost always correct.
    - `applied_by_id`: optional map of memory id → explicit-applied count.
      When given, a bounded `_endorsement_factor` (≤ +10%, same ceiling as
      recency) nudges endorsed memories up — a near-tie breaker, never a
      relevance override. `None` (the default) leaves scores untouched, so
      every existing caller and the package default are byte-stable.
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

    Score semantics vary by mode: keyword/BM25/semantic scores live on
    different scales and are not comparable across modes. Hybrid scores
    are RRF outputs (~0.01-0.05 range, summed `1/(k+rank)` over rankers).
    Use the `relevance` label, not the raw score, when comparing hits
    across modes.
    """
    # Runtime guard against unknown modes. The `SearchMode` Literal pins
    # this at the type-checker layer, but the handler accepts an opaque
    # string from MCP and Python doesn't enforce Literals at call time;
    # without this check, a typo like `mode="emantic"` would fall through
    # the if/elif chain into the `else` branch and silently run hybrid.
    # Raising here makes the failure mode loud at the dispatch boundary
    # regardless of where the bad string came from (handler, CLI, future
    # programmatic client).
    if mode not in ("keyword", "bm25", "semantic", "hybrid"):
        raise ValueError(
            f"unknown search mode {mode!r}; "
            "must be one of: keyword, bm25, semantic, hybrid"
        )
    if mode == "semantic" and semantic_model is None:
        raise ValueError("mode='semantic' requires semantic_model to be provided")

    now = now or datetime.now(timezone.utc)
    raw_tokens = tokenize(query)
    # Strip stopwords from the query — bodies stay unfiltered. If stripping
    # EMPTIES a non-empty token list ("what is the" — or a lone term some
    # future stopword addition absorbs), fall back to the unstripped
    # tokens: stopword curation must never make a real query unanswerable,
    # so the worst case is filler-grade ranking, not silent zero recall.
    # A truly empty query still returns empty rather than serving every
    # memory at score 0, and browse mode keeps its recency-ordered
    # semantics for both empty and stopword-only queries — see
    # `allow_empty_query` above.
    query_tokens = _strip_stopwords(raw_tokens)
    if not query_tokens and raw_tokens and not allow_empty_query:
        query_tokens = raw_tokens
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

    # Tokenize each candidate exactly once per call and thread the streams
    # through every consumer below — the keyword scorer, compute_idf, BM25,
    # and the semantic literal-match block otherwise re-tokenize the same
    # bodies and scopes (6 tokenize calls per memory per hybrid search,
    # ~88% of cumulative search time). Pure perf: see `_MemoryTokens`.
    candidate_tokens = [_memory_tokens(m) for m in candidates]

    if mode == "keyword":
        scored = _score_keyword(
            candidates,
            query_tokens,
            now=now,
            half_life_days=half_life_days,
            applied_by_id=applied_by_id,
            candidate_tokens=candidate_tokens,
        )
        # Sort by score, then created (newer wins on tie), then id as the
        # final discriminator. Without `id` the tiebreaker is undefined for
        # two memories that share both score and created timestamp — a real
        # case under microsecond-tied writes or under tests that mock the
        # clock. ULID-shaped ids are lexically time-ordered, so the final
        # tiebreaker also gives "newer wins" semantics.
        scored.sort(key=lambda x: (x[1], x[0].created, x[0].id), reverse=True)
    elif mode == "bm25":
        scored = _score_bm25(
            candidates,
            query_tokens,
            now=now,
            half_life_days=half_life_days,
            applied_by_id=applied_by_id,
            candidate_tokens=candidate_tokens,
        )
        scored.sort(key=lambda x: (x[1], x[0].created, x[0].id), reverse=True)
    elif mode == "semantic":
        # mypy: semantic_model is not None here (guarded above), but the
        # narrowing doesn't survive the assert-via-raise idiom across the
        # block boundary. Re-assert for the type checker.
        assert semantic_model is not None
        try:
            scored = _score_semantic(
                candidates,
                query,
                semantic_model,
                now=now,
                half_life_days=half_life_days,
                matched_terms_fallback=list(dict.fromkeys(query_tokens)),
                applied_by_id=applied_by_id,
                candidate_tokens=candidate_tokens,
            )
        except Exception as exc:  # noqa: BLE001 — degrade to keyword on encode failure.
            # A LOADED model can still raise at encode() time (device fault,
            # OOM on a large body, a tokenizer edge case). Explicit semantic
            # mode must not crash the search on that — fall back to the
            # keyword ranking so the caller still gets results.
            log.warning(
                "semantic search failed at encode time (%s); "
                "falling back to keyword ranking",
                exc,
            )
            scored = _score_keyword(
                candidates,
                query_tokens,
                now=now,
                half_life_days=half_life_days,
                applied_by_id=applied_by_id,
                candidate_tokens=candidate_tokens,
            )
        scored.sort(key=lambda x: (x[1], x[0].created, x[0].id), reverse=True)
    else:  # mode == "hybrid"
        rankings: list[list[tuple[Memory, float, list[str]]]] = [
            _score_keyword(
                candidates,
                query_tokens,
                now=now,
                half_life_days=half_life_days,
                applied_by_id=applied_by_id,
                candidate_tokens=candidate_tokens,
            ),
            _score_bm25(
                candidates,
                query_tokens,
                now=now,
                half_life_days=half_life_days,
                applied_by_id=applied_by_id,
                candidate_tokens=candidate_tokens,
            ),
        ]
        if semantic_model is not None:
            try:
                rankings.append(
                    _score_semantic(
                        candidates,
                        query,
                        semantic_model,
                        now=now,
                        half_life_days=half_life_days,
                        matched_terms_fallback=list(dict.fromkeys(query_tokens)),
                        applied_by_id=applied_by_id,
                        candidate_tokens=candidate_tokens,
                    )
                )
            except Exception as exc:  # noqa: BLE001 — degrade to lexical fusion.
                # The "hybrid gracefully degrades" guarantee must cover a
                # runtime encode() failure of a loaded model, not just the
                # model-is-None case: fuse the keyword+bm25 rankings already
                # computed instead of crashing the search.
                log.warning(
                    "semantic ranking failed at encode time (%s); "
                    "fusing keyword+bm25 only",
                    exc,
                )
        scored = _hybrid_fuse(rankings, rrf_k=rrf_k)

    return [
        _build_hit(memory, score, matched, query_unique=query_unique)
        for memory, score, matched in scored[:max_results]
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
    return len(intersection) / len(a | b)


def find_similar(
    new_body: str,
    existing: list[Memory],
    *,
    semantic_model: Any | None = None,
    high_threshold: float | None = None,
    medium_threshold: float | None = None,
) -> list[SimilarHit]:
    """Find memories whose content overlaps `new_body` enough to flag.

    Default mode: Jaccard similarity on stopword-stripped token sets with
    pairwise-aware kebab expansion (`_pairwise_content_jaccard`) — symmetric
    and recency-free, unlike `score_memory`. Fast, deterministic, no extra
    deps.

    Semantic mode (when `semantic_model` is non-None): cosine similarity
    on sentence-transformers embeddings. Catches paraphrases that share
    no tokens — "the database" vs "Postgres", "shipped" vs "released".
    Pass a model object with an `encode(text, normalize_embeddings=True)`
    method (e.g. `sentence_transformers.SentenceTransformer`) — see
    `bettermemory.semantic.get_model()` for the loader.

    Thresholds default to the mode's natural range when None: 0.75/0.40
    for Jaccard, 0.85/0.65 for cosine. Pass explicit thresholds to tune.

    Returns hits with similarity >= medium_threshold, sorted descending
    by similarity. Hits below high_threshold are labeled `"medium"`; at
    or above, `"high"`. Empty when `new_body` has no content (or no
    tokens, in Jaccard mode).
    """
    if semantic_model is not None:
        try:
            return _find_similar_semantic(
                new_body,
                existing,
                semantic_model,
                high_threshold=(high_threshold if high_threshold is not None else 0.85),
                medium_threshold=(
                    medium_threshold if medium_threshold is not None else 0.65
                ),
            )
        except Exception as exc:  # noqa: BLE001 — degrade to Jaccard dedup.
            # A loaded model raising at encode() time must not crash the
            # write-dedup gate (memory_write calls find_similar BEFORE it
            # commits). Degrade to lexical Jaccard dedup so the write still
            # completes — but with the Jaccard-NATURAL thresholds, NOT the
            # ones the caller passed. Thresholds supplied alongside a
            # semantic_model are COSINE-calibrated (the write-dedup gate
            # passes semantic_high/medium_threshold = 0.85/0.65); forwarding
            # those to the Jaccard scorer — whose natural high/medium are
            # HIGH_SIMILARITY/MEDIUM_SIMILARITY (0.75/0.40) — would silently
            # neuter the gate, since Jaccard rarely reaches 0.85, letting a
            # near-duplicate the gate should BLOCK commit as a parallel
            # duplicate. Dedup at the lexical scorer's own calibration.
            log.warning(
                "semantic dedup failed at encode time (%s); falling back to Jaccard",
                exc,
            )
            return _find_similar_jaccard(
                new_body,
                existing,
                high_threshold=HIGH_SIMILARITY,
                medium_threshold=MEDIUM_SIMILARITY,
            )

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
# Pre-Round-2 the active and tombstone passes were four separate functions
# (`_find_similar_jaccard`, `_find_similar_semantic`,
# `_find_similar_tombstones_jaccard`, `_find_similar_tombstones_semantic`)
# whose loop bodies were near-clones — same threshold dispatch, same
# tokenisation, same hit-construction shape with only the relevance label
# and the optional `removed_at` / `removed_reason` fields differing
# between active and tombstone passes. The four-way duplication meant
# bug fixes had to land four times. Consolidated below: one Jaccard
# scorer and one semantic scorer, each parameterised by a `build_hit`
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


def _score_similar_semantic(
    new_body: str,
    existing: list[Any],
    model: Any,
    *,
    high_threshold: float,
    medium_threshold: float,
    high_label: str,
    medium_label: str,
    build_hit: Callable[[Any, float, str], SimilarHit | None],
    sort_key: Callable[[SimilarHit], Any],
    cache_key_for: Callable[[Any], tuple[str, str]],
) -> list[SimilarHit]:
    """Cosine-similarity dedup over `existing`, building hits via
    `build_hit`.

    `cache_key_for(memory)` returns the `(id, freshness_key)` tuple
    used to address the embedding cache — the active pass uses
    `(memory.id, memory.updated.isoformat())`; the tombstone pass uses
    `(f"tomb:{memory.id}", memory.removed.isoformat())`. Keeping the
    key derivation outside this function is what lets active and
    tombstone caches coexist for the same memory id without colliding.

    Imports `semantic` lazily so this module loads cleanly even when
    the embeddings extra isn't installed.
    """
    from .semantic import (
        _note_model_dimension,
        cached_embed,
        cosine_similarity_normalized,
        flush_persistent_cache,
    )

    new_body_clean = new_body.strip()
    if not new_body_clean:
        return []

    new_vec = model.encode(new_body_clean, normalize_embeddings=True)
    # First fresh embedding of the run — prime the cache reconcile so a
    # stale-dimension hydrated entry can't reach `cosine` below. See
    # `semantic._note_model_dimension`.
    _note_model_dimension(len(new_vec))

    hits: list[SimilarHit] = []
    for memory in existing:
        body_clean = memory.body.strip()
        if not body_clean:
            continue
        cache_id, cache_freshness = cache_key_for(memory)
        existing_vec = cached_embed(model, cache_id, cache_freshness, body_clean)
        similarity = cosine_similarity_normalized(new_vec, existing_vec)

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
    # End-of-batch hook: persist any newly-computed embeddings as a
    # single atomic write. No-op when persistence isn't configured or
    # nothing changed since the last flush.
    flush_persistent_cache()
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


def _find_similar_semantic(
    new_body: str,
    existing: list[Memory],
    model: Any,
    *,
    high_threshold: float,
    medium_threshold: float,
) -> list[SimilarHit]:
    """Cosine similarity over sentence-transformers embeddings.

    Imports `semantic` lazily so this module loads cleanly even when the
    embeddings extra isn't installed — a caller who never passes a
    `semantic_model` won't trigger the import path.
    """
    return _score_similar_semantic(
        new_body,
        existing,
        model,
        high_threshold=high_threshold,
        medium_threshold=medium_threshold,
        high_label="high",
        medium_label="medium",
        build_hit=_build_active_hit,
        sort_key=_active_sort_key,
        cache_key_for=lambda m: (m.id, m.updated.isoformat()),
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
    semantic_model: Any | None = None,
    high_threshold: float | None = None,
    medium_threshold: float | None = None,
) -> list[SimilarHit]:
    """Like `find_similar`, but scored against tombstoned memories and
    returning hits labeled with the `-removed` relevance suffix.

    Threshold defaults match the active path: 0.75/0.40 for Jaccard,
    0.85/0.65 for cosine. Empty input or empty body returns []. Hits
    are sorted descending by similarity, like `find_similar`.
    """
    if not tombstoned:
        return []

    if semantic_model is not None:
        try:
            return _find_similar_tombstones_semantic(
                new_body,
                tombstoned,
                semantic_model,
                high_threshold=(high_threshold if high_threshold is not None else 0.85),
                medium_threshold=(
                    medium_threshold if medium_threshold is not None else 0.65
                ),
            )
        except Exception as exc:  # noqa: BLE001 — degrade to Jaccard dedup.
            # Same fail-soft as find_similar, with the same threshold care:
            # the caller's cosine thresholds (0.85/0.65) must NOT be applied
            # to the Jaccard scorer (natural 0.75/0.40), or a near-duplicate
            # tombstone would stop surfacing the previously_removed warning.
            # Use the Jaccard-natural defaults.
            log.warning(
                "semantic tombstone dedup failed at encode time (%s); "
                "falling back to Jaccard",
                exc,
            )
            return _find_similar_tombstones_jaccard(
                new_body,
                tombstoned,
                high_threshold=HIGH_SIMILARITY,
                medium_threshold=MEDIUM_SIMILARITY,
            )

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


def _find_similar_tombstones_semantic(
    new_body: str,
    tombstoned: list[TombstonedMemory],
    model: Any,
    *,
    high_threshold: float,
    medium_threshold: float,
) -> list[SimilarHit]:
    """Cosine similarity over sentence-transformers embeddings, against
    tombstoned bodies. Mirrors `_find_similar_semantic` for the active path.

    Cache key uses `removed` rather than `updated` for tombstones: a
    tombstone's body is frozen post-removal (we don't bump `updated`
    on removal), so `removed` is the natural freshness handle and
    distinguishes the cache entry from any active-side cache that
    might exist for the same memory_id (e.g. immediately after a
    restore-then-tombstone cycle). The `tomb:` prefix on the cache id
    is what keeps the active and tombstone caches from colliding for
    the same memory across a restore-then-tombstone cycle.
    """
    return _score_similar_semantic(
        new_body,
        tombstoned,
        model,
        high_threshold=high_threshold,
        medium_threshold=medium_threshold,
        high_label="high-removed",
        medium_label="medium-removed",
        build_hit=_build_tombstone_hit,
        sort_key=_tombstone_sort_key,
        cache_key_for=lambda m: (f"tomb:{m.id}", m.removed.isoformat()),
    )

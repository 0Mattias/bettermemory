"""The tokenizer `search.py` ranks with, and the text the FTS index stores.

Folding, the stopword lists, the plural stemmer, CJK segmentation and
`tokenize` itself, with `fts_index_text` and `fts_match_query`, which
spell the same token stream for the store's FTS5 index. The index keeps
this module's output on disk and `tokenizer_fingerprint` hashes it over
a fixed probe corpus, so a change to any rule or constant here changes
the persisted format and needs the schema bump that function describes.

`search.py` re-exports the names tests and other modules import from it.
Readers in `search.py` and its other `search_*` modules call
`_lexicon.tokenize(...)` through this module, so one patch of
`search_lexicon.tokenize` reaches every caller.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from functools import lru_cache


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
# partly Swedish; a stopword defence that is English-only lets
# non-English text match on pure filler like 'vill'/'att'/'på'). Two
# ground rules:
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
    surface spelling. Consumed by the audit's user-message
    acknowledgement check (`audit.py`), which compares surface forms.
    Everything that SCORES uses `tokenize`."""
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

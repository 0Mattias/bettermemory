"""Write-time groundedness check (T1.3 of the v1.6 plan).

Operationalizes the HaluMem-style "is this proposed memory grounded in
the conversation that produced it?" check. The model passes the recent
turns of the conversation as `source_transcript`; we walk the proposed
body sentence-by-sentence and flag any sentence whose content tokens
have no meaningful overlap with the transcript.

The signal isn't perfect — short factual claims may share tokens with
unrelated transcript text, long sentences may legitimately introduce a
new word — but it catches the well-known failure mode of auto-extraction
systems: models writing "facts" pulled from thin air. A grounded
sentence has *some* anchor in the conversation; a hallucinated one
typically has none.

Calibration:

- Sentences with fewer than `MIN_CONTENT_TOKENS` content tokens are
  exempt from the ratio test (too short for a fraction to mean much;
  "OK." has no semantic anchor to check). But a sub-minimum sentence
  with at least two content tokens and ZERO anchors is still flagged:
  "Lives in Berlin." is a fully verifiable claim, and sharing nothing
  with the transcript is the strongest hallucination signal this
  heuristic has. A single anchor is enough to pass — the conservative
  stance for legitimately tiny claims stays — and because down here a
  single spelling mismatch flips the whole verdict, anchoring for
  this rule alone is alias-tolerant: a token whose spelling has a
  substring or first-char-anchored-subsequence relation with a
  transcript token counts as anchored, so "Prefers Neovim." grounds
  against a transcript that says "nvim" (see `_is_alias_anchored`).
  One-token fragments and trailing-colon fragments (section headers
  like "Action items:") stay skipped entirely.
- A sentence is grounded when at least `GROUNDEDNESS_THRESHOLD` of its
  stopword-stripped content tokens are anchored in the transcript. The
  ratio is computed over the sentence's *original* tokens — each
  conceptual token counts once. A token is anchored when it, or (for
  kebab/snake compounds) any of its parts, appears in the transcript's
  kebab-expanded token set. Expansion stays on the transcript side
  only: one shared compound can't multi-count as several matched
  tokens, and one unmatched compound (an ISO date stamp, say) can't
  multi-count against the sentence. The asymmetry (matched /
  |sentence|, not sentence ∩ transcript / sentence ∪ transcript) is
  deliberate: the transcript is typically much larger than any single
  sentence, so Jaccard would underestimate grounding for legitimate
  body text. We're measuring "is the sentence anchored?", not
  "do these texts cover the same ground?".
- Speaker labels ("User:", "Assistant:") are stripped from the
  transcript before tokenizing — line-leading or mid-line, since
  transcripts joined with spaces instead of newlines carry the same
  labels. They're formatting metadata, not conversation vocabulary,
  and left in place they donate freebie anchors to short fabricated
  claims about the user.
- Joined auxiliary/pronoun contractions ("doesnt", "dont", "thats" —
  the post-apostrophe-collapse spellings) are treated as stopwords on
  both sides. Each expands to pure stopwords (does + not, that + is),
  and as tokens they're near-universal in conversational transcripts,
  so they'd otherwise act as freebie anchors for any negated or
  possessive fabricated claim.

This is a heuristic, not a proof. False negatives (grounded sentences
flagged as ungrounded) happen with creative paraphrasing; false
positives (hallucinated sentences that happen to share generic
vocabulary with the transcript) happen too. The gate is advisory —
`memory_write` only blocks the write when `groundedness_check=True`
AND there's at least one ungrounded sentence AND
`acknowledge_ungrounded` isn't set. The caller can always override.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .search import (
    _KEBAB_SPLIT_RE,
    _expand_kebab,
    _strip_stopwords,
    _tokenize_unstemmed,
    tokenize,
)


# Minimum number of content tokens a sentence needs before we apply
# the ratio test. Below this a fraction is too coarse to be meaningful
# (1/2 vs 2/2 is the only resolution), so sub-minimum sentences only
# fire on the zero-anchor rule below — a short "OK." or "yes." should
# never be flagged as ungrounded.
MIN_CONTENT_TOKENS = 3

# Sub-minimum sentences with at least this many content tokens are
# still checked for the zero-anchor case: "Lives in Berlin." (two
# content tokens) is a fully verifiable claim, and ZERO overlap with
# the transcript is the strongest hallucination signal this heuristic
# has. One anchor passes — legitimately tiny grounded claims stay
# unflagged. One-token fragments stay exempt: "OK." / "Done." carry
# no checkable claim.
_ZERO_ANCHOR_MIN_TOKENS = 2

# Alias rescue (zero-anchor rule ONLY): minimum length of the SHORTER
# spelling in a candidate alias pair. At 4, real alias/abbreviation
# pairs qualify (nvim/neovim, code/vscode, postgres/postgresql) while
# 1-3-letter particles ("vs", "db") can't anchor a claim by being a
# substring of half the dictionary. "k8s"/"Kubernetes" is the
# documented miss of this rescue: numeronyms share no spelling
# relation with their expansion (the '8' replaces the letters), so no
# length tuning covers them — that would need a synonym table, out of
# scope for a spelling-level heuristic.
_ALIAS_MIN_TOKEN_LEN = 4

# Sentence-level overlap ratio required for "grounded". Below this, we
# flag the sentence as ungrounded. 0.30 is calibrated so that a sentence
# of 5 content tokens needs at least 2 of them in the transcript — low
# enough to allow paraphrase, high enough to catch sentences that share
# only generic vocabulary.
GROUNDEDNESS_THRESHOLD = 0.30


# Sentence splitter, four boundary shapes — still deliberately simple,
# heavy NLP isn't worth the false sense of precision:
#
# 1. Terminal punctuation (period / exclamation / question / semicolon),
#    optionally wrapped in closing quotes, brackets, or markdown emphasis
#    ('."', '.”', '.**', '.)'), followed by whitespace. Without the
#    closing-character allowance, a period inside a quote merges two
#    sentences and a hallucinated follower hides behind a grounded quote.
# 2. Paragraph breaks — tolerant of CRLF line endings and blank lines
#    that carry trailing horizontal whitespace, so the encoding of the
#    newline can't flip a verdict.
# 3. Newlines that start a bullet or numbered list item, so each item is
#    evaluated independently (a hallucinated bullet can't hide behind
#    grounded siblings, and a list index can't glue onto the previous
#    fragment). Wrapped prose lines (newline, no list marker) stay
#    pooled with their sentence.
#
# Dotted abbreviation runs ("i.e.", "e.g.", "Ph.D.", "U.S.") are
# collapsed before splitting (see _DOTTED_ABBREV_RE) so their internal
# dots neither split a sentence mid-claim nor shatter into junk tokens.
#
# 4. Full-width terminators (。！？；) split too — with the trailing
#    whitespace OPTIONAL, because CJK prose puts no space after the
#    ideographic full stop. Without this branch a multi-sentence
#    Japanese/Chinese body collapsed into one "sentence", letting a
#    hallucinated clause hide behind a grounded sibling (and inflating
#    the token count past any per-sentence signal).
_SENTENCE_SPLIT_RE = re.compile(
    r"[.!?;][\"'”’)\]*_`]*\s+"
    r"|[。！？；][」』”’）】\"']*\s*"
    r"|(?:\r?\n[^\S\n]*){2,}"
    r"|\r?\n(?=[^\S\n]*(?:[-*•]|\d+[.)])[^\S\n])"
)

# Speaker labels in the transcript ("User:", "Assistant:") are
# transcript formatting metadata, not conversation vocabulary —
# stripped from the transcript side only before tokenizing.
# Word-boundary-anchored rather than line-anchored: transcripts whose
# turns are joined with spaces carry the same labels mid-line, and the
# verdict must not flip on the join character. Only these four speaker
# words directly before a colon are eaten — ordinary "heading: detail"
# prose colons are untouched.
_SPEAKER_LABEL_RE = re.compile(r"(?i)\b(?:user|assistant|system|human)\s*:")

# Runs of 1-2-letter dotted abbreviations: "i.e." -> "ie", "Ph.D." ->
# "PhD", "U.S." -> "US". Letters only, so version numbers ("1.0.2") and
# plain sentence-final periods are untouched.
_DOTTED_ABBREV_RE = re.compile(r"\b(?:[A-Za-z]{1,2}\.){2,}")

# Intra-word apostrophes (ASCII and typographic U+2019): "doesn't"
# otherwise tokenizes as 'doesn' + 't', and those fragments cross-match
# any unrelated contraction in the transcript. Collapsed so the
# contraction stays one token on both sides.
_APOSTROPHE_RE = re.compile(r"(\w)['’](\w)")

# Joined auxiliary/pronoun contractions — the spellings produced by
# the apostrophe collapse above (and their already-apostrophe-free
# variants as commonly typed). Stripped as stopwords on both sides:
# each expands to pure stopwords ("doesnt" = does + not, "thats" =
# that + is), carries no claim content, and is near-universal in
# conversational transcripts — left in, "doesnt" alone grounds any
# fabricated negated claim against any transcript that happens to
# contain a "doesn't". Collision-prone collapses are deliberately
# excluded: "we'll"/"he'll"/"she'll"/"I'll"/"I'd"/"we'd" collapse to
# the real words well / hell / shell / ill / id / wed, which may be
# genuine content tokens.
_CONTRACTION_STOPWORDS = frozenset(
    {
        "aint",
        "arent",
        "cant",
        "couldnt",
        "didnt",
        "doesnt",
        "dont",
        "hadnt",
        "hasnt",
        "havent",
        "hes",
        "im",
        "isnt",
        "ive",
        "lets",
        "mustnt",
        "neednt",
        "shant",
        "shes",
        "shouldnt",
        "thats",
        "theres",
        "theyd",
        "theyll",
        "theyre",
        "theyve",
        "wasnt",
        "werent",
        "weve",
        "whats",
        "wont",
        "wouldnt",
        "youd",
        "youll",
        "youre",
        "youve",
    }
)

# camelCase boundaries get a hyphen inserted so the existing kebab
# expansion covers identifier-spelled facts ("formatOnSave" ->
# "format-On-Save" -> format / on / save) the same way it already
# covers their kebab spellings. Applied to both sides, so identical
# spellings still match each other directly.
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


@dataclass(frozen=True)
class UngroundedClaim:
    """One sentence from the proposed body that didn't anchor to the
    transcript. `sentence` is the verbatim text (trimmed, with dotted
    abbreviations collapsed — "Ph.D." reads "PhD" — but not
    paraphrased) so the caller can see exactly what triggered.
    `overlap_ratio` is the fraction of the sentence's content tokens
    that appear in the transcript — 0.0 means none, the threshold
    means borderline."""

    sentence: str
    overlap_ratio: float

    def to_dict(self) -> dict[str, object]:
        return {
            "sentence": self.sentence,
            "overlap_ratio": round(self.overlap_ratio, 3),
        }


def _collapse_dotted_abbrevs(text: str) -> str:
    """'i.e.' -> 'ie', 'Ph.D.' -> 'PhD', 'U.S.' -> 'US'. Removing the
    dots changes nothing semantic but stops the dots from splitting a
    sentence mid-claim and the letters from shattering into junk
    tokens that never match the undotted spelling."""
    return _DOTTED_ABBREV_RE.sub(lambda m: m.group().replace(".", ""), text)


def _split_sentences(body: str) -> list[str]:
    """Split a body into sentences. Naive — terminal punctuation
    (tolerating closing quotes/emphasis), paragraph break, list-item
    start — but stable: a sentence that genuinely shares no tokens
    with the transcript will still not share any after splitting
    differently. Dotted abbreviations are collapsed first so 'i.e.'
    doesn't isolate its restatement clause as a fragment."""
    parts = _SENTENCE_SPLIT_RE.split(_collapse_dotted_abbrevs(body))
    return [p.strip() for p in parts if p.strip()]


def _normalize_token_text(text: str) -> str:
    """Shared spelling normalization applied to both sides before
    tokenizing: collapse dotted abbreviations and intra-word
    apostrophes, hyphenate camelCase boundaries (must run before
    tokenize() lowercases and destroys the case information)."""
    text = _collapse_dotted_abbrevs(text)
    text = _APOSTROPHE_RE.sub(r"\1\2", text)
    return _CAMEL_BOUNDARY_RE.sub("-", text)


def _strip_contractions(tokens: list[str]) -> list[str]:
    """Drop joined auxiliary/pronoun contractions — the module-local
    extension of search.py's stopword philosophy (each entry expands
    to pure stopwords)."""
    return [t for t in tokens if t not in _CONTRACTION_STOPWORDS]


def _content_tokens(text: str) -> set[str]:
    """Transcript-side tokens to compare against. Same pipeline as the
    dedup path — stopword-stripped, kebab-expanded, lowercased — plus
    the shared spelling normalization and contraction stripping.
    Returns a set so repeated tokens within one side don't inflate
    overlap counts.
    """
    return set(
        _strip_contractions(
            _strip_stopwords(_expand_kebab(tokenize(_normalize_token_text(text))))
        )
    )


def _sentence_content_tokens(sentence: str) -> set[str]:
    """Sentence-side tokens: same normalization and contraction
    stripping, NO kebab expansion. The overlap ratio counts each
    conceptual token once — expansion stays on the transcript side
    (see _is_anchored), mirroring search.py's own index-widens /
    query-stays-narrow asymmetry."""
    return set(
        _strip_contractions(_strip_stopwords(tokenize(_normalize_token_text(sentence))))
    )


def _alias_transcript_tokens(text: str) -> set[str]:
    """Transcript-side tokens for the alias-anchor rescue ONLY:
    identical pipeline to `_content_tokens` except the plural stemmer
    is off. The rescue reasons about SPELLING relations — substring and
    first-char-anchored subsequence — and those are surface properties:
    the stem 'cod' (from "Code") falls under `_ALIAS_MIN_TOKEN_LEN` and
    can no longer anchor 'vscode', flipping a documented pass into a
    flag. The ratio test keeps scoring on stemmed tokens; only the
    rescue compares surfaces."""
    return set(
        _strip_contractions(
            _strip_stopwords(
                _expand_kebab(_tokenize_unstemmed(_normalize_token_text(text)))
            )
        )
    )


def _alias_sentence_tokens(sentence: str) -> set[str]:
    """Sentence-side surface tokens for the alias-anchor rescue — the
    unstemmed twin of `_sentence_content_tokens` (no kebab expansion,
    same rationale)."""
    return set(
        _strip_contractions(
            _strip_stopwords(_tokenize_unstemmed(_normalize_token_text(sentence)))
        )
    )


def _is_anchored(token: str, transcript_tokens: set[str]) -> bool:
    """A sentence token is anchored when it appears in the transcript's
    expanded token set directly, or — for kebab/snake compounds — when
    any of its parts does, or when the dehyphenated join does. The join
    covers dotted-abbreviation folds whose camel split re-hyphenated
    them ("Ph.D." -> "PhD" -> "ph-d"): the parts ph/d never match a
    casual all-lowercase "phd" in the transcript, but the join does, so
    the verdict can't flip on the transcript's casing convention."""
    if token in transcript_tokens:
        return True
    if "-" in token or "_" in token:
        parts = [sub for sub in _KEBAB_SPLIT_RE.split(token) if sub]
        return (
            any(sub in transcript_tokens for sub in parts)
            or "".join(parts) in transcript_tokens
        )
    return False


def _is_subsequence(needle: str, haystack: str) -> bool:
    """True when `needle`'s characters appear in `haystack` in order,
    not necessarily contiguously: 'nvim' ⊑ 'neovim'. (Standard
    consume-the-iterator idiom — each `in` scan resumes where the
    previous one stopped.)"""
    haystack_iter = iter(haystack)
    return all(ch in haystack_iter for ch in needle)


def _is_alias_anchored(token: str, transcript_tokens: set[str]) -> bool:
    """Zero-anchor-rule rescue for alias/abbreviation spellings.

    Terse summary bodies routinely normalize a tool name to its
    canonical spelling while the transcript used the colloquial one:
    "Prefers Neovim." against a transcript that says "nvim".
    Exact-token anchoring calls that ZERO overlap, and at two content
    tokens a single spelling mismatch flips the whole verdict with no
    ratio cushion. So, for the zero-anchor rule ONLY, a sentence
    token also anchors when its spelling relates to a transcript
    token's: order the pair by length — anchored when the shorter (at
    least `_ALIAS_MIN_TOKEN_LEN` chars, so particles can't anchor) is
    a substring of the longer ("code" ⊂ "vscode", "postgres" ⊂
    "postgresql") or, sharing the longer's first character, a
    subsequence of it ("nvim" ⊑ "neovim", and "postgres" ⊑
    "postgre-sql" as the camel split respells PostgreSQL). The length
    ordering covers both directions: the short alias may sit on
    either side of the comparison.

    Deliberately NOT consulted by the ratio test — at three-plus
    content tokens there is cushion for one alias mismatch, and
    spelling-relation anchoring is loose enough (incidental
    containments like "rust" ⊂ "trust" anchor too) that applying it
    everywhere would erode the gate. Confined here, it can only turn
    a would-be flag of a tiny claim into a pass — the precision-first
    direction for this advisory gate.

    Both sides arrive UNSTEMMED (`_alias_sentence_tokens` /
    `_alias_transcript_tokens`): spelling relations are surface
    properties, and the plural stem 'cod' would fall under the length
    gate that keeps particles from anchoring.
    """
    for other in transcript_tokens:
        shorter, longer = sorted((token, other), key=len)
        if len(shorter) < _ALIAS_MIN_TOKEN_LEN:
            continue
        if shorter in longer:
            return True
        if shorter[0] == longer[0] and _is_subsequence(shorter, longer):
            return True
    return False


def check_groundedness(
    body: str,
    transcript: str,
    *,
    min_content_tokens: int = MIN_CONTENT_TOKENS,
    threshold: float = GROUNDEDNESS_THRESHOLD,
) -> list[UngroundedClaim]:
    """Walk `body` sentence-by-sentence and return any sentence whose
    content tokens have less than `threshold` overlap with the
    transcript's token set.

    Returns an empty list when every sentence is sufficiently grounded
    OR when the body has no sentences with enough content tokens to
    evaluate. Sentences below `min_content_tokens` are exempt from the
    ratio test but still flag when they carry at least
    `_ZERO_ANCHOR_MIN_TOKENS` content tokens and share ZERO anchors
    with the transcript — "Lives in Berlin." is a verifiable claim,
    not an "OK.". Anchoring for that zero-anchor rule alone is
    alias-tolerant ("Prefers Neovim." grounds against a transcript
    that says "nvim" — see `_is_alias_anchored`); the ratio test
    keeps exact-token anchoring. With an empty transcript nothing is
    grounded: every
    checkable sentence comes back as ungrounded (overlap_ratio=0.0),
    so the caller sees a clear signal that they passed an empty
    transcript.
    """
    if not body.strip():
        return []

    transcript_stripped = _SPEAKER_LABEL_RE.sub(" ", transcript)
    transcript_tokens = _content_tokens(transcript_stripped)
    # Surface-spelling twin for the alias rescue, built once — see
    # `_alias_transcript_tokens` for why the rescue can't run on stems.
    alias_transcript_tokens = _alias_transcript_tokens(transcript_stripped)
    sentences = _split_sentences(body)

    ungrounded: list[UngroundedClaim] = []
    for sentence in sentences:
        sentence_tokens = _sentence_content_tokens(sentence)
        n_tokens = len(sentence_tokens)
        if n_tokens == 0:
            continue
        matched = sum(1 for t in sentence_tokens if _is_anchored(t, transcript_tokens))
        if n_tokens < min_content_tokens:
            # Too short for the ratio test, but not too short to be a
            # verifiable claim: two-plus content tokens with ZERO
            # anchors is the canonical hallucination shape. One
            # anchor passes (conservative stance for tiny grounded
            # claims); trailing-colon fragments are section headers
            # ("Action items:"), not claims, and stay exempt. Down
            # here one spelling mismatch flips the whole verdict, so
            # the anchor test gets an alias-tolerant rescue (last
            # clause): "Prefers Neovim." must not flag against a
            # transcript that spelled it "nvim". The ratio branch
            # below stays exact-spelling on purpose — see
            # _is_alias_anchored for both rationales.
            if (
                n_tokens >= _ZERO_ANCHOR_MIN_TOKENS
                and matched == 0
                and not sentence.endswith(":")
                and not any(
                    _is_alias_anchored(t, alias_transcript_tokens)
                    for t in _alias_sentence_tokens(sentence)
                )
            ):
                ungrounded.append(UngroundedClaim(sentence=sentence, overlap_ratio=0.0))
            continue
        ratio = matched / n_tokens
        if ratio < threshold:
            ungrounded.append(UngroundedClaim(sentence=sentence, overlap_ratio=ratio))
    return ungrounded


__all__ = [
    "GROUNDEDNESS_THRESHOLD",
    "MIN_CONTENT_TOKENS",
    "UngroundedClaim",
    "check_groundedness",
]

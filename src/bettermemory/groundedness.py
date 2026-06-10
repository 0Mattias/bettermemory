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
  skipped (too short to evaluate; "OK." has no semantic anchor to
  check).
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
- Line-leading speaker labels ("User:", "Assistant:") are stripped
  from the transcript before tokenizing — they're formatting metadata,
  not conversation vocabulary, and left in place they donate freebie
  anchors to short fabricated claims about the user.

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

from .search import _KEBAB_SPLIT_RE, _expand_kebab, _strip_stopwords, tokenize


# Minimum number of content tokens a sentence needs before we evaluate
# its groundedness. Sentences shorter than this are skipped — there's
# nothing useful to check, and a short "OK." or "yes." should never be
# flagged as ungrounded.
MIN_CONTENT_TOKENS = 3

# Sentence-level overlap ratio required for "grounded". Below this, we
# flag the sentence as ungrounded. 0.30 is calibrated so that a sentence
# of 5 content tokens needs at least 2 of them in the transcript — low
# enough to allow paraphrase, high enough to catch sentences that share
# only generic vocabulary.
GROUNDEDNESS_THRESHOLD = 0.30


# Sentence splitter, three boundary shapes — still deliberately simple,
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
_SENTENCE_SPLIT_RE = re.compile(
    r"[.!?;][\"'”’)\]*_`]*\s+"
    r"|(?:\r?\n[^\S\n]*){2,}"
    r"|\r?\n(?=[^\S\n]*(?:[-*•]|\d+[.)])[^\S\n])"
)

# Line-leading speaker labels in the transcript ("User:", "Assistant:")
# are transcript formatting metadata, not conversation vocabulary —
# stripped from the transcript side only before tokenizing.
_SPEAKER_LABEL_RE = re.compile(r"(?im)^\s*(?:user|assistant|system|human)\s*:")

# Runs of 1-2-letter dotted abbreviations: "i.e." -> "ie", "Ph.D." ->
# "PhD", "U.S." -> "US". Letters only, so version numbers ("1.0.2") and
# plain sentence-final periods are untouched.
_DOTTED_ABBREV_RE = re.compile(r"\b(?:[A-Za-z]{1,2}\.){2,}")

# Intra-word apostrophes (ASCII and typographic U+2019): "doesn't"
# otherwise tokenizes as 'doesn' + 't', and those fragments cross-match
# any unrelated contraction in the transcript. Collapsed so the
# contraction stays one token on both sides.
_APOSTROPHE_RE = re.compile(r"(\w)['’](\w)")

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


def _content_tokens(text: str) -> set[str]:
    """Transcript-side tokens to compare against. Same pipeline as the
    dedup path — stopword-stripped, kebab-expanded, lowercased — plus
    the shared spelling normalization. Returns a set so repeated
    tokens within one side don't inflate overlap counts.
    """
    return set(_strip_stopwords(_expand_kebab(tokenize(_normalize_token_text(text)))))


def _sentence_content_tokens(sentence: str) -> set[str]:
    """Sentence-side tokens: same normalization, NO kebab expansion.
    The overlap ratio counts each conceptual token once — expansion
    stays on the transcript side (see _is_anchored), mirroring
    search.py's own index-widens / query-stays-narrow asymmetry."""
    return set(_strip_stopwords(tokenize(_normalize_token_text(sentence))))


def _is_anchored(token: str, transcript_tokens: set[str]) -> bool:
    """A sentence token is anchored when it appears in the transcript's
    expanded token set directly, or — for kebab/snake compounds — when
    any of its parts does."""
    if token in transcript_tokens:
        return True
    if "-" in token or "_" in token:
        return any(
            sub in transcript_tokens for sub in _KEBAB_SPLIT_RE.split(token) if sub
        )
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
    evaluate. An empty transcript triggers a "nothing is grounded"
    fast path: every checkable sentence comes back as ungrounded
    (overlap_ratio=0.0), so the caller sees a clear signal that they
    passed an empty transcript.
    """
    if not body.strip():
        return []

    transcript_tokens = _content_tokens(_SPEAKER_LABEL_RE.sub(" ", transcript))
    sentences = _split_sentences(body)

    ungrounded: list[UngroundedClaim] = []
    for sentence in sentences:
        sentence_tokens = _sentence_content_tokens(sentence)
        if len(sentence_tokens) < min_content_tokens:
            continue
        if not transcript_tokens:
            ungrounded.append(UngroundedClaim(sentence=sentence, overlap_ratio=0.0))
            continue
        matched = sum(1 for t in sentence_tokens if _is_anchored(t, transcript_tokens))
        ratio = matched / len(sentence_tokens)
        if ratio < threshold:
            ungrounded.append(UngroundedClaim(sentence=sentence, overlap_ratio=ratio))
    return ungrounded


__all__ = [
    "GROUNDEDNESS_THRESHOLD",
    "MIN_CONTENT_TOKENS",
    "UngroundedClaim",
    "check_groundedness",
]

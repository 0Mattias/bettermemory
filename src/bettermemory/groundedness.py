"""Write-time groundedness check (T1.3 of the v1.6 plan).

Operationalizes the HaluMem-style "is this proposed memory grounded in
the conversation that produced it?" check. The model passes the recent
turns of the conversation as `source_transcript`; we walk the proposed
body sentence-by-sentence and flag any sentence whose content tokens
have no meaningful overlap with the transcript.

The signal isn't perfect — short factual claims may share tokens with
unrelated transcript text, long sentences may legitimately introduce a
new word — but it catches the failure mode mem0's 97.8% junk audit
documented: models extracting "facts" from thin air. A grounded
sentence has *some* anchor in the conversation; a hallucinated one
typically has none.

Calibration:

- Sentences with fewer than `MIN_CONTENT_TOKENS` content tokens are
  skipped (too short to evaluate; "OK." has no semantic anchor to
  check).
- Sentences whose stopword-stripped, kebab-expanded token set overlaps
  the transcript's token set by at least `GROUNDEDNESS_THRESHOLD` of
  their own tokens are considered grounded. The asymmetry (sentence
  / |sentence|, not sentence ∩ transcript / sentence ∪ transcript) is
  deliberate: the transcript is typically much larger than any single
  sentence, so Jaccard would underestimate grounding for legitimate
  body text. We're measuring "is the sentence anchored?", not
  "do these texts cover the same ground?".

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

from .search import _expand_kebab, _strip_stopwords, tokenize


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


# Sentence splitter: period, exclamation, question, semicolon, double
# newline. We deliberately keep this simple — heavy NLP isn't worth the
# false sense of precision. A sentence that ends with an abbreviation
# ("e.g.") may split on the dot, but the resulting fragments still get
# checked independently and a real claim will still match the transcript.
_SENTENCE_SPLIT_RE = re.compile(r"[.!?;]\s+|\n\n+")


@dataclass(frozen=True)
class UngroundedClaim:
    """One sentence from the proposed body that didn't anchor to the
    transcript. `sentence` is the verbatim text (trimmed but not
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


def _split_sentences(body: str) -> list[str]:
    """Split a body into sentences. Naive — period / exclamation /
    question / semicolon / paragraph break — but stable: a sentence
    that genuinely shares no tokens with the transcript will still
    not share any after splitting differently."""
    parts = _SENTENCE_SPLIT_RE.split(body)
    return [p.strip() for p in parts if p.strip()]


def _content_tokens(text: str) -> set[str]:
    """Tokens to compare against. Same pipeline as the dedup path —
    stopword-stripped, kebab-expanded, lowercased. Returns a set so
    repeated tokens within one side don't inflate overlap counts.
    """
    return set(_strip_stopwords(_expand_kebab(tokenize(text))))


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

    transcript_tokens = _content_tokens(transcript)
    sentences = _split_sentences(body)

    ungrounded: list[UngroundedClaim] = []
    for sentence in sentences:
        sentence_tokens = _content_tokens(sentence)
        if len(sentence_tokens) < min_content_tokens:
            continue
        if not transcript_tokens:
            ungrounded.append(UngroundedClaim(sentence=sentence, overlap_ratio=0.0))
            continue
        overlap = sentence_tokens & transcript_tokens
        ratio = len(overlap) / len(sentence_tokens)
        if ratio < threshold:
            ungrounded.append(UngroundedClaim(sentence=sentence, overlap_ratio=ratio))
    return ungrounded


__all__ = [
    "GROUNDEDNESS_THRESHOLD",
    "MIN_CONTENT_TOKENS",
    "UngroundedClaim",
    "check_groundedness",
]

"""Unit tests for the groundedness checker (T1.3 of the v1.6 plan).

Covers the pure-function surface — sentence splitting, content-token
extraction, overlap-ratio thresholding, and the edge cases (empty
transcript, short sentences, paraphrase tolerance). The server-level
end-to-end is in test_server_groundedness.py.
"""

from __future__ import annotations

from bettermemory.groundedness import (
    GROUNDEDNESS_THRESHOLD,
    MIN_CONTENT_TOKENS,
    UngroundedClaim,
    check_groundedness,
)


def test_empty_body_returns_empty_list() -> None:
    """A body that's empty or whitespace-only has no claims to evaluate.
    Return an empty list, not an error."""
    assert check_groundedness("", "any transcript") == []
    assert check_groundedness("   \n  ", "any transcript") == []


def test_grounded_sentence_passes() -> None:
    """A sentence whose tokens overlap the transcript above the
    threshold should NOT be flagged. This is the no-warning happy
    path — most legitimate writes."""
    transcript = (
        "User: I prefer terse code-driven explanations over prose "
        "paragraphs. Just show me the code."
    )
    body = "The user prefers terse code-driven explanations."
    assert check_groundedness(body, transcript) == []


def test_ungrounded_sentence_flagged() -> None:
    """A sentence with no anchor in the transcript should come back
    flagged. This is the failure mode the gate exists to catch:
    facts pulled from thin air."""
    transcript = "User: I prefer terse code-driven explanations."
    body = "The user lives in Tokyo and owns three cats."
    ungrounded = check_groundedness(body, transcript)
    assert len(ungrounded) == 1
    assert "Tokyo" in ungrounded[0].sentence
    assert ungrounded[0].overlap_ratio < GROUNDEDNESS_THRESHOLD


def test_short_sentences_skipped() -> None:
    """Sentences with fewer than MIN_CONTENT_TOKENS content tokens are
    skipped — no semantic anchor to evaluate. "OK." or "Yes that's
    right." shouldn't fire the gate."""
    assert MIN_CONTENT_TOKENS >= 2  # sanity check
    transcript = "User: any text at all"
    body = "OK. Yes. No. Done."  # all very short
    assert check_groundedness(body, transcript) == []


def test_empty_transcript_flags_everything() -> None:
    """Calling with an empty transcript and a non-trivial body should
    flag every checkable sentence as ungrounded — the gate has no
    signal, surfaces that clearly rather than silently passing."""
    body = "The user prefers terse explanations. Edge cases matter."
    ungrounded = check_groundedness(body, "")
    assert len(ungrounded) == 2
    assert all(c.overlap_ratio == 0.0 for c in ungrounded)


def test_multiple_sentences_evaluated_independently() -> None:
    """Each sentence is evaluated against the transcript on its own.
    A body that mixes a grounded sentence with an ungrounded one
    flags only the ungrounded one."""
    transcript = "User: terse code-driven explanations please."
    body = (
        "The user prefers terse code-driven explanations. "
        "Also their favourite colour is purple-orange."
    )
    ungrounded = check_groundedness(body, transcript)
    assert len(ungrounded) == 1
    assert "purple-orange" in ungrounded[0].sentence


def test_paraphrase_with_shared_keywords_grounds() -> None:
    """The threshold tolerates moderate paraphrase as long as the
    load-bearing keywords are shared. We're catching hallucinations,
    not penalising legitimate rewording."""
    transcript = "User: I want terse explanations."
    body = "The user wants terse explanations from now on."
    assert check_groundedness(body, transcript) == []


def test_to_dict_round_trips() -> None:
    """The to_dict() serialiser must produce a JSON-safe dict for the
    MCP wire surface. overlap_ratio is rounded to 3 decimal places to
    keep the response stable across float precision."""
    claim = UngroundedClaim(sentence="something", overlap_ratio=0.123456789)
    data = claim.to_dict()
    assert data == {"sentence": "something", "overlap_ratio": 0.123}


def test_threshold_parameter_overrides_default() -> None:
    """Custom thresholds let callers tune the gate's sensitivity. A
    very low threshold (0.05) should pass content the default would
    flag; a very high threshold (0.95) should flag content the default
    would pass."""
    transcript = "user prefers terse output"
    body = "The user prefers terse output style consistently in all replies."

    # At the default threshold the sentence is grounded.
    assert check_groundedness(body, transcript) == []

    # Raised threshold: same sentence now flagged.
    ungrounded_strict = check_groundedness(body, transcript, threshold=0.95)
    assert len(ungrounded_strict) == 1


def test_sentence_split_on_semicolon_and_paragraph() -> None:
    """Sentence splitting handles period, exclamation, question,
    semicolon, and paragraph break. Each fragment is evaluated
    independently so a body that uses semicolons can't smuggle an
    ungrounded clause inside a grounded sentence."""
    transcript = "I prefer terse explanations."
    body = "The user prefers terse explanations; also enjoys baking sourdough bread."
    ungrounded = check_groundedness(body, transcript)
    # The baking-sourdough clause should fire as ungrounded.
    assert any("sourdough" in c.sentence for c in ungrounded)

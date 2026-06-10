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


def test_speaker_labels_are_not_anchors() -> None:
    """Line-leading 'User:' / 'Assistant:' labels are transcript
    formatting metadata, not conversation vocabulary. They must not
    donate the token 'user' to a short fabricated claim about the
    user — the exact thin-air-extraction shape the gate exists for."""
    transcript = (
        "User: I prefer terse code-driven explanations.\n"
        "Assistant: Got it, terse it is."
    )
    body = "The user works at Google."
    ungrounded = check_groundedness(body, transcript)
    assert len(ungrounded) == 1
    assert ungrounded[0].overlap_ratio == 0.0


def test_bullet_list_items_evaluated_independently() -> None:
    """Single-newline markdown bullets are separate fragments. A
    hallucinated bullet can't hide behind grounded siblings by
    pooling the whole list into one token set."""
    transcript = (
        "User: I prefer terse replies and code-first examples. "
        "Assistant: Noted - terse replies, code-first examples."
    )
    body = (
        "Preferences:\n"
        "- terse replies\n"
        "- code-first examples\n"
        "- lives in Tokyo with three cats"
    )
    ungrounded = check_groundedness(body, transcript)
    assert len(ungrounded) == 1
    assert "Tokyo" in ungrounded[0].sentence


def test_ie_restatement_clause_not_isolated() -> None:
    """'i.e.' / 'e.g.' are not sentence boundaries. The restatement
    clause after 'i.e.' is by construction a vocabulary-shifted
    rewording — isolated, it can never anchor; pooled with its
    sentence, the grounded whole passes."""
    transcript = (
        "User: don't run the test suite against production - "
        "use the staging database, it's read-only anyway."
    )
    body = "Use the staging DB (read-only) for tests, i.e. never point tests at prod."
    assert check_groundedness(body, transcript) == []


def test_iso_date_stamp_does_not_sink_grounded_fact() -> None:
    """An ISO date stamp counts as ONE unmatched token, not four —
    kebab expansion must not inflate the denominator and flag a body
    whose substantive tokens all anchor."""
    transcript = "User: lets switch to ruff, I prefer it to flake8. Assistant: done."
    body = "Prefers ruff over flake8 (decided 2026-06-09)."
    assert check_groundedness(body, transcript) == []


def test_contraction_fragments_are_not_anchors() -> None:
    """Apostrophe shrapnel ('doesn', 't') must not cross-match an
    unrelated contraction in the transcript and ground a fully
    hallucinated claim."""
    transcript = (
        "User: the build doesn't fail anymore after the cache fix. Assistant: great."
    )
    body = "Doesn't trust Kubernetes at all."
    ungrounded = check_groundedness(body, transcript)
    assert len(ungrounded) == 1
    assert "Kubernetes" in ungrounded[0].sentence
    # And a different contraction can't anchor via the shared 't'.
    assert len(check_groundedness("Can't stand Postgres databases.", transcript)) == 1


def test_contraction_matching_grounded_body_passes() -> None:
    """A grounded body that reuses the transcript's own contraction
    still passes — 'doesn't' matches 'doesn't' as one token."""
    transcript = (
        "User: the build doesn't fail anymore after the cache fix. Assistant: great."
    )
    body = "The build doesn't fail anymore."
    assert check_groundedness(body, transcript) == []


def test_camelcase_matches_prose_and_kebab_parity() -> None:
    """camelCase identifiers split at case boundaries so the kebab
    expansion covers them — the verdict must not flip on the
    identifier's casing convention."""
    transcript = (
        "User: please turn on format on save and set the tab size to 2 "
        "in my editor config. Assistant: done, updated your settings."
    )
    camel = "Wants formatOnSave enabled and tabSize 2."
    kebab = "Wants format-on-save enabled and tab-size 2."
    assert check_groundedness(camel, transcript) == []
    assert check_groundedness(kebab, transcript) == []


def test_dotted_degree_abbreviation_grounds() -> None:
    """'Ph.D.' neither splits the sentence mid-claim nor shatters into
    'ph' + 'd' junk tokens — the dotted spelling grounds against the
    transcript's undotted spelling."""
    transcript = (
        "User: I have a PhD in computational physics from KTH. Assistant: noted."
    )
    body = "The user holds a Ph.D. in computational physics."
    assert check_groundedness(body, transcript) == []


def test_period_inside_quote_still_splits() -> None:
    """Terminal punctuation wrapped in a closing quote is still a
    sentence boundary — a hallucinated follower can't merge into a
    grounded quoted sentence and pass on its overlap."""
    transcript = (
        "User: one rule, deploy only on Fridays, never midweek. "
        "Assistant: understood, I will only deploy on Fridays."
    )
    body = (
        'User insists "deploy only on Fridays." Their staging cluster runs on Hetzner.'
    )
    ungrounded = check_groundedness(body, transcript)
    assert len(ungrounded) == 1
    assert "Hetzner" in ungrounded[0].sentence


def test_numbered_list_header_not_flagged() -> None:
    """A numbered list splits at each item start — the next item's
    index can't glue onto a bare section header and push it over the
    MIN_CONTENT_TOKENS floor. A fully grounded checklist passes."""
    transcript = (
        "User: after each release, refresh the changelog and ping the team in Slack."
    )
    body = "Action items:\n1. Refresh the changelog\n2. Ping the team in Slack"
    assert check_groundedness(body, transcript) == []


def test_crlf_and_padded_blank_line_paragraph_breaks() -> None:
    """Paragraph breaks tolerate CRLF line endings and blank lines
    with trailing whitespace — the newline encoding must not flip
    the verdict on a fabricated paragraph."""
    transcript = (
        "User: let's use Postgres for the job queue. Assistant: agreed, Postgres it is."
    )
    for sep in ("\r\n\r\n", "\n \n"):
        body = f"Uses Postgres for the job queue{sep}Lives in Tokyo and owns three cats"
        ungrounded = check_groundedness(body, transcript)
        assert len(ungrounded) == 1
        assert "Tokyo" in ungrounded[0].sentence
        assert ungrounded[0].overlap_ratio == 0.0


def test_shared_compound_counted_once() -> None:
    """One genuinely shared kebab compound is one matched token, not
    three — hyphenated and unhyphenated anchors weigh identically, so
    an embellished claim flags the same either way."""
    transcript = "User: deps are pyyaml, python-frontmatter, and jinja2."
    hyphenated = "Uses python-frontmatter in the blog pipeline."
    plain = "Uses frontmatter in the blog pipeline."
    flagged_hyphen = check_groundedness(hyphenated, transcript)
    flagged_plain = check_groundedness(plain, transcript)
    assert len(flagged_hyphen) == 1
    assert len(flagged_plain) == 1
    assert flagged_hyphen[0].overlap_ratio == flagged_plain[0].overlap_ratio

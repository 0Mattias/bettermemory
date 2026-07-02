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


def test_midline_speaker_labels_are_not_anchors() -> None:
    """Speaker labels in space-separated-turn transcripts (turns
    joined on one line, no newlines) are still formatting metadata.
    The verdict must not flip on how the turns happen to be joined —
    a mid-line 'User:' must not donate the token 'user'."""
    spaced = (
        "Assistant: how should I format replies? "
        "User: I prefer terse code-driven explanations. "
        "Assistant: Got it, terse it is."
    )
    body = "The user works at Google."
    ungrounded = check_groundedness(body, spaced)
    assert len(ungrounded) == 1
    assert ungrounded[0].overlap_ratio == 0.0


def test_prose_colons_survive_label_stripping() -> None:
    """Only the four speaker words directly before a colon are
    stripped — ordinary 'heading: detail' prose keeps its vocabulary
    and keeps anchoring grounded claims."""
    transcript = "Deploy checklist: run migrations, then restart the worker pool."
    body = "Deploy checklist includes migrations and a worker restart."
    assert check_groundedness(body, transcript) == []


def test_two_token_hallucinated_claim_flagged() -> None:
    """'Lives in Berlin.' (audit's verbatim repro) sits below
    MIN_CONTENT_TOKENS but is a fully verifiable claim. With ZERO
    anchors in the transcript it must flag, not be silently
    skipped."""
    transcript = (
        "User: I prefer terse code-driven explanations.\n"
        "Assistant: Got it, terse it is."
    )
    ungrounded = check_groundedness("Lives in Berlin.", transcript)
    assert len(ungrounded) == 1
    assert ungrounded[0].overlap_ratio == 0.0


def test_two_token_grounded_claim_still_passes() -> None:
    """The conservative stance for legitimately tiny claims holds:
    a short claim with at least one real anchor stays unflagged,
    and one-content-token fragments stay skipped entirely — 'OK.'
    still never fires the gate."""
    transcript = "User: let's use Postgres for the job queue."
    assert check_groundedness("Uses Postgres.", transcript) == []
    # One content token: no semantic anchor to evaluate, still skipped.
    assert check_groundedness("Postgres.", "completely unrelated transcript") == []


def test_alias_spelling_grounds_short_claim() -> None:
    """The zero-anchor rule is alias-tolerant: a terse two-token claim
    whose tool name is spelled canonically must not flag when the
    transcript used the colloquial alias (audit's verbatim repros).
    Covers all three spelling relations: first-char-anchored
    subsequence ('nvim' ⊑ 'neovim'), plain substring ('code' ⊂
    'vscode'), and subsequence through the camelCase split
    ('postgres' ⊑ 'postgre-sql')."""
    assert (
        check_groundedness(
            "Prefers Neovim.", "User: I do all my editing in nvim these days."
        )
        == []
    )
    assert (
        check_groundedness(
            "Prefers PostgreSQL.", "User: let's use postgres for the job queue."
        )
        == []
    )
    assert (
        check_groundedness("Prefers VSCode.", "User: I do everything in VS Code now.")
        == []
    )


def test_alias_rescue_does_not_unflag_unanchored_claim() -> None:
    """The rescue is a spelling relation, not a free pass: a two-token
    claim sharing no substring/subsequence spelling with any
    transcript token still flags at 0.0 — and with an empty
    transcript there is nothing to alias-relate, so the
    empty-transcript signal is preserved too."""
    transcript = (
        "User: I prefer terse code-driven explanations.\n"
        "Assistant: Got it, terse it is."
    )
    ungrounded = check_groundedness("Owns a ferret.", transcript)
    assert len(ungrounded) == 1
    assert ungrounded[0].overlap_ratio == 0.0
    # Empty transcript: the rescue can't manufacture an anchor.
    assert len(check_groundedness("Prefers Neovim.", "")) == 1


def test_alias_rescue_scoped_to_zero_anchor_rule() -> None:
    """Alias tolerance exists ONLY below MIN_CONTENT_TOKENS, where one
    spelling mismatch flips the verdict. At three-plus content tokens
    the ratio branch keeps exact-spelling anchoring — an alias-only
    sentence whose other tokens have no transcript support still
    flags."""
    transcript = "User: these days I do everything in nvim."
    ungrounded = check_groundedness("Prefers Neovim for fast editing.", transcript)
    assert len(ungrounded) == 1
    assert ungrounded[0].overlap_ratio < GROUNDEDNESS_THRESHOLD


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
    hallucinated claim — and the *collapsed* auxiliary ('doesnt')
    must not survive as a freebie anchor either. The bodies are the
    audit's verbatim repros: neither 'trust' nor 'Kubernetes' appears
    anywhere in the transcript, so the claim must flag."""
    transcript = (
        "User: the build doesn't fail anymore after the cache fix. Assistant: great."
    )
    body = "Doesn't trust Kubernetes."
    ungrounded = check_groundedness(body, transcript)
    assert len(ungrounded) == 1
    assert "Kubernetes" in ungrounded[0].sentence
    assert ungrounded[0].overlap_ratio == 0.0
    # And a different contraction can't anchor via the shared 't'
    # (audit's second verbatim pairing: 'can' is a stopword, so the
    # old fragment 't' from the transcript's "don't" was the sole
    # anchor at 1/3 = 0.333).
    other = check_groundedness("Can't stand Postgres.", "User: I don't have time today")
    assert len(other) == 1


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


def test_dotted_abbreviation_grounds_against_lowercase_spelling() -> None:
    """The dotted-abbreviation fold re-hyphenates through the camel
    split ('Ph.D.' -> 'PhD' -> 'ph-d'), whose parts ph/d never match a
    casual all-lowercase 'phd' — the dehyphenated join anchors it, so
    the verdict doesn't flip on the transcript's casing convention."""
    transcript = "User: i got my phd at kth. Assistant: noted."
    body = "Holds a Ph.D."
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


# ---------------------------------------------------------------------------
# Tokenizer v2 — CJK segmentation and multilingual stopwords in the gate
# ---------------------------------------------------------------------------


def test_cjk_hallucinated_claim_flagged() -> None:
    """Audit repro ('CJK bodies bypass the gate entirely'): the exact
    fabricated claim from the finding — user lives in Tokyo, owns three
    cats — against a transcript about editor dark mode. Pre-v2 the
    whole sentence was 2 giant tokens, fell under MIN_CONTENT_TOKENS,
    and passed silently; bigram tokens put it through the ratio test,
    where it shares nothing and flags."""
    body = "用户住在东京，养了三只猫。"
    transcript = (
        "User: 我想把编辑器切换成深色模式。 Assistant: 好的，已经把主题改成深色了。"
    )
    flagged = check_groundedness(body, transcript)
    assert len(flagged) == 1
    assert flagged[0].overlap_ratio == 0.0


def test_cjk_grounded_paraphrase_passes() -> None:
    """The finding's other direction: a grounded CJK restatement used to
    require a character-identical clause to count as anchored. Bigram
    overlap grounds a normal paraphrase."""
    transcript = "User: 部署时间表改了吗？ Assistant: 是的，部署时间表改到每周五下午。"
    body = "部署时间表是每周五下午。"
    assert check_groundedness(body, transcript) == []


def test_fullwidth_terminator_splits_sentences() -> None:
    """The splitter treats 。！？； as sentence boundaries WITHOUT
    requiring trailing whitespace (CJK prose has none), so a
    hallucinated second sentence can't hide behind a grounded first."""
    transcript = "User: 部署时间表改到每周五下午。"
    body = "部署时间表是每周五下午。用户养了三只猫。"
    flagged = check_groundedness(body, transcript)
    assert len(flagged) == 1
    assert "三只猫" in flagged[0].sentence


def test_swedish_hallucination_no_longer_grounds_on_function_words() -> None:
    """Audit repro ('stopword defense is English-only'): a fabricated
    Swedish claim used to clear the 30% bar purely on {vill, att, på}
    against any Swedish transcript. Those are stopwords now, so the
    claim's real content tokens carry the ratio — and they anchor
    nowhere."""
    body = "Användaren vill att alla möten ska bokas på fredagar."
    transcript = (
        "User: Jag vill att temat ska vara mörkt på kvällen. "
        "Assistant: Klart, mörkt tema på kvällen."
    )
    flagged = check_groundedness(body, transcript)
    assert len(flagged) == 1


def test_swedish_grounded_claim_passes() -> None:
    """Symmetry check for the test above: a Swedish claim that IS
    anchored in the transcript's content vocabulary still passes once
    the filler is stripped."""
    transcript = (
        "User: Jag vill att temat ska vara mörkt på kvällen. "
        "Assistant: Klart, mörkt tema på kvällen."
    )
    body = "Mörkt tema på kvällen."
    assert check_groundedness(body, transcript) == []

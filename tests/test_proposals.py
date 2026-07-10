"""Tests for the write-reflex proposal queue + heuristic extractor.

Covers extraction precision (what gets proposed vs rejected), the
on-disk queue round-trip + flock-protected mutation, and the
enqueue/dedup/cap behaviour of `propose_from_exchange`. The MCP review
surface (`memory_proposals`) and the Stop-hook wiring are exercised in
`test_server.py` / `test_hook.py`.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bettermemory.proposals import (
    Proposal,
    ProposalQueue,
    extract_proposals,
    propose_from_exchange,
)


_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# extract_proposals — precision
# ---------------------------------------------------------------------------


def test_extract_catches_first_person_preference() -> None:
    props = extract_proposals(
        "I prefer terse code-driven explanations over long prose paragraphs.",
        now=_NOW,
    )
    assert len(props) == 1
    assert props[0].suggested_category == "user-inference"
    assert "terse code-driven" in props[0].body
    assert props[0].source_excerpt == props[0].body


def test_extract_catches_explicit_remember_request_even_if_command_shaped() -> None:
    # Opens like a request to the assistant ("can you"), but the explicit
    # "remember that" marker overrides the question/command reject.
    props = extract_proposals(
        "Can you remember that we deploy to fly.io for all production releases?",
        now=_NOW,
    )
    assert len(props) == 1
    assert props[0].suggested_category == "fact"


def test_extract_catches_my_setup_fact() -> None:
    props = extract_proposals(
        "My editor is neovim with a heavily customised lua config.",
        now=_NOW,
    )
    assert len(props) == 1
    assert props[0].suggested_category == "user-inference"


def test_extract_rejects_questions() -> None:
    assert (
        extract_proposals("What is the best database for this workload?", now=_NOW)
        == []
    )


def test_extract_rejects_task_requests_to_the_assistant() -> None:
    assert (
        extract_proposals("Could you refactor the auth module for me here?", now=_NOW)
        == []
    )


def test_extract_rejects_transient_state() -> None:
    # Matches a preference pattern but trips a transient marker → not durable.
    assert (
        extract_proposals(
            "I prefer to currently run everything against the staging cluster.",
            now=_NOW,
        )
        == []
    )


def test_extract_rejects_sentences_without_a_durable_marker() -> None:
    assert (
        extract_proposals("The weather today is quite pleasant outside.", now=_NOW)
        == []
    )


def test_extract_rejects_too_short() -> None:
    assert extract_proposals("I like it.", now=_NOW) == []


def test_extract_handles_empty_and_none() -> None:
    assert extract_proposals(None, now=_NOW) == []
    assert extract_proposals("   ", now=_NOW) == []


def test_extract_caps_at_max_proposals() -> None:
    text = (
        "I prefer dark mode for every editor I use. "
        "I always run the linter before committing my code. "
        "We use postgres for the primary datastore everywhere. "
        "My shell of choice is zsh with starship configured."
    )
    props = extract_proposals(text, now=_NOW, max_proposals=2)
    assert len(props) == 2


def test_extract_rejects_mid_sentence_possessive_task_requests() -> None:
    # Bare "my "/"our " used to fire anywhere in the sentence, turning
    # imperative task requests into user-inference proposals.
    assert (
        extract_proposals(
            "Fix the bug in my parser when the input has trailing whitespace.",
            now=_NOW,
        )
        == []
    )
    assert (
        extract_proposals(
            "Refactor our error handling so the workers retry with backoff.",
            now=_NOW,
        )
        == []
    )


def test_extract_rejects_possessive_opener_without_stative_verb() -> None:
    # Pasted third-party bug-report prose: sentence-initial "My" but an
    # event verb, not a stative setup claim about the user.
    assert (
        extract_proposals(
            "My app crashes whenever I rotate the screen on Android 14.",
            now=_NOW,
        )
        == []
    )


def test_extract_rejects_first_person_delegations() -> None:
    # "I want/need YOU to …" is a task request to the assistant
    # (contract (c)), not a preference about the user.
    assert (
        extract_proposals(
            "I want you to refactor this function to use early returns.", now=_NOW
        )
        == []
    )
    assert (
        extract_proposals(
            "I need you to look at the failing CI job on the main branch.", now=_NOW
        )
        == []
    )


def test_extract_rejects_bulleted_task_requests() -> None:
    # Markdown bullet prefixes used to defeat the ^-anchored
    # question/command reject, queuing task lists verbatim (with "- ").
    text = (
        "A few things for this PR:\n"
        "- Can you wire up my new config flag to the CLI parser\n"
        "- Could you also silence the deprecation warning in our test suite"
    )
    assert extract_proposals(text, now=_NOW) == []


def test_extract_catches_preference_after_sentence_adverb() -> None:
    # "However,"/"Whenever" used to prefix-match the unbounded wh-words in
    # the question reject, silently dropping canonical preferences.
    for text in (
        "However, I prefer tabs over spaces for indentation in Makefiles.",
        "Whenever possible, I prefer small focused PRs over big-bang merges.",
    ):
        props = extract_proposals(text, now=_NOW)
        assert len(props) == 1
        assert props[0].suggested_category == "user-inference"


def test_extract_rejects_retrieval_questions() -> None:
    # "Do/Did you remember …?" asks what is already stored — the explicit
    # marker override must not launder it into a fact proposal (and
    # "remember i" must not prefix-match "remember if").
    assert (
        extract_proposals(
            "Do you remember if we already migrated the staging database to "
            "postgres 16?",
            now=_NOW,
        )
        == []
    )
    assert (
        extract_proposals(
            "Did you remember that we deploy from the release branch every Friday?",
            now=_NOW,
        )
        == []
    )


def test_extract_rejects_per_turn_instruction_connectives() -> None:
    # "Make sure to/you" and "Note that" are ubiquitous per-turn
    # instructions, not remember-intent — dropped from the marker list.
    for text in (
        "Make sure to run the full test suite before you commit anything here.",
        "Note that the handler gets called twice when the websocket reconnects.",
        "Please make sure you handle the empty-list case in that refactor.",
    ):
        assert extract_proposals(text, now=_NOW) == []


def test_extract_catches_short_explicit_capture_request() -> None:
    # The full length floor must not defeat an explicit capture
    # instruction; a degenerate fragment is still rejected.
    props = extract_proposals("Remember that I use zsh.", now=_NOW)
    assert len(props) == 1
    assert props[0].suggested_category == "fact"
    assert extract_proposals("Remember that.", now=_NOW) == []


def test_extract_catches_please_remember_colon() -> None:
    # The explicit "remember:" marker overrides the ^please command reject
    # — politeness must not invert the outcome.
    props = extract_proposals(
        "Please remember: I use poetry for dependency management, never pip directly.",
        now=_NOW,
    )
    assert len(props) == 1
    assert props[0].suggested_category == "fact"


def test_extract_catches_remember_this() -> None:
    props = extract_proposals(
        "Remember this: the staging database lives on port 5433 inside the VPN.",
        now=_NOW,
    )
    assert len(props) == 1
    assert props[0].suggested_category == "fact"


def test_extract_catches_remember_this_short_colon_form() -> None:
    # The content after the colon clears the non-marker content bar, so
    # the explicit exemption holds even though "I deploy" matches no
    # preference pattern — the deictic reject must not overreach onto
    # content-bearing "remember this" forms.
    props = extract_proposals("Remember this: I deploy on Fridays.", now=_NOW)
    assert len(props) == 1
    assert props[0].suggested_category == "fact"


def test_extract_rejects_contentless_deictic_remember_requests() -> None:
    # The deictic "remember this" family points at content OUTSIDE the
    # sentence, so the queued body would be the bare request itself —
    # zero durable content. Below the non-marker content bar the sentence
    # loses its explicit exemption (demoted, not hard-dropped) and the
    # normal length floor / question-command gates reject it.
    for text in (
        "Can you remember this?",
        "Please remember this one.",
    ):
        assert extract_proposals(text, now=_NOW) == []


def test_extract_trailing_marker_keeps_explicit_status() -> None:
    # Non-marker content is counted sentence-wide, not just after the
    # marker, so a trailing "keep in mind" keeps explicit (fact) status.
    # Trailing-only counting would demote this to the "I always"
    # preference branch and flip the category to user-inference.
    props = extract_proposals("I always deploy on Fridays, keep in mind.", now=_NOW)
    assert len(props) == 1
    assert props[0].suggested_category == "fact"


def test_extract_joins_hard_wrapped_sentences() -> None:
    # A single mid-sentence newline (hard wrap) must not truncate the
    # proposed body at the wrap point.
    props = extract_proposals(
        "I prefer using rebase-and-merge for all feature branches because it\n"
        "keeps the history linear and makes bisect actually usable.",
        now=_NOW,
    )
    assert len(props) == 1
    assert "bisect actually usable" in props[0].body


def test_extract_keeps_eg_sentence_intact() -> None:
    # "e.g." mid-sentence must not split (and thereby drop) the preference.
    props = extract_proposals(
        "I prefer conventional commits, e.g. feat: and fix: prefixes, in every repo.",
        now=_NOW,
    )
    assert len(props) == 1
    assert "every repo" in props[0].body


def test_extract_still_splits_blank_line_statements() -> None:
    # Guard against over-unwrapping: a blank line is a real boundary.
    props = extract_proposals(
        "I prefer dark mode in every editor for late-night work.\n\n"
        "We use postgres for the primary datastore in every service.",
        now=_NOW,
    )
    assert len(props) == 2


def test_extract_rejects_for_the_future_roadmap_prose() -> None:
    # The bare "for the future" marker fired on deferral/roadmap remarks
    # and laundered them past the let's/please command reject.
    for text in (
        "Let's leave sharding for the future and ship the single-node version.",
        "The dashboard rewrite is planned for the future once the API stabilizes.",
    ):
        assert extract_proposals(text, now=_NOW) == []


def test_extract_rejects_past_tense_narration() -> None:
    # Past-tense forms must not fire the present-tense preference branch.
    for text in (
        "I wanted to ask whether we should pin the Docker base image in CI.",
        "We used a workaround for the missing index until upstream fixed it.",
        "I liked the old dashboard layout better before the redesign shipped.",
        "I needed a workaround for the broken locale detection in the parser.",
    ):
        assert extract_proposals(text, now=_NOW) == []


def test_extract_catches_uncontracted_progressive_setup() -> None:
    # "I am using" / "We are using" (uncontracted copulas) are the same
    # canonical setup facts as "I'm using" / "We're using".
    for text in (
        "I am using Colima instead of Docker Desktop for container workloads.",
        "We are using Terraform Cloud for all infrastructure state management.",
    ):
        props = extract_proposals(text, now=_NOW)
        assert len(props) == 1
        assert props[0].suggested_category == "user-inference"


def test_extract_still_rejects_we_need_task_framing() -> None:
    # The we-branch deliberately does NOT get want/need — "We need to fix
    # …" is task framing, not a setup fact.
    assert (
        extract_proposals(
            "We need to fix the flaky test before cutting the release.", now=_NOW
        )
        == []
    )


def test_extract_rejects_sentence_initial_conditionals() -> None:
    # A hypothetical protasis describes a scenario, not a durable fact.
    for text in (
        "If I use the staging database for this test, we need to reseed it afterwards.",
        "If we use Redis for the cache, the memory footprint roughly doubles.",
        "Suppose I always run the migration first, then the seeding step "
        "would not fail.",
    ):
        assert extract_proposals(text, now=_NOW) == []


def test_extract_catches_mid_sentence_conditional() -> None:
    # The hypothetical reject is sentence-initial only — a trailing
    # condition on a real preference still proposes.
    props = extract_proposals("I always run black if the file is Python.", now=_NOW)
    assert len(props) == 1
    assert props[0].suggested_category == "user-inference"


def test_extract_rejects_negated_contraction_questions() -> None:
    # Negated-contraction openers (and "?!" terminals) are questions even
    # without a plain trailing "?".
    for text in (
        "Shouldn't I use the staging key for this deploy",
        "Wouldn't it be simpler if I use sqlite for the integration tests",
        "Couldn't we avoid the extra roundtrip by caching the token",
        "Didn't I tell you I prefer rebase merges over merge commits?!",
    ):
        assert extract_proposals(text, now=_NOW) == []


def test_extract_mid_sentence_negation_preference_untouched() -> None:
    # Precision pin for the negated-contraction rejects: they are
    # ^-anchored QUESTION OPENERS, so a genuine negative preference whose
    # "don't" sits mid-sentence proposes exactly as it did before the
    # alternatives were added.
    props = extract_proposals(
        "I prefer podman and I don't use Docker anymore.", now=_NOW
    )
    assert len(props) == 1
    assert props[0].suggested_category == "user-inference"
    # The bare negated declaration never matched the preference branch
    # (precision-over-recall: _PREFERENCE_RE has no negation alternatives)
    # — pinned so the reject extension can't be blamed for the miss.
    assert extract_proposals("I don't use Docker anymore.", now=_NOW) == []


def test_extract_dont_forget_still_proposes() -> None:
    # "Don't forget …" carries the explicit marker, which overrides the
    # new don'?t question/command alternative.
    props = extract_proposals(
        "Don't forget that we deploy to fly.io for production releases.",
        now=_NOW,
    )
    assert len(props) == 1
    assert props[0].suggested_category == "fact"


# ---------------------------------------------------------------------------
# extract_proposals — smart-quote (U+2018/U+2019) normalization
#
# macOS/iOS keyboards substitute typographic apostrophes by default, so
# contractions arrive as U+2019 ("I’m", "Let’s", "Don’t"). Every
# contraction-aware pattern in the extractor is written against the ASCII
# apostrophe; without early normalization the smart spelling silently
# inverted outcomes in BOTH directions (preferences/markers missed,
# question/command rejects bypassed). Each case below changes behaviour
# with normalization — its smart-quote outcome differs from the
# pre-normalization one while matching its ASCII twin.
# ---------------------------------------------------------------------------


def test_extract_catches_smart_quote_progressive_setup() -> None:
    # "I’m using …" (U+2019) must hit the same preference branch as the
    # ASCII "I'm using …" — pre-normalization it matched nothing at all.
    for text in (
        "I’m using ripgrep for all file searches in this repo.",
        "We’re using Terraform Cloud for all infrastructure state management.",
    ):
        props = extract_proposals(text, now=_NOW)
        assert len(props) == 1
        assert props[0].suggested_category == "user-inference"
    # Normalization happens before extraction, so the proposed body carries
    # the ASCII spelling (one canonical form for dedup-by-source_excerpt).
    props = extract_proposals(
        "I’m using ripgrep for all file searches in this repo.", now=_NOW
    )
    assert "I'm using" in props[0].body


def test_extract_catches_smart_quote_explicit_marker() -> None:
    # "Don’t forget …" (U+2019) must fire the "don't forget" explicit
    # marker exactly like its ASCII twin (test_extract_dont_forget_still_
    # proposes) — pre-normalization the capture request was dropped.
    props = extract_proposals(
        "Don’t forget that we deploy to fly.io for production releases.",
        now=_NOW,
    )
    assert len(props) == 1
    assert props[0].suggested_category == "fact"


def test_extract_rejects_smart_quote_command_like_ascii_twin() -> None:
    # "Let’s …" must be rejected by the let'?s command alternative exactly
    # like "Let's …". The sentence carries "we always", so the preference
    # branch fires and only the command reject stands between it and a
    # proposal — pre-normalization the curly apostrophe slipped past
    # let'?s and the task request was captured as user-inference.
    smart = "Let’s make sure we always deploy from the release branch."
    ascii_twin = "Let's make sure we always deploy from the release branch."
    assert extract_proposals(ascii_twin, now=_NOW) == []
    assert extract_proposals(smart, now=_NOW) == []


def test_extract_rejects_smart_quote_negated_contraction_question() -> None:
    # A negated-contraction question with NO terminal "?" — the trailing-?
    # reject can't help, so only the shouldn'?t alternative rejects it.
    # The ASCII twin is rejected (pinned by test_extract_rejects_negated_
    # contraction_questions); the U+2019 spelling must match it instead of
    # being captured via the "I always" preference branch.
    smart = "Shouldn’t I always run the formatter before committing here"
    ascii_twin = "Shouldn't I always run the formatter before committing here"
    assert extract_proposals(ascii_twin, now=_NOW) == []
    assert extract_proposals(smart, now=_NOW) == []


# ---------------------------------------------------------------------------
# extract_proposals — explicit-drop telemetry at the transient gate
# ---------------------------------------------------------------------------


def test_explicit_transient_drop_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An explicit "remember…" request whose body trips the transient gate
    is still dropped (no exemption — parity with memory_write's
    acknowledge_transient bar) but the drop must be observable at WARNING:
    the production caller chain (Stop hook → hook.main →
    propose_from_exchange) never configures logging, and Python's
    lastResort handler only emits WARNING and above, so anything quieter
    is a production no-op."""
    with caplog.at_level(logging.WARNING, logger="bettermemory.proposals"):
        props = extract_proposals(
            "Remember that we are currently deploying from the hotfix branch.",
            now=_NOW,
        )
    assert props == []
    records = [r for r in caplog.records if r.name == "bettermemory.proposals"]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    message = records[0].getMessage()
    # The tripped marker name and the dropped excerpt are both in the record.
    assert "'currently'" in message
    assert "Remember that we are currently deploying" in message


def test_non_explicit_transient_drop_stays_silent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Ordinary transient prose (no explicit marker) trips this gate
    # constantly — it must be dropped without logging at ANY level, or the
    # Stop hook's stderr fills with noise.
    with caplog.at_level(logging.DEBUG, logger="bettermemory.proposals"):
        props = extract_proposals(
            "I prefer to currently run everything against the staging cluster.",
            now=_NOW,
        )
    assert props == []
    assert [r for r in caplog.records if r.name == "bettermemory.proposals"] == []


# ---------------------------------------------------------------------------
# extract_proposals — credential gate at capture
#
# The write-reflex mines RAW user text, so a secret-shaped sentence would be
# captured verbatim into `.write_proposals.jsonl` — a plain-text queue that
# `sync push` could carry across hosts (the queue is now gitignored, but this
# is the defense-in-depth layer that keeps the secret off disk in the first
# place). `extract_proposals` runs `find_credential_markers` and DROPS a
# matching sentence at capture, WARNING-logged with the detector KIND only —
# never the sentence or the value.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sentence",
    [
        # Explicit-marker capture (the exact audit repro).
        "Remember that my staging DB password is Xk92mQz7Lp4R9t.",
        # Marker-less first-person setup form — captured via _PREFERENCE_RE.
        "My staging DB password is Xk92mQz7Lp4R9t.",
    ],
)
def test_extract_drops_credential_bearing_sentence_at_capture(
    sentence: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A secret-shaped sentence must be DROPPED at capture, never queued.
    Without this gate the write-reflex writes "my staging DB password is
    <secret>" verbatim into the proposal queue — a plain-text, historically
    sync-pushed file. The drop is WARNING-logged so a swallowed capture stays
    observable, but the log names the detector KIND only: neither the sentence
    nor the raw secret value may reach the logs.

    Mutation-sound: remove the `find_credential_markers` gate from
    `extract_proposals` and BOTH halves fail — the sentence is captured
    (`props` non-empty) and no credential WARNING is emitted."""
    secret = "Xk92mQz7Lp4R9t"  # synthetic test fixture, not a live secret
    with caplog.at_level(logging.WARNING, logger="bettermemory.proposals"):
        props = extract_proposals(sentence, now=_NOW)
    # Dropped — nothing captured.
    assert props == []
    # Observable at WARNING, detector kind named.
    records = [r for r in caplog.records if r.name == "bettermemory.proposals"]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    message = records[0].getMessage()
    assert "generic-secret-assignment" in message
    # The secret value never reaches the log record (kind-only discipline) —
    # not the formatted message, not any interpolated arg in caplog.text.
    assert secret not in message
    assert secret not in caplog.text


def test_extract_still_captures_clean_preference_alongside_credential_gate() -> None:
    """Precision guard: the credential gate must not suppress a perfectly
    durable, secret-free preference. A false positive here would train the
    model to ignore the capture surface."""
    props = extract_proposals(
        "I prefer terse code-driven explanations over long prose paragraphs.",
        now=_NOW,
    )
    assert len(props) == 1
    assert props[0].suggested_category == "user-inference"


# ---------------------------------------------------------------------------
# ProposalQueue — persistence
# ---------------------------------------------------------------------------


def _proposal(body: str, *, pid: str = "01J0", cat: str = "fact") -> Proposal:
    return Proposal(
        id=pid,
        body=body,
        source_excerpt=body,
        suggested_category=cat,
        created=_NOW.isoformat(),
    )


def test_queue_empty_when_no_file(tmp_path: Path) -> None:
    assert ProposalQueue(tmp_path).load() == []


def test_queue_append_and_load_round_trip(tmp_path: Path) -> None:
    q = ProposalQueue(tmp_path)
    q.append(
        [
            _proposal("first body here", pid="a1"),
            _proposal("second body here", pid="a2"),
        ]
    )
    loaded = q.load()
    assert [p.id for p in loaded] == ["a1", "a2"]
    assert loaded[0].body == "first body here"
    # 0o600 on the queue file (carries the user's words — same privacy bar).
    assert (tmp_path / ".write_proposals.jsonl").exists()


def test_queue_append_empty_is_noop(tmp_path: Path) -> None:
    q = ProposalQueue(tmp_path)
    q.append([])
    assert not (tmp_path / ".write_proposals.jsonl").exists()


def test_queue_remove_returns_and_drops(tmp_path: Path) -> None:
    q = ProposalQueue(tmp_path)
    q.append(
        [_proposal("alpha body text", pid="a1"), _proposal("beta body text", pid="a2")]
    )
    removed = q.remove("a1")
    assert removed is not None and removed.id == "a1"
    assert [p.id for p in q.load()] == ["a2"]


def test_queue_remove_unknown_is_none(tmp_path: Path) -> None:
    q = ProposalQueue(tmp_path)
    q.append([_proposal("alpha body text", pid="a1")])
    assert q.remove("nope") is None
    assert len(q.load()) == 1


def test_queue_skips_malformed_lines(tmp_path: Path) -> None:
    path = tmp_path / ".write_proposals.jsonl"
    good = _proposal("good body text", pid="g1")
    import json

    path.write_text(
        "not json at all\n"
        + json.dumps(good.to_dict())
        + "\n"
        + '{"missing": "id and body"}\n',
        encoding="utf-8",
    )
    loaded = ProposalQueue(tmp_path).load()
    assert [p.id for p in loaded] == ["g1"]


# ---------------------------------------------------------------------------
# propose_from_exchange — enqueue / dedup / cap
# ---------------------------------------------------------------------------


def test_propose_from_exchange_enqueues_new(tmp_path: Path) -> None:
    fresh = propose_from_exchange(
        tmp_path,
        user_text="I prefer hands-on tutorials with runnable code, not screenshots.",
        now=_NOW,
    )
    assert len(fresh) == 1
    assert len(ProposalQueue(tmp_path).load()) == 1


def test_propose_from_exchange_dedups_against_queue(tmp_path: Path) -> None:
    text = "I prefer hands-on tutorials with runnable code, not screenshots."
    propose_from_exchange(tmp_path, user_text=text, now=_NOW)
    # Same sentence again → nothing new appended (dedup by source_excerpt).
    again = propose_from_exchange(tmp_path, user_text=text, now=_NOW)
    assert again == []
    assert len(ProposalQueue(tmp_path).load()) == 1


def test_propose_from_exchange_queued_preamble_does_not_mask_new(
    tmp_path: Path,
) -> None:
    """Already-queued sentences must not occupy the per-exchange extraction
    slots: a recurring 3-sentence preamble used to exhaust max_proposals
    every turn, so a novel durable statement later in the message was never
    even extracted (then queue dedup dropped the 3 repeats → net nothing)."""
    preamble = (
        "I prefer concise answers with runnable code over long prose. "
        "I always run mypy and ruff before committing anything here. "
        "We use postgres for the primary datastore in every service."
    )
    first = propose_from_exchange(tmp_path, user_text=preamble, now=_NOW)
    assert len(first) == 3
    fresh = propose_from_exchange(
        tmp_path,
        user_text=preamble
        + " Also, I never want force-pushes on shared branches in this org.",
        now=_NOW,
    )
    assert [p.body for p in fresh] == [
        "Also, I never want force-pushes on shared branches in this org."
    ]
    assert len(ProposalQueue(tmp_path).load()) == 4


def test_propose_from_exchange_respects_max_pending(tmp_path: Path) -> None:
    q = ProposalQueue(tmp_path)
    q.append([_proposal("already queued body", pid="x1")])
    fresh = propose_from_exchange(
        tmp_path,
        user_text="I always squash my commits before opening a pull request.",
        max_pending=1,  # queue already full
        now=_NOW,
    )
    assert fresh == []
    assert len(q.load()) == 1


# ---------------------------------------------------------------------------
# append_within_cap — cap + dedup enforced under the lock (TOCTOU guard)
# ---------------------------------------------------------------------------


def test_append_within_cap_enforces_room_and_dedup(tmp_path: Path) -> None:
    """The cap and the source_excerpt dedup are computed against the
    under-lock snapshot, not a stale pre-lock read — so a batch larger than
    the remaining room is trimmed and queue-duplicates are dropped."""
    q = ProposalQueue(tmp_path)
    q.append([_proposal("existing one", pid="e1")])
    appended = q.append_within_cap(
        [
            _proposal("existing one", pid="dup"),  # dups e1 by excerpt → dropped
            _proposal("brand new two", pid="n2"),
            _proposal("brand new three", pid="n3"),  # over the room of 1 → trimmed
        ],
        max_pending=2,
    )
    assert [p.id for p in appended] == ["n2"]
    assert [p.id for p in q.load()] == ["e1", "n2"]


def test_append_within_cap_dedups_within_the_batch(tmp_path: Path) -> None:
    """Two candidates in ONE batch sharing a source_excerpt (a user repeating
    the same durable sentence twice in one exchange) collapse to a single
    queued proposal. The prior version only deduped against the existing
    queue, so a verbatim repeat double-queued — contradicting the method's
    'can't double-queue the same sentence' docstring."""
    q = ProposalQueue(tmp_path)
    appended = q.append_within_cap(
        [
            _proposal("i always run the linter before committing", pid="r1"),
            _proposal("i always run the linter before committing", pid="r2"),
        ],
        max_pending=20,
    )
    assert [p.id for p in appended] == ["r1"]
    assert [p.id for p in q.load()] == ["r1"]


def test_append_within_cap_returns_empty_when_full(tmp_path: Path) -> None:
    """A full queue admits nothing and leaves the file untouched."""
    q = ProposalQueue(tmp_path)
    q.append([_proposal("a body", pid="a1"), _proposal("b body", pid="b1")])
    assert q.append_within_cap([_proposal("c body", pid="c1")], max_pending=2) == []
    assert [p.id for p in q.load()] == ["a1", "b1"]


def test_append_within_cap_empty_candidates_is_noop(tmp_path: Path) -> None:
    q = ProposalQueue(tmp_path)
    assert q.append_within_cap([], max_pending=5) == []
    assert not (tmp_path / ".write_proposals.jsonl").exists()


# ---------------------------------------------------------------------------
# accept_proposal — credential gate (parity with the memory_write path)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("token", "kind"),
    [
        ("sk-ant-api03-A1bcDefGh2iJkLmNoPqRsTuV", "openai-anthropic-key"),
        ("AKIAIOSFODNN7EXAMPLE", "aws-access-key-id"),
        ("ghp_0123456789abcdefghijklmnopqrstuvwxyz", "github-token"),
    ],
)
def test_accept_proposal_refuses_credential_body(
    tmp_path: Path, token: str, kind: str
) -> None:
    """A proposal whose body embeds a secret-shaped token must be REFUSED on
    accept — the write-reflex captures raw user text, so the accept path is
    another door onto the plain-text (sync'd) store, and it must run the same
    `find_credential_markers` gate `CredentialGate` runs FIRST on memory_write.
    The refusal raises BEFORE the atomic claim, so the proposal stays queued
    (retry contract) and no `.md` is persisted; the error names the detector
    kind, never the value."""
    from bettermemory.config import Config, StorageConfig
    from bettermemory.events import Recorder
    from bettermemory.handlers.proposals import accept_proposal
    from bettermemory.store import Store

    body = f"The deploy uses a secret {token} that got pasted into chat."
    q = ProposalQueue(tmp_path)
    q.append([_proposal(body, pid="c1")])
    config = Config(storage=StorageConfig(directory=str(tmp_path)))
    store = Store(tmp_path)
    recorder = Recorder(root=tmp_path, session_id="sess_test")

    with pytest.raises(ValueError, match=kind) as excinfo:
        accept_proposal(
            store=store,
            config=config,
            recorder=recorder,
            proposal_id="c1",
            scopes=["infrastructure"],
        )

    # Nothing persisted, and the proposal is still queued for the caller to
    # edit or dismiss. (No `.md` file — the durable write never ran.)
    assert list(tmp_path.glob("*.md")) == []
    assert store.load_all() == []
    assert [p.id for p in q.load()] == ["c1"]
    # The error names the detector kind but never echoes the raw secret span.
    assert token not in str(excinfo.value)
    # No event on a refusal — the accept record fires only when the write
    # actually lands (accept_proposal docstring step 6).
    assert not (tmp_path / ".events.jsonl").exists()


def test_accept_proposal_acknowledge_credential_bypasses_refusal(
    tmp_path: Path,
) -> None:
    """The credential gate must expose the SAME `acknowledge_credential=True`
    escape hatch as memory_write / memory_update: a credential-bearing proposal
    is refused by default, but ACCEPTED (written durably) when the caller
    acknowledges it (a proposal that DESCRIBES a documented public/example
    credential pattern). Mutation-sound: drop the parameter (revert the gate to
    an unconditional refuse) and this test fails on the accept half."""
    from bettermemory.config import Config, StorageConfig
    from bettermemory.events import Recorder, iter_events
    from bettermemory.handlers.proposals import accept_proposal
    from bettermemory.store import Store

    token = "AKIAIOSFODNN7EXAMPLE"
    body = f"AWS access-key ids look like {token} — a documented example shape."
    q = ProposalQueue(tmp_path)
    q.append([_proposal(body, pid="ack1")])
    config = Config(storage=StorageConfig(directory=str(tmp_path)))
    store = Store(tmp_path)
    recorder = Recorder(root=tmp_path, session_id="sess_test")

    # Default: refused, proposal stays queued, nothing written.
    with pytest.raises(ValueError, match="aws-access-key-id"):
        accept_proposal(
            store=store,
            config=config,
            recorder=recorder,
            proposal_id="ack1",
            scopes=["infrastructure"],
        )
    assert store.load_all() == []
    assert [p.id for p in q.load()] == ["ack1"]

    # Escape hatch: acknowledged → the durable write lands and the proposal
    # is claimed out of the queue.
    result = accept_proposal(
        store=store,
        config=config,
        recorder=recorder,
        proposal_id="ack1",
        scopes=["infrastructure"],
        acknowledge_credential=True,
    )
    assert result["status"] == "accepted"
    written = store.load_all()
    assert len(written) == 1
    assert written[0].body.strip() == body
    assert q.load() == []
    # The forced override is observable in the result (detector kind only,
    # never the value) — auditability parity with memory_write.
    # Mutation-sound: drop the field and this fails.
    assert result["credentials_acknowledged"] == ["aws-access-key-id"]
    # And the CORE recorded the accept event itself — the single choke point
    # every surface (MCP tool, CLI) shares, so no entry point can accept with
    # acknowledged credentials without the override landing in the audit log.
    # Exactly ONE event (the refusal above recorded nothing); detector kind
    # only, the secret value never reaches the log. Mutation-sound: move the
    # recording back out to the MCP handler and this fails — accept_proposal
    # alone no longer logs.
    accept_events = [
        e
        for e in iter_events(tmp_path)
        if e["kind"] == "memory_proposals" and e.get("action") == "accept"
    ]
    assert len(accept_events) == 1
    assert accept_events[0]["proposal_id"] == "ack1"
    assert accept_events[0]["credentials_acknowledged"] == ["aws-access-key-id"]
    assert token not in (tmp_path / ".events.jsonl").read_text(encoding="utf-8")


def test_cli_proposals_accept_acknowledge_credential_flag(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """`bettermemory proposals accept --acknowledge-credential` — the CLI
    spelling of the escape hatch, exercised through the REAL argparse
    boundary (`bettermemory.server.main()` with a mocked argv), not the
    accept core directly. The escape hatch shipped dead at this surface
    once: the core supported acknowledge_credential, but the CLI had no
    flag, and its refusal message told the user to pass a parameter the
    CLI could not express.

    Pins all three halves: (1) without the flag the accept is refused
    (clean exit 2, proposal still queued, nothing written, value redacted);
    (2) with --acknowledge-credential the write lands and the queue is
    claimed; (3) the forced override is recorded in the audit log from the
    CLI surface too — exactly once, detector kind only — because
    `accept_proposal` records it at the shared choke point.

    Storage is seeded via BETTERMEMORY_DIR + `resolved_directory()` (the
    test_cli_smoke pattern) so the test store and the CLI-resolved store are
    the same realpath on macOS (/var vs /private/var)."""
    import sys as _sys

    from bettermemory.config import load_config
    from bettermemory.events import iter_events
    from bettermemory.server import main as cli_main
    from bettermemory.store import Store

    monkeypatch.setenv("BETTERMEMORY_DIR", str(tmp_path))
    store = Store(load_config().resolved_directory())
    token = "AKIAIOSFODNN7EXAMPLE"
    body = f"AWS access-key ids look like {token} — a documented example shape."
    ProposalQueue(store.root).append([_proposal(body, pid="cliack1")])

    def run_cli(*argv: str) -> None:
        monkeypatch.setattr(_sys, "argv", ["bettermemory", *argv])
        cli_main()

    # (1) Without the flag: parser.error -> exit 2; the message names the
    # detector kind AND the CLI flag spelling, never the secret value; the
    # proposal stays queued and nothing is written.
    with pytest.raises(SystemExit) as exc:
        run_cli("proposals", "accept", "cliack1", "--scope", "infrastructure")
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "aws-access-key-id" in err
    assert "--acknowledge-credential" in err
    assert token not in err
    assert [p.id for p in ProposalQueue(store.root).load()] == ["cliack1"]
    assert store.load_all() == []

    # (2) With the flag: accepted, written, queue claimed.
    run_cli(
        "proposals",
        "accept",
        "cliack1",
        "--scope",
        "infrastructure",
        "--acknowledge-credential",
    )
    assert "Accepted" in capsys.readouterr().out
    assert ProposalQueue(store.root).load() == []
    written = store.load_all()
    assert len(written) == 1
    assert written[0].body.strip() == body

    # (3) The CLI surface logged the forced override — exactly once, kind
    # only, value never in the log. Before the recording moved into the
    # accept core, the CLI path recorded nothing at all.
    accept_events = [
        e
        for e in iter_events(store.root)
        if e["kind"] == "memory_proposals" and e.get("action") == "accept"
    ]
    assert len(accept_events) == 1
    assert accept_events[0]["proposal_id"] == "cliack1"
    assert accept_events[0]["credentials_acknowledged"] == ["aws-access-key-id"]
    assert token not in (store.root / ".events.jsonl").read_text(encoding="utf-8")

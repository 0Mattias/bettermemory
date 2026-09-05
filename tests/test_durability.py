"""Unit tests for durability.py — the transient-marker detector."""

from __future__ import annotations

import pytest

from bettermemory.durability import (
    SHA_MARKER,
    TRANSIENT_PHRASE_MARKERS,
    canonical_marker,
    find_transient_markers,
)


# ---------------------------------------------------------------------------
# Negative cases — durable bodies should never trip the check
# ---------------------------------------------------------------------------


def test_empty_body_no_markers() -> None:
    assert find_transient_markers("") == []


def test_durable_body_no_markers() -> None:
    body = (
        "The auth service uses JWT with rotating refresh tokens. The refresh "
        "token TTL is 14 days; access tokens are 5 minutes."
    )
    assert find_transient_markers(body) == []


def test_word_boundary_currently_in_concurrently() -> None:
    """`currently` mustn't fire inside `concurrently` — distinct word."""
    body = "The lock manager handles concurrently-issued requests fairly."
    hits = find_transient_markers(body)
    assert all(h.marker != "currently" for h in hits)


def test_word_boundary_new_in_news() -> None:
    """`the new` mustn't fire inside `the news`."""
    body = "Skim the news feed once a week to catch breaking changes."
    hits = find_transient_markers(body)
    assert all(h.marker != "the new" for h in hits)


@pytest.mark.parametrize(
    "body",
    [
        # The referential majority: 66 of the 79 firing bodies in the
        # dogfood store looked like these.
        "Look at commit a1b2c3d for the change.",
        "The fix landed in 68aff13; the tag v3.15.1 points at it.",
        "A3 Predictive Intelligence (commit 0e63d2b) introduced the engine.",
        f"Pinned to commit {'a' * 40} for the refactor.",
        # git-describe output — the retired companion pattern's shape.
        "The deployed build is v3.7.1-5-g874b0b0, two commits past the tag.",
        "The image is tagged 2.4.0-12-ge9a3f1c in the registry.",
        # A machine-written ledger field: structurally unfixable by
        # rephrasing, which is the shape _TITLECASE_SKIP_MARKERS already
        # documents as disqualifying for a marker.
        "<!-- audit-loop-state v3 --> last_audited_sha: e3e4ba5 empty_ticks: 0",
        # Hex-shaped identifiers that are not commits at all — the
        # incidental class the retired detector also blocked.
        "The machine-id " + "0123456789abcdef" * 2 + " identifies the host.",
        "Restic snapshot id 57edeb37 holds the pre-crash state.",
        "The prod KMS key is 1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d (eu-north-1).",
    ],
)
def test_commit_citation_is_not_transient(body: str) -> None:
    """A commit SHA is an immutable, verifiable anchor — not state.

    The write gate must not treat one as transient. This is the behaviour
    change the SHA detector's retirement bought, and the guard against any
    future hex-shaped detector quietly reintroducing it: 87% of that
    detector's firings on the live store were citations like these, and
    45 of its 47 blocks were overridden.
    """
    assert find_transient_markers(body) == []


@pytest.mark.parametrize(
    "body",
    [
        "The API changed as of 2.7.0; see the migration notes.",
        "As of Python 3.12 the tokenizer is faster.",
    ],
)
def test_version_pinned_as_of_does_not_fire(body: str) -> None:
    """Bare 'as of' is deliberately not a marker — version-pinned forms
    are durable. Only the dated/time-of-writing variants fire."""
    assert find_transient_markers(body) == []


@pytest.mark.parametrize(
    "body",
    [
        "User works out of the New York office; meetings default to ET.",
        "User has a print subscription to The New Yorker.",
        "Check the New Relic dashboard for latency budgets.",
    ],
)
def test_the_new_proper_noun_does_not_fire(body: str) -> None:
    """'the New <X>' with a capital N is a proper noun, not a new-thing
    reference — the marker requires lowercase 'new'."""
    hits = find_transient_markers(body)
    assert all(h.marker != "the new" for h in hits)


def test_pushed_alone_does_not_fire_unpushed() -> None:
    """The word boundary keeps 'unpushed' from matching inside 'pushed'."""
    body = "The pushed commits are reviewed in batches."
    assert find_transient_markers(body) == []


@pytest.mark.parametrize(
    "body",
    [
        "Tracks work on a GitHub Projects board with Todo, In Progress, "
        "and Done columns.",
        "Remember that I keep active tasks in the In Progress column.",
    ],
)
def test_in_progress_kanban_column_does_not_fire(body: str) -> None:
    """Kanban column names ('In Progress') are durable board descriptions —
    the same rationale that keeps 'wip' off the list. Only the copula
    state-report forms ('is/are in progress') are markers."""
    assert find_transient_markers(body) == []


@pytest.mark.parametrize(
    "body",
    [
        "The deploy script refuses to run when there are uncommitted changes.",
        "Running git clean -fd deletes untracked files for good; dry-run "
        "with -n first.",
        "Use git stash --include-untracked so untracked files survive the stash.",
    ],
)
def test_working_tree_tool_behavior_does_not_fire(body: str) -> None:
    """Durable tool-behavior facts (deploy guards, git-clean caveats, stash
    policies) use the bare phrases — the same dual-use profile as the
    deliberately-absent 'dirty working tree'. The existential guard
    conditional ('when there ARE uncommitted changes') is why that marker
    anchors on has/have only."""
    assert find_transient_markers(body) == []


@pytest.mark.parametrize(
    "body",
    [
        "The rate limiter temporarily blocks an IP after 10 failed login attempts.",
        "fail2ban temporarily bans hosts that fail SSH auth five times.",
        "The CDN temporarily caches 404 responses for sixty seconds.",
    ],
)
def test_temporarily_habitual_behavior_does_not_fire(body: str) -> None:
    """'temporarily <verb>s' is the habitual present tense — designed,
    recurring system behavior, a primary durable write category. Unlike
    'currently', deleting the word flips the meaning (temporary ->
    permanent), so every fire here would train an acknowledge_transient
    rubber-stamp."""
    assert find_transient_markers(body) == []


@pytest.mark.parametrize(
    "body",
    [
        "Prefers not to be pinged in the middle of a focus block.",
        "I prefer doing code review in the middle of the day.",
        "Likes a long walk in the middle of the evening.",
        "The installer always reboots halfway through; this is expected.",
    ],
)
def test_in_flight_idiom_without_work_object_does_not_fire(body: str) -> None:
    """Temporal-generic idiom uses ('middle of a focus block', 'reboots
    halfway through') are durable preferences/behavior — the markers only
    fire with an in-flight-work object (bare gerund or work noun).
    'the evening' guards the gerund shape against time-of-day -ing nouns."""
    assert find_transient_markers(body) == []


@pytest.mark.parametrize(
    "body",
    [
        "Access tokens are refreshed at the moment of expiry, not on a timer.",
        "The lease is re-checked at the moment when it renews.",
    ],
)
def test_at_the_moment_event_trigger_does_not_fire(body: str) -> None:
    """'at the moment of/when <event>' describes durable event-driven
    behavior, never the now-sense — those heads are suppressed."""
    hits = find_transient_markers(body)
    assert all(h.marker != "at the moment" for h in hits)


@pytest.mark.parametrize(
    "body",
    [
        "The cache evicts the least-recently-used entry first.",
        "The Recently Viewed panel lists the last ten memories.",
    ],
)
def test_recently_fixed_terms_do_not_fire(body: str) -> None:
    """Bare 'recently' is deliberately not a marker — only the narrow
    aux+recently / recently+action-verb bigrams fire."""
    assert find_transient_markers(body) == []


def test_time_word_domain_name_does_not_fire() -> None:
    """'tomorrow.io'-class vendor domains are durable infra facts; the
    (?!\\.\\w) lookahead keeps them silent at zero recall cost."""
    body = (
        "Weather data in the dashboard comes from the tomorrow.io API; "
        "the key lives in 1Password."
    )
    hits = find_transient_markers(body)
    assert all(h.marker != "tomorrow" for h in hits)


@pytest.mark.parametrize(
    ("body", "marker"),
    [
        ("User's editor color scheme is Tomorrow Night Bright.", "tomorrow"),
        ("User subscribes to This Week in Rust for ecosystem news.", "this week"),
    ],
)
def test_time_word_title_case_name_does_not_fire(body: str, marker: str) -> None:
    """Proper nouns built on time words ('Tomorrow Night', 'This Week in
    Rust') are durable facts where the name IS the content."""
    hits = find_transient_markers(body)
    assert all(h.marker != marker for h in hits)


@pytest.mark.parametrize(
    "body",
    [
        "Today's metrics dashboard splits by region.",
        "Today’s standup notes live in the wiki.",  # curly apostrophe.
    ],
)
def test_today_possessive_does_not_fire(body: str) -> None:
    """The possessive ('today's date') is the dominant durable use of the
    word and is excluded from the bare-'today' marker."""
    hits = find_transient_markers(body)
    assert all(h.marker != "today" for h in hits)


# ---------------------------------------------------------------------------
# Positive cases — every marker phrase fires
# ---------------------------------------------------------------------------


# The in-flight idioms only fire with a following in-flight-work object
# (see _PATTERN_OVERRIDES) — the generic objectless template below is
# exactly the durable shape they must NOT match, so they get a
# representative transient body instead.
_OBJECT_ANCHORED_BODIES: dict[str, str] = {
    "in the middle of": "Some context, in the middle of migrating the database.",
    "halfway through": "Some context, halfway through the migration, and more.",
}


@pytest.mark.parametrize("phrase", TRANSIENT_PHRASE_MARKERS)
def test_each_phrase_marker_fires(phrase: str) -> None:
    body = _OBJECT_ANCHORED_BODIES.get(
        phrase, f"Some context, {phrase} and more context after."
    )
    hits = find_transient_markers(body)
    assert any(h.marker == phrase for h in hits), (
        f"expected marker {phrase!r} to fire, got {[h.marker for h in hits]}"
    )


def test_phrase_match_is_case_insensitive() -> None:
    body = "CURRENTLY the database is Postgres."
    hits = find_transient_markers(body)
    assert any(h.marker == "currently" for h in hits)


def test_a_commit_citation_adds_no_marker_to_a_body_that_already_fires() -> None:
    """The hex contributes nothing even when the body IS transient.

    "main is at <sha>" is the one class the retired detector legitimately
    caught. What still blocks that body is the hedging word around the
    hash, not the hash — which is why removing the hex detector cost so
    little, and where the residual coverage for branch pointers now lives.
    """
    body = "Currently main is at 68aff13."
    assert {h.marker for h in find_transient_markers(body)} == {"currently"}


def test_sha_marker_name_is_read_side_only() -> None:
    """`SHA_MARKER` is an archive key, not a marker: no producer, live fold.

    Deleting the constant and its fold would look like tidying orphaned
    code and would silently shatter 92 historical events across 54 rows,
    making the override rate that justified the retirement unreproducible
    from the store.
    """
    body = (
        "Shipped across a1b2c3d, 68aff13, 9431b4d, 58a4fa4 and f581121 "
        "over the release window."
    )
    hits = find_transient_markers(body)
    assert not any(h.marker.startswith("sha:") for h in hits)

    # ...but the read-side fold still folds.
    assert canonical_marker("sha:874b0b0") == SHA_MARKER


@pytest.mark.parametrize(
    "body",
    [
        "Today, I migrated the repo to uv; lockfile not regenerated yet.",
        "Today, we cut over DNS to the secondary host.",
    ],
)
def test_fronted_comma_today_fires(body: str) -> None:
    """'Today, I ...' — the grammatically standard fronted-comma form —
    carries the same time-of-writing transience as 'Today I ...'."""
    hits = find_transient_markers(body)
    assert any(h.marker == "today" for h in hits)


@pytest.mark.parametrize(
    "body",
    [
        "Merged the auth refactor to main today.",
        "Earlier today the staging deploy broke on the cert renewal.",
        "The user said today that the search latency feels fine.",
    ],
)
def test_bare_today_fires_in_any_position(body: str) -> None:
    """Trailing/medial 'today' is the natural assistant phrasing when
    summarizing completed work — covered by the bare-'today' marker."""
    hits = find_transient_markers(body)
    assert any(h.marker == "today" for h in hits)


def test_as_of_iso_date_fires() -> None:
    """A dated state snapshot is the canonical transient body; bucketed
    under one 'as of <date>' marker like the SHA loop."""
    body = "As of 2026-06-09 the staging cluster is on k8s 1.29."
    hits = find_transient_markers(body)
    assert any(h.marker == "as of <date>" for h in hits)


def test_scheduled_for_next_week_fires() -> None:
    body = "The Postgres 16 migration is scheduled for next week."
    hits = find_transient_markers(body)
    assert any(h.marker == "next week" for h in hits)


def test_plural_now_use_fires() -> None:
    """Plural subjects conjugate to 'use'/'rely' — same staleness as the
    third-person-singular 'now uses'."""
    body = "We now use uv instead of pip for all dependency management."
    hits = find_transient_markers(body)
    assert any(h.marker == "now use" for h in hits)


def test_unpushed_noun_phrase_fires() -> None:
    """Bare 'unpushed' catches the noun-phrase word order, not just the
    copula form 'is unpushed'."""
    body = "Branch audit-fixes has three unpushed commits with the gate work."
    hits = find_transient_markers(body)
    assert any(h.marker == "unpushed" for h in hits)


@pytest.mark.parametrize(
    ("body", "marker"),
    [
        (
            "The migration from REST to gRPC is in progress; auth is not cut over.",
            "is in progress",
        ),
        ("Two schema refactors are in progress across the repo.", "are in progress"),
    ],
)
def test_in_progress_copula_state_fires(body: str, marker: str) -> None:
    """The copula forms are the genuine state reports — anchoring on them
    loses nothing while keeping kanban column names silent."""
    hits = find_transient_markers(body)
    assert any(h.marker == marker for h in hits)


@pytest.mark.parametrize(
    ("body", "marker"),
    [
        (
            "The bettermemory checkout has uncommitted changes to server.py.",
            "has uncommitted changes",
        ),
        (
            "The branch has uncommitted changes after the hotfix.",
            "has uncommitted changes",
        ),
        (
            "There are untracked files under scripts/ that never got added.",
            "are untracked files",
        ),
        ("The fix is stashed, not committed.", "is stashed"),
    ],
)
def test_working_tree_state_fires(body: str, marker: str) -> None:
    """Working-tree state mutates on the next git command — strictly more
    volatile than the push-distance vocabulary already covered. The
    copula-anchored forms keep these genuine repo-state reports firing."""
    hits = find_transient_markers(body)
    assert any(h.marker == marker for h in hits)


@pytest.mark.parametrize(
    "body",
    [
        "We use Postgres at the moment.",
        "At the moment, the team prefers pnpm.",
        "At the moment the plan is to ship v2.",  # ambiguous head keeps firing.
    ],
)
def test_at_the_moment_now_sense_still_fires(body: str) -> None:
    """Guards the of/when/that lookahead against widening silently."""
    hits = find_transient_markers(body)
    assert any(h.marker == "at the moment" for h in hits)


@pytest.mark.parametrize(
    ("body", "marker"),
    [
        ("The team recently switched from yarn to pnpm.", "recently switched"),
        ("The default branch was recently renamed to main.", "was recently"),
    ],
)
def test_recent_action_bigrams_fire(body: str, marker: str) -> None:
    hits = find_transient_markers(body)
    assert any(h.marker == marker for h in hits)


def test_temporarily_fires() -> None:
    """The body self-declares its transience — the strongest signal."""
    body = (
        "The staging environment is temporarily pointed at the prod read "
        "replica until the migration completes."
    )
    hits = find_transient_markers(body)
    assert any(h.marker == "temporarily" for h in hits)


@pytest.mark.parametrize(
    "body",
    [
        "Temporarily disabled the nightly cron job while we debug the deploy.",
        "We're temporarily using the old endpoint until the gateway ships.",
    ],
)
def test_temporarily_non_habitual_still_fires(body: str) -> None:
    """Guards the habitual-form lookahead against widening: past,
    progressive, and imperative 'temporarily' keep firing — only the
    present-tense third-person-singular shape is exempt."""
    hits = find_transient_markers(body)
    assert any(h.marker == "temporarily" for h in hits)


@pytest.mark.parametrize(
    ("body", "marker"),
    [
        (
            "We are in the middle of migrating the database to Postgres 16.",
            "in the middle of",
        ),
        ("The team is in the middle of a migration off MySQL.", "in the middle of"),
        (
            "We are halfway through the migration; auth still reads the old table.",
            "halfway through",
        ),
        (
            "Halfway through rewriting the parser to drop the backtracking.",
            "halfway through",
        ),
    ],
)
def test_in_flight_idiom_with_work_object_fires(body: str, marker: str) -> None:
    """Both object shapes keep firing: the bare gerund ('migrating the
    database', 'rewriting the parser') and the articled work noun
    ('a migration', 'the migration')."""
    hits = find_transient_markers(body)
    assert any(h.marker == marker for h in hits)


@pytest.mark.parametrize(
    ("body", "marker"),
    [
        ("Deploy tomorrow.", "tomorrow"),  # sentence-final period.
        ("Tomorrow we ship the migration.", "tomorrow"),  # sentence-initial.
        ("Tomorrow I ship the migration.", "tomorrow"),  # pronoun-I follower.
        ("this week we are focusing on auth", "this week"),
    ],
)
def test_time_word_adverb_still_fires(body: str, marker: str) -> None:
    """Guards the domain-name lookahead and title-case skip against
    eating genuine time adverbs."""
    hits = find_transient_markers(body)
    assert any(h.marker == marker for h in hits)


@pytest.mark.parametrize(
    ("body", "marker"),
    [
        ("Deploy plan — Tomorrow we cut over DNS.", "tomorrow"),  # em-dash.
        ("Notes – Yesterday the build broke on CI.", "yesterday"),  # en-dash.
        ("🚀 Tomorrow we ship the migration.", "tomorrow"),  # emoji bullet.
    ],
)
def test_time_word_after_dash_or_emoji_bullet_fires(body: str, marker: str) -> None:
    """Em/en dashes and emoji bullets open a sentence like the ASCII
    bullets do — a capitalized time word after them is the adverb, not
    a mid-sentence proper noun."""
    hits = find_transient_markers(body)
    assert any(h.marker == marker for h in hits)


@pytest.mark.parametrize(
    "body",
    [
        "Theme list — Tomorrow Night Bright is installed.",
        "🎨 Tomorrow Night Bright is the theme.",
    ],
)
def test_title_case_name_after_dash_or_emoji_still_skips(body: str) -> None:
    """The title-case follower check still protects real names at the
    widened sentence openers."""
    hits = find_transient_markers(body)
    assert all(h.marker != "tomorrow" for h in hits)


@pytest.mark.parametrize(
    "body",
    [
        "The new schema replaces the old layout.",
        "Use the new auth flow for service tokens.",
    ],
)
def test_the_new_lowercase_still_fires(body: str) -> None:
    """Sentence-initial 'The new' and mid-sentence 'the new' both keep
    firing — only capital-N proper nouns are exempt."""
    hits = find_transient_markers(body)
    assert any(h.marker == "the new" for h in hits)


@pytest.mark.parametrize(
    "body",
    [
        # The three legitimate statements the integrity benchmark's write
        # path refused on this marker: each names the transition.
        "Mobile crash reporting switched to Bugsnag with the new SDK release; "
        "the mobile team now triages crashes there.",
        "The backend services were upgraded to Python 3.13 with the new base "
        "image; CI tests 3.13 only.",
        "Billing cut over to the billing-db-green cluster after the storage "
        "migration; the previous cluster is decommissioned and the service "
        "writes only to the new one.",
    ],
)
def test_the_new_anchored_by_a_named_transition_does_not_fire(body: str) -> None:
    """'the new X' beside a change cue and a concrete identifier in the same
    sentence is anchored: the reference still reads in a week."""
    hits = find_transient_markers(body)
    assert all(h.marker != "the new" for h in hits)


@pytest.mark.parametrize(
    "body",
    [
        # A cue with no identifier: nothing to anchor "new" to.
        "We switched to the new cluster last quarter.",
        "The team moved to the new tracker for on-call.",
        # An identifier with no cue: a description, not a transition.
        "The new Postgres 16 cluster is faster.",
        # A two-part hyphenated word is not an identifier.
        "We switched to the new read-only replica.",
        # The anchor sits in a different sentence.
        "Billing cut over to billing-db-green. The service writes only to the new one.",
    ],
)
def test_the_new_without_a_named_transition_still_fires(body: str) -> None:
    hits = find_transient_markers(body)
    assert any(h.marker == "the new" for h in hits)


# ---------------------------------------------------------------------------
# Deduplication and bucketing
# ---------------------------------------------------------------------------


def test_repeated_phrase_reported_once() -> None:
    """Same marker twice in one body collapses to one TransientMatch."""
    body = (
        "Currently the tests pass, and the build is currently fine. "
        "But currently we have no CI."
    )
    hits = find_transient_markers(body)
    currently_hits = [h for h in hits if h.marker == "currently"]
    assert len(currently_hits) == 1


def test_multiple_distinct_markers_each_reported() -> None:
    body = "Today I refactored the auth flow. Currently the tests pass."
    hits = find_transient_markers(body)
    markers = {h.marker for h in hits}
    assert "today" in markers
    assert "currently" in markers


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("sha:874b0b0", SHA_MARKER),  # pre-fix name, hash in the marker
        ("sha:1234567", SHA_MARKER),  # all-digit sha[:7] prefix
        (SHA_MARKER, SHA_MARKER),  # idempotent on the canonical name
        ("currently", "currently"),  # phrase markers pass through
        ("as of <date>", "as of <date>"),
        ("sha:874b0b", "sha:874b0b"),  # too short to be a sha[:7] name
        ("sha:zzzzzzz", "sha:zzzzzzz"),  # not hex
    ],
)
def test_canonical_marker_folds_only_legacy_sha_names(
    stored: str, expected: str
) -> None:
    """Read-side fold for events written before the marker was bucketed.

    The event log is append-only and never rewritten, so without this the
    pre-fix history stays shattered a row per commit forever.
    """
    assert canonical_marker(stored) == expected


# ---------------------------------------------------------------------------
# Snippet helper — error-message context
# ---------------------------------------------------------------------------


def test_snippet_includes_match_and_surrounding_context() -> None:
    body = (
        "The deployment pipeline is currently using GitHub Actions for the runner pool."
    )
    hits = find_transient_markers(body)
    currently = next(h for h in hits if h.marker == "currently")
    assert "currently" in currently.snippet.lower()


def test_snippet_collapses_whitespace_to_one_line() -> None:
    body = "Some\n\nlong\n\ncontext\n\ncurrently\n\nspans\n\nlines."
    hits = find_transient_markers(body)
    currently = next(h for h in hits if h.marker == "currently")
    assert "\n" not in currently.snippet


def test_snippet_uses_ellipses_when_truncated() -> None:
    body = ("a" * 100) + " currently the answer is " + ("b" * 100)
    hits = find_transient_markers(body)
    currently = next(h for h in hits if h.marker == "currently")
    assert currently.snippet.startswith("...")
    assert currently.snippet.endswith("...")


def test_snippet_no_leading_ellipsis_at_body_start() -> None:
    body = "Currently the answer is forty-two."
    hits = find_transient_markers(body)
    currently = next(h for h in hits if h.marker == "currently")
    assert not currently.snippet.startswith("...")

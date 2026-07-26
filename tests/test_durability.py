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


def test_six_char_hex_does_not_trigger_sha_marker() -> None:
    """SHA detection requires 7+ chars (git short-SHA default)."""
    body = "The colour value abc123 is blue."  # 6 chars.
    assert find_transient_markers(body) == []


@pytest.mark.parametrize(
    "number",
    [
        "1700000000",  # Unix epoch (10 digits).
        "1234567",  # smallest 7-digit run.
        "8005551212",  # phone-number-shaped id.
        "4042",  # too short anyway, but decimal.
        "9999999999",  # large numeric id.
    ],
)
def test_all_decimal_number_does_not_trigger_sha_marker(number: str) -> None:
    """A purely-decimal 7+ digit token is not a commit hash. Digits are a
    subset of the hex class, so the matched run must contain at least one
    a-f letter — otherwise durable numbers (epochs, phone numbers, ids,
    error codes) fail closed against the very content the gate admits."""
    body = f"The recorded value {number} is the canonical reference."
    hits = find_transient_markers(body)
    assert all(not h.marker.startswith("sha:") for h in hits), (
        f"all-decimal {number!r} must not be flagged as a SHA, "
        f"got {[h.marker for h in hits]}"
    )


def test_uppercase_hex_does_not_trigger_sha_marker() -> None:
    """ULIDs (and other uppercase hex IDs) shouldn't be misread as SHAs."""
    body = "Memory id 01HXYZABCDEF identifies the entry."
    hits = find_transient_markers(body)
    assert all(not h.marker.startswith("sha:") for h in hits)


def test_lowercase_uuid_does_not_trigger_sha_marker() -> None:
    """Hyphens are word boundaries, so the >=7-char hex segments of a
    lowercase UUID would match _SHA_RE on their own — but a UUID is a
    permanent identifier (KMS key, tenant id), not branch state."""
    body = (
        "The prod KMS key for the backups bucket is "
        "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d (eu-north-1)."
    )
    assert find_transient_markers(body) == []


def test_32_hex_machine_id_does_not_trigger_sha_marker() -> None:
    """A maximal exactly-32-hex run is MD5 / machine-id / gist-id length —
    a durable artifact identifier, never a git ref."""
    machine_id = "0123456789abcdef" * 2  # 32 hex chars.
    body = f"The machine-id {machine_id} identifies the host."
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


def test_seven_char_sha_fires() -> None:
    body = "Look at commit a1b2c3d for the change."
    hits = find_transient_markers(body)
    sha_hits = [h for h in hits if h.marker.startswith("sha:")]
    assert sha_hits, "expected SHA hit"
    assert sha_hits[0].marker == SHA_MARKER


def test_forty_char_sha_fires() -> None:
    sha = "a" * 40
    body = f"Pinned to commit {sha} for the refactor."
    hits = find_transient_markers(body)
    assert any(h.marker.startswith("sha:") for h in hits)


def test_forty_one_char_hex_does_not_fire() -> None:
    """Above the SHA upper bound — large hex blobs shouldn't trip."""
    body = "Hash digest " + ("a" * 41) + " is in the cache."
    hits = find_transient_markers(body)
    # The 41-char run has no \b at position 40, so no 40-char prefix
    # match either — the regex only considers maximal hex runs.
    assert all(not h.marker.startswith("sha:") for h in hits)


@pytest.mark.parametrize(
    ("body", "sha"),
    [
        (
            "The deployed build is v3.7.1-5-g874b0b0, two commits past the tag.",
            "874b0b0",
        ),
        ("The image is tagged 2.4.0-12-ge9a3f1c in the registry.", "e9a3f1c"),
    ],
)
def test_git_describe_sha_fires(body: str, sha: str) -> None:
    """git-describe output embeds the hash behind a literal 'g', which
    removes the \\b — the dedicated pattern still buckets it under
    SHA_MARKER, and the hash itself travels in the snippet."""
    hits = find_transient_markers(body)
    sha_hits = [h for h in hits if h.marker == SHA_MARKER]
    assert sha_hits, f"expected {SHA_MARKER!r}, got {[h.marker for h in hits]}"
    assert sha in sha_hits[0].snippet


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


def test_multiple_shas_reported_once_under_one_marker() -> None:
    """Five SHAs in a row collapse to one bucket — reading 5 entries adds
    no signal beyond 'you're putting branch state in memory'."""
    body = (
        "Branch is at a1b2c3d, parent of e4f5a6b, sibling of c7d8e9f, "
        "cherry-picked from b1a2c3d, into trunk b7e8f9a."
    )
    hits = find_transient_markers(body)
    sha_hits = [h for h in hits if h.marker.startswith("sha:")]
    assert len(sha_hits) == 1


def test_different_bodies_share_one_sha_marker_name() -> None:
    """The bucketing that `marker_stats` actually reads.

    Collapsing within a body was already covered; collapsing ACROSS them
    was not, and that is the half the aggregation keys on. While the name
    carried the hash, every write minted a fresh row, so the SHA class
    could never accumulate the override evidence that decides whether the
    marker earns its slot.
    """
    first = find_transient_markers("Shipped in a1b2c3d, see the tag.")
    second = find_transient_markers("Reverted by e4f5a6b later that day.")

    names = {h.marker for h in first + second if h.marker.startswith("sha:")}
    assert names == {SHA_MARKER}, f"SHA marker name is not stable: {names}"


def test_sha_hash_survives_in_the_snippet() -> None:
    """The name is canonical, so the snippet is what carries the hash —
    the caller still gets told which token tripped the gate."""
    hits = find_transient_markers("Pinned to a1b2c3d for the refactor.")
    sha_hit = next(h for h in hits if h.marker == SHA_MARKER)
    assert "a1b2c3d" in sha_hit.snippet


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

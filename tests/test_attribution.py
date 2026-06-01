"""Unit tests for the phrase-match attribution matcher.

The matcher's contract is "high precision, low recall." These tests
pin the precision boundary — sentences just barely below the
candidate threshold get rejected, and sentences with no real overlap
to the reply don't get attributed. Recall is harder to assert and
intentionally not exhaustively tested; the integration test in
test_hook covers the end-to-end happy path.
"""

from __future__ import annotations

import unicodedata

from bettermemory.attribution import (
    AttributionMatch,
    _normalize,
    attribute_uses,
)


# --- Unicode-form fixtures, built programmatically -----------------------------
#
# These accent forms are constructed from a base ASCII letter plus the combining
# acute accent U+0301 (decomposed) and folded to a single precomposed codepoint
# via NFC. They are NOT pasted as raw accented literals on purpose: an editor (or
# a macOS/iCloud filesystem) can silently re-normalize a decomposed literal in
# the source back to its precomposed form, which would erase the very byte
# distinction these tests depend on. Building from chr(0x301) keeps both forms
# byte-distinct on disk no matter what touches the file.

_COMBINING_ACUTE = chr(0x301)

# "café": decomposed = "caf" + "e" + combining-acute; precomposed folds to U+00E9.
_CAFE_DECOMPOSED = "caf" + "e" + _COMBINING_ACUTE
_CAFE_PRECOMPOSED = unicodedata.normalize("NFC", _CAFE_DECOMPOSED)
# "exposé": same shape on the trailing vowel.
_EXPOSE_DECOMPOSED = "expos" + "e" + _COMBINING_ACUTE
_EXPOSE_PRECOMPOSED = unicodedata.normalize("NFC", _EXPOSE_DECOMPOSED)


def test_accent_fixture_forms_are_byte_distinct() -> None:
    """Guard: the two encodings really differ on disk, and NFC collapses them.

    If this ever fails, the editor folded the decomposed source literal into its
    precomposed form (the exact trap that produced a zero-discrimination test in
    a prior attempt). The behavioral tests below would then silently stop testing
    the bug, so assert the distinction explicitly up front.
    """
    assert _CAFE_DECOMPOSED != _CAFE_PRECOMPOSED
    assert _EXPOSE_DECOMPOSED != _EXPOSE_PRECOMPOSED
    # The decomposed form carries the standalone combining mark.
    assert _COMBINING_ACUTE in _CAFE_DECOMPOSED
    assert _COMBINING_ACUTE not in _CAFE_PRECOMPOSED
    # Both forms canonicalize to the same precomposed codepoints.
    assert unicodedata.normalize("NFC", _CAFE_DECOMPOSED) == _CAFE_PRECOMPOSED
    assert _normalize(_CAFE_DECOMPOSED) == _normalize(_CAFE_PRECOMPOSED)
    assert _normalize(_EXPOSE_DECOMPOSED) == _normalize(_EXPOSE_PRECOMPOSED)


def test_verbatim_tier_matches_across_accent_forms() -> None:
    """Verbatim tier: a body written with precomposed accents matches a reply
    that quotes the same sentence with decomposed accents.

    Fails on unpatched code — the normalized needle and haystack differ byte-for
    -byte at the accented characters, so the substring check misses. Passes once
    ``_normalize`` runs NFC first.
    """
    body = (
        f"the {_CAFE_PRECOMPOSED} menu and the {_EXPOSE_PRECOMPOSED} report "
        "were finalised here."
    )
    reply = (
        "earlier note — "
        f"the {_CAFE_DECOMPOSED} menu and the {_EXPOSE_DECOMPOSED} report "
        "were finalised here, all good."
    )
    matches = attribute_uses({"mem1": body}, reply)
    assert len(matches) == 1
    assert matches[0].memory_id == "mem1"


def test_containment_tier_matches_across_accent_forms() -> None:
    """Containment tier: a reordered paraphrase (no surviving verbatim span)
    that reuses two accented + two ASCII content tokens.

    On unpatched code the accent forms tokenize differently — precomposed
    "café"/"exposé" become the truncated "caf"/"expos" while the decomposed
    forms become "cafe"/"expose" — so only the two ASCII tokens overlap. That is
    2 matched tokens at ratio 2/6, below BOTH the matched-token floor (4) and the
    0.60 ratio, so the tier rejects it. With NFC normalization all four content
    tokens overlap (4/6 = 0.67), clearing both gates.
    """
    body = (
        f"the {_CAFE_PRECOMPOSED} {_EXPOSE_PRECOMPOSED} from the menu report "
        "were here today."
    )
    reply = (
        "the report and the menu were produced from "
        f"{_EXPOSE_DECOMPOSED} {_CAFE_DECOMPOSED} downstream steps."
    )
    # Guard: this fixture must exercise the containment tier, not verbatim — no
    # candidate sentence appears as a contiguous span of the reply.
    assert _normalize(body.rstrip(".")) not in _normalize(reply)
    matches = attribute_uses({"mem1": body}, reply)
    assert len(matches) == 1
    assert matches[0].memory_id == "mem1"


def test_empty_inputs_return_empty() -> None:
    assert attribute_uses({}, "any reply") == []
    assert attribute_uses({"id1": "body"}, "") == []
    assert attribute_uses({}, "") == []


def test_exact_match_attributes_first_sentence_only() -> None:
    body = (
        "The auth middleware lives in src/auth/middleware.py. "
        "JWT verification happens in verify_token(). "
        "Sessions are stored in Redis with a 24h TTL."
    )
    reply = (
        "Using your stored note on the auth: "
        "The auth middleware lives in src/auth/middleware.py. "
        "I'll update the imports accordingly."
    )
    matches = attribute_uses({"mem1": body}, reply)
    assert len(matches) == 1
    assert matches[0].memory_id == "mem1"
    assert "auth middleware lives in src/auth/middleware.py" in matches[0].claim_excerpt


def test_no_match_returns_empty() -> None:
    body = "The auth middleware lives in src/auth/middleware.py forever and ever."
    reply = "I'll work on the deployment script and the Kubernetes config."
    assert attribute_uses({"mem1": body}, reply) == []


def test_short_sentence_filtered() -> None:
    """Sentences below the token threshold don't get attributed even when they appear verbatim."""
    body = "Use Redis."
    reply = "I'll use Redis for this."
    assert attribute_uses({"mem1": body}, reply) == []


def test_stopword_heavy_sentence_filtered() -> None:
    """Mostly-stopword sentences don't get attributed — too risky."""
    body = "This is the way and it is the only way that we do it."
    reply = (
        "This is the way and it is the only way that we do it, "
        "as you mentioned earlier and we discussed."
    )
    assert attribute_uses({"mem1": body}, reply) == []


def test_case_insensitive_and_whitespace_collapsed() -> None:
    body = (
        "The auth middleware lives in src/auth/middleware.py for the JWT verification."
    )
    reply = "the auth middleware lives in   src/auth/middleware.py for the jwt verification."
    matches = attribute_uses({"mem1": body}, reply)
    assert len(matches) == 1


def test_multi_memory_independent_attribution() -> None:
    body_a = (
        "The auth middleware lives in src/auth/middleware.py for the JWT verify path."
    )
    body_b = (
        "The metrics dashboard runs at grafana.internal/d/api-latency for oncall watch."
    )
    reply = (
        "The metrics dashboard runs at grafana.internal/d/api-latency for oncall watch — "
        "I'll add a panel."
    )
    matches = attribute_uses({"mem_a": body_a, "mem_b": body_b}, reply)
    # mem_b matches, mem_a doesn't.
    assert len(matches) == 1
    assert matches[0].memory_id == "mem_b"


def test_first_sentence_wins_per_memory() -> None:
    """A memory with two matching sentences produces only one match."""
    body = (
        "The auth middleware lives in src/auth/middleware.py for the JWT verify. "
        "The metrics dashboard runs at grafana.internal/d/api-latency for oncall."
    )
    reply = (
        "The auth middleware lives in src/auth/middleware.py for the JWT verify. "
        "And the metrics dashboard runs at grafana.internal/d/api-latency for oncall."
    )
    matches = attribute_uses({"mem1": body}, reply)
    assert len(matches) == 1
    # The first sentence in the body wins.
    assert "auth middleware" in matches[0].claim_excerpt


def test_excerpt_capped_at_500_chars() -> None:
    long_body = (
        "The auth middleware lives in src/auth/middleware.py for JWT verify and "
        + ("padding " * 200)
        + "end."
    )
    reply = long_body
    matches = attribute_uses({"mem1": long_body}, reply)
    assert len(matches) == 1
    assert len(matches[0].claim_excerpt) <= 500


def test_empty_body_skipped() -> None:
    matches = attribute_uses({"mem_empty": "", "mem_real": "x"}, "anything here goes")
    assert matches == []


def test_match_dataclass_is_hashable_immutable() -> None:
    """AttributionMatch is frozen — useful for set membership in dedup."""
    m = AttributionMatch(memory_id="x", claim_excerpt="y")
    assert hash(m) == hash(AttributionMatch(memory_id="x", claim_excerpt="y"))


# --- Containment tier: paraphrase recall without sacrificing precision -------
#
# The verbatim substring tier alone logged ZERO hook attributions across the
# author's entire event history, because models paraphrase a memory rather than
# quoting it. The containment tier matches when a high fraction of a candidate
# sentence's distinct content tokens appear in the reply (reworded/reordered),
# guarded by an absolute matched-token floor so coincidental topical overlap
# still doesn't attribute.


def test_containment_paraphrase_matches() -> None:
    """Reply reuses the memory's distinctive vocabulary but rewords and
    reorders it — no long verbatim span survives, yet it's the same claim."""
    body = "The retry backoff doubles after each failed webhook delivery attempt."
    reply = (
        "Each failed webhook delivery attempt doubles the retry backoff, "
        "so a flapping endpoint quickly hits the ceiling."
    )
    matches = attribute_uses({"mem1": body}, reply)
    assert len(matches) == 1
    assert matches[0].memory_id == "mem1"


def test_containment_reordered_tokens_match() -> None:
    """Pure reorder, no contiguous verbatim span — containment still links it."""
    body = "Schema migrations run inside a single advisory-locked transaction."
    reply = (
        "We wrap the run in a single transaction that takes an advisory lock "
        "before applying schema migrations."
    )
    assert len(attribute_uses({"mem1": body}, reply)) == 1


def test_containment_deep_reword_does_not_match() -> None:
    """Precision guard: a deep reword that shares only a token or two with the
    memory must NOT attribute — below the matched-token floor."""
    body = "The authentication middleware rejects expired tokens."
    reply = "Tokens that have expired get bounced by the auth layer."
    assert attribute_uses({"mem1": body}, reply) == []


def test_containment_topical_coincidence_does_not_match() -> None:
    """A reply on the same general topic sharing one incidental token ('pool')
    is coincidence, not the memory shaping the reply."""
    body = "The connection pool caps at thirty idle sockets before recycling."
    reply = "Database connections are managed by a pool of background workers."
    assert attribute_uses({"mem1": body}, reply) == []

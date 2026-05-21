"""Unit tests for the phrase-match attribution matcher.

The matcher's contract is "high precision, low recall." These tests
pin the precision boundary — sentences just barely below the
candidate threshold get rejected, and sentences with no real overlap
to the reply don't get attributed. Recall is harder to assert and
intentionally not exhaustively tested; the integration test in
test_hook covers the end-to-end happy path.
"""

from __future__ import annotations

from bettermemory.attribution import AttributionMatch, attribute_uses


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

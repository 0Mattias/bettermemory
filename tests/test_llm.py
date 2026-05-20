"""Tests for the LLM-driven consolidation primitives in `bettermemory.llm`.

Covers the parts that don't need a running Ollama or external API:
proposal validation, hallucinated-ID rejection, prompt construction,
cluster building from a memory list + event log, diff rendering.

The provider-protocol path (OllamaProvider, AnthropicProvider,
OpenAIProvider) is tested separately in `test_consolidate_llm.py` via
a fake provider — exercising the HTTP/SDK round-trip would be a brittle
integration test, and the provider implementations are thin enough that
the surface that matters (parse, validate, apply) is the part covered
here.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from bettermemory.llm import (
    Cluster,
    ClusterMember,
    DemoteTierProposal,
    MemoryExcerpt,
    MergeProposal,
    OllamaProvider,
    ResolveContradictionProposal,
    RewriteRelativeDateProposal,
    build_clusters,
    build_prompt,
    make_provider,
    parse_and_validate,
    render_proposal_diff,
    today_iso,
)
from bettermemory.models import (
    Category,
    Confidence,
    Memory,
    Source,
    generate_ulid,
)


def _make_memory(body: str, category: Category | None = None) -> Memory:
    now = datetime.now(timezone.utc)
    return Memory(
        id=generate_ulid(),
        created=now,
        updated=now,
        scopes=["tools"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        category=category,
        body=body + "\n",
    )


def _make_cluster(members: list[Memory], kind: str = "near_duplicates") -> Cluster:
    return Cluster(
        cluster_id="test-cluster",
        cluster_kind=kind,  # type: ignore[arg-type]
        members=tuple(ClusterMember(memory=m) for m in members),
    )


# ---------------------------------------------------------------------------
# parse_and_validate — the hallucination gate
# ---------------------------------------------------------------------------


def test_parse_valid_merge_proposal() -> None:
    a = _make_memory("postgres on port 5432")
    b = _make_memory("the queue uses postgres at 5432")
    cluster = _make_cluster([a, b])
    raw = json.dumps(
        {
            "proposals": [
                {
                    "type": "merge",
                    "keeper_id": a.id,
                    "duplicate_ids": [b.id],
                    "new_body": "postgres on port 5432 (used by the queue)",
                    "rationale": "same fact phrased two ways",
                }
            ]
        }
    )
    proposals = parse_and_validate(raw, cluster)
    assert len(proposals) == 1
    assert isinstance(proposals[0], MergeProposal)
    assert proposals[0].keeper_id == a.id
    assert proposals[0].duplicate_ids == (b.id,)


def test_parse_rejects_hallucinated_id() -> None:
    """An LLM that produces a memory_id that isn't in the cluster is
    hallucinating. Rejecting it BEFORE the diff renderer is the core
    of the audit-transparency contract."""
    a = _make_memory("real memory")
    cluster = _make_cluster([a])
    raw = json.dumps(
        {
            "proposals": [
                {
                    "type": "merge",
                    "keeper_id": a.id,
                    "duplicate_ids": ["01HXFAKEFAKEFAKEFAKEFAKEFAKE"],
                    "new_body": "anything",
                    "rationale": "doesn't matter — duplicate_id is fake",
                }
            ]
        }
    )
    proposals = parse_and_validate(raw, cluster)
    assert proposals == []


def test_parse_rejects_keeper_in_duplicate_ids() -> None:
    """Keeper id appearing in duplicate_ids is a malformed request —
    the keeper would tombstone itself. Reject."""
    a = _make_memory("memory A")
    b = _make_memory("memory B")
    cluster = _make_cluster([a, b])
    raw = json.dumps(
        {
            "proposals": [
                {
                    "type": "merge",
                    "keeper_id": a.id,
                    "duplicate_ids": [a.id, b.id],
                    "new_body": "anything",
                    "rationale": "self-duplicate",
                }
            ]
        }
    )
    proposals = parse_and_validate(raw, cluster)
    assert proposals == []


def test_parse_rejects_resolve_with_same_winner_and_loser() -> None:
    a = _make_memory("memory A")
    b = _make_memory("memory B")
    cluster = _make_cluster([a, b])
    raw = json.dumps(
        {
            "proposals": [
                {
                    "type": "resolve_contradiction",
                    "winner_id": a.id,
                    "loser_id": a.id,
                    "rationale": "same id twice",
                }
            ]
        }
    )
    proposals = parse_and_validate(raw, cluster)
    assert proposals == []


def test_parse_valid_rewrite_date() -> None:
    a = _make_memory("we shipped today the new auth flow")
    cluster = _make_cluster([a])
    raw = json.dumps(
        {
            "proposals": [
                {
                    "type": "rewrite_relative_date",
                    "memory_id": a.id,
                    "new_body": "we shipped 2026-05-20 the new auth flow",
                    "rationale": "today -> 2026-05-20",
                }
            ]
        }
    )
    proposals = parse_and_validate(raw, cluster)
    assert len(proposals) == 1
    assert isinstance(proposals[0], RewriteRelativeDateProposal)
    assert proposals[0].new_body.startswith("we shipped 2026-05-20")


def test_parse_valid_demote_tier() -> None:
    a = _make_memory("fact that became context", category=Category.FACT)
    cluster = _make_cluster([a])
    raw = json.dumps(
        {
            "proposals": [
                {
                    "type": "demote_tier",
                    "memory_id": a.id,
                    "new_category": "ambient",
                    "rationale": "verifiable claim has been superseded",
                }
            ]
        }
    )
    proposals = parse_and_validate(raw, cluster)
    assert len(proposals) == 1
    assert isinstance(proposals[0], DemoteTierProposal)
    assert proposals[0].new_category == "ambient"


def test_parse_rejects_unknown_demote_category() -> None:
    a = _make_memory("a memory", category=Category.FACT)
    cluster = _make_cluster([a])
    raw = json.dumps(
        {
            "proposals": [
                {
                    "type": "demote_tier",
                    "memory_id": a.id,
                    "new_category": "user-inference",
                    "rationale": "no — user-inference is write-side only",
                }
            ]
        }
    )
    proposals = parse_and_validate(raw, cluster)
    assert proposals == []


def test_parse_strips_markdown_fences() -> None:
    """Some Ollama models wrap JSON in ```json fences despite the
    format=json hint. The validator strips them defensively."""
    a = _make_memory("body a")
    b = _make_memory("body b")
    cluster = _make_cluster([a, b])
    raw = (
        "```json\n"
        + json.dumps(
            {
                "proposals": [
                    {
                        "type": "merge",
                        "keeper_id": a.id,
                        "duplicate_ids": [b.id],
                        "new_body": "merged",
                        "rationale": "same",
                    }
                ]
            }
        )
        + "\n```"
    )
    proposals = parse_and_validate(raw, cluster)
    assert len(proposals) == 1


def test_parse_invalid_json_returns_empty() -> None:
    cluster = _make_cluster([_make_memory("body")])
    proposals = parse_and_validate("not json at all {{{", cluster)
    assert proposals == []


def test_parse_missing_proposals_array_returns_empty() -> None:
    cluster = _make_cluster([_make_memory("body")])
    raw = json.dumps({"not_the_right_key": []})
    proposals = parse_and_validate(raw, cluster)
    assert proposals == []


def test_parse_empty_rationale_rejected() -> None:
    """A proposal without rationale defeats the audit story. Reject."""
    a = _make_memory("body a")
    b = _make_memory("body b")
    cluster = _make_cluster([a, b])
    raw = json.dumps(
        {
            "proposals": [
                {
                    "type": "merge",
                    "keeper_id": a.id,
                    "duplicate_ids": [b.id],
                    "new_body": "merged",
                    "rationale": "   ",
                }
            ]
        }
    )
    proposals = parse_and_validate(raw, cluster)
    assert proposals == []


def test_parse_unknown_type_rejected() -> None:
    a = _make_memory("body")
    cluster = _make_cluster([a])
    raw = json.dumps(
        {
            "proposals": [
                {
                    "type": "rewrite_universe",
                    "memory_id": a.id,
                    "rationale": "from the future",
                }
            ]
        }
    )
    proposals = parse_and_validate(raw, cluster)
    assert proposals == []


# ---------------------------------------------------------------------------
# build_prompt — cluster rendering shape
# ---------------------------------------------------------------------------


def test_build_prompt_includes_today() -> None:
    a = _make_memory("body")
    cluster = _make_cluster([a])
    prompt = build_prompt(cluster, today="2026-05-20")
    assert "Today is 2026-05-20." in prompt


def test_build_prompt_includes_memory_ids() -> None:
    a = _make_memory("body a")
    b = _make_memory("body b")
    cluster = _make_cluster([a, b])
    prompt = build_prompt(cluster, today="2026-05-20")
    assert a.id in prompt
    assert b.id in prompt


def test_build_prompt_truncates_long_bodies() -> None:
    big_body = "x" * 10_000
    a = _make_memory(big_body)
    cluster = _make_cluster([a])
    prompt = build_prompt(cluster, today="2026-05-20")
    # The truncation marker should appear since body exceeds MAX_BODY_CHARS.
    assert "body truncated" in prompt


def test_build_prompt_includes_excerpts_when_present() -> None:
    a = _make_memory("body")
    cluster = Cluster(
        cluster_id="x",
        cluster_kind="near_duplicates",
        members=(
            ClusterMember(
                memory=a,
                applied_count=2,
                excerpts=(
                    MemoryExcerpt(
                        outcome="applied",
                        excerpt="The auth middleware lives at src/auth.py",
                        timestamp="2026-05-19T10:00:00Z",
                    ),
                ),
            ),
        ),
    )
    prompt = build_prompt(cluster, today="2026-05-20")
    assert "auth middleware" in prompt
    assert "[applied]" in prompt


# ---------------------------------------------------------------------------
# build_clusters — heuristic seeds
# ---------------------------------------------------------------------------


def test_build_clusters_unions_overlapping_near_duplicate_pairs() -> None:
    """A 3-way near-duplicate cluster (A↔B, B↔C) should produce one
    cluster with all three members — not two separate two-member
    clusters that the LLM would have to reconcile separately."""
    a = _make_memory("body a")
    b = _make_memory("body b")
    c = _make_memory("body c")
    pairs = [(a.id, b.id), (b.id, c.id)]
    clusters = build_clusters(
        [a, b, c],
        events=[],
        near_duplicate_pairs=pairs,
    )
    assert len(clusters) == 1
    assert len(clusters[0].members) == 3
    assert clusters[0].cluster_kind == "near_duplicates"


def test_build_clusters_handles_no_pairs() -> None:
    a = _make_memory("body")
    clusters = build_clusters([a], events=[], near_duplicate_pairs=None)
    assert clusters == []


def test_build_clusters_seeds_contradiction_from_event() -> None:
    """A `memory_record_use(outcome="contradicted")` event paired with
    a co-retrieval should produce a contradiction-candidate cluster."""
    a = _make_memory("body a")
    b = _make_memory("body b")
    events = [
        {
            "kind": "memory_search",
            "session_id": "s1",
            "memory_ids": [a.id, b.id],
            "ts": "2026-05-20T10:00:00Z",
        },
        {
            "kind": "memory_record_use",
            "session_id": "s1",
            "memory_ids": [a.id],
            "outcome": "contradicted",
            "ts": "2026-05-20T10:01:00Z",
        },
    ]
    clusters = build_clusters([a, b], events=events, near_duplicate_pairs=[])
    contradiction_clusters = [
        c for c in clusters if c.cluster_kind == "contradiction_candidates"
    ]
    assert len(contradiction_clusters) == 1
    member_ids = {m.memory.id for m in contradiction_clusters[0].members}
    assert member_ids == {a.id, b.id}


# ---------------------------------------------------------------------------
# render_proposal_diff — the audit-transparency surface
# ---------------------------------------------------------------------------


def test_render_merge_diff_shows_body_change() -> None:
    a = _make_memory("postgres at 5432")
    b = _make_memory("queue uses postgres")
    by_id = {a.id: a, b.id: b}
    proposal = MergeProposal(
        keeper_id=a.id,
        duplicate_ids=(b.id,),
        new_body="postgres at 5432 (used by the queue)\n",
        rationale="combined",
    )
    rendered = render_proposal_diff(proposal, by_id)
    assert "MERGE" in rendered
    assert "rationale: combined" in rendered
    # Diff lines start with `+` for new content
    assert any(
        line.startswith("+") and "queue" in line for line in rendered.splitlines()
    )


def test_render_resolve_diff_names_winner_and_loser() -> None:
    a = _make_memory("the right version")
    b = _make_memory("the wrong version")
    by_id = {a.id: a, b.id: b}
    proposal = ResolveContradictionProposal(
        winner_id=a.id,
        loser_id=b.id,
        rationale="a is current",
    )
    rendered = render_proposal_diff(proposal, by_id)
    assert "RESOLVE_CONTRADICTION" in rendered
    assert a.id in rendered
    assert b.id in rendered
    assert "tombstoned" in rendered


def test_render_demote_diff_shows_tier_transition() -> None:
    a = _make_memory("a memory", category=Category.FACT)
    by_id = {a.id: a}
    proposal = DemoteTierProposal(
        memory_id=a.id,
        new_category="ambient",
        rationale="no verifiable claims left",
    )
    rendered = render_proposal_diff(proposal, by_id)
    assert "DEMOTE_TIER" in rendered
    assert "fact -> ambient" in rendered


# ---------------------------------------------------------------------------
# Provider factory + Ollama lazy-import gate
# ---------------------------------------------------------------------------


def test_make_provider_ollama_default() -> None:
    provider = make_provider("ollama")
    assert isinstance(provider, OllamaProvider)
    assert provider.name == "ollama"


def test_make_provider_unknown_raises() -> None:
    with pytest.raises(ValueError, match="unknown LLM provider"):
        make_provider("not-a-provider")


def test_make_provider_anthropic_constructs_without_key() -> None:
    """Construction doesn't validate the key — that's deferred to the
    `.propose()` call so a misconfigured CI run fails at the call
    site with a clear message, not at module import."""
    provider = make_provider("anthropic")
    assert provider.name == "anthropic"


def test_today_iso_returns_string_date() -> None:
    """The helper must return an ISO-8601 date string for the prompt
    to substitute deterministically."""
    today = today_iso(datetime(2026, 5, 20, tzinfo=timezone.utc))
    assert today == "2026-05-20"

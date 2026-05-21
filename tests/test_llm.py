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
    ProposeNewProposal,
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
    """Legacy-shape event fixture (kind=`memory_search`/`memory_record_use`,
    `session_id`/`memory_ids` field names) still seeds contradictions.

    Production never wrote this shape from these handlers, but old test
    fixtures and any hand-rolled event logs from before 2.6.3 carry it.
    The fallback in `_collect_contradiction_targets` keeps them working.
    For the canonical-shape regression test that round-trips a real
    `Recorder`, see `test_build_clusters_seeds_contradiction_from_real_recorder`
    below.
    """
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


def test_build_clusters_seeds_contradiction_from_real_recorder(
    tmp_path: object,
) -> None:
    """Regression test for the 2.6.3 field-name fix.

    The canonical `Recorder` writes `kind="search"` with `returned=[…]`
    and `kind="use"` with `ids=[…]` — NOT the `memory_search` /
    `memory_record_use` / `memory_ids` shape the previous test uses.
    This test round-trips events through a real `Recorder` and reads
    them back via `iter_events` so any drift between what production
    emits and what `_collect_contradiction_targets` / `_build_cluster_member`
    consume fails the suite. Mirrors the discipline of the 2.6.2 fix
    for `find_demotion_candidates` in `consolidate.py`.
    """
    from pathlib import Path

    from bettermemory.events import Recorder, iter_events

    root = Path(tmp_path)  # type: ignore[arg-type]
    a = _make_memory("body a")
    b = _make_memory("body b")

    recorder = Recorder(root=root, session_id="s1")
    # Production-shape search event: returned=[…], not memory_ids.
    recorder.record(
        "search",
        query="anything",
        returned=[a.id, b.id],
        relevance=["high", "high"],
    )
    # Production-shape use event: ids=[…], outcome="contradicted",
    # claim_excerpts aligned by index with ids so excerpt aggregation
    # rounds through the same shape.
    recorder.record(
        "use",
        ids=[a.id],
        outcome="contradicted",
        claim_excerpts=["the body of a contradicts the body of b"],
        attribution="model",
    )

    events = list(iter_events(root))
    clusters = build_clusters([a, b], events=events, near_duplicate_pairs=[])

    contradiction_clusters = [
        c for c in clusters if c.cluster_kind == "contradiction_candidates"
    ]
    assert len(contradiction_clusters) == 1, (
        "Real-Recorder events should produce a contradiction cluster — "
        "if this fails, the production event shape has drifted past what "
        "_collect_contradiction_targets reads."
    )
    member_ids = {m.memory.id for m in contradiction_clusters[0].members}
    assert member_ids == {a.id, b.id}

    # And the excerpt aggregation reaches the cluster member too —
    # `_build_cluster_member` reads `ids` (not `memory_ids`) post-fix.
    member_a = next(m for m in contradiction_clusters[0].members if m.memory.id == a.id)
    assert member_a.contradicted_count == 1
    assert any("contradicts" in e.excerpt for e in member_a.excerpts), (
        "claim_excerpt should round-trip through _build_cluster_member"
    )


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


# ---------------------------------------------------------------------------
# ProposeNew — the fifth proposal type (closes the writing-reflex gap)
# ---------------------------------------------------------------------------


def _make_transcript_cluster(memories: list[Memory], transcript: str) -> Cluster:
    return Cluster(
        cluster_id="transcript-facts",
        cluster_kind="transcript_facts",
        members=tuple(ClusterMember(memory=m) for m in memories),
        transcript=transcript,
    )


def test_parse_valid_propose_new() -> None:
    """Happy path: the LLM extracts a durable fact from the transcript
    with a non-general scope, a valid category, a body, a non-empty
    source_excerpt, and a rationale. Validator accepts the proposal."""
    existing = _make_memory("Some unrelated existing memory.")
    cluster = _make_transcript_cluster(
        [existing],
        "[user] My Postgres listens on port 5433.\n[assistant] Got it — saving that.",
    )
    raw = json.dumps(
        {
            "proposals": [
                {
                    "type": "propose_new",
                    "scope": "infrastructure",
                    "category": "fact",
                    "body": "Postgres listens on port 5433, not the default 5432.",
                    "source_excerpt": "[user] My Postgres listens on port 5433.",
                    "rationale": "user-stated infrastructure fact, durable",
                }
            ]
        }
    )
    proposals = parse_and_validate(raw, cluster)
    assert len(proposals) == 1
    assert isinstance(proposals[0], ProposeNewProposal)
    assert proposals[0].scope == "infrastructure"
    assert proposals[0].category == "fact"
    assert "port 5433" in proposals[0].body


def test_parse_rejects_propose_new_without_transcript() -> None:
    """Without a transcript on the cluster, propose_new is non-sensical
    — the LLM has no source to extract from. Reject before the proposal
    can reach the apply path."""
    cluster = _make_cluster([_make_memory("anything")])
    raw = json.dumps(
        {
            "proposals": [
                {
                    "type": "propose_new",
                    "scope": "tools",
                    "category": "fact",
                    "body": "Made-up fact.",
                    "source_excerpt": "nothing real",
                    "rationale": "should be rejected",
                }
            ]
        }
    )
    proposals = parse_and_validate(raw, cluster)
    assert proposals == []


def test_parse_rejects_propose_new_general_scope() -> None:
    """The catch-all `general` scope is forbidden by the prompt; the
    validator enforces it structurally too."""
    cluster = _make_transcript_cluster([_make_memory("x")], "[user] something")
    raw = json.dumps(
        {
            "proposals": [
                {
                    "type": "propose_new",
                    "scope": "general",
                    "category": "fact",
                    "body": "Vaguely scoped fact.",
                    "source_excerpt": "[user] something",
                    "rationale": "wrong scope",
                }
            ]
        }
    )
    proposals = parse_and_validate(raw, cluster)
    assert proposals == []


def test_parse_rejects_propose_new_user_inference_category() -> None:
    """`user-inference` requires explicit user confirmation; the
    consolidate path can't supply that, so the LLM may not propose
    new memories at that tier."""
    cluster = _make_transcript_cluster([_make_memory("x")], "[user] I prefer tabs.")
    raw = json.dumps(
        {
            "proposals": [
                {
                    "type": "propose_new",
                    "scope": "learning-style",
                    "category": "user-inference",
                    "body": "User prefers tabs over spaces.",
                    "source_excerpt": "[user] I prefer tabs.",
                    "rationale": "user preference",
                }
            ]
        }
    )
    proposals = parse_and_validate(raw, cluster)
    assert proposals == []


def test_parse_rejects_propose_new_empty_source_excerpt() -> None:
    """The audit trail requires a transcript quotation. Empty
    source_excerpt is rejected so the provenance line on the new
    memory always points back at concrete text."""
    cluster = _make_transcript_cluster([_make_memory("x")], "[user] anything")
    raw = json.dumps(
        {
            "proposals": [
                {
                    "type": "propose_new",
                    "scope": "tools",
                    "category": "fact",
                    "body": "Made-up fact.",
                    "source_excerpt": "",
                    "rationale": "no provenance",
                }
            ]
        }
    )
    proposals = parse_and_validate(raw, cluster)
    assert proposals == []


def test_build_prompt_includes_transcript_when_present() -> None:
    """A transcript on the cluster surfaces in the prompt under the
    --- BEGIN TRANSCRIPT --- delimiter so the LLM knows to extract
    propose_new proposals from it (not from thin air)."""
    cluster = _make_transcript_cluster(
        [_make_memory("existing-fact")],
        "[user] Distinctive turn content.\n[assistant] Acknowledged.",
    )
    prompt = build_prompt(cluster, today="2026-05-21")
    assert "BEGIN TRANSCRIPT" in prompt
    assert "END TRANSCRIPT" in prompt
    assert "Distinctive turn content" in prompt


def test_build_prompt_omits_transcript_when_absent() -> None:
    """Clusters without a transcript (the existing dedup /
    contradiction kinds) don't get a TRANSCRIPT section — the LLM
    sees the same prompt shape it did before propose_new shipped."""
    cluster = _make_cluster([_make_memory("x")])
    prompt = build_prompt(cluster, today="2026-05-21")
    assert "BEGIN TRANSCRIPT" not in prompt


def test_render_propose_new_diff_shows_new_body() -> None:
    """The propose_new diff renderer treats the proposal as a new
    file: + lines for the body, plus scope / category / rationale /
    source_excerpt labels so the audit trail is one block."""
    proposal = ProposeNewProposal(
        scope="infrastructure",
        category="fact",
        body="Postgres listens on port 5433.",
        source_excerpt="[user] My Postgres listens on port 5433.",
        rationale="durable infrastructure fact",
    )
    rendered = render_proposal_diff(proposal, by_id={})
    assert "PROPOSE_NEW" in rendered
    assert "scope=infrastructure" in rendered
    assert "category=fact" in rendered
    assert "+ Postgres listens on port 5433." in rendered
    assert "source_excerpt:" in rendered

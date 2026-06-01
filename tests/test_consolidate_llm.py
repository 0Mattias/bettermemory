"""Integration tests for `consolidate_llm` — the orchestrator that
takes a Store + LLMProvider and turns proposals into mutations.

Uses a FakeProvider that returns hand-rolled proposal lists so the
test surface covers (a) dry-run gives proposals without mutating, (b)
--apply --yes batch commits everything, (c) --apply without --yes and
without interactive prompt refuses, (d) each proposal type executes
the right store mutation, and (e) provider failures are caught per
cluster without tanking the whole pass.

No Ollama or external API is touched — those are integration-test
territory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from bettermemory.consolidate import build_transcript_cluster, consolidate_llm
from bettermemory.llm import (
    Cluster,
    DemoteTierProposal,
    MergeProposal,
    Proposal,
    ProposeNewProposal,
    ResolveContradictionProposal,
    RewriteRelativeDateProposal,
)
from bettermemory.models import Category, Confidence, Source
from bettermemory.store import Store


@dataclass
class FakeProvider:
    """Returns a fixed list of proposals when asked, regardless of
    the cluster. Tests use this to pin behavior without prompting an
    actual model."""

    name: str = "fake"
    proposals: list[Proposal] = field(default_factory=list)
    fail: bool = False
    call_count: int = 0

    def propose(self, cluster: Cluster, today: str) -> list[Proposal]:
        self.call_count += 1
        if self.fail:
            raise RuntimeError("simulated provider failure")
        return list(self.proposals)


def _make_store_with_dupes(tmp_path: Path) -> tuple[Store, str, str]:
    """Two near-duplicate memories about postgres so the dedup pre-pass
    surfaces them as a cluster the FakeProvider can act on."""
    store = Store(tmp_path)
    a_record = store.write(
        content="postgres on port 5432 used by the queue",
        scopes=["tools"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
    )
    # Identical-enough body to fire the Jaccard dedup at default threshold.
    b_record = store.write(
        content="postgres on port 5432 used by the queue worker",
        scopes=["tools"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
    )
    return store, a_record.id, b_record.id


# ---------------------------------------------------------------------------
# Dry-run path
# ---------------------------------------------------------------------------


def test_dry_run_returns_proposals_without_mutation(tmp_path: Path) -> None:
    """Without --apply, the report carries every proposal but the
    store is untouched."""
    store, a_id, b_id = _make_store_with_dupes(tmp_path)
    provider = FakeProvider(
        proposals=[
            MergeProposal(
                keeper_id=a_id,
                duplicate_ids=(b_id,),
                new_body="postgres on port 5432 (queue + queue worker)\n",
                rationale="combined",
            )
        ]
    )
    report = consolidate_llm(store, provider, apply=False)
    assert not report.applied
    assert len(report.proposals) == 1
    assert report.actions_taken == []
    # Both memories still present.
    ids = {m.id for m in store.load_all()}
    assert ids == {a_id, b_id}


def test_dry_run_with_failing_provider_records_failure(tmp_path: Path) -> None:
    """One bad provider call shouldn't tank the whole consolidation —
    the cluster is marked failed and the rest of the pass continues."""
    store, _a, _b = _make_store_with_dupes(tmp_path)
    provider = FakeProvider(fail=True)
    report = consolidate_llm(store, provider, apply=False)
    assert len(report.failures) >= 1
    assert all("simulated provider failure" in f.reason for f in report.failures)


# ---------------------------------------------------------------------------
# Apply gate
# ---------------------------------------------------------------------------


def test_apply_without_yes_or_interactive_refuses(tmp_path: Path) -> None:
    """The audit-transparency contract: --apply without --yes AND
    without an interactive accept loop refuses to commit anything."""
    store, a_id, b_id = _make_store_with_dupes(tmp_path)
    provider = FakeProvider(
        proposals=[
            MergeProposal(
                keeper_id=a_id,
                duplicate_ids=(b_id,),
                new_body="merged\n",
                rationale="combined",
            )
        ]
    )
    report = consolidate_llm(
        store,
        provider,
        apply=True,
        accept=False,
        interactive_input=None,
    )
    assert report.applied
    assert report.actions_taken == []
    # Both memories still present — no silent commit.
    ids = {m.id for m in store.load_all()}
    assert ids == {a_id, b_id}


def test_apply_yes_batch_commits_merge(tmp_path: Path) -> None:
    """--apply --yes commits every validated proposal."""
    store, a_id, b_id = _make_store_with_dupes(tmp_path)
    provider = FakeProvider(
        proposals=[
            MergeProposal(
                keeper_id=a_id,
                duplicate_ids=(b_id,),
                new_body="postgres on port 5432 (queue + worker)\n",
                rationale="combined",
            )
        ]
    )
    report = consolidate_llm(
        store, provider, apply=True, accept=True, session_id="test-session"
    )
    assert report.applied
    assert len(report.actions_taken) == 2  # keeper update + duplicate tombstone
    # Duplicate is gone; keeper has the new body.
    active_ids = {m.id for m in store.load_all()}
    assert b_id not in active_ids
    assert a_id in active_ids
    keeper = next(m for m in store.load_all() if m.id == a_id)
    assert "queue + worker" in keeper.body


def test_merge_rollback_restores_earlier_tombstoned_duplicates(
    tmp_path: Path, monkeypatch
) -> None:
    """Regression for the 2.6.4 audit. A multi-way merge tombstones
    `duplicate_ids` one by one. If a later tombstone fails, the
    rollback must restore the keeper's body AND un-tombstone every
    duplicate already removed — otherwise the earlier duplicate's
    content survives in neither the keeper (rolled back) nor the
    active set (tombstoned): silent data loss.
    """
    store = Store(tmp_path)
    keeper = store.write(
        content="postgres listens on port 5432 for the task queue",
        scopes=["tools"],
    )
    dup1 = store.write(
        content="postgres listens on port 5432 for the task queue worker",
        scopes=["tools"],
    )
    dup2 = store.write(
        content="postgres listens on port 5432 for the task queue daemon",
        scopes=["tools"],
    )
    provider = FakeProvider(
        proposals=[
            MergeProposal(
                keeper_id=keeper.id,
                duplicate_ids=(dup1.id, dup2.id),
                new_body="postgres on port 5432 (queue + worker + jobs)\n",
                rationale="combined",
            )
        ]
    )

    # Fail the SECOND duplicate's tombstone; the first is already
    # tombstoned by then, so the rollback has to undo it.
    real_tombstone = store.tombstone

    def flaky_tombstone(memory_id, **kw):
        if memory_id == dup2.id:
            raise RuntimeError("simulated tombstone failure")
        return real_tombstone(memory_id, **kw)

    monkeypatch.setattr(store, "tombstone", flaky_tombstone)

    report = consolidate_llm(
        store, provider, apply=True, accept=True, session_id="test-session"
    )

    # consolidate_llm catches the per-cluster raise as a failure.
    assert len(report.failures) >= 1
    active = {m.id for m in store.load_all()}
    # Keeper rolled back to its pre-merge body.
    assert keeper.id in active
    keeper_now = next(m for m in store.load_all() if m.id == keeper.id)
    assert "queue + worker + jobs" not in keeper_now.body
    # dup1 was tombstoned, then restored by the rollback — must be active.
    assert dup1.id in active, (
        "dup1 was tombstoned before dup2 failed; the rollback must "
        "restore it or its content is silently lost"
    )
    # dup2's tombstone never succeeded.
    assert dup2.id in active


def test_interactive_accept_per_proposal(tmp_path: Path) -> None:
    """Interactive mode prompts the user per proposal; only accepted
    ones get committed."""
    store, a_id, b_id = _make_store_with_dupes(tmp_path)
    provider = FakeProvider(
        proposals=[
            MergeProposal(
                keeper_id=a_id,
                duplicate_ids=(b_id,),
                new_body="merged body\n",
                rationale="combined",
            )
        ]
    )

    accept_all_responses = iter(["y"])

    def fake_input(prompt: str) -> str:
        return next(accept_all_responses)

    report = consolidate_llm(
        store,
        provider,
        apply=True,
        accept=False,
        interactive_input=fake_input,
    )
    assert report.applied
    assert len(report.accepted) == 1
    # Duplicate tombstoned.
    assert b_id not in {m.id for m in store.load_all()}


def test_interactive_reject_per_proposal(tmp_path: Path) -> None:
    """A user typing 'n' in interactive mode skips the proposal —
    nothing is committed."""
    store, a_id, b_id = _make_store_with_dupes(tmp_path)
    provider = FakeProvider(
        proposals=[
            MergeProposal(
                keeper_id=a_id,
                duplicate_ids=(b_id,),
                new_body="merged body\n",
                rationale="combined",
            )
        ]
    )

    def fake_input(prompt: str) -> str:
        return "n"

    report = consolidate_llm(
        store,
        provider,
        apply=True,
        accept=False,
        interactive_input=fake_input,
    )
    assert len(report.rejected) == 1
    assert report.actions_taken == []
    # Both still present.
    assert {m.id for m in store.load_all()} == {a_id, b_id}


# ---------------------------------------------------------------------------
# Proposal-type-specific application
# ---------------------------------------------------------------------------


def test_apply_resolve_contradiction_tombstones_loser(tmp_path: Path) -> None:
    store, a_id, b_id = _make_store_with_dupes(tmp_path)
    provider = FakeProvider(
        proposals=[
            ResolveContradictionProposal(
                winner_id=a_id,
                loser_id=b_id,
                rationale="a is current per the latest commit",
            )
        ]
    )
    report = consolidate_llm(store, provider, apply=True, accept=True)
    assert any(a.kind == "llm_resolve_tombstone" for a in report.actions_taken)
    assert b_id not in {m.id for m in store.load_all()}
    assert a_id in {m.id for m in store.load_all()}


def test_apply_rewrite_relative_date_updates_body(tmp_path: Path) -> None:
    store, a_id, _b = _make_store_with_dupes(tmp_path)
    provider = FakeProvider(
        proposals=[
            RewriteRelativeDateProposal(
                memory_id=a_id,
                new_body="we shipped 2026-05-20 the new auth flow\n",
                rationale="today -> 2026-05-20",
            )
        ]
    )
    report = consolidate_llm(store, provider, apply=True, accept=True)
    assert any(a.kind == "llm_rewrite_date" for a in report.actions_taken)
    rewritten = next(m for m in store.load_all() if m.id == a_id)
    assert "2026-05-20" in rewritten.body


def test_apply_demote_tier_retags_category(tmp_path: Path) -> None:
    store, a_id, _b = _make_store_with_dupes(tmp_path)
    provider = FakeProvider(
        proposals=[
            DemoteTierProposal(
                memory_id=a_id,
                new_category="ambient",
                rationale="verifiable claim has been superseded",
            )
        ]
    )
    report = consolidate_llm(store, provider, apply=True, accept=True)
    assert any(a.kind == "llm_demote_tier" for a in report.actions_taken)
    demoted = next(m for m in store.load_all() if m.id == a_id)
    assert demoted.category == Category.AMBIENT


# ---------------------------------------------------------------------------
# Determinism + reporting
# ---------------------------------------------------------------------------


def test_today_passed_through_to_provider(tmp_path: Path) -> None:
    """`today` is the prompt-grounding date for relative-date rewrites.
    The orchestrator must pass it through so tests can pin behavior."""

    captured_today: list[str] = []

    @dataclass
    class CapturingProvider:
        name: str = "capturing"

        def propose(self, cluster: Cluster, today: str) -> list[Proposal]:
            captured_today.append(today)
            return []

    store, _a, _b = _make_store_with_dupes(tmp_path)
    consolidate_llm(
        store,
        CapturingProvider(),
        apply=False,
        today="2026-05-20",
    )
    assert "2026-05-20" in captured_today


def test_report_records_provider_name(tmp_path: Path) -> None:
    store, _a, _b = _make_store_with_dupes(tmp_path)
    provider = FakeProvider(name="ollama-test")
    report = consolidate_llm(store, provider, apply=False)
    assert report.provider_name == "ollama-test"


def test_no_dupes_means_no_clusters(tmp_path: Path) -> None:
    """A store with no duplicate seeds produces zero clusters; the
    provider is never called."""
    store = Store(tmp_path)
    store.write(
        content="lonely memory with nothing to dedup against",
        scopes=["tools"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
    )
    provider = FakeProvider()
    report = consolidate_llm(store, provider, apply=False)
    assert report.cluster_count == 0


# ---------------------------------------------------------------------------
# Transcript-facts cluster + propose_new — Proposal 3 / writing-reflex gap.
# ---------------------------------------------------------------------------


def _make_store_with_existing(tmp_path: Path) -> Store:
    """A small store with one unrelated memory so build_transcript_cluster
    has something to use as 'don't propose duplicates of these'
    context."""
    store = Store(tmp_path)
    store.write(
        content="The metrics dashboard runs at grafana.internal/d/api-latency.",
        scopes=["infrastructure"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
    )
    return store


def test_build_transcript_cluster_loads_plain_text(tmp_path: Path) -> None:
    """A plain `.md` transcript reads through verbatim; the cluster
    carries the transcript content and the existing memories as
    members."""
    store = _make_store_with_existing(tmp_path)
    transcript = tmp_path / "session.md"
    transcript.write_text(
        "User said postgres listens on port 5433.\n"
        "Assistant acknowledged and saved it.",
        encoding="utf-8",
    )
    cluster = build_transcript_cluster(
        transcript_path=transcript,
        memories=store.load_all(),
        events=[],
    )
    assert cluster is not None
    assert cluster.cluster_kind == "transcript_facts"
    assert "port 5433" in cluster.transcript
    assert len(cluster.members) >= 1


def test_load_transcript_does_not_hang_on_fifo(tmp_path: Path) -> None:
    """Regression for the 2.6.4 audit. `_load_transcript` opened the
    path through `bounded_tail_read` with no regular-file guard —
    pointed at a FIFO with no writer, `open("rb")` blocks forever,
    hanging `consolidate --llm --from-transcript`. The `is_file()`
    guard rejects non-regular paths up front.

    Runs `_load_transcript` in a daemon thread: with the guard it
    returns instantly; without it the thread stays blocked in
    `open()` and is still alive after the join timeout.
    """
    import os
    import threading

    from bettermemory.consolidate import _load_transcript

    if not hasattr(os, "mkfifo"):  # pragma: no cover - non-unix
        pytest.skip("os.mkfifo not available")

    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    result: list[str] = []
    worker = threading.Thread(
        target=lambda: result.append(_load_transcript(fifo)), daemon=True
    )
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive(), (
        "_load_transcript hung on a writer-less FIFO — the is_file() "
        "guard before bounded_tail_read is missing"
    )
    assert result == [""]


def test_build_transcript_cluster_loads_jsonl_session(tmp_path: Path) -> None:
    """A `.jsonl` transcript (Claude Code per-session format) gets
    flattened to `[role] text` lines so the LLM sees a readable
    conversation, not raw JSON."""
    store = _make_store_with_existing(tmp_path)
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        "\n".join(
            [
                '{"type": "user", "message": {"content": "My Postgres is on 5433."}}',
                '{"type": "assistant", "message": {"content": [{"type": "text", "text": "Saved."}]}}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    cluster = build_transcript_cluster(
        transcript_path=transcript,
        memories=store.load_all(),
        events=[],
    )
    assert cluster is not None
    assert "[user] My Postgres is on 5433." in cluster.transcript
    assert "[assistant] Saved." in cluster.transcript


def test_build_transcript_cluster_returns_none_for_empty_file(
    tmp_path: Path,
) -> None:
    """An empty / whitespace-only transcript yields no cluster — the
    caller skips the LLM call entirely so we don't spend tokens on
    nothing."""
    transcript = tmp_path / "empty.txt"
    transcript.write_text("   \n\n\n", encoding="utf-8")
    cluster = build_transcript_cluster(
        transcript_path=transcript,
        memories=[],
        events=[],
    )
    assert cluster is None


def test_build_transcript_cluster_returns_none_for_missing_file(
    tmp_path: Path,
) -> None:
    """A missing transcript path is silently skipped; the caller
    upstream surfaces it as a LLMClusterFailure when the consolidate
    pass needs to report the misconfiguration."""
    cluster = build_transcript_cluster(
        transcript_path=tmp_path / "does-not-exist.txt",
        memories=[],
        events=[],
    )
    assert cluster is None


def test_consolidate_with_from_transcript_runs_propose_new(tmp_path: Path) -> None:
    """End-to-end: a `from_transcript` path plus a FakeProvider that
    returns one propose_new proposal. Dry-run returns the proposal;
    --apply --yes writes a new memory whose body carries the
    consolidate-provenance line."""
    store = _make_store_with_existing(tmp_path)
    transcript = tmp_path / "session.md"
    transcript.write_text(
        "[user] My Postgres is on port 5433, not 5432.\n[assistant] Saved.",
        encoding="utf-8",
    )
    proposal = ProposeNewProposal(
        scope="infrastructure",
        category="fact",
        body="Postgres listens on port 5433, not the default 5432.",
        source_excerpt="[user] My Postgres is on port 5433, not 5432.",
        rationale="user-stated infrastructure fact",
    )
    provider = FakeProvider(proposals=[proposal])

    # Dry-run first — the proposal lands in the report; no new memory
    # in the store.
    dry = consolidate_llm(store, provider, apply=False, from_transcript=str(transcript))
    assert any(isinstance(p, ProposeNewProposal) for p in dry.proposals)
    memories_before = store.load_all()
    # One existing memory from _make_store_with_existing.
    assert len(memories_before) == 1

    # Apply with --yes: the new memory lands in the store.
    applied = consolidate_llm(
        store,
        provider,
        apply=True,
        accept=True,
        from_transcript=str(transcript),
    )
    assert any(a.kind == "llm_propose_new" for a in applied.actions_taken)
    memories_after = store.load_all()
    assert len(memories_after) == 2

    # The new memory carries the consolidate-provenance line so the
    # source_excerpt is preserved in the body for future audits.
    new_memory = next(
        m
        for m in memories_after
        if "port 5433" in m.body and "5432" in m.body and "default" in m.body
    )
    assert new_memory.scopes == ["infrastructure"]
    assert "consolidate --llm --from-transcript" in new_memory.body
    assert "[user] My Postgres is on port 5433" in new_memory.body


def test_propose_new_writes_durable_body_despite_transient_provenance(
    tmp_path: Path,
) -> None:
    """Regression: the durability (transient-marker) gate must scan the
    LLM-authored body, NOT body_with_provenance. The prompt asks the model to
    set source_excerpt to the literal transcript turn, which routinely carries
    transient phrasing ('Today I…', 'we just…'). Scanning the combined text
    bounced almost every genuine --from-transcript proposal on a marker in the
    audit citation rather than in the durable claim. A clean durable body must
    still write even when its provenance quote contains a transient phrase.
    """
    store = _make_store_with_existing(tmp_path)
    transcript = tmp_path / "session.md"
    transcript.write_text(
        "[user] Today I decided we should use PostgreSQL on port 5433.\n"
        "[assistant] Saved.",
        encoding="utf-8",
    )
    proposal = ProposeNewProposal(
        scope="infrastructure",
        category="fact",
        # Durable claim — NO transient markers.
        body="The project's PostgreSQL instance listens on port 5433.",
        # Provenance quote — contains the transient phrase 'Today I'.
        source_excerpt="Today I decided we should use PostgreSQL on port 5433.",
        rationale="user-stated infrastructure fact",
    )
    provider = FakeProvider(proposals=[proposal])

    applied = consolidate_llm(
        store, provider, apply=True, accept=True, from_transcript=str(transcript)
    )
    # The durable body wrote despite the transient phrase in its provenance.
    assert any(a.kind == "llm_propose_new" for a in applied.actions_taken)
    assert not applied.failures, f"propose_new failed unexpectedly: {applied.failures}"
    memories_after = store.load_all()
    assert len(memories_after) == 2
    new_memory = next(m for m in memories_after if "port 5433" in m.body)
    # The provenance line (incl. the transient quote) is still stored for audit.
    assert "Today I decided" in new_memory.body


def test_consolidate_without_from_transcript_does_not_call_propose_new(
    tmp_path: Path,
) -> None:
    """Existing behavior: a --llm run without --from-transcript runs
    only the structural-cluster passes. The FakeProvider sees only the
    dedup cluster; propose_new is never relevant."""
    store, _a, _b = _make_store_with_dupes(tmp_path)
    provider = FakeProvider()
    report = consolidate_llm(store, provider, apply=False)
    # FakeProvider was called once (for the dedup cluster). No
    # transcript-facts cluster was built, so no second LLM call.
    assert provider.call_count == 1
    assert not any(isinstance(p, ProposeNewProposal) for p in report.proposals)


# ---------------------------------------------------------------------------
# Remote-provider request timeout (core-robustness)
# ---------------------------------------------------------------------------
#
# AnthropicProvider/OpenAIProvider issue a blocking SDK create() call. Without
# a request timeout a hung provider would block the consolidate pass (and any
# server thread driving it) indefinitely — the Ollama path already bounds its
# HTTP call. These tests inject a fake SDK module (mirroring the providers'
# lazy-import design) and capture the kwargs the create() call receives, so a
# regression that drops the `timeout=` argument fails the suite. No real
# network or SDK is touched.


def _one_member_cluster() -> Cluster:
    import datetime as _dt

    from bettermemory.llm import ClusterMember
    from bettermemory.models import Memory, generate_ulid

    now = _dt.datetime.now(_dt.timezone.utc)
    mem = Memory(
        id=generate_ulid(),
        created=now,
        updated=now,
        scopes=["tools"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body="a body the fake provider never actually reads\n",
    )
    return Cluster(
        cluster_id="c",
        cluster_kind="near_duplicates",
        members=(ClusterMember(memory=mem),),
    )


def test_anthropic_provider_passes_request_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys
    from types import SimpleNamespace

    from bettermemory.llm import DEFAULT_TIMEOUT, AnthropicProvider

    captured: dict[str, object] = {}

    class _FakeMessages:
        def create(self, **kwargs: object) -> object:
            captured.update(kwargs)
            block = SimpleNamespace(type="text", text='{"proposals": []}')
            return SimpleNamespace(content=[block], stop_reason="end_turn")

    class _FakeAnthropic:
        def __init__(self, *, api_key: str) -> None:
            self.messages = _FakeMessages()

    monkeypatch.setitem(
        sys.modules, "anthropic", SimpleNamespace(Anthropic=_FakeAnthropic)
    )

    AnthropicProvider(api_key="sk-test").propose(
        _one_member_cluster(), today="2026-05-20"
    )
    assert captured.get("timeout") == DEFAULT_TIMEOUT


def test_openai_provider_passes_request_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys
    from types import SimpleNamespace

    from bettermemory.llm import DEFAULT_TIMEOUT, OpenAIProvider

    captured: dict[str, object] = {}

    class _FakeCompletions:
        def create(self, **kwargs: object) -> object:
            captured.update(kwargs)
            message = SimpleNamespace(content='{"proposals": []}')
            choice = SimpleNamespace(message=message, finish_reason="stop")
            return SimpleNamespace(choices=[choice])

    class _FakeChat:
        def __init__(self) -> None:
            self.completions = _FakeCompletions()

    class _FakeOpenAI:
        def __init__(self, *, api_key: str) -> None:
            self.chat = _FakeChat()

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=_FakeOpenAI))

    OpenAIProvider(api_key="sk-test").propose(_one_member_cluster(), today="2026-05-20")
    assert captured.get("timeout") == DEFAULT_TIMEOUT

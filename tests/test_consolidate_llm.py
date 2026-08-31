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

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from bettermemory.consolidate import (
    build_transcript_cluster,
    consolidate_llm,
    run_auto_consolidate,
)
from bettermemory.events import Recorder
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
from bettermemory.origin import Origin
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
                # A DURABLE replacement body — the branch runs the same
                # body-content gates `memory_update` runs, and the prior
                # fixture's "the new auth flow" is a transient marker
                # ("the new"). The transient refusal has its own test in
                # the body-replacing-gates section below.
                new_body="we shipped 2026-05-20 the revised auth flow\n",
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


def test_load_transcript_survives_raw_unicode_line_separators(
    tmp_path: Path,
) -> None:
    """A `.jsonl` row whose content embeds RAW U+2028/U+2029 (legal
    inside JSON strings — Node's JSON.stringify emits U+2028 unescaped)
    must still parse. The old `raw.splitlines()` walk shattered such a
    row into fragments that failed `json.loads` and silently vanished
    from the candidate window; splitting on "\\n" only keeps the row
    whole. Mirrors the hook._extract_last_exchange fix."""
    from bettermemory.consolidate import _load_transcript

    row = {
        "type": "user",
        "message": {"content": "postgres runs on port 5433 in staging"},
    }
    path = tmp_path / "session.jsonl"
    path.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    flattened = _load_transcript(path)
    assert "port 5433" in flattened, (
        "the U+2028-bearing row was dropped — _load_transcript is "
        "splitting on unicode line separators again"
    )


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


def test_build_transcript_cluster_skips_synthetic_user_rows(tmp_path: Path) -> None:
    """Regression: `_load_transcript` flattened EVERY `type="user"` JSONL
    row into `[user]` text — `isMeta: true` skill/command expansions,
    `<system-reminder>` injections, `<command-name>` bookkeeping — so the
    transcript_facts cluster handed harness documentation prose to the
    LLM as conversation and `consolidate --llm --from-transcript`
    proposed "facts" distilled from it. Mirrors the round-84 hook.py fix
    (`hook._SYNTHETIC_USER_PREFIXES` plus the row-level `isMeta` check):
    only the human's own row and assistant rows survive."""
    store = _make_store_with_existing(tmp_path)
    rows: list[dict[str, Any]] = [
        # Skill expansion: no envelope tag, row-level isMeta flag only.
        {
            "type": "user",
            "isMeta": True,
            "message": {"content": "# Skill instructions\nAlways prefer prose."},
        },
        # All six envelope tags hook.py treats as synthetic. One carries
        # leading whitespace to exercise the lstrip() before the check.
        {
            "type": "user",
            "message": {"content": "<task-notification>build finished"},
        },
        {"type": "user", "message": {"content": "<command-name>/audit-loop"}},
        {"type": "user", "message": {"content": "<command-message>tick</…>"}},
        {"type": "user", "message": {"content": "<local-command-stdout>ok"}},
        {"type": "user", "message": {"content": "<local-command-caveat>n/a"}},
        {
            "type": "user",
            "message": {"content": "\n  <system-reminder>Use MCP tools only."},
        },
        # The real exchange — the only rows allowed through.
        {"type": "user", "message": {"content": "My Postgres is on 5433."}},
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Saved."}]},
        },
    ]
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )
    cluster = build_transcript_cluster(
        transcript_path=transcript,
        memories=store.load_all(),
        events=[],
    )
    assert cluster is not None
    assert "[user] My Postgres is on 5433." in cluster.transcript
    assert "[assistant] Saved." in cluster.transcript
    # Exactly one user line — every synthetic row was dropped.
    assert cluster.transcript.count("[user]") == 1
    for marker in (
        "Skill instructions",
        "<task-notification>",
        "<command-name>",
        "<command-message>",
        "<local-command-stdout>",
        "<local-command-caveat>",
        "<system-reminder>",
    ):
        assert marker not in cluster.transcript, (
            f"synthetic row {marker!r} leaked into the cluster transcript"
        )


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


def test_propose_new_shared_excerpt_distinct_facts_both_commit(tmp_path: Path) -> None:
    """Regression: the similarity gates must judge the LLM-authored
    claim (proposal.body), NOT body_with_provenance — same scoping
    rationale as the transient-marker gate above. The provenance stamp
    is system-manufactured boilerplate shared by construction between
    every proposal citing the same turn: two distinct facts distilled
    from one user sentence carry an identical excerpt whose tokens
    dominate the Jaccard sets (0.882 stamped vs 0.10 unstamped), so
    gating on the stamped text bounced the second genuine fact as a
    'near-duplicate'. Both must commit."""
    store = _make_store_with_existing(tmp_path)
    transcript = tmp_path / "session.md"
    excerpt = (
        "My dotfiles live in ~/dotfiles and I manage them with GNU stow; "
        "my shell is zsh with the starship prompt."
    )
    transcript.write_text(f"[user] {excerpt}\n[assistant] Saved.", encoding="utf-8")
    proposals: list[Proposal] = [
        ProposeNewProposal(
            scope="personal-context",
            category="fact",
            body="User manages dotfiles in ~/dotfiles with GNU stow.",
            source_excerpt=excerpt,
            rationale="dotfiles location fact",
        ),
        ProposeNewProposal(
            scope="personal-context",
            category="fact",
            body="User's shell is zsh with the starship prompt.",
            source_excerpt=excerpt,
            rationale="shell setup fact",
        ),
    ]
    provider = FakeProvider(proposals=proposals)

    applied = consolidate_llm(
        store, provider, apply=True, accept=True, from_transcript=str(transcript)
    )
    new_actions = [a for a in applied.actions_taken if a.kind == "llm_propose_new"]
    assert len(new_actions) == 2, (
        f"both distinct facts from the shared turn must commit; "
        f"failures: {[(f.cluster_id, f.reason) for f in applied.failures]}"
    )
    assert not applied.failures
    # 1 pre-existing + 2 new.
    assert len(store.load_all()) == 3


def test_stamped_propose_new_facts_survive_auto_consolidate(tmp_path: Path) -> None:
    """Regression (round-88 RED), end-to-end: the write gate already
    judged the unstamped claims (the two distinct facts above commit at
    ~0.11 unstamped similarity), but the consolidate dedup pass
    tokenized the PERSISTED stamped bodies — the shared
    `--from-transcript` stamp pushed two distinct facts to ~0.93
    Jaccard, above even the unattended 0.90 threshold, and the Stop
    hook's `run_auto_consolidate` (apply=True, no human) tombstoned one
    of them. Both must survive the auto pass now that the dedup paths
    strip the stamp the same way the write gate does."""
    store = _make_store_with_existing(tmp_path)
    transcript = tmp_path / "session.md"
    excerpt = (
        "My dotfiles live in ~/dotfiles and I manage them with GNU stow; "
        "my shell is zsh with the starship prompt."
    )
    transcript.write_text(f"[user] {excerpt}\n[assistant] Saved.", encoding="utf-8")
    proposals: list[Proposal] = [
        ProposeNewProposal(
            scope="personal-context",
            category="fact",
            body="User manages dotfiles in ~/dotfiles with GNU stow.",
            source_excerpt=excerpt,
            rationale="dotfiles location fact",
        ),
        ProposeNewProposal(
            scope="personal-context",
            category="fact",
            body="User's shell is zsh with the starship prompt.",
            source_excerpt=excerpt,
            rationale="shell setup fact",
        ),
    ]
    provider = FakeProvider(proposals=proposals)
    applied = consolidate_llm(
        store, provider, apply=True, accept=True, from_transcript=str(transcript)
    )
    assert sum(1 for a in applied.actions_taken if a.kind == "llm_propose_new") == 2
    assert len(store.load_all()) == 3  # 1 pre-existing + 2 stamped facts

    result = run_auto_consolidate(
        store,
        recorder=Recorder(root=tmp_path, session_id="sess_auto"),
        session_id="sess_auto",
        interval_hours=24.0,
        max_memories=500,
    )
    assert result is not None and result["status"] == "ran"
    assert result["tombstoned"] == 0, (
        "the unattended pass tombstoned a genuine fact off the shared provenance stamp"
    )
    assert len(store.load_all()) == 3  # nothing merged away


def test_propose_new_true_near_duplicate_still_bounces(tmp_path: Path) -> None:
    """Counterpart to the shared-excerpt regression: gating on
    proposal.body must NOT weaken true-duplicate detection. A proposal
    whose claim restates an existing memory still bounces with the
    high-overlap rejection."""
    store = _make_store_with_existing(tmp_path)
    transcript = tmp_path / "session.md"
    transcript.write_text(
        "[user] The metrics dashboard runs at grafana.internal/d/api-latency.\n"
        "[assistant] Noted.",
        encoding="utf-8",
    )
    proposal = ProposeNewProposal(
        scope="infrastructure",
        category="fact",
        # Restates the memory _make_store_with_existing already wrote.
        body="The metrics dashboard runs at grafana.internal/d/api-latency.",
        source_excerpt=(
            "The metrics dashboard runs at grafana.internal/d/api-latency."
        ),
        rationale="infrastructure fact",
    )
    provider = FakeProvider(proposals=[proposal])

    applied = consolidate_llm(
        store, provider, apply=True, accept=True, from_transcript=str(transcript)
    )
    assert not any(a.kind == "llm_propose_new" for a in applied.actions_taken)
    assert applied.failures
    assert "high-overlaps existing memory" in applied.failures[0].reason
    assert len(store.load_all()) == 1  # nothing new written


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
        def __init__(self, *, api_key: str, max_retries: int = 2) -> None:
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
        def __init__(self, *, api_key: str, max_retries: int = 2) -> None:
            self.chat = _FakeChat()

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=_FakeOpenAI))

    OpenAIProvider(api_key="sk-test").propose(_one_member_cluster(), today="2026-05-20")
    assert captured.get("timeout") == DEFAULT_TIMEOUT


def test_propose_new_persists_inferred_source(tmp_path: Path) -> None:
    """Regression: an LLM-distilled propose_new memory is machine-inferred,
    not user-stated, so it must persist with source=INFERRED — matching the
    accept-proposal path (handlers/proposals.py, ingest.py). The write used to
    omit source= and default to Source.EXPLICIT, defeating the provenance
    distinction between what the user said and what the LLM inferred.
    """
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

    applied = consolidate_llm(
        store,
        provider,
        apply=True,
        accept=True,
        from_transcript=str(transcript),
    )
    assert any(a.kind == "llm_propose_new" for a in applied.actions_taken)
    assert not applied.failures, f"propose_new failed unexpectedly: {applied.failures}"

    memories_after = store.load_all()
    assert len(memories_after) == 2
    new_memory = next(m for m in memories_after if "port 5433" in m.body)
    # Machine-distilled content — INFERRED, not the write() EXPLICIT default.
    assert new_memory.source == Source.INFERRED


# ---------------------------------------------------------------------------
# LLM-write guardrails memory_write enforces but propose_new used to skip
# ---------------------------------------------------------------------------


def test_propose_new_refuses_credential_in_provenance_excerpt(
    tmp_path: Path,
) -> None:
    """Regression (credential bypass): `_apply_llm_proposal` must scan the
    STAMPED body (body + provenance excerpt) for credential markers before
    writing, mirroring `handlers/write.py`'s credential-first gate. The
    `source_excerpt` is a verbatim transcript quote, so a secret the user
    pasted mid-turn rides into the persisted body even when the LLM-authored
    claim is clean. Without the gate, `consolidate --llm --from-transcript
    --apply --yes` writes the secret-bearing body straight to disk.
    """
    store = _make_store_with_existing(tmp_path)
    transcript = tmp_path / "session.md"
    # Canonical AWS example access-key-id shape — detected by
    # `credentials.find_credential_markers` as `aws-access-key-id`.
    secret = "AKIAIOSFODNN7EXAMPLE"
    transcript.write_text(
        f"[user] our deploy aws key is {secret}\n[assistant] Noted.",
        encoding="utf-8",
    )
    proposal = ProposeNewProposal(
        scope="infrastructure",
        category="fact",
        # Durable claim is CLEAN — the secret lives only in the excerpt,
        # so a fix that scanned proposal.body alone would still leak it.
        body="The deploy pipeline authenticates to AWS with a stored access key.",
        source_excerpt=f"our deploy aws key is {secret}",
        rationale="infra credential fact",
    )
    provider = FakeProvider(proposals=[proposal])

    applied = consolidate_llm(
        store, provider, apply=True, accept=True, from_transcript=str(transcript)
    )
    # Refused: no write, a failure carrying the credential rejection reason.
    assert not any(a.kind == "llm_propose_new" for a in applied.actions_taken)
    assert applied.failures
    assert "secret-shaped token" in applied.failures[0].reason
    # The store is untouched — only the pre-existing memory remains, and the
    # secret never landed on disk.
    remaining = store.load_all()
    assert len(remaining) == 1
    assert not any(secret in m.body for m in remaining)


def test_propose_new_stamps_caller_origin(tmp_path: Path) -> None:
    """Regression (origin=None leak): the propose_new `store.write` must
    thread the caller's `origin` through. Omitting it persists
    `origin=None`, which `origin.should_include_for_caller` treats as
    global — the LLM-distilled memory then surfaces in every scope and
    worktree instead of only where it was distilled. The accept-proposal
    sibling stamps `origin=capture(...)` for the same reason.
    """
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

    origin = Origin(
        cwd="/work/proj",
        repo="git@github.com:me/proj.git",
        worktree_root="/work/proj",
    )
    applied = consolidate_llm(
        store,
        provider,
        apply=True,
        accept=True,
        from_transcript=str(transcript),
        origin=origin,
    )
    assert any(a.kind == "llm_propose_new" for a in applied.actions_taken)
    assert not applied.failures, f"propose_new failed unexpectedly: {applied.failures}"

    new_memory = next(m for m in store.load_all() if "port 5433" in m.body)
    # A non-None origin carrying the caller's repo — reverting the
    # `origin=origin` thread-through lands origin=None and fails here.
    assert new_memory.origin is not None
    assert new_memory.origin.repo == "git@github.com:me/proj.git"


def test_propose_new_rejects_scope_outside_allowlist(tmp_path: Path) -> None:
    """Regression (scopes allowlist bypass): the propose_new write goes
    straight through `store.write`, bypassing `_validate_write_payload`'s
    `allowed_scopes` gate, so the `[scopes] allowed` config would be a
    no-op on this path. `_apply_llm_proposal` must reject a proposal whose
    scope is not in the (non-empty) allowlist.
    """
    store = _make_store_with_existing(tmp_path)
    transcript = tmp_path / "session.md"
    transcript.write_text(
        "[user] My Postgres is on port 5433, not 5432.\n[assistant] Saved.",
        encoding="utf-8",
    )
    proposal = ProposeNewProposal(
        scope="rogue-scope",
        category="fact",
        body="Postgres listens on port 5433, not the default 5432.",
        source_excerpt="[user] My Postgres is on port 5433, not 5432.",
        rationale="user-stated infrastructure fact",
    )
    provider = FakeProvider(proposals=[proposal])

    applied = consolidate_llm(
        store,
        provider,
        apply=True,
        accept=True,
        from_transcript=str(transcript),
        allowed_scopes=["infrastructure", "tools"],
    )
    # Refused: scope not sanctioned, nothing written.
    assert not any(a.kind == "llm_propose_new" for a in applied.actions_taken)
    assert applied.failures
    assert "not in the configured" in applied.failures[0].reason
    assert len(store.load_all()) == 1


def test_propose_new_allows_in_allowlist_scope(tmp_path: Path) -> None:
    """Positive control for the allowlist gate: a proposal whose scope IS
    in the allowlist still writes. Guards against an over-broad reject that
    would bounce every propose_new once an allowlist is configured."""
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

    applied = consolidate_llm(
        store,
        provider,
        apply=True,
        accept=True,
        from_transcript=str(transcript),
        allowed_scopes=["infrastructure", "tools"],
    )
    assert any(a.kind == "llm_propose_new" for a in applied.actions_taken)
    assert not applied.failures, f"propose_new failed unexpectedly: {applied.failures}"
    assert len(store.load_all()) == 2


def test_anthropic_provider_disables_sdk_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The Anthropic SDK defaults to max_retries=2 and retries APITimeoutError,
    # so without max_retries=0 the `timeout=` bound stacks up to 3x against a
    # hung provider. Assert the client is constructed with retries disabled so
    # the timeout is a true single-shot wall-clock bound.
    import sys
    from types import SimpleNamespace

    from bettermemory.llm import AnthropicProvider

    captured_ctor: dict[str, object] = {}

    class _FakeMessages:
        def create(self, **kwargs: object) -> object:
            block = SimpleNamespace(type="text", text='{"proposals": []}')
            return SimpleNamespace(content=[block], stop_reason="end_turn")

    class _FakeAnthropic:
        def __init__(self, *, api_key: str, max_retries: int = 2) -> None:
            captured_ctor["api_key"] = api_key
            captured_ctor["max_retries"] = max_retries
            self.messages = _FakeMessages()

    monkeypatch.setitem(
        sys.modules, "anthropic", SimpleNamespace(Anthropic=_FakeAnthropic)
    )

    AnthropicProvider(api_key="sk-test").propose(
        _one_member_cluster(), today="2026-05-20"
    )
    assert captured_ctor.get("max_retries") == 0


def test_openai_provider_disables_sdk_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Same rationale as the Anthropic branch: the OpenAI SDK defaults to
    # max_retries=2 and retries timeouts, so max_retries=0 is required for the
    # `timeout=` argument to bound the call to a single attempt.
    import sys
    from types import SimpleNamespace

    from bettermemory.llm import OpenAIProvider

    captured_ctor: dict[str, object] = {}

    class _FakeCompletions:
        def create(self, **kwargs: object) -> object:
            message = SimpleNamespace(content='{"proposals": []}')
            choice = SimpleNamespace(message=message, finish_reason="stop")
            return SimpleNamespace(choices=[choice])

    class _FakeChat:
        def __init__(self) -> None:
            self.completions = _FakeCompletions()

    class _FakeOpenAI:
        def __init__(self, *, api_key: str, max_retries: int = 2) -> None:
            captured_ctor["api_key"] = api_key
            captured_ctor["max_retries"] = max_retries
            self.chat = _FakeChat()

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=_FakeOpenAI))

    OpenAIProvider(api_key="sk-test").propose(_one_member_cluster(), today="2026-05-20")
    assert captured_ctor.get("max_retries") == 0


# ---------------------------------------------------------------------------
# The hand-rolled gates nothing executed
# ---------------------------------------------------------------------------
#
# `docs/ROADMAP.md` and the `_WRITE_GATES` header comment
# (`src/bettermemory/handlers/write.py`) both defend these gates as a
# DELIBERATE divergence from the shared `apply_write_gates` chain: they judge
# the LLM-authored claim (`proposal.body`), not the stamped body that
# persists, and rerouting them would silently change which text each rule
# reads. `tests/test_proposals_gate_parity.py` exists to stop anyone
# converting them.
#
# That whole argument rests on the gates working, and an audit on 2026-08-04
# found three of the five refusal arms were unexecuted code — deleting the
# transient gate outright passed all 4,541 tests, and the parity test that
# names itself their guardian asserts on `consolidate.py`'s SOURCE TEXT, so
# it passes over a gate that could not fire. The credential and scope-
# allowlist arms were already covered above; these three close the rest.
#
# Each is written so the mutation it catches is the deletion of the arm it
# names, not merely "a RuntimeError happened": every one asserts on the
# specific reason string AND on the store being unchanged, because a gate
# that refuses after writing would be the worse bug.


def test_propose_new_rejects_a_body_carrying_transient_markers(
    tmp_path: Path,
) -> None:
    """The durability gate, scoped to `proposal.body`.

    `memory_write` answers a transient body with `transient_warning` and an
    `acknowledge_transient` escape. This path has no one to ask — the
    comment in `_apply_llm_proposal` says so — so the only correct move is
    to refuse. Note the SOURCE EXCERPT is deliberately transient-heavy too:
    it is a verbatim conversational turn, and scanning the stamped body
    would bounce almost every genuine proposal on a marker in the citation
    rather than in the claim. If someone reroutes this gate onto
    `body_with_provenance`, the positive controls elsewhere in this module
    go red, not this test.
    """
    store = _make_store_with_existing(tmp_path)
    transcript = tmp_path / "session.md"
    transcript.write_text(
        "[user] right now the deploy is broken\n[assistant] Noted.",
        encoding="utf-8",
    )
    proposal = ProposeNewProposal(
        scope="infrastructure",
        category="fact",
        body="Currently the deploy is broken and we are fixing it today.",
        source_excerpt="[user] right now the deploy is broken",
        rationale="user-reported state",
    )
    provider = FakeProvider(proposals=[proposal])

    applied = consolidate_llm(
        store,
        provider,
        apply=True,
        accept=True,
        from_transcript=str(transcript),
    )

    assert not any(a.kind == "llm_propose_new" for a in applied.actions_taken)
    assert applied.failures
    assert "transient markers" in applied.failures[0].reason
    assert len(store.load_all()) == 1


def test_propose_new_rejects_a_body_over_max_content_bytes(tmp_path: Path) -> None:
    """The size cap, and the one gate deliberately measured on the STAMPED
    body rather than the claim — the provenance line persists, so it counts
    against the cap. The body below is under the cap on its own and over it
    once stamped, which is the only way to tell the two readings apart."""
    store = _make_store_with_existing(tmp_path)
    transcript = tmp_path / "session.md"
    excerpt = "[user] " + "the postgres tuning notes are long. " * 12
    transcript.write_text(excerpt + "\n[assistant] Saved.", encoding="utf-8")
    body = "Postgres runs with shared_buffers at 4GB. " * 6
    proposal = ProposeNewProposal(
        scope="infrastructure",
        category="fact",
        body=body,
        source_excerpt=excerpt,
        rationale="user-stated infrastructure fact",
    )
    provider = FakeProvider(proposals=[proposal])
    cap = len(body.encode("utf-8")) + 10
    assert cap < len((body + excerpt).encode("utf-8")), (
        "fixture no longer distinguishes the stamped body from the claim"
    )

    applied = consolidate_llm(
        store,
        provider,
        apply=True,
        accept=True,
        from_transcript=str(transcript),
        max_content_bytes=cap,
    )

    assert not any(a.kind == "llm_propose_new" for a in applied.actions_taken)
    assert applied.failures
    assert "exceeds max_content_bytes" in applied.failures[0].reason
    assert len(store.load_all()) == 1


def test_propose_new_rejects_a_body_that_reopens_a_tombstone(tmp_path: Path) -> None:
    """The tombstone twin.

    The LLM sees ~8 cluster members as "don't duplicate these" and never
    sees the tombstone set at all, so without this arm `--llm
    --from-transcript` re-creates memories the user deliberately REMOVED —
    the one refusal on this path that protects a decision rather than a
    rule. The tombstone stands until an explicit `memory_restore`.
    """
    store = _make_store_with_existing(tmp_path)
    removed_body = (
        "The staging cluster is decommissioned and its DNS records were "
        "deleted in the 2026 migration."
    )
    removed = store.write(
        content=removed_body,
        scopes=["infrastructure"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
    )
    store.tombstone(removed.id, reason="superseded by the migration runbook")
    assert store.load_tombstones(), "fixture did not produce a tombstone"

    transcript = tmp_path / "session.md"
    transcript.write_text(
        "[user] remind me the staging cluster is gone\n[assistant] Noted.",
        encoding="utf-8",
    )
    proposal = ProposeNewProposal(
        scope="infrastructure",
        category="fact",
        body=removed_body,
        source_excerpt="[user] remind me the staging cluster is gone",
        rationale="user-stated infrastructure fact",
    )
    provider = FakeProvider(proposals=[proposal])

    applied = consolidate_llm(
        store,
        provider,
        apply=True,
        accept=True,
        from_transcript=str(transcript),
    )

    assert not any(a.kind == "llm_propose_new" for a in applied.actions_taken)
    assert applied.failures
    assert "previously-removed" in applied.failures[0].reason


def test_propose_new_rejects_a_body_that_reads_as_a_user_claim(
    tmp_path: Path,
) -> None:
    """The user-inference veto ceremony, on the one entry path that IS
    "a model inferring claims about the user from conversation". A
    third-person user claim ("Mattias prefers tabs") distilled as
    category=fact used to commit with no pending-confirm, where the
    byte-identical body through `memory_write` triggers
    `user_claim_warning`. `_validate_propose_new` whitelists only
    fact/ambient (the user-inference tier needs a confirmation this
    pass can't supply), so the body cannot be rerouted into staging —
    only refused. Scoped to `proposal.body` like the transient gate:
    the excerpt is a verbatim user turn whose first-person phrasing
    ("i prefer …") must NOT bounce the proposal — the positive
    controls above ("My Postgres is on port 5433…" excerpts) pin that
    side.
    """
    store = _make_store_with_existing(tmp_path)
    transcript = tmp_path / "session.md"
    transcript.write_text(
        "[user] i prefer tabs over spaces for indentation\n[assistant] Noted.",
        encoding="utf-8",
    )
    proposal = ProposeNewProposal(
        scope="personal-context",
        category="fact",
        body="Mattias prefers tabs over spaces for indentation.",
        source_excerpt="i prefer tabs over spaces for indentation",
        rationale="user preference",
    )
    provider = FakeProvider(proposals=[proposal])

    applied = consolidate_llm(
        store, provider, apply=True, accept=True, from_transcript=str(transcript)
    )

    assert not any(a.kind == "llm_propose_new" for a in applied.actions_taken)
    assert applied.failures
    assert "claim about the user" in applied.failures[0].reason
    assert len(store.load_all()) == 1  # nothing written


# ---------------------------------------------------------------------------
# Body-replacing branches: verification reset + replacement-body gates
# ---------------------------------------------------------------------------
#
# The merge and rewrite_date branches persist an LLM-authored REPLACEMENT
# body. Two regression families live here:
#
# * the verification/claims reset `handlers/update.py` applies on every
#   content edit must fire on these branches too — round-88 (696bb5d)
#   fixed `preserve_verification` on the metadata-only branches, but the
#   body-edit branches carried `last_verified_at` / `verified_*` /
#   `claims` verbatim onto the new prose, presenting an unreviewed LLM
#   fusion as `staleness_verdict="fresh"` (trust-machinery false-fresh);
# * the body-content gates `memory_update` runs on the equivalent
#   interactive edit surface (credential / transient / size) must judge
#   `proposal.new_body`, hard-refusing like the propose_new arms above —
#   and refusing BEFORE any mutation, because a gate that refuses after
#   the keeper update or a tombstone would be the worse bug.


def test_merge_resets_verification_and_claims_on_the_new_body(
    tmp_path: Path,
) -> None:
    """The merge branch used to build `update_fields = {"body": …}` bare,
    so the keeper's pre-merge attestation rode verbatim onto the
    LLM-authored fusion — a body no human or agent ever verified then
    presented `staleness_verdict="fresh"` on every subsequent
    search/show, and its carried `claims` attached declare-time promises
    to prose that never declared them."""
    store, a_id, b_id = _make_store_with_dupes(tmp_path)
    store.mark_verified(
        a_id,
        verified_paths=["src/deploy.py"],
        claims=["src/deploy.py::PORT=5432"],
    )
    assert store.load_one(a_id).last_verified_at is not None
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

    report = consolidate_llm(store, provider, apply=True, accept=True)

    assert any(a.kind == "llm_merge_keeper" for a in report.actions_taken)
    merged = store.load_one(a_id)
    assert "queue + worker" in merged.body
    # The attestation described the pre-merge prose; carrying it onto
    # the fusion would lie about the new body.
    assert merged.last_verified_at is None
    assert merged.verified_paths == []
    assert merged.claims == []


def test_rewrite_date_resets_verification_and_claims_on_the_new_body(
    tmp_path: Path,
) -> None:
    """Same reset, rewrite_date branch — the arm that specifically
    targets OLDER (hence plausibly verified) memories."""
    store, a_id, _b = _make_store_with_dupes(tmp_path)
    store.mark_verified(
        a_id,
        verified_paths=["src/deploy.py"],
        verified_commits=["abc1234"],
        claims=["src/deploy.py::PORT=5432"],
    )
    provider = FakeProvider(
        proposals=[
            RewriteRelativeDateProposal(
                memory_id=a_id,
                new_body="we shipped 2026-05-20 the revised auth flow\n",
                rationale="today -> 2026-05-20",
            )
        ]
    )

    report = consolidate_llm(store, provider, apply=True, accept=True)

    assert any(a.kind == "llm_rewrite_date" for a in report.actions_taken)
    rewritten = store.load_one(a_id)
    assert "2026-05-20" in rewritten.body
    assert rewritten.last_verified_at is None
    assert rewritten.verified_paths == []
    assert rewritten.verified_commits == []
    assert rewritten.claims == []


def test_merge_rejects_a_new_body_carrying_a_credential(tmp_path: Path) -> None:
    """`memory_update` refuses a credential-shaped token on every body
    edit; the merge branch used to commit one straight through
    `--apply --yes`. Refused before any mutation: keeper body unchanged
    AND the duplicate still active, and the secret never lands on
    disk."""
    store, a_id, b_id = _make_store_with_dupes(tmp_path)
    # Canonical AWS example access-key-id shape, as in the propose_new
    # credential test above.
    secret = "AKIAIOSFODNN7EXAMPLE"
    provider = FakeProvider(
        proposals=[
            MergeProposal(
                keeper_id=a_id,
                duplicate_ids=(b_id,),
                new_body=f"postgres on port 5432; deploy key {secret}\n",
                rationale="combined",
            )
        ]
    )

    report = consolidate_llm(store, provider, apply=True, accept=True)

    assert not any(a.kind == "llm_merge_keeper" for a in report.actions_taken)
    assert report.failures
    assert "secret-shaped token" in report.failures[0].reason
    assert {m.id for m in store.load_all()} == {a_id, b_id}
    assert not any(secret in m.body for m in store.load_all())


def test_rewrite_date_rejects_a_new_body_carrying_transient_markers(
    tmp_path: Path,
) -> None:
    """An LLM rewrite can introduce transient phrasing the original
    gated body never carried ("we just switched…"). `memory_update`
    would answer with `transient_warning` and an escape hatch; this
    path has no one to ask, so it refuses and the target keeps its
    body."""
    store, a_id, _b = _make_store_with_dupes(tmp_path)
    body_before = store.load_one(a_id).body
    provider = FakeProvider(
        proposals=[
            RewriteRelativeDateProposal(
                memory_id=a_id,
                new_body="we just switched the queue to postgres on port 5432\n",
                rationale="today -> resolved",
            )
        ]
    )

    report = consolidate_llm(store, provider, apply=True, accept=True)

    assert not any(a.kind == "llm_rewrite_date" for a in report.actions_taken)
    assert report.failures
    assert "transient markers" in report.failures[0].reason
    assert store.load_one(a_id).body == body_before


def test_merge_rejects_a_new_body_over_max_content_bytes(tmp_path: Path) -> None:
    """The size cap `memory_update` applies to every body edit bounds
    the replacement body too — an over-cap LLM fusion refuses instead
    of committing, and both originals stay active."""
    store, a_id, b_id = _make_store_with_dupes(tmp_path)
    new_body = "postgres on port 5432 (queue + worker), " * 20 + "\n"
    provider = FakeProvider(
        proposals=[
            MergeProposal(
                keeper_id=a_id,
                duplicate_ids=(b_id,),
                new_body=new_body,
                rationale="combined",
            )
        ]
    )
    cap = len(new_body.encode("utf-8")) - 10

    report = consolidate_llm(
        store, provider, apply=True, accept=True, max_content_bytes=cap
    )

    assert not any(a.kind == "llm_merge_keeper" for a in report.actions_taken)
    assert report.failures
    assert "exceeds max_content_bytes" in report.failures[0].reason
    assert {m.id for m in store.load_all()} == {a_id, b_id}


# ---------------------------------------------------------------------------
# demote_tier vs a concurrent verify (the round-88 race, LLM branch)
# ---------------------------------------------------------------------------


def test_demote_tier_preserves_a_verify_landing_mid_pass(tmp_path: Path) -> None:
    """Attestation-loss race: `by_id` is snapshotted at consolidate_llm
    start and the window to the demote apply spans LLM provider calls
    (and interactive accept prompts) — minutes, not microseconds. A
    `memory_verify` landing in that window bumps `last_verified_at`
    WITHOUT bumping `updated`, so the W2 CAS cannot catch it; without
    `preserve_verification=True` the retag wrote the stale snapshot's
    empty verification fields back, silently erasing the attestation
    (and with it the memory's `_pick_keeper` Tier-0 standing). The
    sibling non-LLM demotion retag and dedup scope-merge already pass
    the flag for exactly this reason (round-88, 696bb5d)."""
    store, a_id, _b = _make_store_with_dupes(tmp_path)

    @dataclass
    class VerifyMidPassProvider:
        """Attests the demote target DURING the provider call — after
        consolidate_llm's `by_id` snapshot, before the apply."""

        name: str = "verify-mid-pass"

        def propose(self, cluster: Cluster, today: str) -> list[Proposal]:
            store.mark_verified(a_id, verified_paths=["src/deploy.py"])
            return [
                DemoteTierProposal(
                    memory_id=a_id,
                    new_category="ambient",
                    rationale="superseded",
                )
            ]

    report = consolidate_llm(store, VerifyMidPassProvider(), apply=True, accept=True)

    assert any(a.kind == "llm_demote_tier" for a in report.actions_taken)
    demoted = store.load_one(a_id)
    assert demoted.category == Category.AMBIENT
    # The concurrent attestation survives the retag.
    assert demoted.last_verified_at is not None
    assert demoted.verified_paths == ["src/deploy.py"]

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

from bettermemory.consolidate import consolidate_llm
from bettermemory.llm import (
    Cluster,
    DemoteTierProposal,
    MergeProposal,
    Proposal,
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
    assert provider.call_count == 0

"""Regression tests for the FTS prefilter cap-starvation guard (round 85).

`_handlers.load_search_candidates` threads only `scopes` into the FTS5
top-50 prefilter; the repo/worktree auto-scope filter and session-disabled
scopes apply post-cap in `search._filter_candidates`. Without the guard in
`handlers/search.py`, a store with >50 memories where the caller's
in-filter matches all rank #51+ globally returns ZERO hits on the
prefilter path even though matching memories exist — the cap-starvation
failure mode the `load_search_candidates` docstring itself names. The
guard dry-runs the authoritative filter on a cap-saturated slice and
reloads the full corpus when fewer than the caller's `min_survivors`
candidates survive — `max_results` for a `memory_search` request,
`default_search_width(behavior)` for the silent-miss audit producers,
which describe a search that never happened and so carry no request
width. The two widths differ on purpose; their RANGE does not, because
all of them go through `clamp_search_width`.

Lives in its own file (not test_search*.py — those belong to another
batch this round) and exercises the guard through the MCP tool surface,
mirroring tests/test_server_search_index.py.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from bettermemory import index
from bettermemory.audit import probe_for_miss
from bettermemory.config import BehaviorConfig, Config, StorageConfig
from bettermemory.handlers.search import resolve_search_pool
from bettermemory.hook import run_audit
from bettermemory.models import utcnow
from bettermemory.origin import Origin
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store

REPO_A = "git@github.com:example/repo-a.git"
REPO_B = "git@github.com:example/repo-b.git"


def _build_server(memory_dir: Path) -> Any:
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    return build_server(
        config=cfg,
        store=Store(memory_dir),
        state=SessionState(),
    )


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    content, structured = await server.call_tool(name, kwargs)
    if structured is not None:
        return structured
    if content and hasattr(content[0], "text"):
        return json.loads(content[0].text)
    return None


def _unwrap(res: Any) -> Any:
    return res.get("result", res) if isinstance(res, dict) and "result" in res else res


def _pin_caller_origin(monkeypatch: pytest.MonkeyPatch, origin: Origin) -> None:
    """Pin what `capture_origin` reports for the searching session.

    The handler resolves it through `_handlers` at call time
    (`from .. import _handlers as _h; _h.capture_origin()`), so patching
    the module binding is enough — same pattern as
    tests/test_server_origin.py, but monkeypatch-scoped so it can't leak
    across tests."""
    import bettermemory._handlers as handlers_module

    def fake_capture(cwd: Path | None = None) -> Origin:
        return origin

    monkeypatch.setattr(handlers_module, "capture_origin", fake_capture)


def _write_dense(store: Store, n: int, *, scopes: list[str], origin: Origin | None):
    """`n` short, term-dense "alpha" memories — high BM25, they
    monopolize the FTS top-50 candidate slice."""
    dense = "alpha " * 6
    for i in range(n):
        store.write(content=f"{dense}filler-{i}", scopes=scopes, origin=origin)


def _write_weak(store: Store, n: int, *, scopes: list[str], origin: Origin | None):
    """`n` long memories mentioning "alpha" exactly once — low BM25,
    ranked past the dense block. Returns their ids."""
    padding = " ".join(f"pad{j}" for j in range(40))
    ids = []
    for i in range(n):
        m = store.write(
            content=f"alpha appears once here {padding} doc-{i}",
            scopes=scopes,
            origin=origin,
        )
        ids.append(m.id)
    return ids


def _assert_starved_precondition(memory_dir: Path, weak_ids: list[str]) -> None:
    """Self-validating ranking precondition: the weak matches must rank
    past the 50-row FTS cap, or the test isn't exercising starvation."""
    top50 = {cid for cid, _ in index.query(memory_dir, "alpha", max_results=50)}
    assert len(top50) == 50
    assert not (set(weak_ids) & top50), (
        "precondition drift: weak matches landed inside the FTS top-50, "
        "so the prefilter slice is not starved — densify the decoys"
    )


async def test_repo_filter_starvation_reloads_full_corpus(
    memory_dir: Path, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The round-85 repro: 60 term-dense repo-B matches monopolize the
    FTS top-50; the caller's 3 repo-A matches rank #51+. The prefilter
    slice is 50 repo-B candidates, all of which the post-cap auto-scope
    filter strips — pre-guard, the search returned zero hits while
    matching in-repo memories existed. The guard detects the starved
    cap-saturated slice and reloads via load_all."""
    monkeypatch.setenv("BETTERMEMORY_INDEX_THRESHOLD", "1")
    _write_dense(store, 60, scopes=["tools"], origin=Origin(repo=REPO_B))
    a_ids = _write_weak(store, 3, scopes=["tools"], origin=Origin(repo=REPO_A))
    index.rebuild(memory_dir, store.iter_active())
    _assert_starved_precondition(memory_dir, a_ids)

    server = _build_server(memory_dir)
    _pin_caller_origin(monkeypatch, Origin(cwd="/projects/a", repo=REPO_A))

    hits = _unwrap(await _call(server, "memory_search", query="alpha"))
    assert {h["id"] for h in hits} == set(a_ids), (
        "auto-scoped search starved by the FTS top-50 cap: the in-repo "
        "matches ranked past the cap must still surface via the reload"
    )


async def test_disabled_scope_starvation_reloads_full_corpus(
    memory_dir: Path, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Session-disabled-scope variant: the post-cap filter here is
    `state.disabled_scopes` (no repo/worktree filter in play — the
    caller origin carries no repo). 60 dense matches in the disabled
    scope saturate the prefilter slice; the 3 in-scope matches rank
    past the cap and must still surface."""
    monkeypatch.setenv("BETTERMEMORY_INDEX_THRESHOLD", "1")
    _write_dense(store, 60, scopes=["noise"], origin=None)
    tool_ids = _write_weak(store, 3, scopes=["tools"], origin=None)
    index.rebuild(memory_dir, store.iter_active())
    _assert_starved_precondition(memory_dir, tool_ids)

    server = _build_server(memory_dir)
    _pin_caller_origin(monkeypatch, Origin(cwd="/tmp/elsewhere"))
    await _call(server, "memory_scope_disable", scope="noise")

    hits = _unwrap(await _call(server, "memory_search", query="alpha"))
    assert {h["id"] for h in hits} == set(tool_ids)


async def test_prefilter_path_still_serves_when_filters_leave_survivors(
    memory_dir: Path, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control pinning the guard's cost profile: when the post-cap
    filters leave at least `max_results` survivors in the cap-saturated
    slice, the search keeps serving from the prefilter candidates — no
    load_all reload. Spied via a counting wrapper on Store.load_all."""
    monkeypatch.setenv("BETTERMEMORY_INDEX_THRESHOLD", "1")
    _write_dense(store, 60, scopes=["tools"], origin=Origin(repo=REPO_A))
    index.rebuild(memory_dir, store.iter_active())

    server = _build_server(memory_dir)
    _pin_caller_origin(monkeypatch, Origin(cwd="/projects/a", repo=REPO_A))

    calls = {"n": 0}
    real_load_all = Store.load_all

    def counting_load_all(self: Store, *args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        return real_load_all(self, *args, **kwargs)

    monkeypatch.setattr(Store, "load_all", counting_load_all)

    hits = _unwrap(await _call(server, "memory_search", query="alpha"))
    assert len(hits) == 5  # default max_results, served from the slice
    assert calls["n"] == 0, (
        "the guard must not pay the load_all reload when the prefilter "
        "slice already has enough in-filter survivors"
    )


async def test_masked_saturation_still_triggers_reload(
    memory_dir: Path, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cap-saturated INDEX slice can arrive at the guard with fewer
    than 50 loaded candidates: the per-candidate skips in
    `load_search_candidates` (filename-lookup misses, id/body drift —
    here a backing file deleted after the index was built) shrink the
    loaded list below the cap. The old `len(memories) == 50` check
    never fired on such a slice, so the post-cap repo filter starved
    the search to zero hits while in-repo matches existed past the
    cap. The guard must key on the loader's index-level saturation
    signal, not the post-drop list length."""
    monkeypatch.setenv("BETTERMEMORY_INDEX_THRESHOLD", "1")
    _write_dense(store, 60, scopes=["tools"], origin=Origin(repo=REPO_B))
    a_ids = _write_weak(store, 3, scopes=["tools"], origin=Origin(repo=REPO_A))
    index.rebuild(memory_dir, store.iter_active())
    _assert_starved_precondition(memory_dir, a_ids)

    # Drop one in-slice candidate's backing file WITHOUT reindexing:
    # the index still serves a full 50-row slice, but the loader skips
    # the stale filename, so the guard sees only 49 loaded candidates.
    victim = next(cid for cid, _ in index.query(memory_dir, "alpha", max_results=50))
    victim_file = index.filenames_for_ids(memory_dir, [victim])[victim]
    (memory_dir / victim_file).unlink()

    server = _build_server(memory_dir)
    _pin_caller_origin(monkeypatch, Origin(cwd="/projects/a", repo=REPO_A))

    hits = _unwrap(await _call(server, "memory_search", query="alpha"))
    assert {h["id"] for h in hits} == set(a_ids), (
        "index-saturated slice masked by a loader drop: the in-repo matches "
        "ranked past the cap must still surface via the load_all reload"
    )


# ---------------------------------------------------------------------------
# BM25 corpus statistics across the prefilter boundary
# ---------------------------------------------------------------------------


async def test_prefiltered_search_prices_a_rare_term_off_the_whole_corpus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: with the FTS prefilter live, the IDF the scorer
    actually uses is the corpus's, not the candidate pool's.

    Two choices here are deliberate, and both were arrived at by watching
    the test FAIL to discriminate first:

    * The assertion is on the IDF value, not on result ORDER. Order is the
      more natural contract, but it does not discriminate: when every
      candidate in the pool shares the collapsed weight, the ordering
      among them is unchanged. The number is what broke.
    * The query is a SINGLE rare term. A two-term query like
      "alpha flock" matches every memory, so FTS fills the pool to its
      50-row cap with non-matching-on-`flock` rows and the pool df comes
      out at 3/50 — distorted, but not collapsed, and an assertion
      written against it passes on the unfixed code.

    With a lone rare term the pool IS the match set, every row contains
    the term, and pool df hits 3/3.
    """
    monkeypatch.setenv("BETTERMEMORY_INDEX_THRESHOLD", "1")
    store = Store(tmp_path)
    for i in range(60):
        store.write(content=f"alpha common filler-{i}", scopes=["tools"])
    for i in range(3):
        store.write(content=f"alpha flock rare-{i}", scopes=["tools"])

    seen: list[dict[str, float]] = []
    import bettermemory.search as search_module

    real_compute_idf = search_module.compute_idf

    def spy(memories: Any, **kwargs: Any) -> Any:
        body_idf, scope_idf, avgdl = real_compute_idf(memories, **kwargs)
        seen.append(body_idf)
        return body_idf, scope_idf, avgdl

    monkeypatch.setattr(search_module, "compute_idf", spy)

    server = _build_server(tmp_path)
    await _call(server, "memory_search", query="flock", max_results=5)

    assert seen, "BM25 never ran"
    flock_idf = max(m.get("flock", 0.0) for m in seen)
    # df=3 of N=63 gives a whole-corpus IDF of ~2.9. Priced off a pool of
    # 3 rows that all contain the term, it is ~0.13 — a 22x gap, and the
    # same arithmetic reaches 74x on the 600-memory shape this was first
    # measured on. Anything above 2.0 can only have come from the corpus.
    assert flock_idf > 2.0, (
        f"'flock' priced at IDF {flock_idf:.3f} — that is a pool-derived "
        "denominator; the corpus value for 3-of-63 is ~2.9"
    )


async def test_corpus_stats_are_not_consulted_below_the_prefilter_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Below the threshold the full corpus IS the candidate pool, so the
    lookup would be pure cost. The provider must not fire.

    Guards the "shipped ranking stays byte-stable for small stores" claim
    the change rests on — the overwhelming majority of stores are under
    500 memories and must see no behaviour change at all.
    """
    monkeypatch.setenv("BETTERMEMORY_INDEX_THRESHOLD", "100000")
    store = Store(tmp_path)
    for i in range(5):
        store.write(content=f"alpha filler-{i}", scopes=["tools"])

    calls: list[list[str]] = []
    import bettermemory.index as index_module

    real = index_module.corpus_document_frequencies

    def spy(root: Path, terms: Any, **kwargs: Any) -> Any:
        calls.append(list(terms))
        return real(root, terms, **kwargs)

    monkeypatch.setattr(index_module, "corpus_document_frequencies", spy)

    server = _build_server(tmp_path)
    await _call(server, "memory_search", query="alpha", max_results=5)

    assert calls == [], f"corpus lookup ran below the threshold: {calls}"


# ---------------------------------------------------------------------------
# `min_survivors`: the guard's width is per-caller, and it is load-bearing
# ---------------------------------------------------------------------------


def _build_width_divergence_store(root: Path) -> tuple[Store, list[str], list[str]]:
    """A store engineered so the in-filter survivors of the cap-saturated
    FTS slice land strictly between the two `min_survivors` widths.

    Three cohorts against the query "alpha beta":

    * 40 short repo-B decoys dense in BOTH terms — best BM25, they take
      the head of the top-50 slice, and the caller's repo filter strips
      every one of them post-cap.
    * 12 short origin-less memories mentioning only "alpha" — good enough
      BM25 to fill the rest of the slice, but coverage 1/2 = "medium"
      under `_relevance_label`, which is BELOW the miss threshold.
    * 8 origin-less memories that match both terms once each under ~400
      words of padding — coverage 2/2 = "high", sunk past the cap by BM25
      length normalisation.

    Origin-less memories pass the repo filter as global (so they survive
    post-cap) while carrying no `projects:` scope and no origin repo, which
    keeps `audit._caller_in_top_hit_project` out of the verdict.
    """
    store = Store(root)
    for i in range(40):
        store.write(
            content="alpha beta " * 6 + f"decoy-{i}",
            scopes=["tools"],
            origin=Origin(repo=REPO_B),
        )
    partial_ids = [
        store.write(
            content="alpha " * 6 + f"partial-{i}", scopes=["tools"], origin=None
        ).id
        for i in range(12)
    ]
    pad = " ".join(f"pad{j}" for j in range(400))
    full_ids = [
        store.write(
            content=f"alpha beta {pad} full-{i}", scopes=["tools"], origin=None
        ).id
        for i in range(8)
    ]
    index.rebuild(root, store.iter_active())
    return store, partial_ids, full_ids


def test_min_survivors_width_can_flip_the_probe_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The starvation width is not cosmetic: the same store, the same
    query and the same probe reach OPPOSITE verdicts at the two widths
    `resolve_search_pool`'s callers pass.

    `memory_search` passes the request's `max_results`; both silent-miss
    producers pass `behavior.default_max_results`, because the search they
    describe is the one the model did not make. This pins how far that
    difference travels — not to pool size, but all the way to the miss
    verdict, which reads only the rank-1 hit. It is the evidence
    `resolve_search_pool`'s docstring cites; if the widths are ever
    unified, this test is the thing that has to be re-argued.
    """
    monkeypatch.setenv("BETTERMEMORY_INDEX_THRESHOLD", "1")
    store, partial_ids, full_ids = _build_width_divergence_store(tmp_path)
    caller = Origin(cwd="/projects/a", repo=REPO_A, worktree_root="/projects/a")

    top50 = [cid for cid, _ in index.query(tmp_path, "alpha beta", max_results=50)]
    assert len(top50) == 50, "precondition: the FTS slice must be cap-saturated"
    assert not (set(full_ids) & set(top50)), (
        "precondition drift: the both-terms matches landed inside the FTS "
        "top-50, so the two widths would see the same pool — pad harder"
    )
    survivors = len(set(partial_ids) & set(top50))
    assert 5 < survivors < 25, (
        f"precondition drift: {survivors} in-filter survivors must sit "
        "strictly between the default width (5) and the request width (25), "
        "or only one side of the guard fires"
    )

    pools = {
        width: resolve_search_pool(
            store,
            "alpha beta",
            excluded_scopes=set(),
            repo_filter=caller.repo,
            worktree_filter=caller.worktree_root,
            min_survivors=width,
        )
        for width in (5, 25)
    }
    assert len(pools[5].memories) == 50, "the default width keeps the capped slice"
    assert len(pools[25].memories) == 60, "the request width reloads the full corpus"

    # `now` is pushed past the creation shield: every memory here was
    # written seconds ago and would otherwise be dropped as too young to
    # be retrieval-miss evidence.
    now = utcnow() + timedelta(hours=1)
    verdicts = {}
    rank1 = {}
    for width, pool in pools.items():
        report = probe_for_miss(
            pool.memories,
            "alpha beta",
            recent_events=[],
            session_id="sess-width",
            now=now,
            caller_origin=caller,
            excluded_scopes=set(),
            mode="hybrid",
            corpus_stats_provider=pool.corpus_stats_provider,
        )
        verdicts[width] = report.verdict
        rank1[width] = report.top_hits[0].id if report.top_hits else None

    assert verdicts == {5: "ok", 25: "miss"}, (
        f"expected the width to flip the verdict, got {verdicts}"
    )
    assert rank1[5] in partial_ids
    assert rank1[25] in full_ids


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        # In range: the width IS the knob, and a hardcoded 5 (or the
        # probe's own `_TOP_HITS_RETAINED`) cannot pass by coincidence.
        (7, 7),
        # Above the served cap. `config.py` range-checks nothing, so this
        # loads fine; unclamped it made the probe's guard unsatisfiable
        # (survivors are bounded by the 50-row prefilter slice, so
        # `len(survivors) < 100` always held) and every probe ranked the
        # whole corpus with no corpus-statistics provider.
        (100, 50),
        # At/below zero. Unclamped the guard could never fire, so the
        # probe kept a starved slice production reloads at width 1.
        (0, 1),
    ],
)
async def test_all_three_search_widths_come_from_one_clamp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    configured: int,
    expected: int,
) -> None:
    """Every `min_survivors` that reaches `resolve_search_pool` is the
    config knob narrowed by ONE expression — `clamp_search_width`, via
    `default_search_width` on the two silent-miss producers and via the
    request clamp on a `memory_search` that carries no `max_results`.

    The three used to disagree outside `[1, 50]`: `memory_search` clamped
    its request, the producers passed the raw knob to the identical
    parameter, and nothing validated the knob at load. So the audit's
    starvation guard could sit at a width production can never reach —
    measured at `default_max_results=100` as widths `[100, 100, 50]` and
    at `0` as `[0, 0, 1]`, with the latter flipping a probe verdict
    (`no_signal` where production's width 1 reports `miss`). The previous
    test shows how far a width difference travels; this one pins that the
    three cannot differ by RANGE at all.
    """
    monkeypatch.setenv("BETTERMEMORY_INDEX_THRESHOLD", "1")
    store = Store(tmp_path)
    for i in range(3):
        store.write(content=f"alpha beta filler-{i}", scopes=["tools"])
    index.rebuild(tmp_path, store.iter_active())

    cfg = Config(
        storage=StorageConfig(directory=str(tmp_path)),
        behavior=BehaviorConfig(default_max_results=configured),
    )

    import bettermemory.handlers.audit_turn as audit_turn_mod
    import bettermemory.handlers.search as search_mod

    widths: list[int] = []
    real = search_mod.resolve_search_pool

    def spy(*args: Any, **kwargs: Any) -> Any:
        widths.append(int(kwargs["min_survivors"]))
        return real(*args, **kwargs)

    monkeypatch.setattr(search_mod, "resolve_search_pool", spy)
    monkeypatch.setattr(audit_turn_mod, "resolve_search_pool", spy)

    run_audit(
        user_message="alpha beta",
        assistant_response="hi",
        session_id="sess-widths",
        config=cfg,
    )
    server = build_server(config=cfg, store=Store(tmp_path), state=SessionState())
    await _call(server, "memory_audit_turn", user_message="alpha beta")
    # Production's own default-width search — the thing the two probes
    # claim to describe. Omitting `max_results` is what makes it the
    # comparable width.
    await _call(server, "memory_search", query="alpha beta")

    assert widths == [expected, expected, expected], (
        "the Stop hook, the MCP audit tool and a default-width "
        f"memory_search must all size the starvation guard at {expected} "
        f"for default_max_results={configured}, got {widths}"
    )

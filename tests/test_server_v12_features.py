"""End-to-end tests for the v1.2 surface additions.

Covers the seven changes that landed together:

1. ``ambient`` memory category — write, persist, long-body warning,
   exclusion from dead-weight curation.
2. Dead-weight rule fix + ``cold_memories`` bucket.
3. ``staleness_verdict`` rollup field on every retrieval surface.
4. Auto-``record_use`` via response tokens.
5. ``curation_pending`` rollup in ``memory_scope_overview``.
6. ``scope_mismatch`` warning at ``memory_write`` time.
7. Structured ``verified_claims`` on ``memory_verify``.

Each section is grouped by the change it exercises so a future
maintainer can find the locking tests for one feature without
spelunking. The fixtures match the rest of `test_server.py` —
hermetic per-test memory dir, isolated SessionState.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import BehaviorConfig, Config, StorageConfig
from bettermemory.events import Recorder, iter_events
from bettermemory.origin import Origin
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store
from bettermemory.verify import (
    _VERDICT_FRESH,
    _VERDICT_RAISE_STATUSES,
    _VERDICT_RECOMMENDED,
    _VERDICT_REQUIRED,
)


@pytest.fixture
def memory_dir(tmp_path: Path) -> Path:
    return tmp_path / "memories"


@pytest.fixture
def server(memory_dir: Path) -> Any:
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    return build_server(
        config=cfg,
        store=Store(memory_dir),
        state=SessionState(),
    )


@pytest.fixture
def server_with_state(memory_dir: Path) -> tuple[Any, SessionState, Path]:
    """Variant fixture that exposes the SessionState so use-token tests
    can introspect what the auto-commit pass did. Mirrors `server`
    otherwise."""
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    state = SessionState()
    srv = build_server(
        config=cfg,
        store=Store(memory_dir),
        state=state,
    )
    return srv, state, memory_dir


@pytest.fixture
def stale_server(memory_dir: Path) -> Any:
    """Server with ``verification_stale_days=0`` so any verified memory
    is immediately classified ``stale`` by ``compute_verification_status``
    (any positive elapsed time after the verify call satisfies the
    ``age_seconds > 0`` threshold check). Drives the ``"stale"`` branch
    of the staleness-verdict gate without backdating timestamps."""
    cfg = Config(
        storage=StorageConfig(directory=str(memory_dir)),
        behavior=BehaviorConfig(verification_stale_days=0),
    )
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
    """FastMCP wraps `list[...]` tool responses as `{"result": [...]}` on
    the structured side. Mirror the helper from `test_server.py`."""
    return res.get("result", res) if isinstance(res, dict) and "result" in res else res


# ---------------------------------------------------------------------------
# Change 1 — ambient category
# ---------------------------------------------------------------------------


async def test_ambient_category_commits_immediately(server: Any) -> None:
    """Ambient skips the pending-write gate (same fast path as fact).

    `acknowledge_user_claim=True` because the body is a claim about the
    user filed as `ambient`, and `UserClaimGate` (index 2 of
    `_WRITE_GATES`) refuses exactly that shape — it only exempts
    `user-inference`. The flag keeps this test on its own axis: what is
    under test is that `ambient` takes the SAME fast path as `fact`
    past `PendingGate`, not how the user-claim gate classifies bodies
    (that lives in `tests/test_server_user_claims.py`)."""
    res = await _call(
        server,
        "memory_write",
        content="The user prefers terse code-driven explanations.",
        scopes=["learning-style"],
        category="ambient",
        acknowledge_user_claim=True,
    )
    assert res["status"] == "committed"
    assert res["category"] == "ambient"


async def test_ambient_category_persists_in_frontmatter(
    server: Any, memory_dir: Path
) -> None:
    """The new field round-trips through disk."""
    res = await _call(
        server,
        "memory_write",
        content="The user is based in Boston.",
        scopes=["personal-context"],
        category="ambient",
        acknowledge_user_claim=True,  # user claim filed as ambient; see above
    )
    files = list(memory_dir.glob("*.md"))
    assert len(files) == 1
    raw = files[0].read_text(encoding="utf-8")
    assert "category: ambient" in raw

    # Round-trip via memory_show.
    shown = await _call(server, "memory_show", id=res["id"])
    assert shown["category"] == "ambient"


async def test_ambient_category_long_body_emits_warning(server: Any) -> None:
    """A body of >500 words gets an `ambient_body_long` warning attached
    to the otherwise-successful commit."""
    long_body = " ".join(["word"] * 600)
    res = await _call(
        server,
        "memory_write",
        content=long_body,
        scopes=["personal-context"],
        category="ambient",
    )
    assert res["status"] == "committed"
    assert res.get("warnings") == ["ambient_body_long"]


async def test_ambient_category_short_body_no_warning(server: Any) -> None:
    res = await _call(
        server,
        "memory_write",
        content="Brief ambient context.",
        scopes=["personal-context"],
        category="ambient",
    )
    assert "warnings" not in res or res["warnings"] == []


async def test_fact_category_long_body_does_not_warn(server: Any) -> None:
    """The long-body warning is ambient-specific — fact memories are
    free to be long."""
    long_body = " ".join(["word"] * 600)
    res = await _call(
        server,
        "memory_write",
        content=long_body,
        scopes=["tools"],
        category="fact",
    )
    assert res["status"] == "committed"
    assert "warnings" not in res or res["warnings"] == []


async def test_unknown_category_rejected(server: Any) -> None:
    with pytest.raises(Exception, match="category must be one of"):
        await _call(
            server,
            "memory_write",
            content="x",
            scopes=["tools"],
            category="not-a-category",
        )


async def test_ambient_excluded_from_dead_weight_via_health(server: Any) -> None:
    """An ambient memory with no use signal must not appear in dead_weight."""
    written = await _call(
        server,
        "memory_write",
        content="User prefers code-driven tutorials.",
        scopes=["learning-style"],
        category="ambient",
        acknowledge_user_claim=True,  # user claim filed as ambient; see above
    )
    # Generate a search hit so the memory has retrieval_count > 0 — the
    # condition under which a non-ambient memory would land in dead_weight.
    await _call(server, "memory_search", query="code-driven tutorials")
    health = await _call(server, "memory_health", window_days=0)
    dead_ids = {m["id"] for m in health["dead_weight"]}
    cold_ids = {m["id"] for m in health["cold_memories"]}
    assert written["id"] not in dead_ids
    assert written["id"] not in cold_ids


# ---------------------------------------------------------------------------
# Change 2 — cold_memories bucket exposed via memory_health
# ---------------------------------------------------------------------------


async def test_cold_memories_field_returned_by_health(server: Any) -> None:
    """A fact memory never retrieved (no `memory_search`) and older than
    `window_days` lands in the `cold_memories` bucket — not
    `dead_weight`. The prior version of this test only asserted that
    the key existed and was a list, which a regression that always
    returned `[]` would pass. Drive the routing predicate end-to-end
    so the bucket has to actually carry the written id."""
    written = await _call(
        server,
        "memory_write",
        content="Database connection pool size is 32.",
        scopes=["infrastructure"],
    )
    # `window_days=0` makes any freshly-created memory older than the
    # cutoff (cutoff == now), so the cold predicate (`created < cutoff
    # AND retrieval_count == 0 AND not ambient`) holds without us
    # needing to backdate the file on disk.
    res = await _call(server, "memory_health", window_days=0)
    assert "cold_memories" in res
    cold = res["cold_memories"]
    assert isinstance(cold, list)
    cold_ids = {row["id"] for row in cold}
    assert written["id"] in cold_ids, (
        "never-retrieved fact memory must land in cold_memories"
    )
    # And NOT in dead_weight — that's the retrieved-but-not-applied
    # axis. Pinning both buckets ensures a regression that misroutes
    # cold memories into dead_weight (or vice versa) fails here.
    dead_ids = {row["id"] for row in res["dead_weight"]}
    assert written["id"] not in dead_ids


# ---------------------------------------------------------------------------
# Change 3 — staleness_verdict rollup
# ---------------------------------------------------------------------------


async def test_memory_show_includes_staleness_verdict(server: Any) -> None:
    res = await _call(server, "memory_write", content="A claim.", scopes=["tools"])
    shown = await _call(server, "memory_show", id=res["id"])
    # Never-verified memory → spot_check_required.
    assert shown["staleness_verdict"] == "spot_check_required"


async def test_memory_show_verdict_fresh_after_verify(server: Any) -> None:
    res = await _call(server, "memory_write", content="A claim.", scopes=["tools"])
    await _call(server, "memory_verify", id=res["id"], note="checked")
    shown = await _call(server, "memory_show", id=res["id"])
    assert shown["staleness_verdict"] == "fresh"


async def test_memory_search_hit_includes_staleness_verdict(
    server: Any,
) -> None:
    await _call(
        server,
        "memory_write",
        content="The widget configuration lives in /etc/widget.toml.",
        scopes=["tools"],
    )
    hits = _unwrap(await _call(server, "memory_search", query="widget configuration"))
    assert hits, "expected at least one hit"
    for hit in hits:
        assert "staleness_verdict" in hit
        # Never-verified, so the verdict is required regardless of drift.
        assert hit["staleness_verdict"] == "spot_check_required"


async def test_memory_search_expand_top_recomputes_verdict_on_drift(
    server: Any, tmp_path: Path
) -> None:
    """The expanded top hit re-runs path_drift against the actual body
    and updates the verdict — a fresh-verified memory whose ATTESTED
    path has since vanished is `spot_check_recommended`, not `fresh`."""
    # The cited path carries an EXTENSION on purpose. Since 3.25.2 a
    # non-existent leading-slash candidate with no extension and no
    # existing parent directory reads as an application route rather than
    # a deleted file (`_is_multi_segment_routelike`), so the old
    # extensionless `/this/path/does/not/exist-xyz` fixture no longer
    # produces a drift signal at all. That narrowing is deliberate; what
    # this test guards is the verdict RECOMPUTATION mechanism, so the
    # fixture just has to be a candidate that still reads as a file.
    #
    # It must also be an ATTESTED one now, for the same kind of reason:
    # since path drift got a provenance split, a never-attested path
    # scraped out of prose is advisory evidence and does not move the
    # verdict. A fabricated `/this/path/does/not/exist-xyz.py` cannot be
    # attested either (the write side refuses attestations it cannot
    # stat), so the fixture creates a real file, attests it, and deletes
    # it — verified-then-deleted, which is the class the verdict is
    # supposed to escalate on.
    cited = tmp_path / "expand-top-script.py"
    cited.write_text("print('hi')\n", encoding="utf-8")
    written = await _call(
        server,
        "memory_write",
        content=f"The script lives at `{cited}`.",
        scopes=["tools"],
    )
    await _call(server, "memory_verify", id=written["id"], verified_paths=[str(cited)])
    cited.unlink()
    hits = _unwrap(
        await _call(
            server,
            "memory_search",
            query="script lives at this path",
            expand_top=True,
        )
    )
    assert hits
    top = hits[0]
    if top.get("relevance") == "high":
        # Expanded path triggered.
        assert top["staleness_verdict"] == "spot_check_recommended"
        # The escalating evidence is NAMED, not just counted: the caller
        # can see which miss moved the tier without inferring it.
        assert top["path_drift"]["claim_anchored_missing"] == [str(cited)]


# ---------------------------------------------------------------------------
# Change 3 (cont.) — pin {never, stale} membership of the verdict gate
# ---------------------------------------------------------------------------
#
# `compute_staleness_verdict` (`verify.py`) and
# `ResponseBuilder.attach_commit_drift_counts` (`_response.py`) both
# branch on the same closed-protocol whitelist of verification.status
# values that force the rollup to `spot_check_required`. Since 3.0.0
# the two sites share `_VERDICT_RAISE_STATUSES`; the tests below pin
# both ends of the contract:
#
# - the membership guard (`test_staleness_verdict_raise_statuses_match_
#   frozenset`) catches *additions* to the source set — a new status
#   silently joining the raise list without a regression case;
# - the parametrised end-to-end tests catch *deletions* from the source
#   set — a member silently dropped, downgrading the loudest re-verify
#   signal the server emits. The list is hardcoded (not derived from
#   the frozenset itself); parametrising off the source would silently
#   skip the case when a member is removed instead of failing loudly.
#
# Negative-control: temporarily removing "stale" from
# `_VERDICT_RAISE_STATUSES` flips
# `test_staleness_verdict_via_show[stale]`,
# `test_staleness_verdict_via_search[stale]`,
# `test_staleness_verdict_stale_survives_commit_drift_recompute`, and
# `test_staleness_verdict_matches_across_show_and_search` from passing
# to failing (the stale memory's verdict regresses to
# `spot_check_recommended` or `fresh`); removing "never" flips the
# corresponding `[never]` parametrise cases. The membership guard also
# fails in either case.
#
# SCOPE NOTE, 3.30.0. "Every member forces `spot_check_required`" is no
# longer unconditionally true for `stale`, and these tests must not be
# read as claiming it is. A calendar-stale memory whose commit-drift
# leg returns a MEASURED ZERO now demotes to `fresh` — see
# `verify.compute_staleness_verdict` for why, and
# `tests/test_verify.py::test_verdict_ladder` for the full cell-by-cell
# contract. What keeps every case below valid is the fixture: these
# memories are written into a temp store with no origin repo the caller
# shares, so `compute_commit_drift` returns `None` ("the leg could not
# ask"), which is exactly the input that does NOT demote. So the tests
# still pin the raise path — they pin it for the silent-leg case, which
# is the common one and the one where the loud signal matters.

# Hardcoded so a deletion from `_VERDICT_RAISE_STATUSES` causes the
# corresponding parametrise case to fail (parametrising off the
# frozenset itself would just drop the case, silently). The membership
# guard below ensures additions still require touching this list.
_EXPECTED_RAISE_STATUSES: tuple[str, ...] = ("never", "stale")


_GIT_AVAILABLE = shutil.which("git") is not None
_FAKE_REPO_REMOTE = "git@github.com:example/staleness-verdict-test.git"


def _init_repo(path: Path, *, remote: str = _FAKE_REPO_REMOTE) -> None:
    """Tmp-repo helper mirroring `tests/test_server_commit_drift.py` so
    `attach_commit_drift_counts` has a real cwd to shell `git log`
    against. One commit is enough for `commit_author_timestamps` to
    return a non-None list, which is the gate for the recompute at
    `_response.py:406` to fire."""
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", remote],
        cwd=path,
        check=True,
        capture_output=True,
    )
    env = os.environ.copy()
    env["GIT_AUTHOR_DATE"] = "2020-01-01T00:00:00+00:00"
    env["GIT_COMMITTER_DATE"] = "2020-01-01T00:00:00+00:00"
    env["GIT_AUTHOR_NAME"] = "Test"
    env["GIT_AUTHOR_EMAIL"] = "test@example.com"
    env["GIT_COMMITTER_NAME"] = "Test"
    env["GIT_COMMITTER_EMAIL"] = "test@example.com"
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "ancient"],
        cwd=path,
        check=True,
        capture_output=True,
        env=env,
    )


def _build_stale_server_with_origin(memory_dir: Path, origin: Origin) -> Any:
    """Build a server with ``verification_stale_days=0`` whose
    ``capture_origin`` returns the supplied ``origin``. Mirrors the
    monkeypatch pattern from ``tests/test_server_commit_drift.py`` so the
    test can pin the caller's repo without altering the process cwd."""
    import bettermemory._handlers as handlers_module
    import bettermemory.server as server_module

    state = SessionState()
    cfg = Config(
        storage=StorageConfig(directory=str(memory_dir)),
        behavior=BehaviorConfig(verification_stale_days=0),
    )
    recorder = Recorder(root=memory_dir, session_id=state.session_id)
    srv = build_server(
        config=cfg,
        store=Store(memory_dir),
        state=state,
        recorder=recorder,
    )

    def fake_capture(cwd: Path | None = None) -> Origin:
        return origin

    setattr(handlers_module, "capture_origin", fake_capture)
    setattr(server_module, "capture_origin", fake_capture)
    return srv


async def _write_memory_in_state(server: Any, *, status: str) -> str:
    """Produce a memory whose ``verification.status`` resolves to
    ``status`` against the server's ``verification_stale_days``
    configuration. ``"never"`` skips the verify call; ``"stale"`` calls
    ``memory_verify`` once — paired with ``verification_stale_days=0``
    on ``server``, the immediately-elapsed wall time tips the verdict
    to stale on the next ``compute_verification_status`` call.

    The cited path is CREATED, ATTESTED and then DELETED, which is what
    keeps the tests below on the branch they mean to pin. They exist to
    prove that a raise-status memory with drift lands on
    ``spot_check_required`` — i.e. the ``drifty`` arm's
    ``_VERDICT_RAISE_STATUSES`` membership check. The fixture used to
    get its drift from a bare ``/etc/widget-staleness-pin.toml`` in
    prose, which stopped escalating when path-drift provenance was
    split: an unattested prose token is advisory evidence now, so every
    one of these tests silently slid off the drifty arm and onto the
    stale-demotion arm (they read ``fresh``, via a measured-zero commit
    leg — a real verdict, but not the one being pinned). Attesting the
    path before deleting it restores the escalating input on the same
    branch, and it is also the honest fixture: verified-then-deleted is
    the 3-of-3-real class the split was measured on.

    The temp directory is torn down whole. Parent-directory existence is
    irrelevant to this shape — an attested candidate skips every drop
    rule in ``detect_path_drift`` by design — but leaving the tree behind
    would make the fixture depend on it.
    """
    scratch = Path(tempfile.mkdtemp(prefix="bm-staleness-pin-"))
    try:
        cited = scratch / "widget-staleness-pin.toml"
        cited.write_text("[widget]\n", encoding="utf-8")
        content = f"The widget configuration lives in `{cited}`."
        written = await _call(server, "memory_write", content=content, scopes=["tools"])
        if status == "stale":
            await _call(
                server,
                "memory_verify",
                id=written["id"],
                note="seed",
                # Attest while the file is still there — the write side
                # refuses attestations the attesting machine cannot stat,
                # so the order here is load-bearing.
                verified_paths=[str(cited)],
            )
        elif status != "never":
            raise AssertionError(f"unexpected raise-status fixture: {status!r}")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return str(written["id"])


def test_staleness_verdict_raise_statuses_match_frozenset() -> None:
    """Guard so additions to ``_VERDICT_RAISE_STATUSES`` are mirrored in
    the parametrise list — otherwise a new status joining the raise
    set could ship without regression coverage on either site
    (``verify.py``'s ``compute_staleness_verdict`` or ``_response.py``'s
    ``attach_commit_drift_counts`` recompute)."""
    assert set(_EXPECTED_RAISE_STATUSES) == set(_VERDICT_RAISE_STATUSES)


@pytest.mark.parametrize("status", _EXPECTED_RAISE_STATUSES)
async def test_staleness_verdict_via_show(stale_server: Any, status: str) -> None:
    """Every member of ``_VERDICT_RAISE_STATUSES`` must drive
    ``memory_show``'s ``staleness_verdict`` to ``spot_check_required``
    with a non-null ``verification.recommendation``. Routes through
    ``compute_staleness_verdict`` at ``verify.py:848`` (the canonical
    gate). A silent drop of either member here lets the loudest
    re-verify signal — the verdict consumers branch on first — fall
    through to the drift-only ``spot_check_recommended`` or even
    ``fresh``, treating an unverified or expired memory as ground
    truth."""
    memory_id = await _write_memory_in_state(stale_server, status=status)
    shown = await _call(stale_server, "memory_show", id=memory_id)

    assert shown["verification"]["status"] == status
    assert shown["staleness_verdict"] == "spot_check_required"
    recommendation = shown["verification"]["recommendation"]
    assert recommendation is not None and recommendation.strip(), (
        "raise-status verdicts must carry an actionable recommendation; "
        "got an empty payload"
    )


@pytest.mark.parametrize("status", _EXPECTED_RAISE_STATUSES)
async def test_staleness_verdict_via_search(stale_server: Any, status: str) -> None:
    """Mirror of ``test_staleness_verdict_via_show`` on the
    ``memory_search`` surface. ``hit_to_dict`` in ``_response.py`` also
    routes through ``compute_staleness_verdict`` (``verify.py:848``);
    a silent drop there is independent of the ``memory_show`` path and
    would lower the top-hit verdict on every retrieval. Locks the
    search side of the contract too."""
    memory_id = await _write_memory_in_state(stale_server, status=status)
    # `auto_scope=False` keeps the test independent of the runner's
    # cwd — the memory's `origin.repo` would otherwise depend on
    # whether the test was invoked from inside a checkout.
    hits = _unwrap(
        await _call(
            stale_server,
            "memory_search",
            query="widget configuration staleness",
            auto_scope=False,
        )
    )
    assert hits, "expected at least one hit for the seeded memory"
    hit = next((h for h in hits if h["id"] == memory_id), None)
    assert hit is not None, f"seeded memory {memory_id!r} missing from search results"
    assert hit["verification"]["status"] == status
    assert hit["staleness_verdict"] == "spot_check_required"


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
async def test_staleness_verdict_stale_survives_commit_drift_recompute(
    memory_dir: Path, tmp_path: Path
) -> None:
    """Pins the duplicate of the raise-status gate at ``_response.py:406``.

    ``hit_to_dict`` initialises ``staleness_verdict`` from verification
    + path-drift only; ``attach_commit_drift_counts`` re-derives the
    verdict per hit once the per-search commit-timestamp list has been
    read. The recompute uses its own copy of the
    ``{"never", "stale"}`` membership check — the silent-drop hazard
    the loop targets. If the recompute's gate drifts from
    ``compute_staleness_verdict``'s gate, a stale memory whose
    ``origin.repo`` matches the caller's would re-land on
    ``spot_check_recommended`` (or ``fresh``) the moment commit-drift
    becomes computable, while the same memory's ``memory_show``
    verdict stays ``spot_check_required`` — silent divergence between
    two retrieval surfaces.

    Setup uses the fake-``capture_origin`` pattern from
    ``test_server_commit_drift.py`` so the recompute path is
    deterministically reached: caller in repo R, memory written
    while caller is in repo R (so ``origin.repo == R``), memory
    verified (so ``hit.last_verified_at is not None``),
    ``verification_stale_days=0`` so the verify is immediately
    classified ``stale``.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    origin = Origin(cwd=str(repo), repo=_FAKE_REPO_REMOTE, branch="main")
    server = _build_stale_server_with_origin(memory_dir, origin)

    memory_id = await _write_memory_in_state(server, status="stale")
    hits = _unwrap(
        await _call(
            server,
            "memory_search",
            query="widget configuration staleness",
            auto_scope=False,
        )
    )
    hit = next((h for h in hits if h["id"] == memory_id), None)
    assert hit is not None, (
        f"seeded stale memory {memory_id!r} missing from search results"
    )
    # `commit_drift_count` is present iff `attach_commit_drift_counts`
    # actually ran the recompute against this hit — that's the gate
    # we're trying to exercise. Absence here means the test setup
    # didn't reach `_response.py:406` and the regression case isn't
    # being checked.
    assert "commit_drift_count" in hit, (
        "test setup failed: attach_commit_drift_counts did not recompute "
        "the verdict for this hit, so the duplicate raise-status gate "
        "at _response.py:406 wasn't exercised"
    )
    assert hit["verification"]["status"] == "stale"
    assert hit["staleness_verdict"] == "spot_check_required"


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
async def test_staleness_verdict_matches_across_show_and_search(
    memory_dir: Path, tmp_path: Path
) -> None:
    """Cross-surface invariant: for a single stale memory, the verdict
    returned by ``memory_show`` and by ``memory_search``'s top hit
    must match. A single-site refactor that drops a member from only
    one of the two raise-status whitelists would manifest here as a
    diverging verdict between surfaces — the failure mode the queue
    item explicitly calls out (``memory_show`` vs ``memory_search``
    top-hit divergence)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    origin = Origin(cwd=str(repo), repo=_FAKE_REPO_REMOTE, branch="main")
    server = _build_stale_server_with_origin(memory_dir, origin)

    memory_id = await _write_memory_in_state(server, status="stale")
    shown = await _call(server, "memory_show", id=memory_id)
    hits = _unwrap(
        await _call(
            server,
            "memory_search",
            query="widget configuration staleness",
            auto_scope=False,
        )
    )
    hit = next((h for h in hits if h["id"] == memory_id), None)
    assert hit is not None
    assert shown["staleness_verdict"] == hit["staleness_verdict"], (
        f"memory_show and memory_search disagree on the verdict for "
        f"{memory_id!r}: show={shown['staleness_verdict']!r}, "
        f"search={hit['staleness_verdict']!r} — possible single-site "
        f"drift between verify.py and _response.py"
    )
    assert shown["staleness_verdict"] == "spot_check_required"


# ---------------------------------------------------------------------------
# Change 3 (cont.) — pin verdict TIER STRINGS across the two emission sites
# ---------------------------------------------------------------------------
#
# Symmetric follow-on to the ``_VERDICT_RAISE_STATUSES`` pin above.
# Where that gated the INPUT to the verdict computation, this pins the
# OUTPUT — the three tier strings (``"fresh"``,
# ``"spot_check_recommended"``, ``"spot_check_required"``) the rollup
# emits. ``compute_staleness_verdict`` in ``verify.py`` returns one of
# ``_VERDICT_FRESH`` / ``_VERDICT_RECOMMENDED`` / ``_VERDICT_REQUIRED``;
# the per-search recompute at
# ``ResponseBuilder.attach_commit_drift_counts`` in ``_response.py``
# imports the same constants and re-emits them after folding commit
# drift into the verdict. A rename of any tier in ``verify.py`` that
# didn't reach the recompute would silently desync the
# ``memory_search`` top-hit verdict from the ``memory_show`` verdict
# for the same memory — the same divergence-hazard the input-side pin
# closes, but on the output side.
#
# Two complementary tests:
#
# - ``test_staleness_verdict_tier_string_values_unchanged`` — pins the
#   WIRE values of the three tier strings. The constants are
#   underscore-prefixed (module-private DRY) but the *string values*
#   are observable to MCP clients; a refactor that DRYs the trio must
#   not change the values themselves. Hardcoded literals here (not
#   derived from the constants) so a value flip in ``verify.py``
#   fails the assertion loudly instead of silently agreeing with the
#   renamed constant.
# - ``test_staleness_verdict_string_matches_constant_across_show_and_search``
#   — cross-surface: for a stale memory routed through the
#   recompute path, both ``memory_show`` and ``memory_search``'s top
#   hit must emit exactly ``_VERDICT_REQUIRED``. A site that fell
#   back to a stale literal (e.g. ``"verify_now"`` left over after a
#   rename) would fail one of the two equality checks. The
#   recompute path is reached via the same fake-``capture_origin`` +
#   tmp-git-repo pattern as
#   ``test_staleness_verdict_stale_survives_commit_drift_recompute``,
#   so the assertion exercises the exact site (``_response.py:415``)
#   the OUTPUT-side hazard lives at.
#
# Negative-control: temporarily replacing ``_VERDICT_REQUIRED`` in
# ``verify.py`` with ``"verify_now"`` fails
# ``test_staleness_verdict_tier_string_values_unchanged`` (wire value
# flipped) and the cross-surface test (both surfaces now emit
# ``"verify_now"``, but the constant assertion still matches — caught
# by the wire-value pin). Temporarily flipping only the literal at
# ``_response.py:415`` to ``"verify_now"`` without touching the
# constant fails the cross-surface test only (``memory_show`` still
# emits ``_VERDICT_REQUIRED`` value, the recompute site emits the
# stale literal) — the exact desync the queue item targets.


def test_staleness_verdict_tier_string_values_unchanged() -> None:
    """Pin the wire VALUES of the three tier strings. The constants are
    module-private (DRY across emission sites) but the string values
    are observable to MCP clients; the DRY refactor must not change
    the values themselves. Hardcoded literals here so a value flip in
    ``verify.py`` fails this assertion loudly instead of silently
    agreeing with the renamed constant."""
    assert _VERDICT_FRESH == "fresh"
    assert _VERDICT_RECOMMENDED == "spot_check_recommended"
    assert _VERDICT_REQUIRED == "spot_check_required"


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
async def test_staleness_verdict_string_matches_constant_across_show_and_search(
    memory_dir: Path, tmp_path: Path
) -> None:
    """Cross-surface OUTPUT pin: for one stale memory routed through
    the commit-drift recompute path, both ``memory_show`` and
    ``memory_search``'s top hit must emit a verdict string that
    equals the shared ``_VERDICT_REQUIRED`` constant. Catches the
    specific failure mode the queue item flags — a single-site
    refactor that drops one of the three emission points (or
    re-hardcodes one with a stale literal) would manifest as a
    diverging string between the two surfaces, even when both
    surfaces still produce a syntactically-valid verdict tier.
    Routes through the recompute path (fake-``capture_origin`` +
    tmp-git-repo) so the assertion exercises ``_response.py:415``,
    the OUTPUT-side hazard site."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    origin = Origin(cwd=str(repo), repo=_FAKE_REPO_REMOTE, branch="main")
    server = _build_stale_server_with_origin(memory_dir, origin)

    memory_id = await _write_memory_in_state(server, status="stale")
    shown = await _call(server, "memory_show", id=memory_id)
    hits = _unwrap(
        await _call(
            server,
            "memory_search",
            query="widget configuration staleness",
            auto_scope=False,
        )
    )
    hit = next((h for h in hits if h["id"] == memory_id), None)
    assert hit is not None, f"seeded memory {memory_id!r} missing from search results"
    # Recompute gate: presence of `commit_drift_count` proves
    # `attach_commit_drift_counts` actually ran on this hit — i.e. the
    # OUTPUT-side emission site at `_response.py:415` was reached.
    # Absence means the test setup silently bypassed the recompute and
    # the regression case isn't being checked.
    assert "commit_drift_count" in hit, (
        "test setup failed: attach_commit_drift_counts did not recompute "
        "the verdict for this hit, so the OUTPUT-side tier-string "
        "emission at _response.py:415 wasn't exercised"
    )
    # Both surfaces must emit the *shared constant value*, not just a
    # syntactically-valid verdict tier. A single-site stale literal
    # would pass the surface-equality test in
    # `test_staleness_verdict_matches_across_show_and_search` if the
    # other site also drifted to the same wrong literal, but would
    # fail at least one of these two checks.
    assert shown["staleness_verdict"] == _VERDICT_REQUIRED, (
        f"memory_show emitted {shown['staleness_verdict']!r}, expected "
        f"{_VERDICT_REQUIRED!r} (the shared constant) — possible "
        f"single-site drift away from verify.py's _VERDICT_REQUIRED"
    )
    assert hit["staleness_verdict"] == _VERDICT_REQUIRED, (
        f"memory_search top hit emitted {hit['staleness_verdict']!r}, "
        f"expected {_VERDICT_REQUIRED!r} (the shared constant) — "
        f"possible single-site drift between verify.py's "
        f"_VERDICT_REQUIRED and _response.py's recompute emission"
    )


# ---------------------------------------------------------------------------
# Change 3 (cont.) — pin verdict emission on the memory_list surfaces too
# ---------------------------------------------------------------------------
#
# ``compute_staleness_verdict`` has 5 call sites in
# ``_response.py`` + ``handlers/show.py``:
#
# - ``_response.py:103``  — ``hit_to_dict`` (memory_search)
# - ``_response.py:161``  — ``summary_to_dict`` (memory_list summary)
# - ``_response.py:224``  — ``memory_to_dict`` (memory_list with_bodies)
# - ``_response.py:415``  — ``attach_commit_drift_counts`` recompute
# - ``handlers/show.py``  — memory_show
#
# The cross-surface tests above triangulate three of them
# (memory_show, memory_search hit, and the per-search recompute) but
# leave the two memory_list paths unpinned. A single-site hardcoded
# literal at ``_response.py:161`` or ``:224`` — e.g. a refactor that
# inlined ``"spot_check_required"`` for "clarity" — would silently
# desync the memory_list verdict from every other surface while every
# existing test still passed. This test extends the cross-surface
# coverage to both list shapes (summary + with_bodies) so a literal
# at either site fails loudly.
#
# Negative-control: temporarily hardcoding the literal ``"wrong"`` at
# ``_response.py:161`` flips the summary-path assertion below;
# temporarily hardcoding it at ``_response.py:224`` flips the
# with_bodies-path assertion. Both reverts confirmed.


async def test_staleness_verdict_string_matches_constant_across_list_surfaces(
    stale_server: Any,
) -> None:
    """Cross-surface OUTPUT pin on the memory_list paths: for a stale
    memory routed through both ``memory_list`` (summary) and
    ``memory_list(with_bodies=True)``, the emitted ``staleness_verdict``
    on each returned row must equal the shared ``_VERDICT_REQUIRED``
    constant. Catches a single-site hardcoded literal at
    ``_response.py:161`` (``summary_to_dict``) or ``:224``
    (``memory_to_dict``) — both are independent emission sites the
    other cross-surface tests don't reach.

    Uses the ``stale_server`` fixture (``verification_stale_days=0``)
    plus the existing ``_write_memory_in_state(..., status="stale")``
    helper so the produced memory is classified ``stale`` on the next
    ``compute_verification_status`` call — driving the
    ``_VERDICT_RAISE_STATUSES`` branch that pre-empts every drift
    input."""
    memory_id = await _write_memory_in_state(stale_server, status="stale")

    # Summary path (`_response.py:161` → `summary_to_dict`).
    summary_rows = _unwrap(await _call(stale_server, "memory_list"))
    summary_row = next((r for r in summary_rows if r["id"] == memory_id), None)
    assert summary_row is not None, (
        f"seeded memory {memory_id!r} missing from memory_list summary"
    )
    assert summary_row["verification"]["status"] == "stale"
    assert summary_row["staleness_verdict"] == _VERDICT_REQUIRED, (
        f"memory_list (summary) emitted "
        f"{summary_row['staleness_verdict']!r}, expected "
        f"{_VERDICT_REQUIRED!r} (the shared constant) — possible "
        f"single-site drift away from verify.py's _VERDICT_REQUIRED "
        f"at _response.py:161 (summary_to_dict)"
    )

    # With-bodies path (`_response.py:224` → `memory_to_dict`).
    body_rows = _unwrap(await _call(stale_server, "memory_list", with_bodies=True))
    body_row = next((r for r in body_rows if r["id"] == memory_id), None)
    assert body_row is not None, (
        f"seeded memory {memory_id!r} missing from memory_list with_bodies"
    )
    assert body_row["verification"]["status"] == "stale"
    assert body_row["staleness_verdict"] == _VERDICT_REQUIRED, (
        f"memory_list (with_bodies) emitted "
        f"{body_row['staleness_verdict']!r}, expected "
        f"{_VERDICT_REQUIRED!r} (the shared constant) — possible "
        f"single-site drift away from verify.py's _VERDICT_REQUIRED "
        f"at _response.py:224 (memory_to_dict)"
    )


# ---------------------------------------------------------------------------
# Change 4 — auto-record_use via use_token
# ---------------------------------------------------------------------------


async def test_search_response_includes_use_token_per_hit(server: Any) -> None:
    await _call(
        server,
        "memory_write",
        content="A retrievable fact about widgets.",
        scopes=["tools"],
    )
    hits = _unwrap(await _call(server, "memory_search", query="widgets"))
    assert hits
    for hit in hits:
        assert "use_token" in hit
        assert isinstance(hit["use_token"], str)
        assert hit["use_token"].startswith("use_")
        # Token must NOT be the memory id (opaque correlation handle).
        assert hit["use_token"] != hit["id"]


async def test_show_response_includes_use_token(server: Any) -> None:
    res = await _call(server, "memory_write", content="x", scopes=["tools"])
    shown = await _call(server, "memory_show", id=res["id"])
    assert "use_token" in shown
    assert shown["use_token"].startswith("use_")


async def test_use_token_auto_commits_after_two_turns(
    server_with_state: tuple[Any, SessionState, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue token at turn N; two more memory_* calls (turns N+1, N+2)
    later, the next call (turn N+3) sees the search ids logged as
    auto-applied in the event log.

    The wall-clock floor (3.14 — the Stop hook normally settles a turn's
    retrievals first) is zeroed to exercise the turn axis end-to-end;
    the floor has its own coverage in test_telemetry_v2.py."""
    import bettermemory.session as session_mod

    monkeypatch.setattr(session_mod, "AUTO_COMMIT_MIN_AGE_SECONDS", 0.0)
    srv, _state, memory_dir = server_with_state
    res = await _call(
        srv, "memory_write", content="A retrievable fact.", scopes=["tools"]
    )

    await _call(srv, "memory_search", query="retrievable fact")
    # Turn deltas: search at turn ~2 issued a token. Two more calls
    # advance the counter — by the third call, the auto-commit fires.
    await _call(srv, "memory_list")  # +1
    await _call(srv, "memory_list")  # +2
    await _call(srv, "memory_list")  # +3 — auto-commit fires here

    events = list(iter_events(memory_dir))
    auto_uses = [
        e
        for e in events
        if e.get("kind") == "use"
        and e.get("outcome") == "applied"
        and e.get("auto") is True
    ]
    assert auto_uses, f"expected an auto-applied event; got {events}"
    assert any(res["id"] in (e.get("ids") or []) for e in auto_uses)


async def test_explicit_record_use_overrides_pending_auto_commit(
    server_with_state: tuple[Any, SessionState, Path],
) -> None:
    """An explicit record_use(ignored) for a still-pending token must
    NOT produce an `applied` shadow event in the log."""
    srv, _state, memory_dir = server_with_state
    res = await _call(
        srv, "memory_write", content="A retrievable fact.", scopes=["tools"]
    )
    await _call(srv, "memory_search", query="retrievable fact")
    await _call(srv, "memory_record_use", memory_ids=[res["id"]], outcome="ignored")

    # Advance enough turns for any rogue auto-commit to fire.
    await _call(srv, "memory_list")
    await _call(srv, "memory_list")
    await _call(srv, "memory_list")

    events = list(iter_events(memory_dir))
    use_events = [e for e in events if e.get("kind") == "use"]
    # Find any event citing the memory id.
    for e in use_events:
        if res["id"] in (e.get("ids") or []):
            # Should be the explicit `ignored`, never `applied`+auto.
            if e.get("outcome") == "applied" and e.get("auto") is True:
                pytest.fail(f"explicit override leaked an auto-applied event: {e}")


async def test_explicit_record_use_purges_token(
    server_with_state: tuple[Any, SessionState, Path],
) -> None:
    """After an explicit record_use, the pending token map should
    drop the id so a future auto-commit sweep doesn't double-fire."""
    srv, state, _memory_dir = server_with_state
    res = await _call(
        srv, "memory_write", content="A retrievable fact.", scopes=["tools"]
    )
    await _call(srv, "memory_search", query="retrievable fact")
    assert res["id"] in state.pending_use_tokens
    await _call(srv, "memory_record_use", memory_ids=[res["id"]], outcome="applied")
    assert res["id"] not in state.pending_use_tokens


async def test_use_token_within_ttl_does_not_auto_commit(
    server_with_state: tuple[Any, SessionState, Path],
) -> None:
    """One memory_* call after a search isn't enough for the token to age
    out; the auto-commit pass only fires after `ttl_turns` deltas."""
    srv, _state, memory_dir = server_with_state
    res = await _call(
        srv, "memory_write", content="A retrievable fact.", scopes=["tools"]
    )
    await _call(srv, "memory_search", query="retrievable fact")
    await _call(srv, "memory_list")  # one turn advance — still within TTL

    events = list(iter_events(memory_dir))
    auto_uses = [
        e
        for e in events
        if e.get("kind") == "use"
        and e.get("outcome") == "applied"
        and e.get("auto") is True
        and res["id"] in (e.get("ids") or [])
    ]
    assert not auto_uses, "auto-commit fired before TTL"


async def test_hook_attributed_event_suppresses_auto_commit(
    server_with_state: tuple[Any, SessionState, Path],
) -> None:
    """When the Stop hook has already emitted an `applied` event with
    `attribution="hook"` for a memory, the in-process auto-commit must
    NOT fire a second `applied` event two turns later. The hook
    happens cross-process; the dedup goes via the event log."""
    srv, state, memory_dir = server_with_state
    res = await _call(
        srv, "memory_write", content="A retrievable fact.", scopes=["tools"]
    )
    await _call(srv, "memory_search", query="retrievable fact")
    assert res["id"] in state.pending_use_tokens

    # Simulate the Stop hook attributing this retrieval. The hook
    # writes through Recorder with the same session id the in-process
    # one uses; advance_turn's dedup pass reads the event log.
    from bettermemory.events import Recorder

    Recorder(root=memory_dir, session_id=state.session_id).record(
        "use",
        ids=[res["id"]],
        outcome="applied",
        auto=False,
        attribution="hook",
        claim_excerpts=["A retrievable fact"],
        triggered_from="stop_hook",
    )

    # Advance enough turns for any rogue auto-commit to fire.
    await _call(srv, "memory_list")
    await _call(srv, "memory_list")
    await _call(srv, "memory_list")

    events = list(iter_events(memory_dir))
    use_events_for_id = [
        e
        for e in events
        if e.get("kind") == "use" and res["id"] in (e.get("ids") or [])
    ]
    # Exactly one `applied` event — the hook's. The auto-commit was
    # suppressed by the pending-token purge in _advance_turn.
    applied = [e for e in use_events_for_id if e.get("outcome") == "applied"]
    assert len(applied) == 1, f"expected one applied event; got: {applied}"
    assert applied[0]["attribution"] == "hook"
    # Token cleared from the pending map by the dedup purge.
    assert res["id"] not in state.pending_use_tokens


async def test_production_cross_id_space_hook_event_suppresses_auto_commit(
    server_with_state: tuple[Any, SessionState, Path],
) -> None:
    """Regression for the PRODUCTION id-space gap: the in-process server
    session (`recorder.session_id`, a `sess_<hex>`) and the Stop hook's
    session (the Claude Code transcript id) are DIFFERENT id spaces. The
    real `hook.run_audit` builds its Recorder with
    `session_id=<transcript_id>`, so the `applied, attribution="hook"`
    event it writes is stamped `session=<transcript_id>` — never equal
    to `state.session_id`.

    The covering test above (`test_hook_attributed_event_suppresses_
    auto_commit`) records the hook event under `state.session_id`, the
    SAME id-space, so it never exercised this gap: the old
    `_already_recorded_pending_ids` filter (`session != recorder.session_id`)
    matched it by accident. Under the production shape that filter
    skipped the hook event entirely, the pending token survived, and
    `_advance_turn` fired a SECOND `applied, attribution="auto"` event
    ~2 turns later — a permanent double-count in the append-only log
    that inflates `memory_helped_rate` and the explicit-vs-auto split.

    This test stamps the hook event with a transcript id that is
    explicitly NOT `state.session_id`, then asserts EXACTLY ONE applied
    event total after the auto-commit window passes.
    """
    srv, state, memory_dir = server_with_state
    res = await _call(
        srv, "memory_write", content="A retrievable fact.", scopes=["tools"]
    )
    await _call(srv, "memory_search", query="retrievable fact")
    assert res["id"] in state.pending_use_tokens

    # The Stop hook's transcript id — a DIFFERENT id space from the
    # server's `sess_<hex>`. This is the crux of the production bug.
    transcript_session_id = "claude-code-transcript-abc123"
    assert transcript_session_id != state.session_id, (
        "test precondition: the hook session must differ from the server "
        "session to reproduce the cross-id-space production shape"
    )

    from bettermemory.events import Recorder

    # Mirror `hook.run_audit`: the hook's Recorder carries the transcript
    # id, and every event it writes is tagged `triggered_from="stop_hook"`.
    Recorder(root=memory_dir, session_id=transcript_session_id).record(
        "use",
        ids=[res["id"]],
        outcome="applied",
        auto=False,
        attribution="hook",
        claim_excerpts=["A retrievable fact"],
        triggered_from="stop_hook",
    )

    # Advance enough turns for any rogue auto-commit to fire.
    await _call(srv, "memory_list")
    await _call(srv, "memory_list")
    await _call(srv, "memory_list")

    events = list(iter_events(memory_dir))
    use_events_for_id = [
        e
        for e in events
        if e.get("kind") == "use" and res["id"] in (e.get("ids") or [])
    ]
    applied = [e for e in use_events_for_id if e.get("outcome") == "applied"]
    assert len(applied) == 1, (
        "expected exactly one applied event (the hook's) — a second "
        "auto-applied event means the cross-id-space dedup bridge is "
        f"missing; got: {applied}"
    )
    assert applied[0]["attribution"] == "hook"
    # The dedup must have purged the pending token despite the hook
    # event living under the transcript id, not the server session.
    assert res["id"] not in state.pending_use_tokens


async def test_stale_use_event_does_not_falsely_purge_fresh_token(
    server_with_state: tuple[Any, SessionState, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: pre-2.6.8 the in-process dedup scan matched on
    `(session_id, memory_id)` only, with no timestamp guard. A memory
    retrieved twice in one session (search → record_use → search again)
    would falsely purge the *fresh* second token against the *stale*
    first record_use event, dropping the auto-commit cadence on a
    legitimate new retrieval. The fix is the `event.ts >= token.issued_at`
    filter in `_already_recorded_pending_ids`.

    The 3.14 wall-clock floor is zeroed so the final auto-commit proof
    still runs on the turn axis (its own coverage: test_telemetry_v2).
    """
    import bettermemory.session as session_mod

    monkeypatch.setattr(session_mod, "AUTO_COMMIT_MIN_AGE_SECONDS", 0.0)
    srv, state, memory_dir = server_with_state
    res = await _call(
        srv, "memory_write", content="A retrievable fact.", scopes=["tools"]
    )
    mid = res["id"]

    # Cycle 1: search → explicit record_use → token cleared, log carries
    # a `use, attribution="model"` event timestamped at this moment.
    await _call(srv, "memory_search", query="retrievable fact")
    await _call(
        srv,
        "memory_record_use",
        memory_ids=[mid],
        outcome="applied",
        claim_excerpts=["A retrievable fact"],
    )
    assert mid not in state.pending_use_tokens

    # Cycle 2: fresh search — a brand-new token should be issued and the
    # stale cycle-1 event must NOT purge it. The token's issued_at is
    # strictly after the cycle-1 record_use event's ts.
    await _call(srv, "memory_search", query="retrievable fact")
    assert mid in state.pending_use_tokens, (
        "fresh token from cycle-2 search was falsely purged by the stale "
        "cycle-1 record_use event (missing event-timestamp guard)"
    )

    # And after the TTL passes, the auto-commit DOES fire for the fresh
    # cycle — proving the token is live, not just resident-but-shadowed.
    await _call(srv, "memory_list")
    await _call(srv, "memory_list")
    events = list(iter_events(memory_dir))
    auto_for_cycle_2 = [
        e
        for e in events
        if e.get("kind") == "use"
        and e.get("auto") is True
        and mid in (e.get("ids") or [])
    ]
    assert auto_for_cycle_2, (
        "cycle-2 token never auto-committed — the dedup scan must have "
        "purged it despite the timestamp guard"
    )


def _backdate_use_token(state: SessionState, memory_id: str) -> None:
    """Age one live use-token past the wall-clock eviction TTL.

    Backdating the token, not monkeypatching
    `_PENDING_USE_TOKEN_TTL_SECONDS`: the eviction resolves its cutoff
    from the module constant at call time, and that call-time-resolution
    contract is itself load-bearing elsewhere in this file. Mutating the
    token keeps these tests on the eviction's own axis and is faster
    than idling for half an hour.
    """
    import time as _time

    import bettermemory.session as session_mod

    state.pending_use_tokens[memory_id].issued_at = (
        _time.time() - session_mod._PENDING_USE_TOKEN_TTL_SECONDS - 1
    )


async def test_hook_settled_token_expiring_does_not_report_a_loss(
    server_with_state: tuple[Any, SessionState, Path],
) -> None:
    """A retrieval the Stop hook ALREADY settled, whose token then dies
    of wall clock, must not be reported as a lost retrieval.

    The trap is an ordering one, and it only bites hookful stores.
    `SessionState.advance_turn` evicts wall-clock-expired tokens at the
    TOP of `_advance_turn` — BEFORE the dedup scan that reads the event
    log. So by the time the scan looks, a hook-settled token is already
    out of `state.pending_use_tokens` and invisible to it. A drain that
    only asked "was this still pending?" would file a `use_token_expired`
    for a retrieval the hook attributed half an hour earlier: on a
    properly-wired store — the deployment shape this whole feature is
    supposed to leave untouched — every idle gap would manufacture
    phantom losses. The fix is `extra_pending`: the drained batch is
    folded into the same log scan the live tokens get.

    No hookless fixture can see this, which is why this is the only
    test in the item that constructs a hookful store — one that emits
    `triggered_from="stop_hook"` under a transcript id from a different
    id space than the server's, mirroring `hook.run_audit`.
    """
    srv, state, memory_dir = server_with_state
    res = await _call(
        srv, "memory_write", content="A retrievable fact.", scopes=["tools"]
    )
    mid = res["id"]
    await _call(srv, "memory_search", query="retrievable fact")
    assert mid in state.pending_use_tokens

    # The Stop hook settles the turn's retrieval seconds after the reply.
    transcript_session_id = "claude-code-transcript-expiry"
    assert transcript_session_id != state.session_id
    Recorder(root=memory_dir, session_id=transcript_session_id).record(
        "use",
        ids=[mid],
        outcome="applied",
        auto=False,
        attribution="hook",
        claim_excerpts=["A retrievable fact"],
        triggered_from="stop_hook",
    )

    # ...and then the session goes idle past the eviction TTL. Backdated
    # AFTER the hook wrote, so the hook event is timestamped inside the
    # token's lifetime exactly as production would have it.
    _backdate_use_token(state, mid)

    await _call(srv, "memory_list")

    events = list(iter_events(memory_dir))
    expiries = [e for e in events if e.get("kind") == "use_token_expired"]
    assert not [e for e in expiries if mid in (e.get("ids") or [])], (
        "a retrieval the Stop hook already settled was reported as a "
        f"loss — the eviction outran the dedup scan; got: {expiries}"
    )
    uses = [e for e in events if e.get("kind") == "use" and mid in (e.get("ids") or [])]
    assert len(uses) == 1, f"expected only the hook's use event; got: {uses}"
    assert uses[0]["attribution"] == "hook"
    assert mid not in state.pending_use_tokens


async def test_expired_use_token_emits_event_and_is_not_applied(
    server_with_state: tuple[Any, SessionState, Path],
) -> None:
    """A retrieval nothing settled surfaces as `use_token_expired` — and
    as NOTHING else.

    The event deliberately carries no `outcome`, `auto` or
    `attribution`. Settling evidence-free leftovers as `applied` (the
    shape an earlier draft proposed) would feed cold-endorsement and
    block dead-weight curation wholesale: the retrievals that failed to
    earn any evidence would become the ones vouching for their
    memories. An expiry is the absence of evidence and has to read that
    way. Omitting `attribution` is also what keeps the event inside
    doctor's client-session census, since that is the field
    `eval.is_admin_recorded_event` keys its second axis on.
    """
    import bettermemory.session as session_mod

    srv, state, memory_dir = server_with_state
    res = await _call(
        srv, "memory_write", content="A retrievable fact.", scopes=["tools"]
    )
    mid = res["id"]
    await _call(srv, "memory_search", query="retrievable fact")
    _backdate_use_token(state, mid)

    await _call(srv, "memory_list")

    events = list(iter_events(memory_dir))
    expiries = [e for e in events if e.get("kind") == "use_token_expired"]
    assert len(expiries) == 1, f"expected exactly one expiry event; got {expiries}"
    expiry = expiries[0]
    assert expiry["ids"] == [mid]
    assert expiry["reason"] == "wall_clock_ttl"
    assert expiry["age_seconds"] >= session_mod._PENDING_USE_TOKEN_TTL_SECONDS
    assert expiry["turns_since_issue"] >= 1
    for forbidden in ("outcome", "auto", "attribution"):
        assert forbidden not in expiry, (
            f"the expiry event carries `{forbidden}` — it will be read as "
            "a settlement by at least one downstream surface"
        )
    assert not [
        e for e in events if e.get("kind") == "use" and mid in (e.get("ids") or [])
    ], "an expired token was also recorded as a use — expiry is not applied"


async def test_explicit_record_use_of_an_expired_token_reports_no_loss(
    server_with_state: tuple[Any, SessionState, Path],
) -> None:
    """The model settling a dead token explicitly is a settlement, not a
    loss — even though the settling event is written after the drain.

    Third settlement path, third ordering trap, and the only one where
    the evidence is not in the log at all when the drain looks. The
    hook's `use` is at least already on disk; `memory_record_use`
    writes its `use` AFTER `_advance_turn` returns, so the dedup scan
    structurally cannot see it. Without `override_ids` threaded into
    the drain, one handler call files a `use_token_expired` and a `use`
    for the same id, and the append-only log records that retrieval as
    both settled and lost forever.

    Driven through `call_tool` rather than the handler function so the
    real intra-call ordering is what's under test.
    """
    srv, state, memory_dir = server_with_state
    res = await _call(
        srv, "memory_write", content="A retrievable fact.", scopes=["tools"]
    )
    mid = res["id"]
    await _call(srv, "memory_search", query="retrievable fact")
    assert mid in state.pending_use_tokens

    # Idle past the eviction TTL, then come back and endorse — the
    # `memory_record_use` is this session's next memory_* call, so its
    # own `_advance_turn` is the one that runs the drain.
    _backdate_use_token(state, mid)
    await _call(
        srv,
        "memory_record_use",
        memory_ids=[mid],
        outcome="applied",
        claim_excerpts=["A retrievable fact"],
    )

    events = list(iter_events(memory_dir))
    settled = {
        i for e in events if e.get("kind") == "use" for i in (e.get("ids") or [])
    }
    expired = {
        i
        for e in events
        if e.get("kind") == "use_token_expired"
        for i in (e.get("ids") or [])
    }
    # Anti-vacuity: the explicit settlement must actually be on disk,
    # otherwise "not in expired" would hold for a call that did nothing.
    assert mid in settled
    assert mid not in expired, (
        "the id the model explicitly recorded a use for was ALSO filed as "
        "an expired retrieval — the same retrieval is now permanently "
        "logged as both settled and lost"
    )
    assert not (settled & expired)


async def test_hookless_session_loses_no_retrieval_silently(
    server_with_state: tuple[Any, SessionState, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The acceptance criterion, as a closure over the event log.

    Every id a hookless session retrieved must end up accounted for by
    exactly one of the two settlement surfaces: a `use` event (the
    in-process auto-commit did its job) or a `use_token_expired` event
    (the session went idle and the token died). The two must be
    disjoint — an id in both would mean the same retrieval was counted
    as settled AND lost.

    The second set did not exist before this landed, and the closure
    simply didn't hold: the difference was thrown away by a bare `del`, and
    downstream the memory read as "retrieved, never applied" — the
    exact shape dead-weight curation punishes.

    The wall-clock floor on the auto-commit is zeroed so both fates are
    reachable inside one test; the floor's own coverage lives in
    test_telemetry_v2.py.
    """
    import bettermemory.session as session_mod

    monkeypatch.setattr(session_mod, "AUTO_COMMIT_MIN_AGE_SECONDS", 0.0)
    srv, state, memory_dir = server_with_state
    idle = await _call(
        srv,
        "memory_write",
        content="Alpha fact about kayak maintenance schedules.",
        scopes=["tools"],
    )
    shown = await _call(
        srv,
        "memory_write",
        content="Beta fact about ukulele tuning pegs.",
        scopes=["tools"],
    )

    # Both mint sites: search (`_attach_use_tokens`) and show
    # (`issue_use_tokens`).
    await _call(srv, "memory_search", query="kayak maintenance")
    await _call(srv, "memory_show", id=shown["id"])

    # One retrieval goes idle past the eviction TTL; the rest ride the
    # turn counter into the auto-commit.
    _backdate_use_token(state, idle["id"])
    await _call(srv, "memory_list")
    await _call(srv, "memory_list")
    await _call(srv, "memory_list")

    assert not state.pending_use_tokens, (
        "a token is still live — the closure below would be vacuously "
        "satisfiable by tokens that simply haven't been decided yet"
    )

    events = list(iter_events(memory_dir))
    retrieved: set[str] = set()
    settled: set[str] = set()
    expired: set[str] = set()
    for e in events:
        kind = e.get("kind")
        if kind == "search":
            retrieved |= set(e.get("returned") or [])
        elif kind == "show":
            retrieved.add(e["id"])
        elif kind == "use":
            settled |= set(e.get("ids") or [])
        elif kind == "use_token_expired":
            expired |= set(e.get("ids") or [])

    assert idle["id"] in expired
    assert shown["id"] in settled
    assert not (settled & expired), (
        f"ids {sorted(settled & expired)} are recorded as both settled and lost"
    )
    assert retrieved == settled | expired, (
        "retrievals fell off the log with no settlement and no expiry: "
        f"{sorted(retrieved - (settled | expired))}"
    )


# ---------------------------------------------------------------------------
# Change 5 — curation_pending in memory_scope_overview
# ---------------------------------------------------------------------------


async def test_scope_overview_returns_curation_pending(server: Any) -> None:
    res = await _call(server, "memory_scope_overview")
    assert "curation_pending" in res
    assert set(res["curation_pending"].keys()) == {
        "stale",
        "never_verified",
        "drifted",
        "cold",
        "dead",
        "silent_misses",
        "unique_silent_miss_memories",
        "cold_endorsement_memories",
        "conflicts",
    }
    # All counts must be integers.
    for v in res["curation_pending"].values():
        assert isinstance(v, int)


async def test_scope_overview_curation_pending_zero_on_empty(server: Any) -> None:
    res = await _call(server, "memory_scope_overview")
    assert res["curation_pending"] == {
        "stale": 0,
        "never_verified": 0,
        "drifted": 0,
        "cold": 0,
        "dead": 0,
        "silent_misses": 0,
        "unique_silent_miss_memories": 0,
        "cold_endorsement_memories": 0,
        "conflicts": 0,
    }


async def test_scope_overview_curation_never_verified_increments(
    server: Any,
) -> None:
    """A freshly-written memory has no last_verified_at, so the
    `never_verified` count climbs by one."""
    await _call(server, "memory_write", content="A new fact.", scopes=["tools"])
    res = await _call(server, "memory_scope_overview")
    assert res["curation_pending"]["never_verified"] == 1


async def test_scope_overview_dead_count_rides_the_telemetry_gate(
    server: Any, memory_dir: Path
) -> None:
    """`curation_pending.dead` gates on Stop-hook telemetry, on BOTH arms.

    The third production entry point for the dead-weight honesty gate
    (`memory_health`'s `report_for_directory` and the consolidate
    demotion pass have their own end-to-end tests). It needs one because
    the gate's default is `None` = "the caller did not measure; assume
    covered": a handler that forgets to derive and pass the count never
    gates at all, and every pure-function test still passes.

    Both arms, because `curation_pending_new_since_last_session` makes
    its own `curation_counts` call and can be wired wrong on its own.
    And behavioural rather than structural: the standing AST guard
    (`test_every_production_dead_weight_call_arms_the_telemetry_gate`)
    proves the kwarg is passed, not that the value passed is the count
    of hook rows in the log this call is reading.
    """
    from datetime import datetime, timedelta, timezone

    from bettermemory.events import EVENT_LOG_FILENAME

    now = datetime.now(timezone.utc)

    def _append(*rows: dict[str, Any]) -> None:
        """Append raw event rows carrying explicit timestamps.

        Written to the legacy untagged log rather than through
        `Recorder`, which always stamps `ts` with the wall clock: a
        dead-weight fixture needs a retrieval older than BOTH the
        30-day window and the 2-day endorsement grace, and there is no
        way to backdate through the writer. Readers merge this file with
        the per-session shards, so the handler sees one stream.
        """
        path = memory_dir / EVENT_LOG_FILENAME
        with path.open("a", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")

    store = Store(memory_dir)
    written = store.write(
        content="Release tags go out only after the CI matrix is green.",
        scopes=["tools"],
    )
    # Age it past the window on disk — `Store.write` stamps `created` /
    # `updated` with now, and the rollup keys on the freshest
    # maintenance touch.
    aged = now - timedelta(days=60)
    for path, mem in store.iter_active():
        if mem.id == written.id:
            store._write_path(
                path, mem.model_copy(update={"created": aged, "updated": aged})
            )
            break

    # One warm-up call, purely to learn the server recorder's session
    # id. The retrieval row below has to carry it: an event stamped with
    # any OTHER session becomes the newest prior-session boundary, and a
    # boundary younger than the memory's `created` would make the delta
    # arm drop the row for being old news rather than for being
    # unmeasurable — the two look identical from the assertion.
    await _call(server, "memory_scope_overview")
    sessions = [ev.get("session") for ev in iter_events(memory_dir)]
    session_id = sessions[-1]
    assert isinstance(session_id, str) and session_id

    _append(
        # The prior-session boundary the delta arm deltas against.
        {
            "ts": (now - timedelta(days=120)).isoformat(),
            "session": "sess_before",
            "kind": "search",
            "returned": [],
        },
        # The retrieval that makes the memory dead weight: past the
        # endorsement grace, never followed by an apply.
        {
            "ts": (now - timedelta(days=50)).isoformat(),
            "session": session_id,
            "kind": "search",
            "returned": [written.id],
        },
    )

    res = await _call(server, "memory_scope_overview")
    delta = res["curation_pending_new_since_last_session"]
    assert delta is not None, "no prior-session boundary — the delta arm never ran"
    assert res["curation_pending"]["dead"] == 0, (
        "hookless store reported dead weight through memory_scope_overview"
    )
    assert delta["dead"] == 0, "the delta arm did not arm the gate"

    # The positive control: same store, same memory, one Stop-hook row.
    _append(
        {
            "ts": (now - timedelta(days=40)).isoformat(),
            "session": session_id,
            "kind": "turn_audited",
            "triggered_from": "stop_hook",
            "verdict": "ok",
        }
    )

    res = await _call(server, "memory_scope_overview")
    delta = res["curation_pending_new_since_last_session"]
    assert delta is not None
    assert res["curation_pending"]["dead"] == 1
    assert delta["dead"] == 1


# Wire-shape parity for the DESC_* tool descriptions — the prose the LLM
# client sees has to enumerate the same bucket keys the runtime emits, or
# the model will branch on names that don't exist (or miss names that do).
# The existing pins above lock the runtime side
# (`test_scope_overview_returns_curation_pending`,
# `test_scope_overview_curation_pending_zero_on_empty`); these two pin
# the prose side via regex extraction so a future docstring edit that
# drops or renames a bucket fails CI instead of silently misleading
# clients.


def test_desc_memory_scope_overview_enumerates_curation_pending_keys() -> None:
    """`DESC_MEMORY_SCOPE_OVERVIEW` prose lists the seven
    `curation_pending` keys in a brace-delimited block. Extract them via
    regex and assert set equality against the runtime wire shape (which
    is pinned at `test_scope_overview_curation_pending_zero_on_empty`)."""
    import re

    from bettermemory.handlers.scope_overview import DESC_MEMORY_SCOPE_OVERVIEW

    # The prose lays out the rollup as:
    #     "{stale, never_verified, drifted, cold, dead, "
    #     "silent_misses, unique_silent_miss_memories, "
    #     "cold_endorsement_memories}"
    # The literal C-style string concatenation in the source becomes one
    # contiguous "{...}" at runtime — the regex matches that block.
    match = re.search(r"\{([a-z_,\s]+)\}", DESC_MEMORY_SCOPE_OVERVIEW)
    assert match is not None, (
        "DESC_MEMORY_SCOPE_OVERVIEW no longer contains a brace-delimited "
        "list of curation_pending bucket names. The prose lost its "
        "self-documenting structure; restore the `{name, name, ...}` "
        "block or update this extraction."
    )
    extracted = {name.strip() for name in match.group(1).split(",")}

    expected = {
        "stale",
        "never_verified",
        "drifted",
        "cold",
        "dead",
        "silent_misses",
        "unique_silent_miss_memories",
        "cold_endorsement_memories",
        "conflicts",
    }
    assert extracted == expected, (
        "DESC_MEMORY_SCOPE_OVERVIEW's curation_pending key list drifted "
        f"from the runtime wire shape. Only in prose: "
        f"{sorted(extracted - expected)}; only in runtime: "
        f"{sorted(expected - extracted)}. Sync the docstring with the "
        "dict returned by `curation_counts` (and the test_server_v12 pin "
        "above)."
    )


def test_desc_memory_health_enumerates_report_bucket_keys() -> None:
    """`DESC_MEMORY_HEALTH` prose enumerates every bucket key returned by
    `HealthReport.to_dict()`. Regex-extract the bucket region between
    "Returns buckets" and "CLI equivalent", then assert set equality
    against the expected bucket names — drift here misleads clients
    about what `memory_health` actually returns."""
    import re
    from datetime import datetime, timezone

    from bettermemory.handlers.health import DESC_MEMORY_HEALTH

    # Slice the bucket-enumeration region. Anchoring on the surrounding
    # prose ("Returns buckets" / "CLI equivalent:") keeps the extraction
    # robust against future edits that add unrelated backticked tokens
    # outside the bucket list (e.g. a tool-reference in a leading sentence).
    start = DESC_MEMORY_HEALTH.index("Returns buckets")
    end = DESC_MEMORY_HEALTH.index("CLI equivalent:")
    region = DESC_MEMORY_HEALTH[start:end]

    # Bucket names appear backticked inside the region. Parameter
    # references — `window_days`, `min_applied`, `resolution_timeline`,
    # `verification_stale_days`, and the cross-tool reference
    # `memory_audit_turn` — also live here; filter them out explicitly so
    # the assertion focuses on actual bucket names.
    all_ticked = set(re.findall(r"`([a-z_][a-z_0-9]*)`", region))
    NON_BUCKET = {
        "window_days",
        "min_applied",
        "resolution_timeline",
        "verification_stale_days",
        "memory_audit_turn",
        # `recommendations` row shape — fields documented inline to
        # explain the digest, not bucket names.
        "kind",
        "summary",
        "action",
        "count",
        "memory_ids",
        "scope",
        # Recommendation `kind` enum values — listed inline so the
        # model can switch over them; not bucket names.
        "remove_dead_weight",
        "resolve_contradicted",
        "cleanup_cold_endorsements",
        "verify_drifted",
        "fix_typo_scopes",
        # `silent_misses` sub-fields — documented inline to explain the
        # dedup + tombstone-filter contract on the payload itself, not
        # buckets in their own right.
        "audited_total",
        "miss_total",
        "unique_miss_memories",
    }
    extracted = all_ticked - NON_BUCKET

    # DERIVED from `HealthReport.to_dict()`, not hand-listed. The
    # hand-listed version of this set is how `telemetry_coverage` shipped
    # as an undocumented wire key past the one test whose docstring
    # claims to prevent exactly that: a literal `expected` never consults
    # `to_dict()`, so a key added to both the report and the literal's
    # blind spot is invisible here. Building it from the real shape means
    # the NEXT new key fails this test until someone decides where it is
    # documented.
    #
    # `recommendations` is technically a derived digest rather than a raw
    # bucket, but it appears in `to_dict()` and is enumerated alongside
    # the buckets in DESC for the same model-discovery reason, so the
    # derivation picks it up on purpose.
    from bettermemory.health import HealthReport

    wire_keys = set(
        HealthReport(
            generated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            window_days=30,
            total_active_memories=0,
            total_events=0,
            distinct_sessions=0,
        ).to_dict()
    )
    # Report metadata — surfaced in the same DESC string but ABOVE the
    # sliced region, because they describe the run rather than name a
    # bucket.
    METADATA = {
        "generated_at",
        "window_days",
        "total_active_memories",
        "total_events",
        "distinct_sessions",
    }
    # Keys that are real wire keys and deliberately NOT in the bucket
    # region. Each needs a reason, because "exclude it" is also how a key
    # goes undocumented:
    #
    # - `telemetry_coverage` is documented after the `CLI equivalent:`
    #   line, i.e. outside this slice by construction. It is not a
    #   curation bucket — it is the honesty gate that says whether
    #   `dead_weight` was measurable at all — and putting it in the
    #   bucket list would invite the model to read it as another pile of
    #   rows to act on.
    # - `recent_silent_misses` is the inline triage subset of the
    #   `silent_misses` bucket (the bounded newest-first list carrying
    #   `event_id` for `memory_acknowledge_miss`). The DESC sends the
    #   model to `memory_audit_turn` for that workflow and documents the
    #   payload in `docs/api.md`; it has never been enumerated here.
    DOCUMENTED_OUTSIDE_THE_BUCKET_REGION = {
        "telemetry_coverage",
        "recent_silent_misses",
    }
    expected = wire_keys - METADATA - DOCUMENTED_OUTSIDE_THE_BUCKET_REGION
    assert extracted == expected, (
        "DESC_MEMORY_HEALTH's enumerated bucket names drifted from "
        f"HealthReport.to_dict(). Only in prose: "
        f"{sorted(extracted - expected)}; only in runtime: "
        f"{sorted(expected - extracted)}. Sync the docstring with the "
        "report's wire shape — clients build mental models from this "
        "description. A key that genuinely belongs outside the "
        "'Returns buckets' region goes in "
        "DOCUMENTED_OUTSIDE_THE_BUCKET_REGION above, WITH the sentence "
        "of prose that documents it somewhere else."
    )

    # ...and "documented elsewhere in this DESC" has to be true, or the
    # exclusion set is just a second blind spot with better comments.
    assert "`telemetry_coverage`" in DESC_MEMORY_HEALTH[end:], (
        "DESC_MEMORY_HEALTH excludes `telemetry_coverage` from the bucket "
        "region on the grounds that it is documented after the "
        "`CLI equivalent:` line — and it no longer is. Either restore "
        "that sentence or move the key into the bucket enumeration."
    )


def test_desc_strings_use_cold_endorsement_memories_not_endorsement_debt() -> None:
    """Pin the rename target: the DESC strings for `memory_health` and
    `memory_scope_overview` must enumerate the NEW name
    `cold_endorsement_memories` and NOT the OLD `endorsement_debt`.

    Catches future drift where someone copy-pastes prose from an older
    release or a stale comment back into the active DESC strings. The
    old name was renamed because it suggested per-turn counting; any
    re-introduction silently regresses the dashboard-clarity fix.

    Recommendation-kind drift is covered alongside: the
    `cleanup_cold_endorsements` recommendation kind must appear, and
    the legacy `cleanup_endorsement_debt` must not."""
    from bettermemory.handlers.health import DESC_MEMORY_HEALTH
    from bettermemory.handlers.scope_overview import DESC_MEMORY_SCOPE_OVERVIEW
    from bettermemory.handlers.write import DESC_MEMORY_WRITE

    for desc_name, desc in (
        ("DESC_MEMORY_HEALTH", DESC_MEMORY_HEALTH),
        ("DESC_MEMORY_SCOPE_OVERVIEW", DESC_MEMORY_SCOPE_OVERVIEW),
        ("DESC_MEMORY_WRITE", DESC_MEMORY_WRITE),
    ):
        assert "endorsement_debt" not in desc, (
            f"{desc_name} still references the legacy `endorsement_debt` "
            "name. Rename to `cold_endorsement_memories` — the field was "
            "renamed because it counts memories, not turns, and "
            "endorsement_debt misled readers into per-turn interpretation."
        )
        assert "cleanup_endorsement_debt" not in desc, (
            f"{desc_name} still references the legacy "
            "`cleanup_endorsement_debt` recommendation kind. Rename to "
            "`cleanup_cold_endorsements`."
        )

    assert "cold_endorsement_memories" in DESC_MEMORY_HEALTH, (
        "DESC_MEMORY_HEALTH must enumerate `cold_endorsement_memories` "
        "so the model knows the bucket exists and what it counts."
    )
    assert "cold_endorsement_memories" in DESC_MEMORY_SCOPE_OVERVIEW, (
        "DESC_MEMORY_SCOPE_OVERVIEW must enumerate "
        "`cold_endorsement_memories` in the curation_pending rollup."
    )
    assert "cleanup_cold_endorsements" in DESC_MEMORY_HEALTH, (
        "DESC_MEMORY_HEALTH must enumerate `cleanup_cold_endorsements` "
        "in the closed recommendation-kinds set so the model can "
        "switch over the kind exhaustively."
    )


# ---------------------------------------------------------------------------
# Change 6 — scope_mismatch warning at write time
# ---------------------------------------------------------------------------


async def test_scope_mismatch_fires_when_body_cites_other_project_name(
    server: Any,
) -> None:
    """Seed a `projects:foo` memory, then write a body that mentions
    `foo` while declaring a different scope. The gate should fire."""
    await _call(
        server,
        "memory_write",
        content="A foo project fact.",
        scopes=["projects:foo"],
    )
    res = await _call(
        server,
        "memory_write",
        content=("When working on foo, the build script lives at scripts/build.sh."),
        scopes=["tools"],
    )
    assert res["status"] == "scope_mismatch"
    assert "projects:foo" in res["suggested_scopes"]


async def test_scope_mismatch_does_not_persist(server: Any, memory_dir: Path) -> None:
    """A scope_mismatch return must not commit the body."""
    await _call(
        server,
        "memory_write",
        content="A foo project fact.",
        scopes=["projects:foo"],
    )
    # File count BEFORE the second write.
    before = len(list(memory_dir.glob("*.md")))
    res = await _call(
        server,
        "memory_write",
        content="Working on foo means setting FOO_DEBUG=1.",
        scopes=["tools"],
    )
    assert res["status"] == "scope_mismatch"
    after = len(list(memory_dir.glob("*.md")))
    assert before == after


async def test_acknowledge_scope_mismatch_overrides_and_commits(
    server: Any, memory_dir: Path
) -> None:
    """Setting `acknowledge_scope_mismatch=True` skips the gate; the
    write commits despite the cross-scope reference."""
    await _call(
        server,
        "memory_write",
        content="A foo project fact.",
        scopes=["projects:foo"],
    )
    res = await _call(
        server,
        "memory_write",
        content="Working on foo, FOO_DEBUG=1 is the canonical toggle.",
        scopes=["tools"],
        acknowledge_scope_mismatch=True,
    )
    assert res["status"] == "committed"
    files = list(memory_dir.glob("*.md"))
    assert len(files) == 2


async def test_scope_mismatch_skipped_when_scope_already_declared(
    server: Any,
) -> None:
    """A multi-scope write that DOES carry the relevant project tag is
    fine — the body legitimately mentions another project."""
    await _call(
        server,
        "memory_write",
        content="A foo project fact.",
        scopes=["projects:foo"],
    )
    res = await _call(
        server,
        "memory_write",
        content="Working on foo with the canonical setup.",
        scopes=["projects:foo", "tools"],
    )
    assert res["status"] == "committed"


async def test_scope_mismatch_silent_when_no_project_scopes(server: Any) -> None:
    """Empty store has no `projects:*` scopes to lean on; the gate
    should pass through silently."""
    res = await _call(
        server,
        "memory_write",
        content="A first-write fact about foo and bar.",
        scopes=["tools"],
    )
    assert res["status"] == "committed"


# ---------------------------------------------------------------------------
# Change 7 — verified_claims on memory_verify
# ---------------------------------------------------------------------------


def _extant_path(tmp_path: Path, name: str = "attested.txt") -> str:
    """A path that EXISTS on every platform, in POSIX form.

    `memory_verify` refuses attestations naming paths the attesting machine
    cannot stat, so these tests can no longer use a realistic-looking
    literal. `/etc/hosts` was the previous choice and it exists on Linux and
    macOS but not on windows-latest — which is what made it a CI failure
    rather than a local one. `as_posix()` keeps the string free of
    backslashes so it survives YAML frontmatter round-tripping unescaped,
    which the raw-text assertion below depends on.
    """
    target = tmp_path / name
    target.write_text("attested\n", encoding="utf-8")
    return target.as_posix()


async def test_verify_accepts_structured_claims(
    server: Any, memory_dir: Path, tmp_path: Path
) -> None:
    extant = _extant_path(tmp_path)
    res = await _call(
        server,
        "memory_write",
        content=f"The config file lives at {extant} on this host.",
        scopes=["tools"],
    )
    verified = await _call(
        server,
        "memory_verify",
        id=res["id"],
        verified_paths=[extant],
        verified_versions=["macOS-15.0"],
    )
    assert verified["verified_paths"] == [extant]
    assert verified["verified_versions"] == ["macOS-15.0"]


async def test_verify_persists_structured_claims(
    server: Any, memory_dir: Path, tmp_path: Path
) -> None:
    extant = _extant_path(tmp_path)
    res = await _call(
        server,
        "memory_write",
        content=f"The config file lives at {extant}.",
        scopes=["tools"],
    )
    await _call(
        server,
        "memory_verify",
        id=res["id"],
        verified_paths=[extant],
    )
    files = list(memory_dir.glob("*.md"))
    raw = files[0].read_text(encoding="utf-8")
    assert "verified_paths" in raw
    assert extant in raw


async def test_show_after_verify_marks_path_verified(
    server: Any, tmp_path: Path
) -> None:
    """A path the caller has attested AND that still exists shows up in
    `path_drift.verified` on memory_show."""
    extant = tmp_path / "exists.txt"
    extant.write_text("hello")
    res = await _call(
        server,
        "memory_write",
        content=f"The thing lives at `{extant}`.",
        scopes=["tools"],
    )
    await _call(
        server,
        "memory_verify",
        id=res["id"],
        verified_paths=[str(extant)],
    )
    shown = await _call(server, "memory_show", id=res["id"])
    assert shown["path_drift"] is not None
    assert str(extant) in shown["path_drift"]["verified"]


async def test_verify_passing_none_preserves_prior_lists(
    server: Any, tmp_path: Path
) -> None:
    """Calling memory_verify a second time without verified_paths
    preserves the previously-attested list — None means 'no change',
    not 'clear'."""
    extant = _extant_path(tmp_path)
    res = await _call(server, "memory_write", content="A claim.", scopes=["tools"])
    await _call(
        server,
        "memory_verify",
        id=res["id"],
        verified_paths=[extant],
    )
    after_no_arg = await _call(server, "memory_verify", id=res["id"])
    assert after_no_arg["verified_paths"] == [extant]


async def test_verify_passing_empty_list_clears_prior(
    server: Any, tmp_path: Path
) -> None:
    """An explicit empty list is the 'clear' signal — distinct from
    None."""
    extant = _extant_path(tmp_path)
    res = await _call(server, "memory_write", content="A claim.", scopes=["tools"])
    await _call(
        server,
        "memory_verify",
        id=res["id"],
        verified_paths=[extant],
    )
    cleared = await _call(server, "memory_verify", id=res["id"], verified_paths=[])
    assert cleared["verified_paths"] == []


# ---------------------------------------------------------------------------
# Path-drift provenance on the wire — evidence stays, escalation narrows
# ---------------------------------------------------------------------------
#
# `verify.py`'s unit tests pin the split itself. These pin the half that
# unit tests structurally cannot: that the new bucket TRAVELS. A bucket
# that exists on `PathDriftReport` and never reaches a tool caller is the
# failure mode `dropped_as_route`'s reach note documents — it has to ride
# `MemoryHit` → `hit_to_dict` → each independently-gated emit site, and
# each of those gates is written out longhand at its own site.
#
# The pair below is the acceptance criterion stated as two responses to
# almost the same memory: same body, same freshness, one attested and one
# not. The unattested one must still SHOW its missing path and must not
# escalate; the attested one must do both.


async def _write_citing_a_vanished_file(
    server: Any, tmp_path: Path, *, attest: bool, name: str
) -> tuple[str, Path]:
    """Write a memory citing a real file, verify it, then delete the file.

    With `attest=True` the verify call names the path, which is what makes
    the later absence claim-anchored evidence; with `attest=False` the
    identical absence is a prose citation nobody reviewed.
    """
    cited = tmp_path / name
    cited.write_text("x\n", encoding="utf-8")
    written = await _call(
        server,
        "memory_write",
        content=f"The provenance fixture cites `{cited}` for its config.",
        scopes=["tools"],
    )
    await _call(
        server,
        "memory_verify",
        id=written["id"],
        **({"verified_paths": [str(cited)]} if attest else {}),
    )
    cited.unlink()
    return str(written["id"]), cited


async def test_show_surfaces_a_prose_miss_without_escalating(
    server: Any, tmp_path: Path
) -> None:
    """A calendar-fresh memory whose PROSE-cited path vanished: the path
    is still named on the wire, and the verdict stays `fresh`.

    Before the provenance split this read `spot_check_recommended`, on
    evidence measured ~0-of-15 real. The fix is not to hide the evidence
    — `path_drift.missing` is unchanged — it is to stop the evidence from
    speaking as a verdict.
    """
    memory_id, cited = await _write_citing_a_vanished_file(
        server, tmp_path, attest=False, name="prose-only.toml"
    )
    shown = await _call(server, "memory_show", id=memory_id)

    assert shown["path_drift"]["missing"] == [str(cited)]
    assert shown["path_drift"]["claim_anchored_missing"] == []
    assert shown["staleness_verdict"] == "fresh"


async def test_show_escalates_on_an_attested_miss(server: Any, tmp_path: Path) -> None:
    """The mirror: identical body, identical deletion, but the path was
    attested at verify time. Verified-then-deleted is the 3-of-3-real
    class, so this one escalates AND names which path did it."""
    memory_id, cited = await _write_citing_a_vanished_file(
        server, tmp_path, attest=True, name="attested.toml"
    )
    shown = await _call(server, "memory_show", id=memory_id)

    assert shown["path_drift"]["missing"] == [str(cited)]
    assert shown["path_drift"]["claim_anchored_missing"] == [str(cited)]
    assert shown["staleness_verdict"] == "spot_check_recommended"


async def test_per_hit_search_block_keeps_the_full_missing_set(
    server: Any, tmp_path: Path
) -> None:
    """The per-hit `path_drift` block is REBUILT from `MemoryHit` fields
    rather than serialised from the report, so it is the surface where a
    new bucket most easily fails to travel.

    Both halves are pinned here: the triage count and the path list stay
    FULL-set (nothing the caller could act on before disappeared), and
    the escalating bucket is omitted entirely when it is empty — additive
    like `expected_absent` and `dropped_as_route`, so the common hit does
    not grow a key that is always `[]`.
    """
    memory_id, cited = await _write_citing_a_vanished_file(
        server, tmp_path, attest=False, name="prose-hit.toml"
    )
    hits = _unwrap(
        await _call(
            server, "memory_search", query="provenance fixture config", auto_scope=False
        )
    )
    hit = next(h for h in hits if h["id"] == memory_id)

    assert hit["path_drift_missing"] == 1
    assert hit["path_drift"]["missing"] == [str(cited)]
    assert "claim_anchored_missing" not in hit["path_drift"]
    assert hit["staleness_verdict"] == "fresh"


async def test_per_hit_search_block_names_the_escalating_miss(
    server: Any, tmp_path: Path
) -> None:
    """…and carries the bucket the moment it fires, on a non-expanded
    hit. Without the `MemoryHit` field this is exactly where the split
    would go invisible: `hit_to_dict` has no report to serialise."""
    memory_id, cited = await _write_citing_a_vanished_file(
        server, tmp_path, attest=True, name="attested-hit.toml"
    )
    hits = _unwrap(
        await _call(
            server, "memory_search", query="provenance fixture config", auto_scope=False
        )
    )
    hit = next(h for h in hits if h["id"] == memory_id)

    assert hit["path_drift"]["claim_anchored_missing"] == [str(cited)]
    assert hit["staleness_verdict"] == "spot_check_recommended"


async def test_list_with_bodies_agrees_with_show_on_provenance(
    server: Any, tmp_path: Path
) -> None:
    """`memory_list(with_bodies=True)` computes its own verdict from its
    own `detect_path_drift` call — a fifth site, easy to miss. A prose
    miss must not raise it there either, or the list view would nag about
    memories `memory_show` reads as fresh."""
    memory_id, _ = await _write_citing_a_vanished_file(
        server, tmp_path, attest=False, name="prose-list.toml"
    )
    rows = _unwrap(await _call(server, "memory_list", with_bodies=True))
    row = next(r for r in rows if r["id"] == memory_id)
    shown = await _call(server, "memory_show", id=memory_id)

    assert row["staleness_verdict"] == "fresh"
    assert row["staleness_verdict"] == shown["staleness_verdict"]


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
async def test_commit_drift_recompute_does_not_re_broaden_the_path_leg(
    memory_dir: Path, tmp_path: Path
) -> None:
    """The per-search recompute is a SECOND verdict emission site, and it
    used to take its path input from the serialised hit dict — where the
    only available number is the full-set triage count.

    That is the one place the provenance split could be undone by a
    single line without any other surface noticing, so the fixture is
    built to discriminate: a stale memory with a PROSE-only missing path
    and a measured-zero commit leg. Reading the claim-anchored count
    (zero) lets the stale-demotion arm fire and the hit reads `fresh`;
    reading the dict's full count (one) makes it drifty and the hit reads
    `spot_check_required`. Same response, opposite tiers.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    origin = Origin(cwd=str(repo), repo=_FAKE_REPO_REMOTE, branch="main")
    server = _build_stale_server_with_origin(memory_dir, origin)

    cited = tmp_path / "prose-recompute.toml"
    cited.write_text("x\n", encoding="utf-8")
    written = await _call(
        server,
        "memory_write",
        content=f"The provenance fixture cites `{cited}` for its config.",
        scopes=["tools"],
    )
    await _call(server, "memory_verify", id=written["id"], note="seed")
    cited.unlink()

    hits = _unwrap(
        await _call(
            server, "memory_search", query="provenance fixture config", auto_scope=False
        )
    )
    hit = next(h for h in hits if h["id"] == written["id"])
    assert "commit_drift_count" in hit, (
        "test setup failed: attach_commit_drift_counts did not recompute the "
        "verdict for this hit, so the recompute's path input isn't exercised"
    )
    assert hit["commit_drift_count"] == 0
    assert hit["verification"]["status"] == "stale"
    assert hit["path_drift_missing"] == 1, "the prose evidence must still ship"
    assert hit["staleness_verdict"] == "fresh"

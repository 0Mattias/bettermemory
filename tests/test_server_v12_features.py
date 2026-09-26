"""End-to-end tests for the v1.2 surface additions.

Covers the seven changes that landed together:

1. ``ambient`` memory category — write, persist, long-body warning,
   exclusion from dead-weight curation.
2. Dead-weight rule fix + ``cold_memories`` bucket.
3. ``staleness_verdict`` rollup field on every retrieval surface.
4. Auto-``record_use`` via response tokens.
5. (the session-start rollup; its tool is gone, `health.curation_counts`
   is pinned in `tests/test_server_commit_drift.py`)
6. ``scope_mismatch`` warning at ``memory_write`` time.
7. Structured ``verified_claims`` on ``memory_verify``.

Each section is grouped by the change it exercises so a future
maintainer can find the locking tests for one feature without
spelunking. The fixtures match the rest of `test_server.py` —
hermetic per-test memory dir, isolated SessionState.
"""

from __future__ import annotations
from ._mcp import call_tool as _mcp_call

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import BehaviorConfig, Config, StorageConfig
from bettermemory.events import Recorder
from bettermemory.health import report_for_store
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
def server_with_state(memory_dir: Path) -> tuple[Any, SessionState, Store]:
    """Variant fixture that exposes the SessionState so use-token tests
    can introspect what the auto-commit pass did, and the store so they
    can read the event log. Mirrors `server` otherwise."""
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    state = SessionState()
    store = Store(memory_dir)
    srv = build_server(
        config=cfg,
        store=store,
        state=state,
    )
    return srv, state, store


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
    """Invoke a tool and return its structured payload.

    Delegates to `tests/_mcp.py`, which owns the SDK's return shape so
    the mcp 2.x port edits one function rather than forty-four.
    """
    return await _mcp_call(server, name, kwargs)


def _unwrap(res: Any) -> Any:
    """The SDK wraps `list[...]` tool responses as `{"result": [...]}` on
    the structured side. Mirror the helper from `test_server.py`."""
    return res.get("result", res) if isinstance(res, dict) and "result" in res else res


async def _advance(srv: Any) -> None:
    """One memory tool turn that issues no use token and settles none:
    what the use-token tests need to age a token by a turn."""
    await _call(srv, "memory_admin", action="enable_scope", scope="never-disabled")


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


async def test_ambient_category_persists_in_the_store(
    server: Any, memory_dir: Path
) -> None:
    """The new field round-trips through the store."""
    res = await _call(
        server,
        "memory_write",
        content="The user is based in Boston.",
        scopes=["personal-context"],
        category="ambient",
        acknowledge_user_claim=True,  # user claim filed as ambient; see above
    )
    [stored] = Store.open(memory_dir).load_all()
    assert stored.category is not None and stored.category.value == "ambient"

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


async def test_ambient_excluded_from_dead_weight_via_health(
    server: Any, memory_dir: Path
) -> None:
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
    health = report_for_store(Store.open(memory_dir), window_days=0).to_dict()
    dead_ids = {m["id"] for m in health["dead_weight"]}
    cold_ids = {m["id"] for m in health["cold_memories"]}
    assert written["id"] not in dead_ids
    assert written["id"] not in cold_ids


# ---------------------------------------------------------------------------
# Change 2 — cold_memories bucket exposed via memory_health
# ---------------------------------------------------------------------------


async def test_cold_memories_field_returned_by_health(
    server: Any, memory_dir: Path
) -> None:
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
    res = report_for_store(Store.open(memory_dir), window_days=0).to_dict()
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


def _commit_file(repo: Path, relpath: str) -> None:
    """Commit ``relpath`` (already written under ``repo``) with the same
    frozen 2020 identity and dates ``_init_repo`` uses, so the file has
    history that predates any verify the test performs. The hit surface
    classifies commit-drift APPLICABILITY on its quiescent branch the
    same way ``memory_show`` does: an anchor that escapes the repo, or
    that no commit ever touched, is not applicable and the recompute
    never runs for it. A fixture that wants the recompute to fire must
    therefore cite a file INSIDE the repo WITH history."""
    env = os.environ.copy()
    env["GIT_AUTHOR_DATE"] = "2020-01-02T00:00:00+00:00"
    env["GIT_COMMITTER_DATE"] = "2020-01-02T00:00:00+00:00"
    env["GIT_AUTHOR_NAME"] = "Test"
    env["GIT_AUTHOR_EMAIL"] = "test@example.com"
    env["GIT_COMMITTER_NAME"] = "Test"
    env["GIT_COMMITTER_EMAIL"] = "test@example.com"
    subprocess.run(["git", "add", relpath], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", f"add {relpath}"],
        cwd=repo,
        check=True,
        capture_output=True,
        env=env,
    )


def _build_stale_server_with_origin(
    memory_dir: Path, origin: Origin, monkeypatch: pytest.MonkeyPatch
) -> Any:
    """Build a server with ``verification_stale_days=0`` whose
    ``capture_origin`` returns the supplied ``origin``. Mirrors the
    monkeypatch pattern from ``tests/test_server_commit_drift.py`` so the
    test can pin the caller's repo without altering the process cwd, and
    so the fake is restored at teardown instead of leaking into later
    tests that rely on the real capture."""
    import bettermemory._handlers as handlers_module
    import bettermemory.server as server_module

    state = SessionState()
    cfg = Config(
        storage=StorageConfig(directory=str(memory_dir)),
        behavior=BehaviorConfig(verification_stale_days=0),
    )
    store = Store(memory_dir)
    recorder = Recorder(store=store, session_id=state.session_id)
    srv = build_server(
        config=cfg,
        store=store,
        state=state,
        recorder=recorder,
    )

    def fake_capture(cwd: Path | None = None) -> Origin:
        return origin

    monkeypatch.setattr(handlers_module, "capture_origin", fake_capture)
    monkeypatch.setattr(server_module, "capture_origin", fake_capture)
    return srv


async def _write_memory_in_state(
    server: Any, *, status: str, repo: Path | None = None
) -> str:
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

    With ``repo`` given, the cited file lives INSIDE that repo and is
    committed there (``_commit_file``) before the memory is written, then
    deleted the same way. That is the shape the commit-drift recompute
    tests need: the per-hit surface classifies applicability on its
    quiescent branch, so an anchor outside the caller's repo omits the
    count and the recompute never runs.
    """
    if repo is not None:
        cited = repo / "widget-staleness-pin.toml"
        cited.write_text("[widget]\n", encoding="utf-8")
        _commit_file(repo, cited.name)
        written = await _write_and_attest(server, cited, status=status)
        cited.unlink()
        return str(written["id"])
    scratch = Path(tempfile.mkdtemp(prefix="bm-staleness-pin-"))
    try:
        cited = scratch / "widget-staleness-pin.toml"
        cited.write_text("[widget]\n", encoding="utf-8")
        written = await _write_and_attest(server, cited, status=status)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return str(written["id"])


async def _write_and_attest(server: Any, cited: Path, *, status: str) -> Any:
    """The write-then-attest half of ``_write_memory_in_state``, shared by
    its scratch-directory and in-repo shapes."""
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
    return written


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
    memory_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
    server = _build_stale_server_with_origin(memory_dir, origin, monkeypatch)

    memory_id = await _write_memory_in_state(server, status="stale", repo=repo)
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
    memory_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
    server = _build_stale_server_with_origin(memory_dir, origin, monkeypatch)

    memory_id = await _write_memory_in_state(server, status="stale", repo=repo)
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
    memory_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
    server = _build_stale_server_with_origin(memory_dir, origin, monkeypatch)

    memory_id = await _write_memory_in_state(server, status="stale", repo=repo)
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
    server_with_state: tuple[Any, SessionState, Store],
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
    srv, _state, store = server_with_state
    res = await _call(
        srv, "memory_write", content="A retrievable fact.", scopes=["tools"]
    )

    await _call(srv, "memory_search", query="retrievable fact")
    # Turn deltas: search at turn ~2 issued a token. Two more calls
    # advance the counter — by the third call, the auto-commit fires.
    await _advance(srv)  # +1
    await _advance(srv)  # +2
    await _advance(srv)  # +3 — auto-commit fires here

    events = list(store.iter_events())
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
    server_with_state: tuple[Any, SessionState, Store],
) -> None:
    """An explicit record_use(ignored) for a still-pending token must
    NOT produce an `applied` shadow event in the log."""
    srv, _state, store = server_with_state
    res = await _call(
        srv, "memory_write", content="A retrievable fact.", scopes=["tools"]
    )
    await _call(srv, "memory_search", query="retrievable fact")
    await _call(srv, "memory_record_use", memory_ids=[res["id"]], outcome="ignored")

    # Advance enough turns for any rogue auto-commit to fire.
    await _advance(srv)
    await _advance(srv)
    await _advance(srv)

    events = list(store.iter_events())
    use_events = [e for e in events if e.get("kind") == "use"]
    # Find any event citing the memory id.
    for e in use_events:
        if res["id"] in (e.get("ids") or []):
            # Should be the explicit `ignored`, never `applied`+auto.
            if e.get("outcome") == "applied" and e.get("auto") is True:
                pytest.fail(f"explicit override leaked an auto-applied event: {e}")


async def test_explicit_record_use_purges_token(
    server_with_state: tuple[Any, SessionState, Store],
) -> None:
    """After an explicit record_use, the pending token map should
    drop the id so a future auto-commit sweep doesn't double-fire."""
    srv, state, _store = server_with_state
    res = await _call(
        srv, "memory_write", content="A retrievable fact.", scopes=["tools"]
    )
    await _call(srv, "memory_search", query="retrievable fact")
    assert res["id"] in state.pending_use_tokens
    await _call(srv, "memory_record_use", memory_ids=[res["id"]], outcome="applied")
    assert res["id"] not in state.pending_use_tokens


async def test_use_token_within_ttl_does_not_auto_commit(
    server_with_state: tuple[Any, SessionState, Store],
) -> None:
    """One memory_* call after a search isn't enough for the token to age
    out; the auto-commit pass only fires after `ttl_turns` deltas."""
    srv, _state, store = server_with_state
    res = await _call(
        srv, "memory_write", content="A retrievable fact.", scopes=["tools"]
    )
    await _call(srv, "memory_search", query="retrievable fact")
    await _advance(srv)  # one turn advance — still within TTL

    events = list(store.iter_events())
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
    server_with_state: tuple[Any, SessionState, Store],
) -> None:
    """When the Stop hook has already emitted an `applied` event with
    `attribution="hook"` for a memory, the in-process auto-commit must
    NOT fire a second `applied` event two turns later. The hook
    happens cross-process; the dedup goes via the event log."""
    srv, state, store = server_with_state
    res = await _call(
        srv, "memory_write", content="A retrievable fact.", scopes=["tools"]
    )
    await _call(srv, "memory_search", query="retrievable fact")
    assert res["id"] in state.pending_use_tokens

    # Simulate the Stop hook attributing this retrieval. The hook
    # writes through Recorder with the same session id the in-process
    # one uses; advance_turn's dedup pass reads the event log.
    from bettermemory.events import Recorder

    Recorder(store=store, session_id=state.session_id).record(
        "use",
        ids=[res["id"]],
        outcome="applied",
        auto=False,
        attribution="hook",
        claim_excerpts=["A retrievable fact"],
        triggered_from="stop_hook",
    )

    # Advance enough turns for any rogue auto-commit to fire.
    await _advance(srv)
    await _advance(srv)
    await _advance(srv)

    events = list(store.iter_events())
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
    server_with_state: tuple[Any, SessionState, Store],
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
    srv, state, store = server_with_state
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
    Recorder(store=store, session_id=transcript_session_id).record(
        "use",
        ids=[res["id"]],
        outcome="applied",
        auto=False,
        attribution="hook",
        claim_excerpts=["A retrievable fact"],
        triggered_from="stop_hook",
    )

    # Advance enough turns for any rogue auto-commit to fire.
    await _advance(srv)
    await _advance(srv)
    await _advance(srv)

    events = list(store.iter_events())
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
    server_with_state: tuple[Any, SessionState, Store],
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
    srv, state, store = server_with_state
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
    await _advance(srv)
    await _advance(srv)
    events = list(store.iter_events())
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
    server_with_state: tuple[Any, SessionState, Store],
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
    srv, state, store = server_with_state
    res = await _call(
        srv, "memory_write", content="A retrievable fact.", scopes=["tools"]
    )
    mid = res["id"]
    await _call(srv, "memory_search", query="retrievable fact")
    assert mid in state.pending_use_tokens

    # The Stop hook settles the turn's retrieval seconds after the reply.
    transcript_session_id = "claude-code-transcript-expiry"
    assert transcript_session_id != state.session_id
    Recorder(store=store, session_id=transcript_session_id).record(
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

    await _advance(srv)

    events = list(store.iter_events())
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
    server_with_state: tuple[Any, SessionState, Store],
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

    srv, state, store = server_with_state
    res = await _call(
        srv, "memory_write", content="A retrievable fact.", scopes=["tools"]
    )
    mid = res["id"]
    await _call(srv, "memory_search", query="retrievable fact")
    _backdate_use_token(state, mid)

    await _advance(srv)

    events = list(store.iter_events())
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
    server_with_state: tuple[Any, SessionState, Store],
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
    srv, state, store = server_with_state
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

    events = list(store.iter_events())
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
    server_with_state: tuple[Any, SessionState, Store],
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
    srv, state, store = server_with_state
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
    await _advance(srv)
    await _advance(srv)
    await _advance(srv)

    assert not state.pending_use_tokens, (
        "a token is still live — the closure below would be vacuously "
        "satisfiable by tokens that simply haven't been decided yet"
    )

    events = list(store.iter_events())
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
    # Record count BEFORE the second write.
    before = len(Store.open(memory_dir).load_all())
    res = await _call(
        server,
        "memory_write",
        content="Working on foo means setting FOO_DEBUG=1.",
        scopes=["tools"],
    )
    assert res["status"] == "scope_mismatch"
    after = len(Store.open(memory_dir).load_all())
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
    assert len(Store.open(memory_dir).load_all()) == 2


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
    stored = Store.open(memory_dir).load_one(res["id"])
    assert stored.verified_paths == [extant]


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
    identical absence is a prose citation nobody reviewed. The tool refuses
    to land that second stamp since 7.3.0 (a verify attesting nothing on a
    memory whose cited paths resolve), so the unattested shape — which
    stores stamped before then still carry — is produced through the
    policy-free store primitive, and what these tests pin is how the read
    side treats it.
    """
    cited = tmp_path / name
    cited.write_text("x\n", encoding="utf-8")
    written = await _call(
        server,
        "memory_write",
        content=f"The provenance fixture cites `{cited}` for its config.",
        scopes=["tools"],
    )
    if attest:
        await _call(
            server, "memory_verify", id=written["id"], verified_paths=[str(cited)]
        )
    else:
        Store(tmp_path / "memories").mark_verified(written["id"])
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


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
async def test_commit_drift_recompute_does_not_re_broaden_the_path_leg(
    memory_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
    server = _build_stale_server_with_origin(memory_dir, origin, monkeypatch)

    # Inside the repo and committed there: the recompute only runs for an
    # anchor the commit leg applies to (see ``_commit_file``).
    cited = repo / "prose-recompute.toml"
    cited.write_text("x\n", encoding="utf-8")
    _commit_file(repo, cited.name)
    written = await _call(
        server,
        "memory_write",
        content=f"The provenance fixture cites `{cited}` for its config.",
        scopes=["tools"],
    )
    # An unattested stamp, the shape the recompute has to read as prose
    # evidence; the tool refuses to land one on this body, so the store
    # primitive stamps it (see `_write_citing_a_vanished_file`).
    Store(memory_dir).mark_verified(written["id"])
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

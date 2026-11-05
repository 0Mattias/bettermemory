"""A history that applies and was not read never reads fresh (ENG-220's
review, round E).

`origin_dates.commit_author_timestamps` tells a history that does not apply
(None: no repository the files name, no git) from one that applies and was
not read (`origin_history.HistoryUnread`): git met its timeout, failed on the
head named by its hash, the history is longer than the memo's budget, or the
clone is shallow. Each cause reaches every surface. The search hit, the
expanded top hit and memory_show flag the row (`commit_drift_unread: true`),
drop it to `spot_check_required` and name the cause and its remedy in the
recommendation; `verify.compute_commit_drift` returns the cause; the health
rollups count the row under `unread`, with its cause and its id. A memory
whose anchors all escape the repository reads not applicable whatever the
cause, and a root where no history applies stays unconfirmed.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from bettermemory import (
    _caches,
    _response,
    health,
    health_render,
    health_stats,
    origin_dates,
    origin_history,
    verify,
)
from bettermemory.health_model import CrossRepoDrift, MemoryStats
from bettermemory.origin import Origin
from bettermemory.store import Store
from bettermemory.verify import ClaimAnchor, compute_commit_drift
from bettermemory.verify_basis import (
    UNREAD_RECOMMENDATIONS,
    CommitDriftUnread,
    unread_recommendation,
)

from .test_commit_drift_cache import (
    _REMOTE,
    _T0,
    _T1,
    _V1,
    _attach,
    _by_id,
    _caller,
    _commit,
    _git,
    _repo,
    _write,
    keyed_by,  # noqa: F401
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

_CAUSES = ["timeout", "error", "over_budget", "shallow"]
#: The remote `_repo` gives every repository, which the estate check reads.
_URL = _REMOTE
#: A path no repository here holds: a memory citing it alone reads not
#: applicable, history read or not.
_OUTSIDE = "/opt/elsewhere/app.ini"


def _unread(
    cause: str, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> CommitDriftUnread:
    """Make the whole history of `repo` meet `cause`, and return the cause
    the readers answer: the log meets its timeout or exits non-zero, the
    memo keeps no date, or a shallow file lists the root commit."""
    if cause == "over_budget":
        monkeypatch.setattr(origin_history, "_TIMESTAMPS_MEMO_SLOTS", 0)
        return CommitDriftUnread.OVER_BUDGET
    if cause == "shallow":
        root = _git(repo, "rev-list", "--max-parents=0", "HEAD")
        (repo / ".git" / "shallow").write_text(f"{root}\n", encoding="utf-8")
        return CommitDriftUnread.SHALLOW
    real_run = subprocess.run

    def run(argv: Any, *args: Any, **kwargs: Any) -> Any:
        if "--format=%at" in argv:
            if cause == "timeout":
                raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 5.0))
            return subprocess.CompletedProcess(argv, 128, b"", b"fatal: bad object\n")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    return CommitDriftUnread(cause)


def _scenario(
    tmp_path: Path, memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Origin, str, str, list[Any]]:
    """A repository with its remote; a memory citing notes0.md, which one
    commit since the stamp edited (1 on the dates), and one citing only a
    path outside every repository."""
    repo = _repo(tmp_path)
    _commit(repo, "seed", when=_T0, files={"notes0.md": "a\n"})
    store = Store(memory_dir)
    caller = _caller(repo)
    cited = _write(
        store, caller, monkeypatch, f"widget rule a lives in {repo / 'notes0.md'}"
    )
    outside = _write(store, caller, monkeypatch, f"widget rule b lives in {_OUTSIDE}")
    _commit(repo, "post", when=_T1, files={"notes0.md": "a1\n"})
    return repo, caller, cited, outside, store.load_all()


@pytest.mark.usefixtures("keyed_by")
@pytest.mark.parametrize("cause", _CAUSES)
def test_a_search_hit_whose_history_was_not_read_is_flagged(
    cause: str, tmp_path: Path, memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read, the hit counts 1. Not read, for each cause, the hit carries no
    count, is flagged, reads spot_check_required and names the cause, on
    every search, whether the files or git's answer key the rows; the hit
    citing only an outside path reads not applicable either way. Neither is
    kept in the per-hit memo, save a timeout's for a while, so the next
    read after the cause clears counts again."""
    repo, caller, cited, outside, memories = _scenario(
        tmp_path, memory_dir, monkeypatch
    )
    before, _ = _attach(memories, caller, monkeypatch)
    assert _by_id(before)[cited]["commit_drift_count"] == 1, "premise: it applies"
    assert "commit_drift_count" not in _by_id(before)[outside]
    _caches.clear_all()
    why = _unread(cause, repo, monkeypatch)
    for _ in range(2):
        out, _ = _attach(memories, caller, monkeypatch)
        hit = _by_id(out)[cited]
        assert hit["commit_drift_unread"] is True
        assert "commit_drift_count" not in hit and "commit_drift_basis" not in hit
        assert hit["staleness_verdict"] == "spot_check_required"
        assert hit["verification"]["recommendation"] == unread_recommendation(why)
        other = _by_id(out)[outside]
        assert "commit_drift_unread" not in other and "commit_drift_count" not in other
    assert not _response._DRIFT_MEMO
    assert bool(_response._DRIFT_TIMED_OUT) == (cause == "timeout")


@pytest.mark.parametrize("cause", _CAUSES)
async def test_every_surface_names_the_cause(
    cause: str, tmp_path: Path, memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """memory_show, the search hit and the expanded top hit flag the row
    and name the cause; memory_admin's health counts it under `unread`,
    with the cause and the id, and lists no row for it."""
    import bettermemory._handlers as handlers_module
    from bettermemory.config import Config, StorageConfig
    from bettermemory.events import Recorder
    from bettermemory.location import resolve_caller
    from bettermemory.server import build_server
    from bettermemory.session import SessionState

    from ._mcp import call_tool

    verified = datetime.now(timezone.utc) - timedelta(hours=1)
    repo = _repo(tmp_path)
    _commit(
        repo, "seed", when=verified - timedelta(hours=1), files={"notes0.md": "a\n"}
    )
    store = Store(memory_dir)
    config = Config(storage=StorageConfig(directory=str(memory_dir)))
    caller = _caller(repo)
    home = resolve_caller(store, config, str(repo)).home()
    memory_id = _write(
        store,
        caller,
        monkeypatch,
        f"quokka widget rule lives in {repo / 'notes0.md'}",
        at=verified,
        home=home,
    )
    _commit(
        repo, "post", when=verified + timedelta(minutes=30), files={"notes0.md": "a1\n"}
    )
    state = SessionState()
    server = build_server(
        config=config,
        store=store,
        state=state,
        recorder=Recorder(store=store, session_id=state.session_id),
    )
    monkeypatch.setattr(handlers_module, "capture_origin", lambda cwd=None: caller)
    shown = await call_tool(server, "memory_show", {"id": memory_id})
    assert shown["commit_drift"]["commits_since_verify"] == 1, "premise: it applies"
    _caches.clear_all()
    why = _unread(cause, repo, monkeypatch)
    rows = [await call_tool(server, "memory_show", {"id": memory_id})]
    for expand_top in (False, True):
        raw = await call_tool(
            server,
            "memory_search",
            {"query": "quokka widget rule", "expand_top": expand_top},
        )
        hits = raw.get("result", raw) if isinstance(raw, dict) else raw
        assert [hit["id"] for hit in hits] == [memory_id]
        rows.append(dict(hits[0]))
    assert rows[0]["commit_drift"] is None and "body" in rows[2]
    for row in rows:
        assert row["commit_drift_unread"] is True
        assert row["staleness_verdict"] == "spot_check_required"
        assert row["verification"]["recommendation"] == unread_recommendation(why)
        assert "commit_drift_count" not in row
    report = await call_tool(server, "memory_admin", {"action": "health"})
    debt = report["commit_drift_debt"]
    assert (debt["rows"], debt["unread"]) == ([], 1)
    assert debt["unread_causes"] == {why.value: 1}
    assert debt["unread_ids"] == [memory_id]


def _stats(memory_id: str) -> MemoryStats:
    return MemoryStats(
        id=memory_id,
        scopes=[],
        summary="s",
        created=_V1,
        updated=_V1,
        last_verified_at=_V1,
    )


class _At:
    """An anchoring stand-in that resolves every cited commit at `root`."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def for_record(self, memory_id: str) -> _At:
        return self

    def commit_root(self, cited: str | None) -> tuple[str, str]:
        return str(self.root), "stand-in"


@pytest.mark.parametrize("cause", _CAUSES)
def test_the_show_count_and_every_rollup_read_the_cause(
    cause: str, tmp_path: Path, memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`compute_commit_drift` returns the cause for m, and None for x, whose
    one anchor is outside the repository. The health rollups give m no row
    and count it under `unread`, with its cause: `commit_drift_debt`, a root
    its cited commit resolves at (`_drift_rows_by_root`), and the estate
    check's groups at a live view and at a recorded worktree; the text
    report names the cause. A root where no history applies (a folder that
    is no repository) leaves its candidates unconfirmed, not unread."""
    repo, _, _, _, _ = _scenario(tmp_path, memory_dir, monkeypatch)
    why = _unread(cause, repo, monkeypatch)
    anchor = ClaimAnchor(root=str(repo), rule="home_checkout")
    for body, want in ((f"lives in {repo / 'notes0.md'}", why), (_OUTSIDE, None)):
        _caches.clear_all()
        got = compute_commit_drift(
            _V1, _URL, caller_origin=None, body=f"widget rule {body}", anchor=anchor
        )
        assert got is want
    candidates = [_stats("m"), _stats("x")]
    legs: dict[str, Any] = {
        "anchor_paths_by_id": {"m": (str(repo / "notes0.md"),), "x": (_OUTSIDE,)},
        "claims_by_id": None,
        "verified_head_by_id": None,
    }
    debt = health._compute_commit_drift_debt(
        by_id={stats.id: stats for stats in candidates},
        origin_repo_by_id={"m": _URL, "x": _URL},
        caller_origin=Origin(cwd=str(repo), repo=_URL, branch=None),
        **legs,
    )
    assert debt is not None
    unread = {"unread": 1, "unread_causes": {why.value: 1}, "unread_ids": ["m"]}
    assert debt.rows == [] and debt.to_dict().items() >= unread.items()
    rows, unconfirmed, by_id = health._drift_rows_by_root(
        candidates,
        cwd_path=repo,
        timestamps=why,
        toplevel=None,
        head=None,
        anchoring=cast(Any, _At(repo)),
        **legs,
    )
    assert (rows, unconfirmed, by_id) == ([], 0, {"m": why})
    nowhere = tmp_path / "nowhere"
    nowhere.mkdir()
    rows, unconfirmed, by_id = health._drift_rows_by_root(
        candidates,
        cwd_path=repo,
        timestamps=why,
        toplevel=None,
        head=None,
        anchoring=cast(Any, _At(nowhere)),
        **legs,
    )
    assert (rows, unconfirmed, by_id) == ([], 2, {})
    live = health._cross_repo_group(_URL, str(repo), candidates, **legs)
    skipped: list[dict[str, str]] = []
    recorded = health._recorded_root_group(
        _URL, str(repo), repo, candidates, skipped, **legs
    )
    assert skipped == []
    for group in (live, recorded):
        assert group is not None and group.rows == []
        assert group.to_dict().items() >= unread.items()
    assert live is not None
    report = SimpleNamespace(
        commit_drift_debt=debt, cross_repo_drift=CrossRepoDrift(groups=[live])
    )
    text = "\n".join(health_render._drift_lines(cast(Any, report)))
    assert f"unread (1: {why.value} 1)" in text and "caught up" not in text
    assert "0 drifted, 1 unread of 2 candidate(s)" in text


def test_no_git_and_no_repository_read_not_applicable_never_unread(
    tmp_path: Path, memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not applicable stays as it was. With no git binary, the history
    reads None at a head the files name by its hash, and nothing is
    flagged: the search hit carries no count and no flag, and the health
    rollup has no debt. A folder that is no repository reads None too."""
    repo, caller, cited, outside, memories = _scenario(
        tmp_path, memory_dir, monkeypatch
    )
    real_run = subprocess.run

    def run(argv: Any, *args: Any, **kwargs: Any) -> Any:
        if argv and argv[0] == "git":
            raise FileNotFoundError(2, "No such file or directory", "git")
        return real_run(argv, *args, **kwargs)

    nowhere = tmp_path / "nowhere"
    nowhere.mkdir()
    monkeypatch.setattr(subprocess, "run", run)
    _caches.clear_all()
    assert origin_dates.commit_author_timestamps(repo) is None
    assert origin_dates.commit_author_timestamps(nowhere) is None
    out, _ = _attach(memories, caller, monkeypatch)
    for memory_id in (cited, outside):
        hit = _by_id(out)[memory_id]
        assert "commit_drift_unread" not in hit and "commit_drift_count" not in hit
        recommendation = hit["verification"].get("recommendation")
        assert recommendation not in UNREAD_RECOMMENDATIONS
    debt = health._compute_commit_drift_debt(
        by_id={"m": _stats("m")},
        origin_repo_by_id={"m": _URL},
        caller_origin=Origin(cwd=str(repo), repo=_URL, branch=None),
        anchor_paths_by_id={"m": (str(repo / "notes0.md"),)},
        claims_by_id=None,
        verified_head_by_id=None,
    )
    assert debt is None


def test_where_no_key_is_read_a_shallow_clone_reads_unread_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under a variable that moves git's discovery neither the files nor
    `githead.locate` key the history, which is read from HEAD and kept
    under no key: the dates read, and a shallow file reads them unread,
    `shallow`, there too. A folder that is no repository reads None."""
    from bettermemory import origin

    monkeypatch.setenv("GIT_DISCOVERY_ACROSS_FILESYSTEM", "1")
    repo = _repo(tmp_path)
    _commit(repo, "seed", when=_T0, files={"notes0.md": "a\n"})
    _commit(repo, "post", when=_T1, files={"notes0.md": "a1\n"})
    _caches.clear_all()
    assert origin.toplevel_and_head_from_files(repo) is None, "premise: no key"
    assert origin_history.located_key(repo) is None, "premise: no key"
    read = origin_dates.commit_author_timestamps(repo)
    assert isinstance(read, list) and len(read) == 2
    _unread("shallow", repo, monkeypatch)
    assert origin_dates.commit_author_timestamps(repo) is CommitDriftUnread.SHALLOW
    nowhere = tmp_path / "nowhere"
    nowhere.mkdir()
    assert origin_dates.commit_author_timestamps(nowhere) is None


def test_a_walk_given_up_reads_unread_only_where_the_leg_applies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The walk from the stamp's head and the listing of its commits both
    meet their timeout. The per-hit resolver, memory_show's count and the
    rollups' candidate loop read a memory citing a path in the repository
    unread, `timeout`, and one citing only a path outside it not
    applicable, as they do with the walk read: no history decides that."""
    repo = _repo(tmp_path)
    anchor = _commit(repo, "seed", when=_T0, files={"notes0.md": "a\n"})
    _commit(repo, "post", when=_T1, files={"notes0.md": "a1\n"})
    head = _git(repo, "rev-parse", "HEAD")
    real_run = subprocess.run
    met: list[Any] = []

    def run(argv: Any, *args: Any, **kwargs: Any) -> Any:
        if "--boundary" in argv:
            met.append(argv)
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 5.0))
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    cases = (
        (str(repo / "notes0.md"), CommitDriftUnread.TIMEOUT),
        (_OUTSIDE, None),
    )
    for cited, want in cases:
        _caches.clear_all()
        got = verify.resolve_hit_drift(
            cwd=repo,
            toplevel=repo,
            head=head,
            timestamps=lambda: origin_dates.commit_author_timestamps(repo),
            anchors=(cited,),
            claims=[],
            since=_V1,
            verified_head=anchor,
            walked={},
        )
        assert got is want, cited
        _caches.clear_all()
        shown = compute_commit_drift(
            _V1,
            _URL,
            caller_origin=None,
            body=f"widget rule lives in {cited}",
            anchor=ClaimAnchor(root=str(repo), rule="home_checkout"),
            verified_head=anchor,
        )
        assert shown is want, cited
        _caches.clear_all()
        history = origin_dates.commit_author_timestamps(repo)
        assert isinstance(history, list)
        rows, unread = health_stats._drift_rows_for_candidates(
            [_stats("m")],
            root=repo,
            timestamps=history,
            toplevel=repo,
            anchor_paths_by_id={"m": (cited,)},
            claims_by_id=None,
            verified_head_by_id={"m": anchor},
            head=head,
        )
        assert (rows, unread) == ([], {} if want is None else {"m": want}), cited
    assert met, "premise: the walk met its timeout"


def test_a_shallow_clone_reads_unread_until_it_is_unshallowed(
    tmp_path: Path, memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clone two commits deep is cut at the commit before the change. A
    memory on the dates reads unread for the cut, and is kept under no key,
    so the search after `git fetch --unshallow` counts it: 1. A memory
    stamped at the commit the cut lists has a window wholly inside the
    shallow history (the walk's range holds no cut) and counts 1 before
    and after."""
    source = _repo(tmp_path, "source")
    _commit(source, "seed", when=_T0, files={"notes0.md": "a\n", "notes1.md": "b\n"})
    held = _commit(
        source, "held", when=_T0 + timedelta(days=1), files={"other.md": "o\n"}
    )
    _commit(source, "post", when=_T1, files={"notes0.md": "a1\n", "notes1.md": "b1\n"})
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", "--depth=2", source.as_uri(), str(clone))
    shallow = clone / ".git" / "shallow"
    assert shallow.read_text(encoding="utf-8").split() == [held], "premise: the cut"
    store = Store(memory_dir)
    caller = _caller(clone)
    dated = _write(
        store, caller, monkeypatch, f"widget rule a lives in {clone / 'notes0.md'}"
    )
    walked = _write(
        store,
        caller,
        monkeypatch,
        f"widget rule b lives in {clone / 'notes1.md'}",
        verified_head=held,
    )
    memories = store.load_all()
    out, _ = _attach(memories, caller, monkeypatch)
    hit = _by_id(out)[dated]
    assert hit["commit_drift_unread"] is True and "commit_drift_count" not in hit
    assert hit["verification"]["recommendation"] == unread_recommendation(
        CommitDriftUnread.SHALLOW
    )
    inside = _by_id(out)[walked]
    assert "commit_drift_unread" not in inside
    assert (inside["commit_drift_count"], inside["commit_drift_basis"]) == (
        1,
        "reachability",
    )
    assert all(key[6] == held for key in _response._DRIFT_MEMO), "the walked row only"
    _git(clone, "fetch", "-q", "--unshallow")
    assert not shallow.exists(), "premise: unshallowed"
    out, _ = _attach(memories, caller, monkeypatch)
    for memory_id in (dated, walked):
        hit = _by_id(out)[memory_id]
        assert "commit_drift_unread" not in hit
        assert hit["commit_drift_count"] == 1

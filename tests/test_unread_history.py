"""A history that applies and was not read never reads fresh (ENG-220's
review, round E).

`origin_dates.commit_author_timestamps` tells a history that does not apply
(None: no repository the files name, no git, a repository git will not read
or a branch with no commit) from one that applies and was not read
(`origin_history.HistoryUnread`): git met its timeout, failed on the head,
the history is longer than the memo's budget, or the clone is shallow. Each cause reaches every surface. The search hit, the
expanded top hit and memory_show flag the row (`commit_drift_unread: true`),
drop it to `spot_check_required` and name the cause and its remedy in the
recommendation; `verify.compute_commit_drift` returns the cause; the health
rollups count the row under `unread`, with its cause and its id. A memory
whose anchors all escape the repository reads not applicable whatever the
cause, and a root where no history applies stays unconfirmed.
"""

from __future__ import annotations

import os
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
    githead,
    health,
    health_render,
    health_stats,
    origin,
    origin_dates,
    origin_history,
    verify,
)
from bettermemory.health_model import (
    CommitDriftDebt,
    CrossRepoDrift,
    CrossRepoDriftGroup,
    MemoryStats,
    unread_fields,
)
from bettermemory.health_recommend import _compute_recommendations
from bettermemory.origin import Origin
from bettermemory.store import Store
from bettermemory.verify import ClaimAnchor, compute_commit_drift
from bettermemory.verify_basis import (
    UNREAD_RECOMMENDATIONS,
    CommitDriftUnread,
    unread_recommendation,
)

from .test_commit_drift_cache import (
    _EXTENSION,
    _NAMED_HISTORY,
    _REMOTE,
    _T0,
    _T1,
    _V1,
    _WHOLE_HISTORY,
    _attach,
    _by_id,
    _caller,
    _commit,
    _git,
    _history_calls,
    _repo,
    _whole_history_logs,
    _write,
    discovery,  # noqa: F401
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
    (spot,) = [
        recommendation
        for recommendation in report["recommendations"]
        if recommendation["kind"] == "spot_check_unread"
    ]
    assert (spot["count"], spot["memory_ids"]) == (1, [memory_id])
    assert f"({why.value} 1)" in spot["summary"]


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


def _unread_report(
    caller: dict[str, CommitDriftUnread], groups: list[dict[str, CommitDriftUnread]]
) -> Any:
    """A report whose only buckets are the caller's checkout's commit drift
    and the estate's groups, with `caller` and each group unread."""
    debt = CommitDriftDebt(current_repo=_URL, current_cwd="/w", **unread_fields(caller))
    estate = CrossRepoDrift(
        groups=[
            CrossRepoDriftGroup(
                repo=f"git@example.com:g/{n}.git",
                worktree_root=f"/g{n}",
                candidates=len(unread),
                **unread_fields(unread),
            )
            for n, unread in enumerate(groups)
        ]
    )
    return SimpleNamespace(
        dead_weight=[],
        contradicted=[],
        cold_endorsement_memories=SimpleNamespace(total=0, rows=[]),
        commit_drift_debt=debt,
        cross_repo_drift=estate,
        provenance=None,
        rare_scopes=[],
    )


def test_health_recommends_a_spot_check_of_every_unread_memory() -> None:
    """One unread memory is enough: the recommendation counts the caller's
    checkout and each estate group, names each cause and how many, and its
    action names the spot-check, the re-stamp and each present cause's
    remedy, a shallow clone's fetch among them; its ids are capped at 10.
    Nothing unread, no recommendation."""
    timeout, shallow = CommitDriftUnread.TIMEOUT, CommitDriftUnread.SHALLOW
    assert _compute_recommendations(_unread_report({}, [{}])) == []
    (one,) = _compute_recommendations(_unread_report({"m": shallow}, []))
    assert (one.kind, one.count, one.memory_ids) == ("spot_check_unread", 1, ["m"])
    assert "(shallow 1)" in one.summary and "git fetch --unshallow" in one.action
    assert "memory_verify" in one.action and "timeout" not in one.action
    caller = {f"c{n}": timeout for n in range(8)}
    groups = [{f"g{n}": shallow for n in range(4)}, {"e": CommitDriftUnread.ERROR}]
    (many,) = _compute_recommendations(_unread_report(caller, groups))
    assert many.count == 13 and len(many.memory_ids) == 10
    assert many.memory_ids == [*caller, "g0", "g1"]
    assert "(error 1, shallow 4, timeout 8)" in many.summary
    for remedy in ("git log", "git fetch --unshallow", "run health again"):
        assert remedy in many.action
    assert "longer than" not in many.action


#: What the read from HEAD answers under GIT_DIR, and how many plumbing
#: probes it ran to tell it: none where the log answers or no repository's
#: files are there.
_REFUSED = {
    "answered": (list, 0),
    "no repository": (None, 0),
    "git refuses the repository": (CommitDriftUnread.ERROR, 1),
    "objects unreadable": (CommitDriftUnread.ERROR, 0),
    "unborn": (None, 2),
    "error": (CommitDriftUnread.ERROR, 2),
    "probe timeout": (CommitDriftUnread.TIMEOUT, 1),
    "log timeout": (CommitDriftUnread.TIMEOUT, 0),
}


@pytest.mark.parametrize("case", list(_REFUSED))
def test_under_git_dir_a_refused_head_is_told_apart_by_plumbing(
    case: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """GIT_DIR, here naming another repository, moves git's discovery, so
    nothing keys the read and the log runs from HEAD; every git the package
    runs drops GIT_DIR (`_git.SCRUBBED_ENV`), so the log and the probes
    alike read the directory's own repository. Where git exits non-zero on
    HEAD, the exit of plumbing run there tells why: a branch with no commit
    (`rev-parse --verify -q HEAD` fails) reads None, as a folder whose files
    name no repository and that no `.git` entry stands above does with no
    git run at all; a repository git will not read (its config git dies
    on: `rev-parse --git-dir` fails while its `.git` stands there) reads
    `error`, as one whose objects directory no one may search does with no
    git run (ENG-220's review, round F), and so does a repository whose
    head resolves and whose history git cannot walk (its root commit's
    object gone); a probe that meets its timeout reads `timeout`. A log
    that answers, or that meets its own timeout (`timeout`), runs neither
    probe."""
    elsewhere = _repo(tmp_path, "elsewhere")
    _commit(elsewhere, "other", when=_T0, files={"other.md": "o\n"})
    repo = _repo(tmp_path)
    if case == "no repository":
        repo = tmp_path / "plain"
        repo.mkdir()
    elif case != "unborn":
        root = _commit(repo, "seed", when=_T0, files={"notes0.md": "a\n"})
        _commit(repo, "post", when=_T1, files={"notes0.md": "a1\n"})
        if case in ("error", "probe timeout"):
            gone = repo / ".git" / "objects" / root[:2] / root[2:]
            gone.chmod(0o644)
            gone.unlink()
        elif case == "git refuses the repository":
            config = repo / ".git" / "config"
            with config.open("a", encoding="utf-8") as handle:
                handle.write("[include]\n\tpath\n")
        elif case == "objects unreadable":
            objects = repo / ".git" / "objects"
            objects.chmod(0)
            request.addfinalizer(lambda: objects.chmod(0o755))
    real_run = subprocess.run
    probes: list[Any] = []

    def run(argv: Any, *args: Any, **kwargs: Any) -> Any:
        if "rev-parse" in argv:
            probes.append(argv)
        if (case, "rev-parse" in argv, "--format=%at" in argv) in (
            ("probe timeout", True, False),
            ("log timeout", False, True),
        ):
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 5.0))
        return real_run(argv, *args, **kwargs)

    monkeypatch.setenv("GIT_DIR", str(elsewhere / ".git"))
    monkeypatch.setattr(subprocess, "run", run)
    _caches.clear_all()
    assert origin_history.located_key(repo) is None, "premise: nothing keys it"
    want, probed = _REFUSED[case]
    got = origin_dates.commit_author_timestamps(repo)
    if want is list:
        assert isinstance(got, list) and len(got) == 2, "the directory's own"
    else:
        assert got is want
    assert len(probes) == probed
    if case in ("error", "git refuses the repository"):
        # Keyed, the log names the head by its hash, which the repository
        # holds: git's refusal of it is an error with no probe.
        monkeypatch.delenv("GIT_DIR")
        _caches.clear_all()
        probes.clear()
        assert origin_history.located_key(repo) is not None, "premise: keyed"
        assert origin_dates.commit_author_timestamps(repo) is want
        assert probes == []


#: Each shape of a head git cannot resolve: what the history reads, and how
#: many plumbing calls past the two probes tell it (none where the files do).
_HEADS = {
    "unborn": (None, 0),
    "garbage ref": (CommitDriftUnread.ERROR, 0),
    "NUL-filled ref": (CommitDriftUnread.ERROR, 0),
    "empty ref": (CommitDriftUnread.ERROR, 0),
    "packed-refs git dies on": (CommitDriftUnread.ERROR, 1),
    "packed-refs git dies on, a directory at the ref": (CommitDriftUnread.ERROR, 1),
    "chained to a missing ref": (None, 0),
    "chained to a garbage ref": (CommitDriftUnread.ERROR, 0),
    "unborn, files unsettled": (None, 2),
    "garbage ref, files unsettled": (CommitDriftUnread.ERROR, 1),
    "unborn, files unsettled, a git with no --exists": (CommitDriftUnread.ERROR, 2),
    "reftable unborn": (None, 2),
}


def _break_head(repo: Path, case: str) -> None:
    """Give `repo`'s branch the head shape `case` names, after two commits
    (none for an unborn branch)."""
    if "unborn" in case:
        return
    _commit(repo, "seed", when=_T0, files={"notes0.md": "a\n"})
    _commit(repo, "post", when=_T1, files={"notes0.md": "a1\n"})
    ref = repo / ".git" / "refs" / "heads" / "main"
    if case.startswith("garbage ref"):
        ref.write_text("not a hash\n", encoding="utf-8")
    elif case == "NUL-filled ref":
        ref.write_bytes(b"\0" * 41)
    elif case == "empty ref":
        ref.write_bytes(b"")
    elif case.startswith("chained to"):
        ref.write_text("ref: refs/heads/x\n", encoding="utf-8")
        if case.endswith("garbage ref"):
            (ref.parent / "x").write_text("not a hash\n", encoding="utf-8")
    elif case.startswith("packed-refs git dies on"):
        _git(repo, "pack-refs", "--all")
        assert not ref.exists(), "premise: the branch is packed alone"
        packed = repo / ".git" / "packed-refs"
        lines = packed.read_text(encoding="utf-8").splitlines()
        packed.write_text("\n".join([*lines[:-1], "zzzz refs/heads/main", ""]))
        if case.endswith("a directory at the ref"):
            # No loose file, as `_lookup` reads a directory there: the
            # files leave it to the plumbing.
            ref.mkdir()


@pytest.mark.parametrize("moved", [False, True], ids=["files", "GIT_DIR"])
@pytest.mark.parametrize("case", list(_HEADS))
def test_a_ref_git_cannot_read_is_told_from_an_unborn_branch(
    case: str, moved: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``git rev-parse --verify -q HEAD`` fails on an unborn branch and on a
    branch whose ref git cannot read alike, and no key is read for either.
    The files tell them apart (`githead.head_unborn`): an unborn branch has
    neither a loose file nor a ``packed-refs`` line and reads None, as no
    history applies; a loose file of garbage, NULs or nothing is a ref that
    exists, and reads `error`, unread, never not applicable (ENG-220's
    review, round F); a chain of symbolic refs is read to its end. Where
    the files do not settle it (a ``packed-refs`` they decline, a reftable
    repository, here also the files read as unsettled) the plumbing does:
    ``git symbolic-ref -q HEAD`` names the branch at the chain's end, which
    ``git show-ref --exists`` finds missing (exit 2: unborn) or not; a
    symbolic-ref that fails, and a git that knows no ``--exists``, read
    `error`. Alike under GIT_DIR, which every git the package runs
    drops."""
    if case == "reftable unborn":
        repo = tmp_path / "repo"
        made = subprocess.run(
            ["git", "init", "-q", "-b", "main", "--ref-format=reftable", str(repo)],
            capture_output=True,
        )
        if made.returncode != 0:
            pytest.skip("this git writes no reftable repository")
    else:
        repo = _repo(tmp_path)
        _break_head(repo, case)
    if "files unsettled" in case:
        monkeypatch.setattr(githead, "head_unborn", lambda gd: None)
    if moved:
        elsewhere = _repo(tmp_path, "elsewhere")
        _commit(elsewhere, "other", when=_T0, files={"other.md": "o\n"})
        monkeypatch.setenv("GIT_DIR", str(elsewhere / ".git"))
    real_run = subprocess.run
    plumbed: list[Any] = []

    def run(argv: Any, *args: Any, **kwargs: Any) -> Any:
        if "symbolic-ref" in argv or "show-ref" in argv:
            plumbed.append(argv)
        if "--exists" in argv and case.endswith("no --exists"):
            # A git before 2.43 knows no such option.
            return subprocess.CompletedProcess(argv, 129, b"", b"error: unknown option")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    _caches.clear_all()
    assert origin_history.located_key(repo) is None, "premise: nothing keys it"
    want, calls = _HEADS[case]
    assert origin_dates.commit_author_timestamps(repo) is want
    assert len(plumbed) == calls


@pytest.mark.usefixtures("keyed_by")
@pytest.mark.parametrize("case", ["garbage ref", "NUL-filled ref"])
def test_a_ref_git_cannot_read_reads_unread_on_every_surface(
    case: str, tmp_path: Path, memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The branch's loose ref holds garbage or NULs. Its history applies and
    was not read: the search hit is flagged and reads spot_check_required,
    memory_show's count and the health rollup read `error`, whether the
    files or git's answer would key the rows (ENG-220's review, round F)."""
    repo, caller, cited, _, memories = _scenario(tmp_path, memory_dir, monkeypatch)
    before, _ = _attach(memories, caller, monkeypatch)
    assert _by_id(before)[cited]["commit_drift_count"] == 1, "premise: it applies"
    ref = repo / ".git" / "refs" / "heads" / "main"
    ref.write_bytes(b"not a hash\n" if case == "garbage ref" else b"\0" * 41)
    why = CommitDriftUnread.ERROR
    _caches.clear_all()
    out, _ = _attach(memories, caller, monkeypatch)
    hit = _by_id(out)[cited]
    assert hit["commit_drift_unread"] is True and "commit_drift_count" not in hit
    assert hit["staleness_verdict"] == "spot_check_required"
    assert hit["verification"]["recommendation"] == unread_recommendation(why)
    _caches.clear_all()
    shown = compute_commit_drift(
        _V1,
        _URL,
        caller_origin=None,
        body=f"widget rule lives in {repo / 'notes0.md'}",
        anchor=ClaimAnchor(root=str(repo), rule="home_checkout"),
    )
    assert shown is why
    _caches.clear_all()
    debt = health._compute_commit_drift_debt(
        by_id={"m": _stats("m")},
        origin_repo_by_id={"m": _URL},
        caller_origin=Origin(cwd=str(repo), repo=_URL, branch=None),
        anchor_paths_by_id={"m": (str(repo / "notes0.md"),)},
        claims_by_id=None,
        verified_head_by_id=None,
    )
    assert debt is not None and debt.rows == []
    unread = {"unread": 1, "unread_causes": {why.value: 1}, "unread_ids": ["m"]}
    assert debt.to_dict().items() >= unread.items()


@pytest.mark.usefixtures("keyed_by")
def test_a_repository_nothing_reads_reads_unread_never_not_applicable(
    tmp_path: Path,
    memory_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """A repository whose objects directory no one may search is read by
    neither git nor the files: git's discovery passes over its `.git` as no
    git directory, and so does `githead`'s. Its `.git` still stands there
    (`githead.entry_above`), so its history applies and was not read: it
    reads `error`, never not applicable (ENG-220's review, round F), on the
    search hit, memory_show's count and the health rollup, whether the
    files or git's answer would key the rows; with no root read, so does
    the hit citing only an outside path. A folder that no `.git` or `HEAD`
    entry stands at or above still reads None."""
    repo, caller, cited, outside, memories = _scenario(
        tmp_path, memory_dir, monkeypatch
    )
    objects = repo / ".git" / "objects"
    objects.chmod(0)
    request.addfinalizer(lambda: objects.chmod(0o755))
    why = CommitDriftUnread.ERROR
    _caches.clear_all()
    assert origin_history.located_key(repo) is None, "premise: nothing reads it"
    assert origin_dates.commit_author_timestamps(repo) is why
    out, _ = _attach(memories, caller, monkeypatch)
    hit = _by_id(out)[cited]
    assert hit["commit_drift_unread"] is True and "commit_drift_count" not in hit
    assert hit["staleness_verdict"] == "spot_check_required"
    assert hit["verification"]["recommendation"] == unread_recommendation(why)
    # With no root that git or the files read, no anchor can be shown to
    # escape the repository (`verify.applicable_unread`): the hit citing
    # only an outside path is flagged too, never read fresh.
    assert _by_id(out)[outside]["commit_drift_unread"] is True
    _caches.clear_all()
    shown = compute_commit_drift(
        _V1,
        _URL,
        caller_origin=None,
        body=f"widget rule lives in {repo / 'notes0.md'}",
        anchor=ClaimAnchor(root=str(repo), rule="home_checkout"),
    )
    assert shown is why
    _caches.clear_all()
    debt = health._compute_commit_drift_debt(
        by_id={"m": _stats("m")},
        origin_repo_by_id={"m": _URL},
        caller_origin=Origin(cwd=str(repo), repo=_URL, branch=None),
        anchor_paths_by_id={"m": (str(repo / "notes0.md"),)},
        claims_by_id=None,
        verified_head_by_id=None,
    )
    assert debt is not None and debt.rows == []
    unread = {"unread": 1, "unread_causes": {why.value: 1}, "unread_ids": ["m"]}
    assert debt.to_dict().items() >= unread.items()
    nowhere = tmp_path / "nowhere"
    nowhere.mkdir()
    assert not githead.entry_above(nowhere), "premise: no entry stands above"
    assert origin_dates.commit_author_timestamps(nowhere) is None


@pytest.mark.skipif(os.name != "posix", reason="a directory's search bit is POSIX's")
def test_an_entry_no_stat_reads_counts_as_one(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    """`githead.entry_above` counts a ``.git`` it cannot stat for a reason
    other than absence as an entry: a repository may stand there."""
    locked = tmp_path / "locked"
    inner = locked / "inner"
    inner.mkdir(parents=True)
    locked.chmod(0o600)
    request.addfinalizer(lambda: locked.chmod(0o755))
    assert githead.entry_above(inner)
    locked.chmod(0o755)
    assert not githead.entry_above(inner)


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


@pytest.mark.usefixtures("keyed_by")
def test_a_cut_the_head_never_reaches_counts_on_every_surface(
    tmp_path: Path, memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The repository fetched an unrelated branch with ``--depth=1``: its
    shallow file lists that branch's tip, which HEAD never reaches. The
    search hit counts 1 and is not flagged, memory_show's count reads 1 and
    the health rollup's row 1, whether the files or git's answer key the
    rows (ENG-220's review, round F)."""
    repo, caller, cited, _, memories = _scenario(tmp_path, memory_dir, monkeypatch)
    other = _repo(tmp_path, "other")
    _commit(other, "o0", when=_T0, files={"b.md": "0\n"})
    _commit(other, "o1", when=_T1, files={"b.md": "1\n"})
    _git(repo, "fetch", "-q", "--depth=1", other.as_uri(), "main:refs/heads/other")
    assert (repo / ".git" / "shallow").exists(), "premise: a cut"
    _caches.clear_all()
    out, _ = _attach(memories, caller, monkeypatch)
    hit = _by_id(out)[cited]
    assert "commit_drift_unread" not in hit and hit["commit_drift_count"] == 1
    _caches.clear_all()
    shown = compute_commit_drift(
        _V1,
        _URL,
        caller_origin=None,
        body=f"widget rule lives in {repo / 'notes0.md'}",
        anchor=ClaimAnchor(root=str(repo), rule="home_checkout"),
    )
    assert not isinstance(shown, CommitDriftUnread) and shown is not None
    assert shown.commits_since_verify == 1
    _caches.clear_all()
    debt = health._compute_commit_drift_debt(
        by_id={"m": _stats("m")},
        origin_repo_by_id={"m": _URL},
        caller_origin=Origin(cwd=str(repo), repo=_URL, branch=None),
        anchor_paths_by_id={"m": (str(repo / "notes0.md"),)},
        claims_by_id=None,
        verified_head_by_id=None,
    )
    assert debt is not None and debt.unread == 0
    assert [row.commits_since_verify for row in debt.rows] == [1]


def test_a_line_the_named_log_does_not_print_declines_the_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Where a cut has the log name each commit, a line of any other shape
    (here a hash with no date) declines the whole read, `error`, as a
    line the dates' own log does not print does: no commit drops out of
    the count."""
    repo = _repo(tmp_path)
    _commit(repo, "seed", when=_T0, files={"notes0.md": "a\n"})
    _commit(repo, "post", when=_T1, files={"notes0.md": "a1\n"})
    (repo / ".git" / "shallow").write_text(f"{'f' * 40}\n", encoding="utf-8")
    real_run = subprocess.run

    def run(argv: Any, *args: Any, **kwargs: Any) -> Any:
        done = real_run(argv, *args, **kwargs)
        if "--format=%H %at" in argv:
            first, _, rest = done.stdout.partition(b"\n")
            done.stdout = first.split(b" ")[0] + b"\n" + rest
        return done

    _caches.clear_all()
    read = origin_dates.commit_author_timestamps(repo)
    assert isinstance(read, list) and len(read) == 2, "premise: a cut not reached"
    monkeypatch.setattr(subprocess, "run", run)
    _caches.clear_all()
    assert origin_dates.commit_author_timestamps(repo) is CommitDriftUnread.ERROR


#: Each fetch into a full clone of five commits: whether the shallow file's
#: cut is one HEAD's history reaches.
_CUTS = {
    "a depth-1 fetch of an unrelated branch": False,
    "a depth-1 fetch of origin's main, not merged": False,
    "a depth-1 fetch of origin's main, merged": True,
    "the unrelated branch merged in": True,
    "a clone two commits deep": True,
    "a shallow file that is not read": True,
}


@pytest.mark.parametrize("moved", [False, True], ids=["keyed", "no key"])
@pytest.mark.parametrize("shape", list(_CUTS))
def test_a_cut_reads_shallow_only_where_the_history_reaches_it(
    shape: str,
    moved: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """A shallow file's cut stops git's log only where HEAD's history reaches
    it. A full clone that fetched another branch, or origin's main, with
    ``--depth=1`` and merged neither holds a cut its main branch never
    reaches: its dates are read whole and count, where the cut alone read
    them `shallow` (ENG-220's review, round F). The log then names each
    commit, as the walk's does, and lists a cut wherever the history stops
    at one: origin's main reset onto, the fetched branch merged in and a
    clone two commits deep read `shallow`, and so does a shallow file that
    is not read. The merge follows a count at the head before it, under the
    same shallow file, so it extends that count, and the range it adds
    lists the cut. Alike where no key is read."""
    source = _repo(tmp_path, "source")
    for day in range(5):
        _commit(
            source,
            f"c{day}",
            when=_T0 + timedelta(days=day),
            files={"a.md": f"{day}\n"},
        )
    other = _repo(tmp_path, "other")
    for day in range(3):
        _commit(
            other, f"o{day}", when=_T1 + timedelta(days=day), files={"b.md": f"{day}\n"}
        )
    clone = tmp_path / "clone"
    depth = ["--depth=2"] if shape == "a clone two commits deep" else []
    _git(tmp_path, "clone", "-q", *depth, source.as_uri(), str(clone))
    if moved:
        monkeypatch.setenv("GIT_DISCOVERY_ACROSS_FILESYSTEM", "1")
    if "unrelated branch" in shape:
        _git(clone, "fetch", "-q", "--depth=1", other.as_uri(), "main:refs/heads/other")
    _caches.clear_all()
    first = origin_dates.commit_author_timestamps(clone)
    if shape != "a clone two commits deep":
        assert isinstance(first, list) and len(first) == 5, "premise: main whole"
    if shape.endswith("merged in"):
        _git(clone, "merge", "-q", "--allow-unrelated-histories", "-m", "m", "other")
    else:
        _caches.clear_all()
    if "origin's main" in shape:
        _commit(source, "c5", when=_T0 + timedelta(days=6), files={"a.md": "5\n"})
        _git(clone, "fetch", "-q", "--depth=1", "origin", "main")
        if shape.endswith(", merged"):
            # The fetched tip is a cut with no parent here: reset onto it,
            # as a pull that fetched it shallow leaves the branch.
            _git(clone, "reset", "-q", "--hard", "origin/main")
    elif shape == "a shallow file that is not read":
        shallow = clone / ".git" / "shallow"
        shallow.write_text(f"{_git(clone, 'rev-parse', 'HEAD~4')}\n", encoding="utf-8")
        shallow.chmod(0)
        request.addfinalizer(lambda: shallow.chmod(0o644))
    assert (clone / ".git" / "shallow").exists(), "premise: a shallow file"
    got = origin_dates.commit_author_timestamps(clone)
    if _CUTS[shape]:
        assert got is CommitDriftUnread.SHALLOW
    else:
        assert isinstance(got, list) and len(got) == 5, "the main branch whole"


def _clone_beside_a_cut(tmp_path: Path, depth: int = 1) -> tuple[Path, Path]:
    """A full clone of five commits on main that fetched a branch of
    another repository (three commits) `depth` commits deep into
    ``other``, and that repository: the clone's shallow file lists a cut
    its main branch never reaches. The clone is read once, whole."""
    source = _repo(tmp_path, "source")
    for day in range(5):
        _commit(
            source,
            f"c{day}",
            when=_T0 + timedelta(days=day),
            files={"a.md": f"{day}\n"},
        )
    other = _repo(tmp_path, "other")
    for day in range(3):
        _commit(
            other, f"o{day}", when=_T1 + timedelta(days=day), files={"b.md": f"{day}\n"}
        )
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", source.as_uri(), str(clone))
    _git(
        clone,
        "fetch",
        "-q",
        f"--depth={depth}",
        other.as_uri(),
        "main:refs/heads/other",
    )
    assert (clone / ".git" / "shallow").exists(), "premise: a cut"
    _caches.clear_all()
    first = origin_dates.commit_author_timestamps(clone)
    assert isinstance(first, list) and len(first) == 5, "premise: main whole"
    return clone, other


@pytest.mark.usefixtures("discovery")
@pytest.mark.parametrize("move", ["a commit", "a merge of a full branch"])
def test_a_head_move_under_a_cut_it_never_reaches_is_extended(
    move: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full clone that fetched an unrelated branch with ``--depth=1``
    holds a cut its main branch never reaches, and a head that moves on
    there, by a commit or a merge of a branch with its whole history,
    reads only the commits it adds, as it does with no shallow file: one
    process, the range's log, and no read of the whole history (ENG-220's
    review, round F). The range names each commit, none a cut, so the
    dates count, the same as a read with nothing kept, and share the
    earlier head's dates and cuts."""
    clone, _ = _clone_beside_a_cut(tmp_path)
    head = _git(clone, "rev-parse", "HEAD")
    first = origin_dates.commit_author_timestamps(clone)
    assert isinstance(first, list)
    if move == "a commit":
        _commit(clone, "c5", when=_T0 + timedelta(days=9), files={"a.md": "5\n"})
    else:
        _git(clone, "checkout", "-q", "-b", "side", "HEAD~2")
        _commit(clone, "s0", when=_T0 + timedelta(days=9), files={"s.md": "s\n"})
        _git(clone, "checkout", "-q", "main")
        _git(clone, "merge", "-q", "--no-ff", "-m", "m", "side")
    moved = _git(clone, "rev-parse", "HEAD")
    calls = _history_calls(monkeypatch)
    got = origin_dates.commit_author_timestamps(clone)
    assert calls == [(*_EXTENSION, f"{head}..{moved}")], "no whole read"
    assert isinstance(got, list) and len(got) == len(first) + (
        1 if move == "a commit" else 2
    )
    assert got[0] is first[0], "the earlier head's dates, shared"
    earlier, later = origin_history._TIMESTAMPS_MEMO.values()
    assert later.cut is earlier.cut, "and its cuts, held once"
    _caches.clear_all()
    assert got == origin_dates.commit_author_timestamps(clone)


@pytest.mark.usefixtures("discovery")
@pytest.mark.parametrize("depth", [1, 2], ids=["the cut merged", "a cut behind it"])
def test_a_merge_that_brings_a_cut_in_reads_shallow_off_the_range(
    depth: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fetched branch merged into a main branch whose history was
    counted, under the same shallow file: the range the merge adds lists
    the cut, the fetched tip itself or, fetched two deep, its parent, so
    the extension reads `shallow` from that one log, counted as a failed
    read, with no read of the whole history; and the next call answers
    the same with no process."""
    clone, _ = _clone_beside_a_cut(tmp_path, depth)
    head = _git(clone, "rev-parse", "HEAD")
    cut = (clone / ".git" / "shallow").read_text(encoding="utf-8").split()
    assert cut == [_git(clone, "rev-parse", f"other~{depth - 1}")], "premise"
    _git(clone, "merge", "-q", "--allow-unrelated-histories", "-m", "m", "other")
    moved = _git(clone, "rev-parse", "HEAD")
    calls = _history_calls(monkeypatch)
    failed = origin.failed_git_calls()
    got = origin_dates.commit_author_timestamps(clone)
    assert got is CommitDriftUnread.SHALLOW
    assert calls == [(*_EXTENSION, f"{head}..{moved}")], "no whole read"
    assert origin.failed_git_calls() == failed + 1
    calls.clear()
    assert origin_dates.commit_author_timestamps(clone) is CommitDriftUnread.SHALLOW
    assert calls == []


#: How the shallow file changes between two heads, and the log the second
#: head's history is then read whole by.
_CHANGED = {
    "a cut added": _NAMED_HISTORY,
    "the file rewritten as it was": _NAMED_HISTORY,
    "a cut added within the stamp's tick": _NAMED_HISTORY,
    "the clone unshallowed": _WHOLE_HISTORY,
}


@pytest.mark.usefixtures("discovery")
@pytest.mark.parametrize("change", list(_CHANGED))
def test_a_shallow_file_changed_between_heads_reads_the_history_whole(
    change: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An earlier head's history was checked against the cuts the shallow
    file listed then; once the file changed, its cuts may stop the history
    anywhere, so the next head is no extension of it and its history is
    read whole, as with nothing kept: another branch fetched with
    ``--depth=1``, the file written again with the same lines, the clone
    unshallowed. A change the file's stamp does not see (here the guard
    is held still) is told by the cuts themselves."""
    clone, other = _clone_beside_a_cut(tmp_path)
    shallow = clone / ".git" / "shallow"
    if change == "a cut added within the stamp's tick":
        monkeypatch.setattr(origin_history, "history_guard", lambda root: ("held",))
        _caches.clear_all()
        assert isinstance(origin_dates.commit_author_timestamps(clone), list)
    if change.startswith("a cut added"):
        third = _repo(tmp_path, "third")
        _commit(third, "t0", when=_T1, files={"t.md": "t\n"})
        _git(clone, "fetch", "-q", "--depth=1", third.as_uri(), "main:refs/heads/t")
        assert len(shallow.read_text(encoding="utf-8").split()) == 2, "premise"
    elif change == "the file rewritten as it was":
        lines = shallow.read_bytes()
        shallow.unlink()
        shallow.write_bytes(lines)
    else:
        _git(
            clone, "fetch", "-q", "--unshallow", other.as_uri(), "main:refs/heads/other"
        )
        assert not shallow.exists(), "premise: no cut"
    moved = _commit(clone, "c5", when=_T0 + timedelta(days=9), files={"a.md": "5\n"})
    calls = _history_calls(monkeypatch)
    got = origin_dates.commit_author_timestamps(clone)
    assert _whole_history_logs(calls) == [(*_CHANGED[change], moved)]
    assert isinstance(got, list) and len(got) == 6
    _caches.clear_all()
    assert got == origin_dates.commit_author_timestamps(clone)


def test_an_entry_checked_against_other_cuts_is_no_base() -> None:
    """The memo gives an entry as the base a head extends only under the
    guard it was read under and the cuts its history was checked against;
    the base carries the entry's own cuts."""
    cut = frozenset({"a" * 40})
    origin_history.remember(("/r", "1" * 40), [_T0], ("g",), cut)
    assert origin_history.base_for("/r", ("g",), frozenset({"b" * 40})) is None
    assert origin_history.base_for("/r", ("g",), frozenset()) is None
    assert origin_history.base_for("/r", ("h",), cut) is None
    assert origin_history.base_for("/q", ("g",), cut) is None
    found = origin_history.base_for("/r", ("g",), frozenset({"a" * 40}))
    assert found is not None and found[:2] == ("1" * 40, [_T0])
    assert found[2] is cut

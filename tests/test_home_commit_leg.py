"""A memory read outside its home repository is counted at its home
(ENG-229).

A memory homed in repository A, with a commit in A since its stamp, read
`fresh` from `memory_show` and `memory_search` wherever the caller's LINKS
admission did not count its home: from repository B, a plain folder, a
bare clone of A, inside A's `.git`, or in A with its `objects` directory
unreadable. No commit leg ran and nothing said so, while health's estate
check counted the same memory at A's live view and listed it drifted.

Now the commit leg of every record the caller's checkout does not count
is read where the estate check reads it (`commit_legs.CommitLegs`): at
`ClaimAnchor.commit_root` of the commit its stamp names. Where nothing
here reads it, the row is not checked (`commit_drift_unchecked: true`)
and never `fresh`; a record with no home row anchors nowhere (decision
B23), so it is not checked on any per-memory surface. Each placement is read
on three legs: a commit to a cited path, a declared symbol claim a commit
made false, and an attested file a commit deleted (the path leg, which
ran before too). Every surface reads one verdict, and health lists the
memory where the per-memory surfaces read it.

Repositories are built with git under ``tmp_path`` with a neutral
configuration, as tests/test_health_anchoring.py builds its own.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from bettermemory import _response, location, origin, origin_dates, verify
from bettermemory._response import ResponseBuilder
from bettermemory.commit_legs import (
    NO_HISTORY,
    NO_HOME,
    UNCHECKED_RECOMMENDATIONS,
    UNCONFIRMED,
    CommitLegUnchecked,
    unchecked_recommendation,
    unchecked_verdict,
)
from bettermemory.config import Config, StorageConfig
from bettermemory.events import Recorder
from bettermemory.machine import MachineIdentity, open_machine
from bettermemory.membership import Membership
from bettermemory.membership_records import GLOBAL, Declaration
from bettermemory.project_file import mint_id
from bettermemory.recall import prompt_recall
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store

from ._mcp import call_tool
from .conftest import set_git_discovery_ceiling
from .test_commit_drift_cache import _git_args, _whole_history_logs

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

_A = "git@github.com:example/home-a.git"
_B = "git@github.com:example/other-b.git"
_QUERY = "zebrafish loader"
#: Health's reason for a record with no home row.
_NO_HOME_SKIP = "no home: no checkout's commits are counted until it is homed"
#: "plant" is a folder whose `.git` file names A's git directory, which git
#: follows and membership refuses (ENG-158): the folder is outside any
#: repository for every reader here.
_PLACES = ["B", "plain", "bare", "dotgit", "objects", "plant"]
_LEGS = ["commit", "claim", "path"]
#: Search with auto-scope admits the memory where the caller resolves to
#: A's project: a bare clone of A and A's own `.git`.
_ADMITTED = {"bare", "dotgit"}
#: A directory no one may search reads no history, never fresh (ENG-220).
_UNREADABLE = sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0)


@pytest.fixture(autouse=True)
def neutral_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No user or system git configuration, a fixed author and committer,
    no inherited repository variables, no workspace declaration."""
    config = tmp_path / "gitconfig"
    config.write_text("[gc]\n\tauto = 0\n[maintenance]\n\tauto = false\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_INDEX_FILE",
        "BETTERMEMORY_WORKSPACE",
    ):
        monkeypatch.delenv(name, raising=False)
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Fixture")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "fixture@example.invalid")
        monkeypatch.setenv(f"GIT_{role}_DATE", "@1000000000 +0000")
    set_git_discovery_ceiling(tmp_path, monkeypatch)


@pytest.fixture
def this_machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MachineIdentity:
    """The process's machine, read from fixed facts under the test's own
    directory, not from the host."""
    identity = open_machine(
        tmp_path / "machine", hardware=lambda: None, hostname=lambda: "fixture-host"
    )
    monkeypatch.setattr(location, "process_machine", lambda: identity)
    return identity


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def _repo(path: Path, remote: str) -> Path:
    """A repository with `remote` and one commit holding ``src/a.py``
    (defining ``load``) and ``src/b.py``."""
    (path / "src").mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "remote", "add", "origin", remote)
    (path / "README.md").write_text(f"{path.name} {os.urandom(6).hex()}\n")
    (path / "src" / "a.py").write_text("def load():\n    return 1\n")
    (path / "src" / "b.py").write_text("def parse():\n    return 2\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "files")
    return path


def _commit(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


#: Per leg: the body, what memory_verify attests, and the commit since.
_BODIES = {
    "commit": "The zebrafish loader lives in `src/a.py`.",
    "claim": "The zebrafish loader is the function load.",
    "path": "The zebrafish loader parses with `src/b.py`.",
}
_VERIFY: dict[str, dict[str, list[str]]] = {
    "commit": {"verified_paths": ["src/a.py"]},
    "claim": {"claims": ["src/a.py::load"]},
    "path": {"verified_paths": ["src/b.py"]},
}


def _since(repo: Path, leg: str) -> None:
    """The commit in A since the stamp: one to the cited file, one that
    makes the claimed symbol false, one that deletes the attested file."""
    if leg == "commit":
        with (repo / "src" / "a.py").open("a") as handle:
            handle.write("changed = True\n")
    elif leg == "claim":
        (repo / "src" / "a.py").write_text("def other():\n    return 1\n")
    else:
        (repo / "src" / "b.py").unlink()
    _commit(repo, f"since {leg}")


@dataclass
class World:
    """A store and its server, repository A with a memory homed there, and
    the places a caller can stand outside it."""

    store: Store
    config: Config
    server: Any
    home: Path
    other: Path
    plain: Path
    bare: Path
    monkeypatch: pytest.MonkeyPatch

    def stand_at(self, cwd: Path) -> None:
        """Every handler's origin capture names `cwd`, captured for real."""
        import bettermemory._handlers as handlers_module

        def fake_capture(at: Path | None = None) -> Any:
            return origin.capture(cwd)

        self.monkeypatch.setattr(handlers_module, "capture_origin", fake_capture)

    def place(self, where: str) -> Path:
        return {
            "home": self.home,
            "B": self.other,
            "plain": self.plain,
            "bare": self.bare,
            "dotgit": self.home / ".git",
            "objects": self.home,
            "plant": self.home.parent / "plant",
        }[where]

    async def homed(self, leg: str) -> str:
        """A memory written and verified at A, so homed there."""
        self.stand_at(self.home)
        written = self.store.write(
            content=_BODIES[leg], scopes=["tools"], origin=origin.capture(self.home)
        )
        await call_tool(
            self.server, "memory_verify", {"id": written.id, **_VERIFY[leg]}
        )
        return written.id


@pytest.fixture
def world(
    memory_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    this_machine: MachineIdentity,
) -> Iterator[World]:
    work = tmp_path / "work"
    home = _repo(work / "alpha", _A)
    other = _repo(work / "beta", _B)
    plain = work / "plain"
    plain.mkdir()
    plant = work / "plant"
    plant.mkdir()
    (plant / ".git").write_text(f"gitdir: {home / '.git'}\n")
    bare = work / "alpha.git"
    _git(work, "clone", "-q", "--bare", str(home), str(bare))
    store = Store(memory_dir)
    config = Config(storage=StorageConfig(directory=str(memory_dir)))
    state = SessionState()
    server = build_server(
        config=config,
        store=store,
        state=state,
        recorder=Recorder(store=store, session_id=state.session_id),
    )
    made = World(store, config, server, home, other, plain, bare, monkeypatch)
    yield made
    if (made.home / ".git" / "objects").exists():
        (made.home / ".git" / "objects").chmod(0o755)
    store.close()


@dataclass
class Surfaces:
    """One memory as every surface reads it for one caller."""

    show: dict[str, Any]
    hit: dict[str, Any] | None
    top: dict[str, Any] | None
    auto: dict[str, Any] | None
    health: dict[str, Any]


async def _surfaces(world: World, rid: str, where: str) -> Surfaces:
    """memory_show, the hit of an unscoped search, the expanded top hit,
    the auto-scope hit (where it admits the memory) and health, read
    from `where`; A's `objects` made unreadable for "objects"."""
    world.stand_at(world.place(where))
    objects = world.home / ".git" / "objects"
    if where == "objects":
        objects.chmod(0)
    try:
        show = await call_tool(world.server, "memory_show", {"id": rid})
        found = await call_tool(
            world.server, "memory_search", {"query": _QUERY, "auto_scope": False}
        )
        top = await call_tool(
            world.server,
            "memory_search",
            {"query": _QUERY, "auto_scope": False, "expand_top": True},
        )
        auto = await call_tool(world.server, "memory_search", {"query": _QUERY})
        health = await call_tool(world.server, "memory_admin", {"action": "health"})
    finally:
        objects.chmod(0o755)
    # A record with no home row is admitted nowhere, so no search reads it
    # (`admission`); memory_show and health still do.
    hits = [h for h in found["result"] if h["id"] == rid]
    tops = [h for h in top["result"][:1] if h["id"] == rid]
    assert len(hits) == len(tops)
    assert all("body" in h for h in tops)
    autos = [h for h in auto["result"] if h["id"] == rid]
    return Surfaces(
        show,
        hits[0] if hits else None,
        tops[0] if tops else None,
        autos[0] if autos else None,
        health,
    )


def _estate(health: dict[str, Any]) -> dict[str, Any]:
    """What health's estate check and caller rollup say of the memories:
    the ids listed drifted, by each, the ids counted unread, the skip
    reasons."""
    owned: set[str] = set()
    foreign: set[str] = set()
    unread: set[str] = set()
    reasons: list[str] = []
    debt = health.get("commit_drift_debt")
    if debt is not None:
        owned |= {row["id"] for row in debt["rows"]}
        unread |= set(debt.get("unread_ids", []))
    estate = health.get("cross_repo_drift")
    if estate is not None:
        for group in estate["groups"]:
            foreign |= {row["id"] for row in group["rows"]}
            unread |= set(group.get("unread_ids", []))
        reasons = [skip["reason"] for skip in estate["skipped"]]
    return {
        "drifted": owned | foreign,
        "twice": owned & foreign,
        "unread": unread,
        "skipped": reasons,
    }


def _agree(rid: str, read: Surfaces) -> None:
    """Every surface reads the memory one way: the verdict, the count, the
    flags; and health lists it where the per-memory surfaces read it."""
    show = read.show
    count = (
        show["commit_drift"]["commits_since_verify"]
        if show["commit_drift"] is not None
        else None
    )
    rows = [row for row in (read.hit, read.top, read.auto) if row is not None]
    for row in rows:
        assert row["staleness_verdict"] == show["staleness_verdict"]
        assert row.get("commit_drift_count") == count
        for flag in ("commit_drift_unread", "commit_drift_unchecked"):
            assert bool(row.get(flag)) == bool(show.get(flag)), flag
        assert (
            row["verification"]["recommendation"]
            == (show["verification"]["recommendation"])
        )
    listed = _estate(read.health)
    assert rid not in listed["twice"]
    assert (rid in listed["drifted"]) == bool(count)
    assert (rid in listed["unread"]) == bool(show.get("commit_drift_unread"))


@pytest.mark.parametrize("leg", _LEGS)
@pytest.mark.parametrize("where", _PLACES)
async def test_every_surface_counts_a_memory_away_from_home_at_its_home(
    world: World, where: str, leg: str
) -> None:
    """From B, a plain folder, a bare clone, `.git` and with A's `objects`
    unreadable, on each leg: the commit since the stamp is counted at A
    (read unread where A's objects cannot be read), never `fresh`, and
    every surface, health included, reads the same."""
    if where == "objects" and _UNREADABLE:
        pytest.skip("permission bits do not keep this user out")
    rid = await world.homed(leg)
    _since(world.home, leg)
    if where == "B":
        # A commit in B to its own file of the cited name, authored after
        # the stamp: B's history is not the memory's, and counts nowhere.
        world.monkeypatch.setenv("GIT_AUTHOR_DATE", "@4000000000 +0000")
        _since(world.other, leg)
        world.monkeypatch.setenv("GIT_AUTHOR_DATE", "@1000000000 +0000")

    read = await _surfaces(world, rid, where)
    _agree(rid, read)
    show = read.show
    assert read.hit is not None and read.top is not None
    assert (read.auto is not None) == (where in _ADMITTED)
    if leg == "path":
        # The path leg ran at A's live view before too.
        assert show["path_drift"] is not None
        assert [Path(p).name for p in show["path_drift"]["missing"]] == ["b.py"]
    if where == "objects":
        assert show["commit_drift"] is None
        assert show["commit_drift_unread"] is True
        assert show["staleness_verdict"] == "spot_check_required"
        return
    assert show["staleness_verdict"] == "spot_check_recommended"
    assert show["commit_drift"]["status"] == "drift"
    assert show["commit_drift"]["commits_since_verify"] == 1
    assert "commit_drift_unread" not in show
    assert "commit_drift_unchecked" not in show
    if leg == "claim":
        assert read.hit["claim_drift"] == {
            "checked": 1,
            "drifted": ["src/a.py::load"],
        }


@pytest.mark.parametrize("where", [p for p in _PLACES if p != "objects"])
async def test_nothing_since_the_stamp_reads_fresh_and_clean(
    world: World, where: str
) -> None:
    """With no commit since the stamp the memory reads fresh from every
    place, its leg read clean at A: fresh means checked and found fresh."""
    rid = await world.homed("commit")
    read = await _surfaces(world, rid, where)
    _agree(rid, read)
    assert read.show["staleness_verdict"] == "fresh"
    assert read.show["commit_drift"]["status"] == "clean"


async def test_no_live_checkout_holding_the_commit_reads_not_checked(
    world: World,
) -> None:
    """A gone from this machine (moved, with no session at its new path):
    no live checkout here holds the commit the stamp names, so from B the
    leg is not checked, the memory reads `spot_check_recommended` with the
    cause named, and health skips it as unconfirmed."""
    rid = await world.homed("commit")
    os.rename(world.home, world.home.parent / "moved")
    world.home = world.home.parent / "moved"

    read = await _surfaces(world, rid, "B")
    _agree(rid, read)
    show = read.show
    assert show["commit_drift"] is None
    assert show["commit_drift_unchecked"] is True
    assert show["staleness_verdict"] == "spot_check_recommended"
    assert show["verification"]["recommendation"] == unchecked_recommendation(
        CommitLegUnchecked(UNCONFIRMED)
    )
    assert any(
        reason.startswith(
            "unconfirmed: its home checkout has no live view here and the "
            "caller stands in no checkout holding its cited commit"
        )
        for reason in _estate(read.health)["skipped"]
    )


@pytest.mark.parametrize("where", ["B", "home"])
async def test_a_home_with_no_history_reads_not_checked(
    world: World, where: str
) -> None:
    """A's live view on an unborn branch: no history applies at the home,
    so the leg is not checked, never fresh, and health skips the home as
    git unreachable there; from A itself too, where the caller's checkout
    counts the memory and reads no history either."""
    rid = await world.homed("commit")
    _git(world.home, "checkout", "-q", "--orphan", "fresh")

    read = await _surfaces(world, rid, where)
    _agree(rid, read)
    assert read.show["commit_drift_unchecked"] is True
    assert read.show["staleness_verdict"] == "spot_check_recommended"
    assert read.show["verification"]["recommendation"] == (
        unchecked_recommendation(CommitLegUnchecked(NO_HISTORY))
    )
    assert "git unreachable in the home checkout" in _estate(read.health)["skipped"]


async def test_a_record_with_no_home_row_is_not_checked(
    world: World,
) -> None:
    """A record with no home row anchors nowhere until the migration homes
    it (decision B23: the verification path compares no remotes), so
    `memory_show` reads it not checked, in its own checkout and from B
    alike, and never `fresh`, and health lists it skipped, no home, with
    a commit since its stamp and with none: the estate check read it at
    its recorded worktree, and listed it drifted, or clean, where show
    read it not checked (ENG-229's review). Its directory moved to another
    repository's remote changes none of it."""
    world.stand_at(world.home)
    written = world.store.write(
        content=_BODIES["commit"],
        scopes=["tools"],
        origin=origin.capture(world.home),
    )
    world.store.mark_verified(
        written.id,
        verified_paths=["src/a.py"],
        verified_head=_git(world.home, "rev-parse", "HEAD"),
    )
    rid = written.id
    assert Membership(world.store, location.process_machine().id).home(rid) is None

    for since in (False, True):
        if since:
            _since(world.home, "commit")
        for where in ("home", "B"):
            read = await _surfaces(world, rid, where)
            assert read.hit is None and read.auto is None
            assert read.show["commit_drift"] is None
            assert read.show["commit_drift_unchecked"] is True
            assert read.show["staleness_verdict"] == "spot_check_recommended"
            assert read.show["verification"]["recommendation"] == (
                unchecked_recommendation(CommitLegUnchecked(NO_HOME))
            )
            listed = _estate(read.health)
            assert rid not in listed["drifted"], (since, where)
            assert listed["skipped"] == [_NO_HOME_SKIP], (since, where)

    _git(world.home, "remote", "set-url", "origin", "git@github.com:example/c.git")
    read = await _surfaces(world, rid, "B")
    assert read.show["commit_drift_unchecked"] is True
    assert read.show["staleness_verdict"] == "spot_check_recommended"
    listed = _estate(read.health)
    assert rid not in listed["drifted"]
    assert listed["skipped"] == [_NO_HOME_SKIP]


async def test_a_record_naming_no_repository_has_no_commit_leg(world: World) -> None:
    """A record written in a plain folder and stamped there names no
    repository: no repository's commits speak for it, so it has no commit
    leg on any surface, reads fresh, and health does not list it."""
    world.stand_at(world.plain)
    (world.plain / "notes.md").write_text("zebrafish\n")
    written = world.store.write(
        content="The zebrafish loader notes live in `notes.md`.",
        scopes=["tools"],
        origin=origin.capture(world.plain),
    )
    await call_tool(
        world.server,
        "memory_verify",
        {"id": written.id, "verified_paths": [str(world.plain / "notes.md")]},
    )
    read = await _surfaces(world, written.id, "B")
    _agree(written.id, read)
    assert read.show["commit_drift"] is None
    assert "commit_drift_unchecked" not in read.show
    assert read.show["staleness_verdict"] == "fresh"
    assert _estate(read.health)["skipped"] == []


@pytest.mark.parametrize("where", ["home", "B", "plain"])
async def test_a_global_record_has_no_commit_leg(world: World, where: str) -> None:
    """A record homed in no project (moved to ``global``) has no repository
    whose commits speak for it, as the caller's admission already rules
    (`Admission.drift_counts`): no surface reads a commit leg for it,
    with a commit since in the checkout it was written in, before it is
    verified again and after, and health neither lists it drifted nor
    skips it. Verified again it stamps no commit, so a leg read not checked
    there could never clear (ENG-229's review)."""
    rid = await world.homed("commit")
    _since(world.home, "commit")
    Membership(world.store, location.process_machine().id).declare(
        Declaration("move", "os:test", record=rid, target=GLOBAL)
    )
    for verified_again in (False, True):
        if verified_again:
            world.stand_at(world.home)
            await call_tool(
                world.server,
                "memory_verify",
                {"id": rid, "verified_paths": ["src/a.py"]},
            )
        read = await _surfaces(world, rid, where)
        _agree(rid, read)
        assert read.show["commit_drift"] is None, verified_again
        assert "commit_drift_unchecked" not in read.show
        listed = _estate(read.health)
        assert rid not in listed["drifted"]
        assert listed["skipped"] == []


@pytest.mark.parametrize("where", ["B", "plain", "home"])
async def test_a_leg_that_resolves_to_none_reads_no_whole_history(
    world: World, where: str
) -> None:
    """A memory homed in A whose one anchor lies outside A, stamped at A's
    head: its leg resolves to None at A (no count applies there), on the
    walk from the stamp's head, which reads no dates. A history applies at
    A wherever A's head resolved, so neither A's pass nor the caller's
    reads the whole history to say so: a cold show ran a whole-history log
    for it, 2 s on a home of 400,000 commits (ENG-229's review). Read from
    B, a plain folder and A itself, cold and warm: no whole-history log,
    no count, not flagged, `fresh`."""
    outside = world.plain / "settings.json"
    outside.write_text("{}\n")
    world.stand_at(world.home)
    written = world.store.write(
        content=f"The zebrafish loader reads `{outside}`.",
        scopes=["tools"],
        origin=origin.capture(world.home),
    )
    await call_tool(
        world.server,
        "memory_verify",
        {"id": written.id, "verified_paths": [str(outside)]},
    )
    assert world.store.load_one(written.id).verified_head, "premise: on the walk"
    world.stand_at(world.place(where))
    calls: list[tuple[str, ...]] = []
    real_run = subprocess.run

    def spy(argv: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(_git_args(argv))
        return real_run(argv, *args, **kwargs)

    world.monkeypatch.setattr(subprocess, "run", spy)
    for _ in ("cold", "warm"):
        shown = await call_tool(world.server, "memory_show", {"id": written.id})
        assert shown["commit_drift"] is None
        assert "commit_drift_unchecked" not in shown
        assert shown["staleness_verdict"] == "fresh"
    assert _whole_history_logs(calls) == []


def test_not_checked_is_never_fresh_and_never_lowers_a_verdict() -> None:
    assert unchecked_verdict("fresh") == "spot_check_recommended"
    assert unchecked_verdict("spot_check_recommended") == "spot_check_recommended"
    assert unchecked_verdict("spot_check_required") == "spot_check_required"
    assert len(UNCHECKED_RECOMMENDATIONS) == 4


async def test_a_record_the_callers_checkout_counts_is_the_caller_rollups(
    world: World,
) -> None:
    """From A, after its remote moved to another URL: the caller's
    admission counts the memory's home there, though its recorded
    repository is no longer the caller's, so memory_show counts it in the
    caller's checkout, and so does health's caller rollup, not the estate
    check."""
    rid = await world.homed("commit")
    _since(world.home, "commit")
    _git(world.home, "remote", "set-url", "origin", "git@github.com:example/moved.git")

    read = await _surfaces(world, rid, "home")
    _agree(rid, read)
    assert read.show["commit_drift"]["commits_since_verify"] == 1
    debt = read.health["commit_drift_debt"]
    assert debt is not None and [row["id"] for row in debt["rows"]] == [rid]
    estate = read.health.get("cross_repo_drift")
    assert estate is None or not any(group["rows"] for group in estate["groups"])


async def test_a_home_with_no_remote_is_counted_there(world: World) -> None:
    """A memory homed in a checkout with no remote names no URL to match,
    and is counted at its home's live view all the same, as the estate
    check counts it."""
    bare_home = world.home.parent / "noremote"
    _repo(bare_home, _A)
    _git(bare_home, "remote", "remove", "origin")
    world.home = bare_home
    rid = await world.homed("commit")
    # A remote is no prerequisite for recording a commit cut. The home's
    # history is counted from the stamped head even without a URL.
    for role in ("AUTHOR", "COMMITTER"):
        world.monkeypatch.setenv(f"GIT_{role}_DATE", "@4000000000 +0000")
    _since(bare_home, "commit")

    read = await _surfaces(world, rid, "B")
    assert read.show["commit_drift"]["basis"] == "reachability"
    _agree(rid, read)
    assert read.show["commit_drift"]["commits_since_verify"] == 1
    assert rid in _estate(read.health)["drifted"]


async def test_a_folder_projects_record_has_no_commit_leg(world: World) -> None:
    """A record homed in a folder project (a project file, no git) names no
    repository: no surface reads a commit leg for it, health included."""
    folder = world.home.parent / "notes"
    folder.mkdir()
    (folder / "bettermemory.toml").write_text(
        f'[project]\nid = "{mint_id()}"\nname = "notes"\n'
    )
    (folder / "plan.md").write_text("zebrafish\n")
    world.stand_at(folder)
    # Homed at the folder's resolution, as a write there homes it.
    written = world.store.write(
        content="The zebrafish loader plan lives in `plan.md`.",
        scopes=["tools"],
        origin=origin.capture(folder),
        home=location.resolve_caller(world.store, world.config, str(folder)).home(),
    )
    await call_tool(
        world.server, "memory_verify", {"id": written.id, "verified_paths": ["plan.md"]}
    )
    assert Membership(world.store, location.process_machine().id).home(written.id)

    read = await _surfaces(world, written.id, "B")
    _agree(written.id, read)
    assert read.show["commit_drift"] is None
    assert "commit_drift_unchecked" not in read.show
    assert read.show["staleness_verdict"] == "fresh"
    assert _estate(read.health)["skipped"] == []


def _shown(**parts: Any) -> Any:
    calendar = verify.compute_verification_status(
        datetime.now(timezone.utc), now=datetime.now(timezone.utc), stale_after_days=30
    )
    base: dict[str, Any] = {
        "commit_drift_unread": None,
        "commit_drift_unchecked": None,
        "verification": calendar,
    }
    return SimpleNamespace(**{**base, **parts})


def test_the_top_hit_keeps_the_expansions_check() -> None:
    """The expanded top hit says what its own read found: not checked where
    the expansion did not check it, the flag and its recommendation gone
    where it did."""
    from bettermemory.handlers.search import _expand_unread

    deps = cast(Any, SimpleNamespace(responses=ResponseBuilder(stale_after_days=30)))
    unchecked = CommitLegUnchecked(UNCONFIRMED, "no_live_checkout")
    row: dict[str, Any] = {
        "verification": {"status": "fresh", "recommendation": None},
        "staleness_verdict": "fresh",
        "commit_drift_count": 0,
        "commit_drift_basis": "reachability",
    }
    _expand_unread(deps, row, _shown(commit_drift_unchecked=unchecked))
    assert row == {
        "verification": {
            "status": "fresh",
            "recommendation": unchecked_recommendation(unchecked),
        },
        "staleness_verdict": "spot_check_recommended",
        "commit_drift_unchecked": True,
    }
    _expand_unread(deps, row, _shown())
    assert "commit_drift_unchecked" not in row
    assert row["verification"]["recommendation"] is None


def test_a_remote_stamp_keeps_its_own_recommendation() -> None:
    row: dict[str, Any] = {
        "verification": {"status": "remote", "recommendation": "remote remedy"},
        "staleness_verdict": "spot_check_required",
    }
    ResponseBuilder.apply_commit_drift_unchecked(row, CommitLegUnchecked(NO_HISTORY))
    assert row["commit_drift_unchecked"] is True
    assert row["staleness_verdict"] == "spot_check_required"
    assert row["verification"]["recommendation"] == "remote remedy"


async def test_the_hook_delivers_a_pointer_and_reads_no_commit_leg(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The prompt recall from a bare clone of A, which resolves to A's
    project, delivers the memory as a pointer with no verdict and reads no
    commit history on the hook's deadline: `memory_show` reads the leg."""
    rid = await world.homed("commit")
    _since(world.home, "commit")
    # Past the recall's creation shield, as tests/test_recall_prompt.py
    # backdates its memories.
    stamp = datetime.now(timezone.utc) - timedelta(hours=1)
    world.store.put_memory(
        world.store.load_one(rid).model_copy(
            update={"created": stamp, "updated": stamp}
        )
    )

    def refused(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the hook read a commit leg")

    # Where the reads are made: every history read goes through the
    # dates' reader, and the drift pass reads it under its own name.
    monkeypatch.setattr(origin_dates, "_read_author_timestamps", refused)
    monkeypatch.setattr(_response, "commit_author_timestamps", refused)
    monkeypatch.setattr(_response._DriftPass, "lookup", refused)
    result = prompt_recall(
        prompt="where does the zebrafish loader live",
        session_id="transcript-home-leg",
        origin=origin.capture(world.bare),
        config=world.config,
        store=world.store,
    )
    assert rid in result.stdout
    for word in ("fresh", "spot_check", "commit_drift", "not checked"):
        assert word not in result.stdout

"""Membership resolution through the real capture (RS-F6, design 3.1, 3.2,
3.6, 7 and 8.7).

Each test is a row of design section 7, run through `location.capture`
and `location.resolve_caller` on git fixtures under the test's own
directory, a scratch store and a machine of the test's own: moves on one
filesystem and across (a copy and a delete, the same new objects a move to
another volume makes), copies, remotes, commit-less repositories, reused
paths, worktrees, unreadable old paths, GIT_DIR and separate git
directories, bare and mirror clones, shallow clones, history rewrites,
folder projects, nested files, submodules, sparse checkouts, a clock a
day off, concurrent resolutions, synced folders, a migrated laptop, a
restored store, and containers posting a location the daemon cannot read.
Git runs with no user or system configuration and a fixed author.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from bettermemory import checkout_token, location, membership, project_file
from bettermemory.config import Config, MembershipConfig
from bettermemory.location import (
    NO_MACHINE,
    Caller,
    Exchange,
    answer,
    capture,
    identity_to_json,
    resolve_caller,
    resolve_here,
    resolve_hook,
)
from bettermemory.machine import MachineIdentity, open_machine
from bettermemory.membership import (
    ASK,
    DEFERRED,
    GLOBAL,
    GLOBAL_OUTCOME,
    INDETERMINATE,
    NEW,
    SEARCH_AUTO_SCOPE,
    SESSION_BRIEF,
    SILENT,
    Declaration,
    EpisodeRef,
    Handoff,
    Membership,
    Resolution,
    admitted,
)
from bettermemory.models import utcnow
from bettermemory.store import Store

from .conftest import settled

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

posix_only = pytest.mark.skipif(
    sys.platform == "win32" or getattr(os, "geteuid", lambda: -1)() == 0,
    reason="POSIX permission bits, which bind only below root",
)

_PRINCIPAL = "os:fixture"


# ---------------------------------------------------------------------------
# A neutral git and its fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def neutral_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No user or system configuration reaches git (signing, hooks, the
    default branch), a fixed author and committer, and no variable that
    points git or bettermemory somewhere else."""
    empty = tmp_path / "gitconfig-empty"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    monkeypatch.setenv("FILTER_BRANCH_SQUELCH_WARNING", "1")
    for who in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{who}_NAME", "Fixture")
        monkeypatch.setenv(f"GIT_{who}_EMAIL", "fixture@example.invalid")
    for var in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CEILING_DIRECTORIES",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_CONFIG",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_COUNT",
        "GIT_AUTHOR_DATE",
        "GIT_COMMITTER_DATE",
        "BETTERMEMORY_WORKSPACE",
    ):
        monkeypatch.delenv(var, raising=False)


def sh(*args: str | Path, cwd: str | Path | None = None) -> str:
    done = subprocess.run(
        [str(a) for a in args], cwd=cwd, capture_output=True, text=True, check=False
    )
    if done.returncode != 0:
        raise AssertionError(f"{args} failed: {done.stderr.strip()}")
    return done.stdout.strip()


def git(cwd: str | Path, *args: str | Path) -> str:
    return sh("git", *args, cwd=cwd)


_SEQ = [0]


def make_repo(path: Path, *, commits: int = 1, remote: str | None = None) -> Path:
    """A repository with a history of its own: each one gets a fresh
    marker in its commits' contents and messages, so two made apart share
    no commit and no commit fingerprint (author, dates and message, Hf)
    even when made in the same second."""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    (path / "src").mkdir(exist_ok=True)
    _SEQ[0] += 1
    marker = f"{os.getpid()}-{_SEQ[0]}-{time.time_ns()}"
    for i in range(commits):
        (path / "src" / f"mod{i}.py").write_text(f"VALUE = {i}\n")
        (path / "README.md").write_text(f"fixture {path.name} {marker} rev {i}\n")
        git(path, "add", "-A")
        git(path, "commit", "-q", "-m", f"rev {i} {marker}")
    if remote:
        git(path, "remote", "add", "origin", remote)
    return path


def commit(path: Path, message: str = "more") -> str:
    _SEQ[0] += 1
    (path / "src").mkdir(exist_ok=True)
    (path / "src" / f"x{_SEQ[0]}_{time.time_ns()}.py").write_text("X = 1\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", f"{message} {_SEQ[0]}")
    return git(path, "rev-parse", "HEAD")


def unborn(path: Path, *, remote: str | None = None, note: str | None = None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    if note is not None:
        (path / "notes.md").write_text(note)
    if remote:
        git(path, "remote", "add", "origin", remote)
    return path


def clone(src: Path, dst: Path, *args: str, url: str | None = None) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    sh("git", "clone", "-q", *args, src.resolve().as_uri(), dst)
    if url is not None:
        git(dst, "remote", "set-url", "origin", url)
    return dst


def copy(src: Path, dst: Path) -> Path:
    """`cp -R`: every file and directory a new filesystem object."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst, symlinks=True)
    return dst


def move_across(src: Path, dst: Path) -> Path:
    """A move to another filesystem: the copy and the delete `mv` makes
    there, so nothing keeps its object."""
    copy(src, dst)
    remove(src)
    return dst


def move(src: Path, dst: Path) -> Path:
    """A move on one filesystem: a rename, every object kept."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.rename(src, dst)
    return dst


def remove(path: Path) -> None:
    for p in [path, *path.rglob("*")]:
        if not p.is_symlink():
            try:
                p.chmod(0o755 if p.is_dir() else 0o644)
            except OSError:
                pass
    shutil.rmtree(path)


# ---------------------------------------------------------------------------
# The lab: writes and reads through the real capture and the store
# ---------------------------------------------------------------------------


@dataclass
class _Rec:
    id: str
    kind: str
    session: str
    episode: EpisodeRef | None = None


@dataclass
class Lab:
    """One scenario: a scratch store, this test's machine, and the records
    written through `resolve_caller` as `memory_write` writes them (the
    resolution's home and HEAD as a witness in the record's own
    transaction). `root`, which holds the store and the machine's state
    directory, is on local disk (`local_tmp_path`); the repositories a
    test builds are under its `tmp_path`, the filesystem under test."""

    root: Path
    write_token: bool = True
    strict: bool = False
    state: str = "state"
    store: Store = field(init=False)
    config: Config = field(init=False)
    identity: MachineIdentity = field(init=False)
    records: dict[str, _Rec] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.store = Store(self.root / "store")
        self.config = Config(
            membership=MembershipConfig(
                write_token=self.write_token, strict=self.strict
            )
        )
        self.identity = open_machine(self.root / self.state)

    def close(self) -> None:
        self.store.close()

    def policy(
        self, *, write_token: bool | None = None, strict: bool | None = None
    ) -> None:
        if write_token is not None:
            self.config.membership.write_token = write_token
        if strict is not None:
            self.config.membership.strict = strict
        location.forget()

    def caller(
        self,
        cwd: str | Path | None,
        *,
        identity: MachineIdentity | None = None,
        session: str | None = None,
    ) -> Caller:
        return resolve_caller(
            self.store,
            self.config,
            None if cwd is None else str(cwd),
            session=session,
            identity=identity or self.identity,
        )

    def write(
        self,
        label: str,
        cwd: str | Path | None,
        *,
        kind: str = "memory",
        session: str = "s",
        identity: MachineIdentity | None = None,
    ) -> Resolution:
        who = self.caller(cwd, identity=identity, session=session)
        with self.store.batch():
            if kind == "memory":
                m = self.store.write(
                    content=f"memory {label}",
                    scopes=["tools"],
                    session=session,
                    home=who.home(),
                )
                rec = _Rec(m.id, kind, session)
            else:
                e = self.store.write_episode(
                    session_id=session, body=f"episode {label}", home=who.home()
                )
                rec = _Rec(e.id, kind, session, EpisodeRef(e.id, session, e.created))
            who.add_head_witness("write")
        self.records[label] = rec
        return who.resolution

    def model(self, identity: MachineIdentity | None = None) -> Membership:
        return Membership(self.store, (identity or self.identity).id)

    def admitted(self, res: Resolution, surface: str = SESSION_BRIEF) -> list[str]:
        mine = {r.id: label for label, r in self.records.items() if r.kind == "memory"}
        homes = self.model().homes(list(mine))
        return sorted(
            mine[rid] for rid, home in homes.items() if admitted(surface, home, res)
        )

    def view(
        self,
        cwd: str | Path,
        *,
        identity: MachineIdentity | None = None,
        session: str | None = None,
    ) -> tuple[Resolution, list[str]]:
        """A session at `cwd` (`resolve_here`) and the memories its brief
        admits."""
        res = resolve_here(
            self.store,
            self.config,
            str(cwd),
            session=session,
            identity=identity or self.identity,
        )
        return res, self.admitted(res)

    def handoff(
        self, res: Resolution, identity: MachineIdentity | None = None
    ) -> Handoff:
        episodes = [r.episode for r in self.records.values() if r.episode is not None]
        return self.model(identity).handoff(res, episodes)

    def project_of(self, label: str) -> str | None:
        home = self.model().home(self.records[label].id)
        return None if home is None else home.project

    def checkout_of(self, label: str) -> str | None:
        home = self.model().home(self.records[label].id)
        return None if home is None else home.checkout

    def rows(self, *kinds: str) -> list[str]:
        return [r.kind for r in self.store.membership_log(kinds or None)]

    def _declaring(self, identity: MachineIdentity | None = None) -> Membership:
        """The model `bettermemory project` declares through, with the
        token writer of `cli/project_cmd.open_model`."""
        policy = self.config.membership.write_token
        me = identity or self.identity

        def write_token(k: membership.Checkout) -> tuple[str | None, str]:
            if k.git_dir is None or k.machine != me.id:
                return None, "skipped:other_machine"
            wrote = checkout_token.remint(
                k.git_dir, policy=policy, bare=k.kind == membership.BARE
            )
            return wrote.token, wrote.status

        return Membership.open(self.store, me, token_writer=write_token)

    def answer(self, cwd: str | Path, choice: str, target: str | None = None) -> str:
        """`bettermemory project join <id>` or `bettermemory project new`
        in the checkout at `cwd`, for its open question."""
        res = self.caller(cwd).resolution
        model = self._declaring()
        asked = [p for p in model.pending() if p.checkout == res.checkout]
        assert asked, f"no open question at {cwd}: {res.rule}"
        pr = asked[0]
        if choice == "join":
            decl = Declaration(
                "join",
                _PRINCIPAL,
                checkout=res.checkout,
                proposal=pr.id,
                target=target or pr.candidates[0],
            )
        else:
            decl = Declaration(
                "new",
                _PRINCIPAL,
                checkout=res.checkout,
                proposal=pr.id,
                name=Path(cwd).name,
            )
        done = model.declare(decl)
        location.forget()
        return str(done.project)

    def init_file(
        self, directory: Path, name: str, *, new: bool = False, boundary: bool = False
    ) -> str:
        """`bettermemory project init [--new]` in `directory`
        (`cli/project_cmd._init`): the project file written (replacing one
        there, `--rebind`, unless `new`) and the declaration made with the
        folder, the model deciding from it (decision B17): at the
        checkout's root the file is bound to the checkout's project, or
        with --new to a project of its own; below it the folder becomes a
        part of the project enclosing it (the caller's project there); in
        a plain folder its first sight makes a folder project."""
        res = self.caller(directory).resolution
        path = directory / project_file.PROJECT_FILE_NAME
        existing = project_file.parse(path) if path.exists() else None
        written = project_file.write(
            directory, name=name, boundary=boundary, rebind=existing is not None
        )
        location.forget()
        model = self._declaring()
        checkout = res.checkout
        below = False
        if checkout is None:
            again = self.caller(directory).resolution
            if again.checkout is None:
                return written.id
            checkout, action = again.checkout, "init"
        else:
            below = bool(model.below_root(model.checkout(checkout), str(directory)))
            action = (
                "init_new" if new else "init_rebind" if existing is not None else "init"
            )
        asked = [p for p in model.pending() if p.checkout == checkout] if new else []
        if below:
            asked = [
                p
                for p in asked
                if p.kind == membership.SUBPROJECT_CONTRADICTION
                and Path(str(p.evidence.get("dir"))) == directory
            ]
        else:
            asked = [p for p in asked if p.kind != membership.SUBPROJECT_CONTRADICTION]
        model.declare(
            Declaration(
                action,
                _PRINCIPAL,
                checkout=checkout,
                proposal=asked[0].id if asked else None,
                target=res.project if below else None,
                file_id=written.id,
                name=name,
                directory=str(directory),
                boundary=boundary,
                replaced=(
                    existing.id
                    if isinstance(existing, project_file.ProjectFile)
                    else None
                ),
            )
        )
        location.forget()
        return written.id


@pytest.fixture
def lab_of(local_tmp_path: Path) -> Iterator[Callable[..., Lab]]:
    made: list[Lab] = []

    def make(name: str = "lab", **kw: Any) -> Lab:
        lab = Lab(local_tmp_path / name, **kw)
        made.append(lab)
        return lab

    yield make
    for lab in made:
        lab.close()


@pytest.fixture
def lab(lab_of: Callable[..., Lab]) -> Lab:
    return lab_of()


def _codes(res: Resolution) -> set[str]:
    return set(res.rule.split(":", 1)[-1].split("+"))


@pytest.fixture
def no_birth(monkeypatch: pytest.MonkeyPatch) -> None:
    """A platform or filesystem that reports no birth time, in the capture
    and in the model's reading of a view, which reads objects the same way
    (`location.path_liveness`; design 8.7, E15)."""
    monkeypatch.setattr(location, "REPORT_BIRTH", False)
    location.forget()


@contextlib.contextmanager
def posing(path: Path, key: location.Key) -> Iterator[None]:
    """The capture reads `key` as `path`'s object: an inode number handed
    to a new repository, or a remount that renumbers the object (E11 c,
    E15)."""
    real = location.object_key
    target = os.path.realpath(path)

    def posed(p: str, st: os.stat_result | None = None) -> location.Key | None:
        if os.path.realpath(p) == target:
            return key
        return real(p, st)

    location.object_key = posed  # type: ignore[assignment]
    location.forget()
    try:
        yield
    finally:
        location.object_key = real
        location.forget()


def _git_dir_key(path: Path) -> location.Key:
    key = location.object_key(str(path / ".git"))
    assert key is not None
    return key


def birth_reported(where: Path) -> bool:
    """Whether the capture reads a birth time on the filesystem under
    `where`, read as the product reads it (`location.object_key`): not on
    NFSv3, nor under the `no_birth` fixture or
    ``BETTERMEMORY_TEST_NO_BIRTH=1`` (design 8.7, E15)."""
    key = location.object_key(str(where))
    assert key is not None
    return key[2] is not None


def known_rule(where: Path, rung: str = "object", without_birth: str = "token") -> str:
    """The rule a sighting decided at `rung` answers by on the filesystem
    under `where`: the rung alone where birth time decides the object, and
    without birth time the rung with the witness that proved it, as
    `Membership._resolve_repository` spells it. A known checkout
    (``object``) is confirmed by its token (``token``), or without one by
    its last HEAD (``last_head``), decision B18; a new linked worktree of a
    known repository (``common_object``) is proven by git's own pointers
    (``pointer``)."""
    return rung if birth_reported(where) else f"{rung}+{without_birth}"


# ---------------------------------------------------------------------------
# 7.1 Individual: moves, copies, remotes, commit-less, reused paths (E01 to E08)
# ---------------------------------------------------------------------------


def test_i01_move_on_one_filesystem_is_silent(tmp_path: Path, lab: Lab) -> None:
    """I1, I15 (E01): a remote-less checkout renamed keeps its git
    directory's object, so the same checkout answers silently with every
    memory and its run-state; a plain folder re-created at the old path
    with a `.git` folder that is no repository is outside any project and
    changes nothing for the moved checkout."""
    a = make_repo(tmp_path / "Desktop" / "proj", commits=2)
    for label in ("m1", "m2", "m3"):
        lab.write(label, a)
    lab.write("e1", a, kind="episode", session="s-before")
    res, brief = lab.view(a)
    assert brief == ["m1", "m2", "m3"]

    b = move(a, tmp_path / "Developer" / "proj")
    res, brief = lab.view(b)
    assert (res.outcome, res.rule, brief) == (
        SILENT,
        known_rule(tmp_path),
        ["m1", "m2", "m3"],
    )
    assert "checkout_moved" in lab.rows("checkout_moved")
    hand = lab.handoff(res)
    assert (hand.session_id, hand.rule) == ("s-before", "own_checkout")

    (a / "src").mkdir(parents=True)
    (a / "src" / "mod0 2.py").write_text("VALUE = 0\n")
    (a / ".git").mkdir()
    (a / ".git" / "index 3").write_text("")
    res, brief = lab.view(b)
    assert brief == ["m1", "m2", "m3"]
    shell, shell_brief = lab.view(a)
    assert (shell.outcome, shell.rule, shell_brief) == (
        GLOBAL_OUTCOME,
        "outside_repository",
        [],
    )


def test_i02_move_across_filesystems_joins_by_token_and_history(
    tmp_path: Path, lab: Lab
) -> None:
    """I2 (E02 a, E11): a born remote-less checkout moved to another
    filesystem has a new object; its token and its history keep it in the
    project, silently, and the gone checkout's last session is offered,
    labelled."""
    a = make_repo(tmp_path / "vol" / "born", commits=3)
    lab.write("a1", a)
    lab.write("a2", a)
    lab.write("ae", a, kind="episode", session="s-a")
    b = move_across(a, tmp_path / "disk" / "born")
    res, brief = lab.view(b)
    assert (res.outcome, res.rule, brief) == (SILENT, "join:H+T", ["a1", "a2"])
    hand = lab.handoff(res)
    assert (hand.session_id, hand.rule) == ("s-a", "inherited_from_non_live_checkout")
    assert hand.label


def test_i03_commit_less_with_a_remote_moved_joins_by_token_and_remote(
    tmp_path: Path, lab: Lab
) -> None:
    """I3 (E02 c): no commit on either side; the token and the remote."""
    u = unborn(tmp_path / "vol" / "u", remote="https://example.invalid/team/u.git")
    lab.write("c1", u)
    v = move_across(u, tmp_path / "disk" / "u")
    res, brief = lab.view(v)
    assert (res.outcome, res.rule, brief) == (SILENT, "join:R+T", ["c1"])


def test_i04_commit_less_remote_less_moved_joins_by_its_token_alone(
    tmp_path: Path, lab: Lab
) -> None:
    """I4 (E02 b, E19 b): nothing but the token survives, and it is
    enough: silent, nothing asked, no line. The token is the only file
    written, in the git directory's info folder; the next session is the
    same checkout by its object, and its first commit changes nothing."""
    n = unborn(tmp_path / "vol" / "n", note="work in progress\n")
    lab.write("b1", n)
    n2 = move_across(n, tmp_path / "disk" / "n")
    res, brief = lab.view(n2)
    assert (res.outcome, res.rule, brief, res.lines) == (SILENT, "join:T", ["b1"], ())
    assert (n2 / ".git" / "info" / "bettermemory").is_file()
    assert sorted(p.name for p in n2.iterdir()) == [".git", "notes.md"]
    assert git(n2, "status", "--porcelain") == "?? notes.md"
    res, brief = lab.view(n2)
    assert (res.rule, brief) == (known_rule(tmp_path), ["b1"])
    git(n2, "add", "-A")
    git(n2, "commit", "-q", "-m", "first")
    res, brief = lab.view(n2)
    assert brief == ["b1"]


def test_i05_commit_less_copy_seen_after_the_original_is_deleted_joins_by_token(
    tmp_path: Path, lab: Lab
) -> None:
    """I5 (E19 c, delete first): the copy carries the token and no live
    checkout does: the token alone, silent."""
    o = unborn(tmp_path / "c" / "orig", note="plan\n")
    lab.write("o1", o)
    k = copy(o, tmp_path / "c" / "copy")
    remove(o)
    res, brief = lab.view(k)
    assert (res.outcome, res.rule, brief) == (SILENT, "join:T", ["o1"])


def test_i06_commit_less_copy_while_the_original_lives_asks_once(
    tmp_path: Path, lab: Lab
) -> None:
    """I6 (E19 c, first sight while the original lives): one token on two
    live checkouts with no commit in common is a question (L1), nothing
    of the original delivered; it stays open across sessions and when the
    original goes; answered `project new`, the copy is a project of its
    own with a token of its own and is not asked again."""
    o = unborn(tmp_path / "c" / "orig", note="plan\n")
    lab.write("o1", o)
    k = copy(o, tmp_path / "c" / "copy")
    res, brief = lab.view(k)
    assert (res.outcome, res.rule, brief) == (ASK, "ask:token_shared_no_commit", [])
    assert any('answer="separate"' in line for line in res.lines)
    again, _ = lab.view(k)
    assert again.outcome == ASK
    assert any("may be project" in line for line in again.lines)
    remove(o)
    after, brief = lab.view(k)
    assert (after.outcome, brief) == (ASK, [])
    lab.answer(k, "new")
    settled, brief = lab.view(k)
    assert (settled.outcome, settled.lines, brief) == (SILENT, (), [])
    model = lab.model()
    assert (
        model.checkout(str(settled.checkout)).token
        != model.checkout(str(lab.checkout_of("o1"))).token
    )


@pytest.mark.parametrize("remote", [None, "https://example.invalid/team/copyme.git"])
def test_i08_copy_while_the_original_lives_shares_memories_not_run_state(
    tmp_path: Path, lab: Lab, remote: str | None
) -> None:
    """I8 (E03): `cp -R` while the original lives: one project, two
    checkouts; project memories both ways, run-state per checkout."""
    a = make_repo(tmp_path / "orig", commits=2, remote=remote)
    lab.write("m_orig", a)
    lab.write("e_orig", a, kind="episode", session="s-orig")
    c = copy(a, tmp_path / "copy")
    lab.write("m_copy", c)
    lab.write("e_copy", c, kind="episode", session="s-copy")
    rc, bc = lab.view(c)
    ra, ba = lab.view(a)
    assert bc == ba == ["m_copy", "m_orig"]
    assert rc.project == ra.project and rc.checkout != ra.checkout
    assert lab.handoff(rc).session_id == "s-copy"
    assert lab.handoff(ra).session_id == "s-orig"


@pytest.mark.parametrize("first", ["C", "D"])
def test_i09_copies_after_the_original_is_deleted_answer_alike_in_either_order(
    tmp_path: Path, lab: Lab, first: str
) -> None:
    """I9 (E04): two copies, the original deleted: both see both memories
    and are offered the original's last run-state, labelled, whichever is
    seen first."""
    a = make_repo(tmp_path / "orig", commits=2)
    lab.write("m1", a)
    lab.write("m2", a)
    lab.write("e1", a, kind="episode", session="s-orig")
    c, d = copy(a, tmp_path / "copyC"), copy(a, tmp_path / "copyD")
    remove(a)
    order = (c, d) if first == "C" else (d, c)
    for path in order:
        res, brief = lab.view(path)
        assert (res.outcome, brief) == (SILENT, ["m1", "m2"])
        hand = lab.handoff(res)
        assert (hand.session_id, hand.rule) == (
            "s-orig",
            "inherited_from_non_live_checkout",
        )


def test_i10_remote_added_then_changed_in_place_is_silent(
    tmp_path: Path, lab: Lab
) -> None:
    """I10 (E05): the same git directory before and after a remote is
    added and its URL changed: every memory stays."""
    a = make_repo(tmp_path / "proj", commits=2)
    lab.write("before1", a)
    lab.write("before2", a)
    git(a, "remote", "add", "origin", "https://example.invalid/team/proj.git")
    lab.write("after1", a)
    res, brief = lab.view(a)
    assert (res.outcome, brief) == (SILENT, ["after1", "before1", "before2"])
    git(
        a,
        "remote",
        "set-url",
        "origin",
        "https://example.invalid/team/proj-renamed.git",
    )
    lab.write("after2", a)
    res, brief = lab.view(a)
    assert (res.outcome, brief) == (SILENT, ["after1", "after2", "before1", "before2"])


def test_t01_fresh_clone_joins_by_history_and_remote_and_a_shallow_one_asks(
    tmp_path: Path, lab: Lab
) -> None:
    """T1, I24 (E05): a fresh full clone is the project by its history and
    its remote; a shallow clone at a tip no witness names holds only the
    remote, one witness: asked once, nothing delivered until `project
    join` answers it."""
    url = "https://example.invalid/team/proj.git"
    a = make_repo(tmp_path / "proj", commits=2, remote=url)
    lab.write("m1", a)
    f = clone(a, tmp_path / "elsewhere" / "proj", url=url)
    res, brief = lab.view(f)
    assert (res.outcome, res.rule, brief) == (SILENT, "join:H+R", ["m1"])
    commit(a, "unrecorded tip")
    s = clone(a, tmp_path / "ci" / "proj", "--depth", "1", url=url)
    res, brief = lab.view(s)
    assert (res.outcome, res.rule, brief) == (ASK, "ask:one_witness", [])
    assert any('answer="join"' in line for line in res.lines)
    lab.answer(s, "join")
    res, brief = lab.view(s)
    assert (res.outcome, brief) == (SILENT, ["m1"])


def test_i11_commit_less_moved_then_given_a_remote_and_a_commit(
    tmp_path: Path, lab: Lab
) -> None:
    """I11 (E06): an unborn repository renamed is the same checkout by its
    object; a remote and its first commit change nothing; a copy made
    after the first commit joins by history, remote and token."""
    a = unborn(tmp_path / "Desktop" / "fresh", note="untracked notes\n")
    lab.write("u1", a)
    lab.write("u2", a)
    lab.write("ue", a, kind="episode", session="s-unborn")
    b = move(a, tmp_path / "Developer" / "fresh")
    res, brief = lab.view(b)
    assert (res.rule, brief) == (known_rule(tmp_path), ["u1", "u2"])
    assert lab.handoff(res).session_id == "s-unborn"
    git(b, "remote", "add", "origin", "https://example.invalid/team/fresh.git")
    assert lab.view(b)[1] == ["u1", "u2"]
    git(b, "add", "-A")
    git(b, "commit", "-q", "-m", "first")
    lab.write("born1", b)
    assert lab.view(b)[1] == ["born1", "u1", "u2"]
    c = copy(b, tmp_path / "copy" / "fresh")
    res, brief = lab.view(c)
    assert (res.rule, brief) == ("join:H+R+T", ["born1", "u1", "u2"])


def test_i12_another_repository_at_a_reused_path_is_a_new_project(
    tmp_path: Path, lab: Lab
) -> None:
    """I12 (E07, E15 a): a different repository made at a path a moved
    checkout left is a new project (L2 naming the checkout that was
    there); the moved one keeps its own. A foreign repository naming the
    project's remote without its history is one witness, asked; what is
    written in it reaches neither the original's brief nor its search."""
    a = make_repo(tmp_path / "work" / "app", commits=2)
    lab.write("p1", a)
    lab.write("p2", a)
    b = move(a, tmp_path / "moved" / "app")
    make_repo(a, commits=1)
    lab.write("q1", a)
    ra, brief_a = lab.view(a)
    rb, brief_b = lab.view(b)
    assert brief_a == ["q1"] and brief_b == ["p1", "p2"]
    assert ra.project != rb.project

    url = "https://example.invalid/team/app.git"
    git(b, "remote", "add", "origin", url)
    lab.write("p3", b)
    h = make_repo(tmp_path / "downloads" / "app", commits=1, remote=url)
    rh, brief_h = lab.view(h)
    assert (rh.outcome, rh.rule, brief_h) == (ASK, "ask:one_witness", [])
    assert rh.lines
    lab.write("h1", h)
    rb, brief_b = lab.view(b)
    assert "h1" not in brief_b
    assert "h1" not in lab.admitted(rb, SEARCH_AUTO_SCOPE)


def test_i12_reused_path_first_sight_says_who_was_there(
    tmp_path: Path, lab: Lab
) -> None:
    """I12 (E07): the first sight of the repository at the reused path
    is NEW, and its line names the checkout seen there before and the
    command that joins it."""
    a = make_repo(tmp_path / "work" / "app", commits=2)
    first = lab.write("p1", a)
    move(a, tmp_path / "moved" / "app")
    make_repo(a, commits=1)
    res, _ = lab.view(a)
    assert res.outcome == NEW
    assert any(
        "was here before" in line and f"project join {first.project}" in line
        for line in res.lines
    )


def test_i15_plain_folder_at_the_old_path_of_a_remote_checkout(
    tmp_path: Path, lab: Lab
) -> None:
    """I15 (E08): a moved checkout with a remote keeps its memories once
    a plain folder with conflict-copy names and a `.git` folder that is
    no repository appears at its old path; membership never reads the old
    path."""
    a = make_repo(
        tmp_path / "Desktop" / "game",
        commits=2,
        remote="https://example.invalid/team/game.git",
    )
    lab.write("g1", a)
    lab.write("g2", a)
    b = move(a, tmp_path / "Developer" / "game")
    assert lab.view(b)[1] == ["g1", "g2"]
    (a / "src").mkdir(parents=True)
    (a / "src" / "CharacterEditor 2.cs").write_text("// copy\n")
    (a / ".git").mkdir()
    (a / ".git" / "index 3").write_text("")
    assert lab.view(b)[1] == ["g1", "g2"]


@pytest.mark.parametrize("remote", [None, "https://example.invalid/team/wt.git"])
def test_i16_linked_worktrees_added_moved_removed_and_re_added(
    tmp_path: Path, lab: Lab, remote: str | None
) -> None:
    """I16 (E09): a linked worktree shares the project; `git worktree
    move` and a plain mv keep its checkout (its administrative directory
    in the common git directory); removed or deleted, its memories stay
    with the project; re-added at the same name and path it is a new
    checkout of the project by the shared object store, and does not
    inherit the removed one's run-state."""
    p = make_repo(tmp_path / "main", commits=2, remote=remote)
    lab.write("p1", p)
    lab.write("pe", p, kind="episode", session="s-main")
    w = tmp_path / "wt" / "feat"
    git(p, "worktree", "add", "-q", "-b", "feat", w)
    lab.write("w1", w)
    lab.write("we", w, kind="episode", session="s-feat")
    rp, bp = lab.view(p)
    rw, bw = lab.view(w)
    assert bp == bw == ["p1", "w1"]
    assert rp.project == rw.project and rp.checkout != rw.checkout

    w2 = tmp_path / "wt" / "feat-moved"
    git(p, "worktree", "move", w, w2)
    res, brief = lab.view(w2)
    assert (res.rule, brief) == (known_rule(tmp_path), ["p1", "w1"])
    assert lab.handoff(res).session_id == "s-feat"

    w3 = move(w2, tmp_path / "elsewhere" / "feat")
    res, _ = lab.view(w3)
    assert res.rule == known_rule(tmp_path)
    subprocess.run(
        ["git", "worktree", "repair"], cwd=w3, capture_output=True, check=False
    )
    git(p, "worktree", "remove", "--force", w3)
    assert "w1" in lab.view(p)[1]

    w4 = tmp_path / "wt" / "tmp"
    git(p, "worktree", "add", "-q", "-b", "tmp", w4)
    lab.write("t1", w4)
    remove(w4)
    assert "t1" in lab.view(p)[1]
    git(p, "worktree", "prune")

    git(p, "worktree", "add", "-q", "-b", "feat2", w)
    res, _ = lab.view(w)
    assert (res.rule, res.project) == (
        known_rule(tmp_path, "common_object", "pointer"),
        rp.project,
    )
    assert lab.handoff(res).session_id is None


@pytest.mark.usefixtures("no_birth")
@pytest.mark.parametrize("remote", [None, "https://example.invalid/team/wt.git"])
def test_i16_without_birth_worktrees_join_by_their_pointers(
    tmp_path: Path, lab: Lab, remote: str | None
) -> None:
    """I16 (E09, E15) without birth time: a new linked worktree carries no
    token of the primary's, so witness O cannot prove the object store
    they share; the worktree's git directory under the primary's
    ``worktrees/`` proves it (``common_object+pointer``). Moved by `git
    worktree move` or a plain mv, it is its own checkout by its token;
    removed, its memories stay with the project; re-added at the same name
    and path, a new checkout of the project by its pointers again, without
    the removed one's run-state."""
    p = make_repo(tmp_path / "main", commits=2, remote=remote)
    first = lab.write("p1", p)
    lab.write("pe", p, kind="episode", session="s-main")
    w = tmp_path / "wt" / "feat"
    git(p, "worktree", "add", "-q", "-b", "feat", w)
    added = lab.write("w1", w)
    assert (added.outcome, added.rule, added.project) == (
        SILENT,
        "common_object+pointer",
        first.project,
    )
    assert added.checkout != first.checkout
    lab.write("we", w, kind="episode", session="s-feat")
    assert lab.view(p)[1] == lab.view(w)[1] == ["p1", "w1"]

    w2 = tmp_path / "wt" / "feat-moved"
    git(p, "worktree", "move", w, w2)
    res, brief = lab.view(w2)
    assert (res.rule, res.checkout, brief) == (
        "object+token",
        added.checkout,
        ["p1", "w1"],
    )
    assert lab.handoff(res).session_id == "s-feat"
    w3 = move(w2, tmp_path / "elsewhere" / "feat")
    res, _ = lab.view(w3)
    assert (res.rule, res.checkout) == ("object+token", added.checkout)
    subprocess.run(
        ["git", "worktree", "repair"], cwd=w3, capture_output=True, check=False
    )
    git(p, "worktree", "remove", "--force", w3)
    assert lab.view(p)[1] == ["p1", "w1"]

    git(p, "worktree", "prune")
    git(p, "worktree", "add", "-q", "-b", "feat2", w)
    res, brief = lab.view(w)
    assert (res.rule, res.project, brief) == (
        "common_object+pointer",
        first.project,
        ["p1", "w1"],
    )
    assert res.checkout not in (first.checkout, added.checkout)
    assert lab.handoff(res).session_id is None


@pytest.mark.usefixtures("no_birth")
def test_i16_without_birth_a_worktree_of_a_new_repository_at_a_reused_number_stays_apart(
    tmp_path: Path, lab: Lab
) -> None:
    """I16, X15 (E15 a) without birth time: a repository deleted and another
    made at its path, its git directory given the deleted one's inode
    number. A worktree of the new one, seen first, names the recorded
    checkout's git directory by its pointers and its object store by that
    number, but the recorded checkout is not live there (the new git
    directory lacks its token): no link, nothing of the old project."""
    x = make_repo(tmp_path / "a" / "proj", commits=2)
    lab.write("x1", x)
    recorded = _recorded_key(lab, "x1")
    assert recorded[2] is None
    remove(x)
    y = make_repo(tmp_path / "a" / "proj", commits=1)
    w = tmp_path / "a" / "feat"
    git(y, "worktree", "add", "-q", "-b", "feat", w)
    with posing(y / ".git", recorded):
        res, brief = lab.view(w)
    assert not res.rule.startswith("common_object")
    assert res.project != lab.project_of("x1") and brief == []


@posix_only
@pytest.mark.parametrize("remote", [None, "https://example.invalid/team/locked.git"])
def test_i17_old_path_unreadable_keeps_the_moved_checkout(
    tmp_path: Path, lab: Lab, remote: str | None
) -> None:
    """I17 (E11 a): the old path's parent denies access (EACCES) after a
    move; membership never reads it: the object keeps the memory and the
    run-state."""
    parent = tmp_path / "locked"
    a = make_repo(parent / "proj", commits=2, remote=remote)
    lab.write("m1", a)
    lab.write("e1", a, kind="episode", session="s-old")
    b = move(a, tmp_path / "moved" / "proj")
    parent.chmod(0o000)
    try:
        if os.stat(parent).st_mode & 0o777:
            # An SMB mount without the unix extensions (nounix) does not
            # set this mode, so it cannot deny access to the old path.
            pytest.skip("E11 a: the filesystem under tmp_path ignores chmod")
        if os.access(parent, os.R_OK | os.X_OK):
            pytest.fail("the fixture could not deny access to the old path")
        res, brief = lab.view(b)
        assert (res.outcome, brief) == (SILENT, ["m1"])
        assert lab.handoff(res).session_id == "s-old"
    finally:
        parent.chmod(0o755)


def test_i17_detached_original_offers_its_run_state_and_live_again_does_not(
    tmp_path: Path, lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I17 (E11 b): the original on a volume that is detached (its path
    unreachable), a copy on the internal disk: the copy joins by token and
    history, and the unreadable original's last run-state is offered,
    labelled; once the volume is back, two live checkouts of one project,
    and the copy no longer takes the original's run-state."""
    o = make_repo(tmp_path / "vol" / "proj", commits=2)
    lab.write("v1", o)
    lab.write("ve", o, kind="episode", session="s-volume")
    c = copy(o, tmp_path / "internal" / "proj")
    hidden = tmp_path / "vol-detached"
    move(tmp_path / "vol", hidden)
    res, brief = lab.view(c)
    assert (res.rule, brief) == ("join:H+T", ["v1"])
    hand = lab.handoff(res)
    assert hand.session_id == "s-volume" and hand.rule.startswith("inherited")
    move(hidden, tmp_path / "vol")
    location.forget()
    rc, bc = lab.view(c)
    ro, bo = lab.view(o)
    assert bc == bo == ["v1"]
    assert lab.handoff(rc).session_id is None


def test_i18_remount_renumbering_the_object_rekeys_the_checkout(
    tmp_path: Path, lab_of: Callable[..., Lab], monkeypatch: pytest.MonkeyPatch
) -> None:
    """I18 (E11 c): a remount that renumbers the git directory (SMB and
    NFS can): while the recorded object is still live (its view answers
    live, as another namespace's reading of the path would), another
    object at the path is another checkout of the project; once the
    recorded object is live nowhere, the same path, the history and the
    token re-key the same checkout, and its run-state continues."""
    lab = lab_of("live")
    m = make_repo(tmp_path / "share" / "proj", commits=2)
    lab.write("r1", m)
    real = _git_dir_key(m)
    recorded = lab.checkout_of("r1")
    reader = membership.path_liveness

    def still_live(k: membership.Checkout, v: membership.View) -> str:
        return membership.LIVE if k.id == recorded else reader(k, v)

    monkeypatch.setattr(membership, "path_liveness", still_live)
    with posing(m / ".git", (str(int(real[0]) + 1), str(int(real[1]) + 7), None)):
        res, _ = lab.view(m)
    monkeypatch.setattr(membership, "path_liveness", reader)
    assert res.checkout != lab.checkout_of("r1")
    assert res.rule.startswith("join:") and res.project == lab.project_of("r1")

    gone = lab_of("gone")
    m2 = make_repo(tmp_path / "share2" / "proj", commits=2)
    with posing(m2 / ".git", ("1", "2", "3")):
        gone.write("r1", m2)
        gone.write("re", m2, kind="episode", session="s-share")
    k0 = gone.checkout_of("r1")
    res, brief = gone.view(m2)
    assert res.checkout == k0 and res.rule.startswith("rekeyed:") and "B" in _codes(res)
    assert (res.outcome, brief) == (SILENT, ["r1"])
    assert "checkout_rekeyed" in gone.rows("checkout_rekeyed")
    assert gone.handoff(res).session_id == "s-share"


def test_i18_restored_from_a_backup_at_its_path_is_the_same_checkout(
    tmp_path: Path, lab: Lab
) -> None:
    """I18 (E37 a, b): deleted and restored from a backup at its path (its
    token and history came back with it): the same checkout, its run-state
    continues, decided by what the restore made of its git directory's
    object. A new inode number: re-keyed (B). The deleted directory's
    number again, which ext4 can hand straight back: with birth time
    another object at rung 2 and the same number at rung 4, the same
    checkout without a re-key row (B20, ``same_object``); without birth
    time nothing tells the two apart but the token restored with it, which
    confirms the checkout by witness O (E15, ``object+token``). The backup
    restored to a second path while the original lives is a copy: a
    second checkout, silent, which takes nothing of the original's
    run-state."""
    p = make_repo(tmp_path / "work" / "proj", commits=2)
    lab.write("m1", p)
    lab.write("e1", p, kind="episode", session="s-before")
    k0 = lab.checkout_of("m1")
    before = _git_dir_key(p)
    backup = copy(p, tmp_path / "backup" / "proj")
    remove(p)
    copy(backup, p)
    renumbered = _git_dir_key(p)[:2] != before[:2]
    res, brief = lab.view(p)
    assert (res.outcome, res.checkout, brief) == (SILENT, k0, ["m1"])
    if renumbered:
        assert res.rule.startswith("rekeyed")
        assert lab.rows("checkout_rekeyed") == ["checkout_rekeyed"]
    elif birth_reported(tmp_path):
        assert res.rule == "same_object:B+H+T"
        assert lab.rows("checkout_rekeyed") == []
    else:
        assert res.rule == "object+token"
        assert lab.rows("checkout_rekeyed") == []
    assert lab.handoff(res).rule == "own_checkout"
    second = copy(backup, tmp_path / "restored-copy" / "proj")
    rb, bb = lab.view(second)
    assert (rb.outcome, bb) == (SILENT, ["m1"]) and rb.checkout != k0
    assert lab.handoff(rb).rule == "none"


@pytest.mark.parametrize("birth", [True, False], ids=["birth", "no-birth"])
def test_i18_restored_with_its_old_inode_number_is_the_same_checkout(
    tmp_path: Path, lab: Lab, monkeypatch: pytest.MonkeyPatch, birth: bool
) -> None:
    """I18 (E37 a), E15, decision B20: the restore of the test above handed
    the deleted git directory's inode number again, posed here so that
    every filesystem shows the case. With birth time: another object at
    rung 2 and the same number at rung 4, so the path, the history and
    the token keep the checkout without a re-key row. Without birth time:
    the token restored with it confirms the object at rung 2."""
    if not birth:
        monkeypatch.setattr(location, "REPORT_BIRTH", False)
        location.forget()
    elif not birth_reported(tmp_path):
        pytest.skip("I18 with birth time: this filesystem reports none")
    p = make_repo(tmp_path / "work" / "proj", commits=2)
    lab.write("m1", p)
    lab.write("e1", p, kind="episode", session="s-before")
    k0 = lab.checkout_of("m1")
    old = _git_dir_key(p)
    backup = copy(p, tmp_path / "backup" / "proj")
    remove(p)
    copy(backup, p)
    new = _git_dir_key(p)
    assert new[2] != old[2] if birth else new[2] is None
    with posing(p / ".git", (old[0], old[1], new[2])):
        res, brief = lab.view(p)
        assert (res.outcome, res.checkout, brief) == (SILENT, k0, ["m1"])
        assert res.rule == ("same_object:B+H+T" if birth else "object+token")
        assert lab.rows("checkout_rekeyed") == []
        # The view is live by the same reading afterwards (B18): the
        # checkout keeps its run-state, and a copy of the backup at a
        # second path, a new checkout, takes none of it.
        assert lab.handoff(res).rule == "own_checkout"
        second = copy(backup, tmp_path / "restored-copy" / "proj")
        rb, bb = lab.view(second)
        assert (rb.outcome, bb) == (SILENT, ["m1"]) and rb.checkout != k0
        assert lab.handoff(rb).rule == "none"


# ---------------------------------------------------------------------------
# Object identity without birth time (E15, E33)
# ---------------------------------------------------------------------------


def _stamps_seen(lab: Lab, label: str, path: Path) -> tuple[Any, Any]:
    """The init stamps recorded for `label`'s checkout and the ones read at
    `path` now: what an init-stamp assertion prints when it fails, so a
    filesystem that reports a file's times differently from one reading
    to the next names the field."""
    k = lab.model().checkout(str(lab.checkout_of(label)))
    return (k.stamps, location._init_stamps(str(path / ".git")))


def _recorded_key(lab: Lab, label: str) -> location.Key:
    k = lab.model().checkout(str(lab.checkout_of(label)))
    assert k.object_key is not None
    return (k.object_key.device, k.object_key.inode, k.object_key.birth)


@pytest.mark.usefixtures("no_birth")
def test_x15_reused_inode_number_without_birth_is_refuted(
    tmp_path: Path, lab: Lab
) -> None:
    """X15, I12 (E15 a): with no birth time, a new repository given a
    deleted one's device and inode number lacks the checkout's token and
    last HEAD: another checkout of another project."""
    x = make_repo(tmp_path / "a" / "proj", commits=2)
    lab.write("x1", x)
    recorded = _recorded_key(lab, "x1")
    assert recorded[2] is None
    remove(x)
    y = make_repo(tmp_path / "a" / "proj", commits=1)
    with posing(y / ".git", recorded):
        res, brief = lab.view(y)
    assert res.checkout != lab.checkout_of("x1")
    assert res.project != lab.project_of("x1") and brief == []


@pytest.mark.usefixtures("no_birth")
def test_i14_moves_without_birth_continue_by_token(tmp_path: Path, lab: Lab) -> None:
    """I14 (E15 b, c): without birth time a rename keeps the checkout,
    born or commit-less, the token confirming the object."""
    a = make_repo(tmp_path / "b" / "proj", commits=2)
    lab.write("b1", a)
    b = move(a, tmp_path / "b2" / "proj")
    res, brief = lab.view(b)
    assert (res.checkout, res.rule, brief) == (
        lab.checkout_of("b1"),
        "object+token",
        ["b1"],
    )
    u = unborn(tmp_path / "c" / "proj")
    lab.write("u1", u)
    u2 = move(u, tmp_path / "c2" / "proj")
    res, brief = lab.view(u2)
    assert (res.outcome, res.rule, brief) == (SILENT, "object+token", ["u1"])
    assert lab.view(u2)[1] == ["u1"]


def test_x15_with_birth_a_reused_inode_number_is_another_object(
    tmp_path: Path, lab: Lab
) -> None:
    """X15 (E15 d): with birth time the reused device and inode number
    with another birth is another object, decided without a spawn. Its
    twin without birth time is `test_x15_reused_inode_number_without_birth_is_refuted`."""
    if not birth_reported(tmp_path):
        pytest.skip("X15 with birth time: this filesystem reports none")
    x = make_repo(tmp_path / "d" / "proj", commits=2)
    lab.write("x1", x)
    recorded = _recorded_key(lab, "x1")
    remove(x)
    y = make_repo(tmp_path / "d" / "proj", commits=1)
    birth = _git_dir_key(y)[2]
    assert birth is not None and birth != recorded[2]
    with posing(y / ".git", (recorded[0], recorded[1], birth)):
        res, _ = lab.view(y)
    assert res.checkout != lab.checkout_of("x1")


@pytest.mark.usefixtures("no_birth")
@pytest.mark.parametrize("write_token", [True, False])
def test_i13_commit_less_recreated_at_the_same_path_without_birth_is_new(
    tmp_path: Path, lab_of: Callable[..., Lab], write_token: bool
) -> None:
    """I13, X7 (E15 e, f): an unborn repository deleted and re-created at
    the same path with the same inode number, no birth time: with the
    token written, the new one lacks it; with the write denied by policy,
    its init stamps differ (A2). Either way a new project, said so (L2)."""
    lab = lab_of(write_token=write_token)
    e = unborn(tmp_path / "e" / "proj")
    lab.write("e1", e)
    recorded = _recorded_key(lab, "e1")
    has_token = (e / ".git" / "info" / "bettermemory").exists()
    assert has_token is write_token
    remove(e)
    time.sleep(0.01)
    unborn(e)
    with posing(e / ".git", recorded):
        res, brief = lab.view(e)
    assert (res.outcome, res.rule, brief) == (NEW, "new:reused_path", [])
    assert res.checkout != lab.checkout_of("e1")
    assert any("was here before" in line for line in res.lines)


@pytest.mark.usefixtures("no_birth")
def test_i14_commit_less_move_without_birth_or_token_continues_by_init_stamps(
    tmp_path: Path, lab_of: Callable[..., Lab]
) -> None:
    """I14, X7 (E15 g): the token write denied by policy, no birth time: a
    real unborn move keeps the checkout by its init stamps (A2)."""
    lab = lab_of(write_token=False)
    g = settled(unborn(tmp_path / "g" / "proj"))
    lab.write("g1", g)
    assert not (g / ".git" / "info" / "bettermemory").exists()
    g2 = move(g, tmp_path / "g2" / "proj")
    res, brief = lab.view(g2)
    assert (res.checkout, res.rule, brief) == (
        lab.checkout_of("g1"),
        "object+init_stamps",
        ["g1"],
    ), _stamps_seen(lab, "g1", g2)


@pytest.mark.usefixtures("no_birth")
def test_i33_a_clock_a_day_off_changes_no_decision(
    tmp_path: Path, lab_of: Callable[..., Lab], monkeypatch: pytest.MonkeyPatch
) -> None:
    """I33 (E33): no birth time and no token; the clock of this process a
    day ahead or behind (or the filesystem's, the same thing): a moved
    unborn checkout continues by its init stamps and one re-created at
    the same path with the same inode number is refused, at every skew."""
    real_time = time.time
    decisions: dict[str, tuple[str, list[str], str]] = {}
    stamps: dict[str, Any] = {}
    for name, skew in (("none", 0.0), ("ahead_1d", 86400.0), ("behind_1d", -86400.0)):
        monkeypatch.setattr(time, "time", lambda skew=skew: real_time() + skew)
        location.forget()
        moving = lab_of(f"move-{name}", write_token=False)
        a = settled(unborn(tmp_path / name / "a" / "proj"))
        moving.write("a1", a)
        a2 = move(a, tmp_path / name / "a2" / "proj")
        ra, ba = moving.view(a2)
        stamps[name] = _stamps_seen(moving, "a1", a2)
        again = lab_of(f"recreate-{name}", write_token=False)
        b = unborn(tmp_path / name / "b" / "proj")
        again.write("b1", b)
        recorded = _recorded_key(again, "b1")
        remove(b)
        unborn(b)
        with posing(b / ".git", recorded):
            rb, _ = again.view(b)
        decisions[name] = (ra.rule, ba, rb.rule)
    monkeypatch.setattr(time, "time", real_time)
    assert decisions["none"] == (
        "object+init_stamps",
        ["a1"],
        "new:reused_path",
    ), stamps["none"]
    assert decisions["ahead_1d"] == decisions["behind_1d"] == decisions["none"]


@pytest.mark.usefixtures("no_birth")
def test_i14_e33_commit_less_move_without_birth_or_token_is_live_at_its_new_path(
    tmp_path: Path, lab_of: Callable[..., Lab]
) -> None:
    """I14, E33, decision B18: no birth time and the token write denied by
    policy. A commit-less checkout renamed continues by its init stamps,
    and its view is live by the same rule: at its own path before the
    move, at the new path after it (`live_view` names it), while the old
    path is gone."""
    lab = lab_of(write_token=False)
    g = settled(unborn(tmp_path / "g" / "proj"))
    lab.write("g1", g)
    model = lab.model()
    k = model.checkout(str(lab.checkout_of("g1")))
    assert k.token is None and k.last_head is None and k.stamps
    assert k.object_key is not None and k.object_key.birth is None
    assert (model.view_state(k), model.live_view(k)) == (membership.LIVE, str(g))
    g2 = move(g, tmp_path / "g2" / "proj")
    res, brief = lab.view(g2)
    assert (res.checkout, res.rule, brief) == (
        k.id,
        "object+init_stamps",
        ["g1"],
    ), _stamps_seen(lab, "g1", g2)
    assert "checkout_moved" in lab.rows("checkout_moved")
    moved = model.checkout(k.id)
    old, new = moved.views_on(lab.identity.id)
    assert (old.path, new.path) == (str(g), str(g2))
    assert model.view_state(moved, old) == membership.GONE
    assert model.view_state(moved) == membership.LIVE
    assert model.live_view(moved) == str(g2)


@pytest.mark.usefixtures("no_birth")
@pytest.mark.parametrize("write_token", [False, True])
def test_i13_e33_recreated_without_birth_is_replaced_and_resolves_new(
    tmp_path: Path, lab_of: Callable[..., Lab], write_token: bool
) -> None:
    """I13, E33, decision B18: a commit-less repository deleted and
    re-created at the same path with the same inode number, no birth time,
    with or without its token: the recorded checkout's view there is
    replaced (no live view), and the new repository is a new project, the
    line naming the checkout that was there (L2)."""
    lab = lab_of(write_token=write_token)
    e = unborn(tmp_path / "e" / "proj")
    lab.write("e1", e)
    recorded = _recorded_key(lab, "e1")
    model = lab.model()
    k = model.checkout(str(lab.checkout_of("e1")))
    assert (k.token is not None) is write_token
    remove(e)
    time.sleep(0.01)
    unborn(e)
    with posing(e / ".git", recorded):
        assert model.view_state(k) == membership.REPLACED
        assert model.live_view(k) is None
        res, brief = lab.view(e)
    assert (res.outcome, res.rule, brief) == (NEW, "new:reused_path", [])
    assert res.checkout != k.id
    assert any(
        f"checkout {k.id} of" in line and "was here before" in line
        for line in res.lines
    )


@pytest.mark.usefixtures("no_birth")
def test_x15_born_checkout_without_birth_or_token_is_live_through_its_last_head(
    tmp_path: Path, lab_of: Callable[..., Lab]
) -> None:
    """X15, decision B18: no birth time and no token (the policy): a
    checkout with commits stays live at its path after HEAD advances,
    its recorded last HEAD held there; renamed and committed again, the
    same checkout by the last HEAD held, live at the new path, the old
    one gone."""
    lab = lab_of(write_token=False)
    a = make_repo(tmp_path / "h" / "proj", commits=2)
    lab.write("h1", a)
    model = lab.model()
    k = model.checkout(str(lab.checkout_of("h1")))
    assert k.token is None and k.last_head is not None
    head = commit(a)
    assert head != k.last_head
    assert (model.view_state(k), model.live_view(k)) == (membership.LIVE, str(a))
    a2 = move(a, tmp_path / "h2" / "proj")
    commit(a2)
    res, brief = lab.view(a2)
    assert (res.outcome, res.checkout, res.rule, brief) == (
        SILENT,
        k.id,
        "object+last_head",
        ["h1"],
    )
    moved = model.checkout(k.id)
    old = moved.views_on(lab.identity.id)[0]
    assert model.view_state(moved, old) == membership.GONE
    assert model.live_view(moved) == str(a2)


@pytest.mark.usefixtures("no_birth")
def test_i33_e33_liveness_is_the_same_at_every_clock_skew(
    tmp_path: Path, lab_of: Callable[..., Lab], monkeypatch: pytest.MonkeyPatch
) -> None:
    """I33 (E33), decision B18: the model's clock (`Membership._now`) and
    this process's clock a day ahead or behind, no birth time, no token: a
    moved commit-less checkout is live at its new path and gone at the old
    one, and one re-created at the same path with the same inode number is
    replaced and resolves new, the same at every skew."""
    real_time = time.time
    real_init = Membership.__init__
    answers: dict[str, tuple[Any, ...]] = {}
    stamps: dict[str, Any] = {}
    for name, skew in (("none", 0.0), ("ahead_1d", 86400.0), ("behind_1d", -86400.0)):

        def skewed(self: Membership, *args: Any, skew: float = skew, **kw: Any) -> None:
            kw.setdefault("now", lambda: utcnow() + timedelta(seconds=skew))
            real_init(self, *args, **kw)

        monkeypatch.setattr(Membership, "__init__", skewed)
        monkeypatch.setattr(time, "time", lambda skew=skew: real_time() + skew)
        location.forget()
        lab = lab_of(f"skew-{name}", write_token=False)
        off = (lab.model()._now() - utcnow()).total_seconds()
        assert abs(off - skew) < 60
        a = settled(unborn(tmp_path / name / "a" / "proj"))
        lab.write("a1", a)
        a2 = move(a, tmp_path / name / "a2" / "proj")
        moved, _ = lab.view(a2)
        stamps[name] = _stamps_seen(lab, "a1", a2)
        model = lab.model()
        k = model.checkout(str(moved.checkout))
        old = k.views_on(lab.identity.id)[0]
        b = unborn(tmp_path / name / "b" / "proj")
        lab.write("b1", b)
        recorded = _recorded_key(lab, "b1")
        kb = model.checkout(str(lab.checkout_of("b1")))
        remove(b)
        unborn(b)
        with posing(b / ".git", recorded):
            replaced = model.view_state(kb)
            rb, _ = lab.view(b)
        answers[name] = (
            moved.rule,
            model.live_view(k) == str(a2),
            model.view_state(k, old),
            replaced,
            rb.outcome,
        )
    monkeypatch.setattr(time, "time", real_time)
    monkeypatch.setattr(Membership, "__init__", real_init)
    assert answers["none"] == (
        "object+init_stamps",
        True,
        membership.GONE,
        membership.REPLACED,
        NEW,
    ), stamps["none"]
    assert answers["ahead_1d"] == answers["behind_1d"] == answers["none"]


# ---------------------------------------------------------------------------
# The token (E19), denied writes (E20), planted identity (E21)
# ---------------------------------------------------------------------------


def test_i04_the_token_is_one_file_in_the_git_directory_and_git_leaves_it(
    tmp_path: Path, lab: Lab
) -> None:
    """Design 1.1 (E19 a): `.git/info/bettermemory`, a header and 26
    characters; git status clean; `git clone` does not copy it; gc,
    repack, worktree prune and update-server-info leave it as it was."""
    a = make_repo(tmp_path / "a" / "proj", commits=1)
    lab.write("a1", a)
    tok = a / ".git" / "info" / "bettermemory"
    lines = tok.read_text().splitlines()
    assert len(lines) == 2 and lines[0] == checkout_token.TOKEN_HEADER
    assert len(lines[1]) == 26
    assert git(a, "status", "--porcelain") == ""
    c = clone(a, tmp_path / "a" / "clone")
    assert not (c / ".git" / "info" / "bettermemory").exists()
    before = tok.read_text()
    wt = tmp_path / "a" / "wt"
    git(a, "worktree", "add", "-q", wt, "-b", "wt")
    remove(wt)
    git(a, "worktree", "prune")
    git(a, "gc", "-q", "--prune=now")
    git(a, "repack", "-a", "-d", "-q")
    git(a, "update-server-info")
    assert tok.read_text() == before


@pytest.mark.parametrize("order", ["delete-first", "sight-first"])
def test_i09_born_copy_with_the_original_deleted_answers_alike_in_either_order(
    tmp_path: Path, lab: Lab, order: str
) -> None:
    """I9 (E19 d): born and remote-less, copied, the original deleted
    before or after the copy's first sight: the token and the history,
    silent, the same answer."""
    o = make_repo(tmp_path / "orig", commits=2)
    lab.write("m1", o)
    k = copy(o, tmp_path / "copy")
    if order == "sight-first":
        lab.view(k)
    remove(o)
    res, brief = lab.view(k)
    assert (res.outcome, brief) == (SILENT, ["m1"])


def test_i16_worktree_token_and_a_whole_repository_moved_across(
    tmp_path: Path, lab: Lab
) -> None:
    """I16 (E19 e): a linked worktree's token sits in its own git
    directory (`.git/worktrees/<name>/info`); the whole repository moved
    to another filesystem, the worktree seen first: its token and history
    keep it, and the primary joins the same project."""
    p = make_repo(tmp_path / "vol" / "wtrepo" / "main", commits=2)
    w = tmp_path / "vol" / "wtrepo" / "feat"
    git(p, "worktree", "add", "-q", "-b", "feat", w)
    lab.write("p1", p)
    lab.write("w1", w)
    assert (p / ".git" / "worktrees" / "feat" / "info" / "bettermemory").is_file()
    assert not (w / "bettermemory").exists()
    dest = move_across(tmp_path / "vol" / "wtrepo", tmp_path / "disk" / "wtrepo")
    git(dest / "main", "worktree", "repair", dest / "feat")
    res, brief = lab.view(dest / "feat")
    assert (res.outcome, brief) == (SILENT, ["p1", "w1"])
    rp, bp = lab.view(dest / "main")
    assert bp == ["p1", "w1"] and rp.project == res.project


@posix_only
def test_i07_token_write_denied_records_the_failure_and_asks_by_content(
    tmp_path: Path, lab: Lab
) -> None:
    """I7 (E20 a to c): `.git/info` or all of `.git` read-only: the token
    write fails (EACCES, status failed:readonly) and the checkout records it; a known
    read-only checkout still resolves by its object. Commit-less and
    read-only, copied to another filesystem with the original deleted:
    the content witness names the candidate, one question, and the line
    says what could not be written; answered, delivered."""
    a = make_repo(tmp_path / "a" / "proj", commits=1)
    (a / ".git" / "info").chmod(0o555)
    try:
        first = lab.write("a1", a)
        k = lab.model().checkout(str(first.checkout))
        assert (k.token, k.token_status) == (None, "failed:readonly")
        assert not (a / ".git" / "info" / "bettermemory").exists()
        res, brief = lab.view(a)
        assert (res.rule, brief) == (
            known_rule(tmp_path, without_birth="last_head"),
            ["a1"],
        )
    finally:
        (a / ".git" / "info").chmod(0o755)

    u = unborn(tmp_path / "vol" / "draft")
    (u / "outline.md").write_text("one\ntwo\n")
    (u / "notes").mkdir()
    (u / "notes" / "n1.md").write_text("n\n")
    for p in [u / ".git", *(u / ".git").rglob("*")]:
        if p.is_dir():
            p.chmod(0o555)
    try:
        lab.write("u1", u)
        k = lab.model().checkout(str(lab.checkout_of("u1")))
        assert k.token is None and k.manifest is not None
        u2 = copy(u, tmp_path / "disk" / "draft")
    finally:
        for p in [u2 / ".git", *(u2 / ".git").rglob("*")]:
            if p.is_dir():
                p.chmod(0o755)
    remove(u)
    for p in [u2 / ".git", *(u2 / ".git").rglob("*")]:
        if p.is_dir():
            p.chmod(0o555)
    try:
        res, brief = lab.view(u2)
        assert (res.rule, brief) == ("ask:content_only", [])
        assert any(
            "could not write" in line and "failed:readonly" in line
            for line in res.lines
        )
        lab.answer(u2, "join", lab.project_of("u1"))
        res, brief = lab.view(u2)
        assert (res.outcome, brief) == (SILENT, ["u1"])
    finally:
        for p in [u2 / ".git", *(u2 / ".git").rglob("*")]:
            if p.is_dir():
                p.chmod(0o755)


def test_x07_token_write_skipped_by_policy_asks_by_content_and_says_so(
    tmp_path: Path, lab_of: Callable[..., Lab]
) -> None:
    """X7, I7 (E20 d): `membership.write_token = false`: no token, status
    skipped:policy; moved across filesystems, the content witness names
    the candidate and the line names the policy."""
    lab = lab_of(write_token=False)
    d = unborn(tmp_path / "vol" / "policy")
    (d / "spec.md").write_text("spec\n")
    first = lab.write("d1", d)
    k = lab.model().checkout(str(first.checkout))
    assert k.token_status == "skipped:policy"
    assert not (d / ".git" / "info" / "bettermemory").exists()
    d2 = move_across(d, tmp_path / "disk" / "policy")
    res, _ = lab.view(d2)
    assert res.rule == "ask:content_only"
    assert any("skipped:policy" in line for line in res.lines)


@posix_only
def test_i07_born_read_only_with_a_remote_joins_without_a_token(
    tmp_path: Path, lab: Lab
) -> None:
    """X8 (E20 e): born, read-only, with a remote, copied to another
    filesystem and the original deleted: history and remote, silent, no
    token anywhere."""
    e = make_repo(
        tmp_path / "vol" / "svc",
        commits=2,
        remote="https://example.invalid/acme/svc.git",
    )
    dirs = [p for p in [e / ".git", *(e / ".git").rglob("*")] if p.is_dir()]
    for p in dirs:
        p.chmod(0o555)
    try:
        lab.write("e1", e)
    finally:
        for p in dirs:
            p.chmod(0o755)
    e2 = move_across(e, tmp_path / "disk" / "svc")
    res, brief = lab.view(e2)
    assert (res.rule, brief) == ("join:H+R", ["e1"])


@posix_only
def test_i19_a_repository_git_refuses_is_read_from_its_files(
    tmp_path: Path, lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I19 (E20 f): git refuses another owner's repository (dubious
    ownership); the capture reads HEAD, refs, config and the object
    indexes itself, history and remote join it, and no token is written
    into another owner's repository."""
    url = "https://example.invalid/acme/shared.git"
    f = make_repo(tmp_path / "f" / "theirs", commits=2, remote=url)
    lab.write("f1", f)
    g = clone(f, tmp_path / "f" / "colleague-tree", url=url)
    head = git(g, "rev-parse", "HEAD")
    monkeypatch.setenv("GIT_TEST_ASSUME_DIFFERENT_OWNER", "1")
    refused = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=g, capture_output=True, text=True
    )
    assert refused.returncode != 0 and "dubious ownership" in refused.stderr
    owner = getattr(os, "geteuid", lambda: 0)()
    monkeypatch.setattr(os, "geteuid", lambda: owner + 1)
    cap = capture(str(g), write_token=True, use_cache=False)
    assert cap.head == head
    assert cap.token_status == "skipped:foreign_owner"
    res, brief = lab.view(g)
    assert (res.rule, brief) == ("join:H+R", ["f1"])
    assert not (g / ".git" / "info" / "bettermemory").exists()


@pytest.mark.parametrize("count", [4, 2])
def test_i07a_commit_less_content_candidates_are_bounded(
    tmp_path: Path, lab_of: Callable[..., Lab], count: int
) -> None:
    """I7a (E20 g): under the policy switch, `count` empty commit-less
    "draft" repositories each with a memory, all deleted, then a new
    empty "draft": with more candidates than the 3 a question names, NEW
    with the count and the commands; with two, one question naming both."""
    lab = lab_of(write_token=False)
    pids = set()
    for i in range(count):
        d = unborn(tmp_path / "g" / f"t{i}" / "draft")
        lab.write(f"g{i}", d)
        pids.add(lab.project_of(f"g{i}"))
        remove(d.parent)
    n = unborn(tmp_path / "g" / "new" / "draft")
    res, brief = lab.view(n)
    assert brief == [] and len(pids) == count
    if count > 3:
        assert (res.outcome, res.rule) == (NEW, "new")
        assert any(
            "of 4 commit-less projects" in line
            and "more than the 3" in line
            and "project join" in line
            for line in res.lines
        )
    else:
        assert res.rule == "ask:content_only"
        assert set(res.candidates) == pids


def _plant_token(src_git_dir: Path, dst: Path) -> None:
    (dst / ".git" / "info").mkdir(parents=True, exist_ok=True)
    shutil.copy(
        src_git_dir / "info" / "bettermemory", dst / ".git" / "info" / "bettermemory"
    )
    location.forget()


def test_t06_a_planted_token_asks_and_reaches_only_a_commit_less_project(
    tmp_path: Path, lab_of: Callable[..., Lab]
) -> None:
    """T6 (E21 a, a2, a3): P's token planted in an unborn repository while
    P's checkout lives is a contradiction, asked, nothing delivered, and
    its writes stay apart; with P's checkout gone, the token alone
    against a project with commits is one witness, asked. A commit-less
    project's token reaches it only while its own checkout is not live."""
    lab = lab_of("p")
    p = make_repo(
        tmp_path / "work" / "core",
        commits=3,
        remote="https://example.invalid/acme/core.git",
    )
    lab.write("secret", p)
    a = unborn(tmp_path / "hostile" / "a")
    _plant_token(p / ".git", a)
    res, brief = lab.view(a)
    assert (res.rule, brief) == ("ask:token_shared_no_commit", [])
    lab.write("w_a", a)
    rp, bp = lab.view(p)
    assert "w_a" not in bp and "w_a" not in lab.admitted(rp, SEARCH_AUTO_SCOPE)

    gone = lab_of("gone")
    pg = make_repo(tmp_path / "work" / "gone", commits=2)
    gone.write("g_secret", pg)
    ag = unborn(tmp_path / "hostile" / "a-later")
    _plant_token(pg / ".git", ag)
    remove(pg)
    res, brief = gone.view(ag)
    assert (res.rule, brief) == ("ask:one_witness", [])

    live = lab_of("q-live")
    q = unborn(tmp_path / "mine" / "q")
    live.write("q_note", q)
    a3 = unborn(tmp_path / "hostile" / "a3")
    _plant_token(q / ".git", a3)
    res, brief = live.view(a3)
    assert (res.rule, brief) == ("ask:token_shared_no_commit", [])

    dead = lab_of("q-gone")
    q2 = unborn(tmp_path / "mine" / "q2")
    dead.write("q_note", q2)
    a2 = unborn(tmp_path / "hostile" / "a2")
    _plant_token(q2 / ".git", a2)
    remove(q2)
    res, brief = dead.view(a2)
    assert (res.rule, brief) == ("join:T", ["q_note"])


def _committed_file(lab: Lab, repo: Path, name: str) -> str:
    fid = lab.init_file(repo, name)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "project file")
    lab.view(repo)
    return fid


def test_t06_a_planted_or_copied_project_file_is_one_witness(
    tmp_path: Path, lab: Lab
) -> None:
    """T6 (E21 b, c, f): P's project file planted in a repository with its
    own commits and no remote, or copied into a commit-less one, is one
    witness, asked, nothing delivered, its writes apart; in a repository
    holding another project's commits it is a contradiction, never
    joined."""
    p = make_repo(
        tmp_path / "work" / "core",
        commits=3,
        remote="https://example.invalid/acme/core.git",
    )
    lab.write("secret", p)
    _committed_file(lab, p, "core")
    b = make_repo(tmp_path / "hostile" / "b", commits=2)
    shutil.copy(p / "bettermemory.toml", b / "bettermemory.toml")
    git(b, "add", "-A")
    git(b, "commit", "-q", "-m", "plant")
    res, brief = lab.view(b)
    assert (res.rule, brief) == ("ask:one_witness", [])
    lab.write("w_b", b)
    assert "w_b" not in lab.view(p)[1]

    c = unborn(tmp_path / "hostile" / "c")
    shutil.copy(p / "bettermemory.toml", c / "bettermemory.toml")
    res, brief = lab.view(c)
    assert (res.rule, brief) == ("ask:one_witness", [])

    o = make_repo(tmp_path / "work" / "other", commits=2)
    lab.write("other_note", o)
    f = clone(o, tmp_path / "hostile" / "f")
    shutil.copy(p / "bettermemory.toml", f / "bettermemory.toml")
    res, brief = lab.view(f)
    assert (res.rule, brief) == ("ask:file_names_other_history", [])


def test_t05_commits_and_remote_join_and_under_a3_ask(
    tmp_path: Path, lab_of: Callable[..., Lab]
) -> None:
    """T5, T2 (E21 d, e): P's commits and remote without the file join
    (the clone-equivalent, D8), asked instead under A3
    (`membership.strict`); P's commits and its committed file join, under
    A3 too (the file is the declaration)."""
    url = "https://example.invalid/acme/core.git"
    lab = lab_of("plain")
    p = make_repo(tmp_path / "work" / "core", commits=3, remote=url)
    lab.write("secret", p)
    _committed_file(lab, p, "core")
    d = clone(p, tmp_path / "hostile" / "d", url=url)
    git(d, "rm", "-q", "bettermemory.toml")
    git(d, "commit", "-q", "-m", "drop the file")
    res, brief = lab.view(d)
    assert (res.rule, brief) == ("join:H+R", ["secret"])
    e = clone(p, tmp_path / "hostile" / "e")
    git(e, "remote", "remove", "origin")
    res, brief = lab.view(e)
    assert (res.rule, brief) == ("join:F+H", ["secret"])

    strict = lab_of("strict", strict=True)
    strict.write("secret", p)
    d2 = clone(p, tmp_path / "hostile" / "d2", url=url)
    git(d2, "rm", "-q", "bettermemory.toml")
    git(d2, "commit", "-q", "-m", "drop the file")
    res, brief = strict.view(d2)
    assert res.rule.startswith("ask:") and brief == []
    lab.policy(strict=True)
    e3 = copy(e, tmp_path / "hostile" / "e3")
    (e3 / ".git" / "info" / "bettermemory").unlink()
    res, brief = lab.view(e3)
    assert (res.rule, brief) == ("join:F+H", ["secret"])


def test_i05a_orphan_branch_carrying_a_commit_less_token_is_decided_by_history(
    tmp_path: Path, lab: Lab
) -> None:
    """I5a (E21 g): a born repository on an orphan branch (HEAD unborn,
    commits on other refs) carrying a commit-less project's token: born,
    so its history decides: with its remote the project it was cloned
    from, silent; without one asked; never the token's project."""
    u0 = unborn(tmp_path / "work" / "unborn0")
    lab.write("p0_note", u0)
    url = "https://example.invalid/acme/qsvc.git"
    qr = make_repo(tmp_path / "work" / "qsvc", commits=2, remote=url)
    lab.write("q_note", qr)
    outs = {}
    for label, keep in (("with_remote", True), ("no_remote", False)):
        oc = clone(qr, tmp_path / "hostile" / f"orphan_{label}", url=url)
        if not keep:
            git(oc, "remote", "remove", "origin")
        git(oc, "checkout", "-q", "--orphan", "fresh")
        shutil.copy(
            u0 / ".git" / "info" / "bettermemory", oc / ".git" / "info" / "bettermemory"
        )
        cap = capture(str(oc), write_token=True, use_cache=False)
        assert cap.head is None and cap.born
        outs[label] = lab.view(oc)
    res, brief = outs["with_remote"]
    assert (res.rule, brief) == ("join:H+R", ["q_note"])
    res, brief = outs["no_remote"]
    assert res.rule.startswith("ask:") and brief == []


# ---------------------------------------------------------------------------
# Templates (E22), folder projects (E23)
# ---------------------------------------------------------------------------


def test_t07_commit_less_template_copies_ask_once_each(
    tmp_path: Path, lab_of: Callable[..., Lab]
) -> None:
    """T7 (E22 a, a2): a commit-less template copied twice while it lives:
    each copy asked once; answered `project new`, three projects, each
    with its own, no question left. The template deleted first: the first
    copy seen takes its line by the token alone, the second is asked."""
    lab = lab_of("live")
    t = unborn(tmp_path / "a" / "template", note="template\n")
    lab.write("t_note", t)
    a1, a2 = copy(t, tmp_path / "a" / "product1"), copy(t, tmp_path / "a" / "product2")
    res, brief = lab.view(a1)
    assert (res.rule, brief) == ("ask:token_shared_no_commit", [])
    lab.answer(a1, "new")
    lab.write("p1_note", a1)
    res, brief = lab.view(a2)
    assert (res.rule, brief) == ("ask:token_shared_no_commit", [])
    lab.answer(a2, "new")
    lab.write("p2_note", a2)
    r1, b1 = lab.view(a1)
    r2, b2 = lab.view(a2)
    assert (b1, b2, lab.view(t)[1]) == (["p1_note"], ["p2_note"], ["t_note"])
    assert r1.lines == () and r2.lines == ()
    assert lab.rows("proposal_opened").count("proposal_opened") == 2

    gone = lab_of("gone")
    t2 = unborn(tmp_path / "a2" / "template", note="template\n")
    gone.write("t_note", t2)
    b1p, b2p = copy(t2, tmp_path / "a2" / "one"), copy(t2, tmp_path / "a2" / "two")
    remove(t2)
    assert gone.view(b1p)[0].rule == "join:T"
    assert gone.view(b2p)[0].rule == "ask:token_shared_no_commit"


def test_t08_template_clones_made_products_with_init_new_are_separate(
    tmp_path: Path, lab: Lab
) -> None:
    """T8 (E22 b): born clones of a template, each made a product with
    `project init --new` and the file committed: separate projects; a
    teammate's fresh clone of a product resolves to it, no question."""
    starter = "https://example.invalid/acme/starter.git"
    s = make_repo(tmp_path / "b" / "starter", commits=2, remote=starter)
    lab.write("starter_note", s)
    prods = {}
    for name in ("alpha", "beta"):
        pd = clone(s, tmp_path / "b" / name, url=starter)
        lab.view(pd)
        lab.init_file(pd, name, new=True)
        git(
            pd,
            "remote",
            "set-url",
            "origin",
            f"https://example.invalid/acme/{name}.git",
        )
        git(pd, "add", "-A")
        git(pd, "commit", "-q", "-m", "project file")
        lab.write(f"{name}_note", pd)
        prods[name] = pd
    ba, bb, bs = (
        lab.view(prods["alpha"])[1],
        lab.view(prods["beta"])[1],
        lab.view(s)[1],
    )
    assert "alpha_note" in ba and "beta_note" not in ba
    assert "beta_note" in bb and "alpha_note" not in bb
    assert bs == ["starter_note"]
    tm = clone(
        prods["alpha"],
        tmp_path / "b" / "teammate-alpha",
        url="https://example.invalid/acme/alpha.git",
    )
    res, brief = lab.view(tm)
    assert res.outcome == SILENT and "alpha_note" in brief and "F" in _codes(res)


def test_t09_t10_template_clone_keeping_or_re_pointing_its_remote(
    tmp_path: Path, lab: Lab
) -> None:
    """T9, T10 (E22 c): a clone keeping the template's remote joins (D8);
    one re-pointed to its own remote holds the history only and is
    asked; the first, re-pointed later, is asked once (L5); answered
    `project new`, the template sees only its own."""
    kit = "https://example.invalid/acme/kit.git"
    s2 = make_repo(tmp_path / "c" / "starter", commits=2, remote=kit)
    lab.write("kit_note", s2)
    k1 = clone(s2, tmp_path / "c" / "keeps", url=kit)
    k2 = clone(s2, tmp_path / "c" / "repointed", url=kit)
    git(k2, "remote", "set-url", "origin", "https://example.invalid/acme/newthing.git")
    commit(k2, "newthing work")
    res, brief = lab.view(k1)
    assert (res.rule, brief) == ("join:H+R", ["kit_note"])
    res, brief = lab.view(k2)
    assert (res.rule, brief) == ("ask:one_witness", [])
    git(k1, "remote", "set-url", "origin", "https://example.invalid/acme/other.git")
    res, _ = lab.view(k1)
    assert res.outcome == ASK and any("now names" in line for line in res.lines)
    lab.answer(k1, "new")
    lab.write("other_note", k1)
    assert lab.view(s2)[1] == ["kit_note"]


def test_t10a_use_this_template_asks_once_offering_init_new(
    tmp_path: Path, lab: Lab
) -> None:
    """T10a (E22 e): the template's tree and committed project file under a
    fresh root commit and a remote of its own: the file is one witness,
    asked once, offering `project init --new`; answered so, the product
    and a teammate's clone are silent with its own memories, and the
    template keeps its own."""
    url_t, url_p = (
        "https://example.invalid/acme/starter-kit.git",
        "https://example.invalid/acme/new-app.git",
    )
    tk = make_repo(tmp_path / "tpl" / "starter-kit", commits=1, remote=url_t)
    for i in range(3):
        (tk / f"lib{i}.py").write_text(f"# library {i}\n" + "VALUE = 1\n" * 30)
    git(tk, "add", "-A")
    git(tk, "commit", "-q", "-m", "library")
    _committed_file(lab, tk, "starter-kit")
    lab.write("kit_secret", tk)
    app = tmp_path / "products" / "new-app"
    shutil.copytree(tk, app, ignore=shutil.ignore_patterns(".git"))
    git(app, "init", "-q", "-b", "main")
    git(app, "add", "-A")
    git(app, "commit", "-q", "-m", "Initial commit")
    git(app, "remote", "add", "origin", url_p)
    res, brief = lab.view(app)
    assert (res.rule, brief) == ("ask:one_witness", [])
    assert any("bettermemory project init --new" in line for line in res.lines)
    lab.init_file(app, "new-app", new=True)
    git(app, "add", "-A")
    git(app, "commit", "-q", "-m", "its own project file")
    lab.write("app_note", app)
    rb, bb = lab.view(app)
    tm = clone(app, tmp_path / "teammate" / "new-app", url=url_p)
    rt, bt = lab.view(tm)
    assert (rb.outcome, bb, rt.outcome, bt) == (
        SILENT,
        ["app_note"],
        SILENT,
        ["app_note"],
    )
    assert lab.view(tk)[1] == ["kit_secret"]
    assert lab.model().pending() == []


def _vault(path: Path) -> Path:
    (path / "Daily").mkdir(parents=True)
    for i in range(6):
        (path / "Daily" / f"day-{i + 1}.md").write_text(f"note {i}\n" * (i + 1))
    (path / ".obsidian").mkdir()
    (path / ".obsidian" / "app.json").write_text("{}\n")
    return path


def test_i29_i30_folder_project_made_moved_copied_and_its_file_copied(
    tmp_path: Path, lab: Lab
) -> None:
    """I29, I30 (E23 a to f): a plain folder with no project file is
    outside any project (global); `project init` makes it a project and a
    subfolder resolves to it; renamed, the same folder object; moved to
    another filesystem or copied, the file and the content; the file
    alone in a folder of other content is one witness, asked."""
    v = _vault(tmp_path / "Documents" / "vault")
    pre = lab.write("pre", v)
    assert (pre.outcome, pre.project) == (GLOBAL_OUTCOME, GLOBAL)
    lab.init_file(v, "vault")
    lab.write("v1", v)
    lab.write("v2", v / "Daily")
    res, brief = lab.view(v / "Daily")
    assert res.rule in ("folder_object", "new_folder") and brief == ["v1", "v2"]
    v2 = move(v, tmp_path / "Archive" / "vault")
    res, brief = lab.view(v2)
    assert (res.rule, brief) == ("folder_object", ["v1", "v2"])
    v3 = move_across(v2, tmp_path / "vol" / "vault")
    res, brief = lab.view(v3)
    assert (res.rule, brief) == ("join:C+F", ["v1", "v2"])
    v4 = copy(v3, tmp_path / "Copies" / "vault")
    res, brief = lab.view(v4)
    assert (res.rule, brief) == ("join:C+F", ["v1", "v2"])
    x = tmp_path / "Downloads" / "other"
    x.mkdir(parents=True)
    (x / "unrelated.txt").write_text("x\n")
    shutil.copy(v3 / "bettermemory.toml", x / "bettermemory.toml")
    res, brief = lab.view(x)
    assert (res.rule, brief) == ("ask:one_witness", [])


def test_i31_nested_folder_projects_and_a_repository_inside_one(
    tmp_path: Path, lab_of: Callable[..., Lab]
) -> None:
    """I31, X4 (E23 g, h, i): an inner folder project is the outer one's
    sub-project: the inner session gets both, the outer brief its own and
    its search reaches the inner one. A repository inside a folder
    project is its own project and the folder's memories reach it, not
    the other way. A Perforce workspace marker changes nothing."""
    lab = lab_of("nested")
    o = tmp_path / "Writing"
    o.mkdir()
    (o / "style.md").write_text("style\n")
    lab.init_file(o, "writing")
    lab.write("outer_note", o)
    i = o / "novel"
    i.mkdir()
    (i / "ch1.md").write_text("one\n")
    lab.init_file(i, "novel")
    lab.write("inner_note", i)
    ri, bi = lab.view(i)
    ro, bo = lab.view(o)
    assert bi == ["inner_note", "outer_note"] and bo == ["outer_note"]
    assert "inner_note" in lab.admitted(ro, SEARCH_AUTO_SCOPE)

    rlab = lab_of("repo")
    wk = tmp_path / "Work"
    wk.mkdir()
    rlab.init_file(wk, "work")
    rlab.write("work_note", wk)
    g = make_repo(wk / "service", commits=2)
    rlab.write("svc_note", g)
    rg, bg = rlab.view(g)
    rw, bw = rlab.view(wk)
    assert bg == ["svc_note", "work_note"] and rg.project != rw.project
    assert bw == ["work_note"]

    plab = lab_of("p4")
    pw = tmp_path / "p4" / "depot-ws"
    pw.mkdir(parents=True)
    (pw / ".p4config").write_text("P4CLIENT=ws\n")
    (pw / "main.c").write_text("int main(){}\n")
    before = plab.write("p4_before", pw)
    plab.init_file(pw, "depot")
    plab.write("p4_note", pw)
    assert before.project == GLOBAL and plab.view(pw)[1] == ["p4_note"]


def test_i31_e23_g_init_inside_a_folder_project_makes_a_nested_folder_project(
    tmp_path: Path, lab: Lab
) -> None:
    """I31 (E23 g), decision B17: `project init` in a folder below a folder
    project's root makes a folder project nested in it, its parent the
    enclosing one; the outer folder keeps its project. The folder's first
    sight is its checkout (the declaration's rung). The inner session gets
    its own memories and the outer's; the outer brief its own, and its
    search reaches the inner one's."""
    o = tmp_path / "Writing"
    o.mkdir()
    (o / "style.md").write_text("style\n")
    lab.init_file(o, "writing")
    outer = lab.write("outer_note", o)
    i = o / "novel"
    i.mkdir()
    (i / "ch1.md").write_text("one\n")
    fid = lab.init_file(i, "novel")
    model = lab.model()
    inner = model.file_binding(fid)
    assert inner is not None and inner != outer.project
    nested = model.project(inner)
    assert nested.folder and nested.parent == outer.project
    assert model.checkout(str(outer.checkout)).project == outer.project
    first = lab.write("inner_note", i)
    assert (first.outcome, first.project, first.rule) == (
        SILENT,
        inner,
        "declared_folder",
    )
    assert first.checkout != outer.checkout
    ri, bi = lab.view(i)
    assert (ri.project, ri.checkout, ri.rule) == (
        inner,
        first.checkout,
        "folder_object",
    )
    assert ri.chain == (inner, outer.project)
    assert bi == ["inner_note", "outer_note"]
    ro, bo = lab.view(o)
    assert (ro.project, bo) == (outer.project, ["outer_note"])
    assert lab.admitted(ro, SEARCH_AUTO_SCOPE) == ["inner_note", "outer_note"]


def test_i31_e23_h_a_repository_inside_a_folder_project_has_it_as_its_parent(
    tmp_path: Path, lab: Lab
) -> None:
    """I31 (E23 h), decisions D13 and B17: a repository whose root lies in
    a folder project is a project of its own whose chain continues with
    the folder project: the folder's memories reach the repository's
    sessions, the repository's do not reach the folder's brief. `project
    init` at the repository's root binds its project; in a folder below
    it, a part of it, and the chain runs part, repository, folder."""
    wk = tmp_path / "Work"
    wk.mkdir()
    lab.init_file(wk, "work")
    work = lab.write("work_note", wk)
    g = make_repo(wk / "service", commits=2)
    svc = lab.write("svc_note", g)
    assert svc.project not in (None, work.project)
    rg, bg = lab.view(g)
    assert rg.chain == (svc.project, work.project)
    assert bg == ["svc_note", "work_note"]
    rw, bw = lab.view(wk)
    assert (rw.project, bw) == (work.project, ["work_note"])
    root_file = lab.init_file(g, "service")
    model = lab.model()
    assert model.file_binding(root_file) == svc.project
    api = g / "api"
    api.mkdir()
    fid = lab.init_file(api, "api")
    part = model.file_binding(fid)
    assert part is not None and model.project(part).parent == svc.project
    assert not model.project(part).folder
    lab.write("api_note", api)
    ra, ba = lab.view(api)
    assert (ra.outcome, ra.project, ra.checkout) == (SILENT, part, svc.checkout)
    assert ra.chain == (part, svc.project, work.project)
    assert ba == ["api_note", "svc_note", "work_note"]
    assert lab.view(g)[1] == ["svc_note", "work_note"]
    assert lab.view(wk)[1] == ["work_note"]


def _restore_in_place(path: Path) -> None:
    """A restore from a backup at the same path: every object new. The
    backup's objects are made while the original's still exist, so none
    of them can be handed a number the original holds (ext4 gives a freed
    inode straight back to the next file made), and the backup is renamed
    into place, which keeps them."""
    backup = copy(path, path.parent / f"{path.name}-backup")
    remove(path)
    move(backup, path)


@pytest.mark.usefixtures("no_birth")
def test_b20_folder_project_without_birth_is_its_own_object_and_rekeys_on_a_new_one(
    tmp_path: Path, lab: Lab
) -> None:
    """Decision B20: a folder project on a filesystem without birth time,
    seen three times: one ``checkout_registered`` row and no
    ``checkout_rekeyed`` row, the same checkout silently each time by its
    object confirmed by its file, and its view live. Restored in place (a
    new inode number), it is re-keyed with its row."""
    v = _vault(tmp_path / "Documents" / "vault")
    lab.init_file(v, "vault")
    first = lab.write("v1", v)
    seen = [lab.view(v) for _ in range(3)]
    assert [(r.outcome, r.checkout, r.rule, b) for r, b in seen] == [
        (SILENT, first.checkout, "folder_object", ["v1"])
    ] * 3
    assert lab.rows("checkout_registered") == ["checkout_registered"]
    assert lab.rows("checkout_rekeyed") == []
    model = lab.model()
    k = model.checkout(str(first.checkout))
    assert k.object_key is not None and k.object_key.birth is None
    assert model.live_view(k) == str(v)
    before = location.object_key(str(v))
    _restore_in_place(v)
    after = location.object_key(str(v))
    assert before is not None and after is not None and before[:2] != after[:2]
    res, brief = lab.view(v)
    assert (res.outcome, res.checkout, brief) == (SILENT, first.checkout, ["v1"])
    assert res.rule.startswith("rekeyed:")
    assert lab.rows("checkout_rekeyed") == ["checkout_rekeyed"]
    assert lab.rows("checkout_registered") == ["checkout_registered"]


@pytest.mark.usefixtures("no_birth")
def test_b20_repository_without_birth_or_token_is_its_own_object_and_rekeys_on_a_new_one(
    tmp_path: Path, lab_of: Callable[..., Lab]
) -> None:
    """Decision B20: a repository on a filesystem without birth time under
    write_token=false, seen again with the same inode number: the same
    checkout silently, one ``checkout_registered`` row and no
    ``checkout_rekeyed`` row. Restored in place (a new inode number), it
    is re-keyed with its row."""
    lab = lab_of(write_token=False)
    r = make_repo(tmp_path / "work" / "proj", commits=2)
    first = lab.write("r1", r)
    seen = [lab.view(r) for _ in range(3)]
    assert [(x.outcome, x.checkout, b) for x, b in seen] == [
        (SILENT, first.checkout, ["r1"])
    ] * 3
    assert {x.rule for x, _b in seen} == {"object+last_head"}
    assert lab.rows("checkout_registered") == ["checkout_registered"]
    assert lab.rows("checkout_rekeyed") == []
    before = _git_dir_key(r)
    _restore_in_place(r)
    assert _git_dir_key(r)[:2] != before[:2]
    res, brief = lab.view(r)
    assert (res.outcome, res.checkout, brief) == (SILENT, first.checkout, ["r1"])
    assert res.rule.startswith("rekeyed:")
    assert lab.rows("checkout_rekeyed") == ["checkout_rekeyed"]
    assert lab.rows("checkout_registered") == ["checkout_registered"]


def test_i30a_git_init_in_a_folder_project_rekeys_its_checkout(
    tmp_path: Path, lab: Lab
) -> None:
    """I30a (E23 j): `git init` in a folder project: the folder checkout
    re-keyed to the git directory, silent, its memories delivered; the
    first commit changes nothing."""
    vt = tmp_path / "notes" / "vault"
    vt.mkdir(parents=True)
    (vt / "ideas.md").write_text("ideas\n")
    lab.init_file(vt, "vault")
    first = lab.write("vault_note", vt)
    git(vt, "init", "-q", "-b", "main")
    location.forget()
    res, brief = lab.view(vt)
    assert (res.rule, res.outcome, res.checkout, brief) == (
        "rekeyed:F+O",
        SILENT,
        first.checkout,
        ["vault_note"],
    )
    assert "checkout_rekeyed" in lab.rows("checkout_rekeyed")
    git(vt, "add", "-A")
    git(vt, "commit", "-q", "-m", "first")
    res, brief = lab.view(vt)
    assert (res.rule, res.checkout, brief) == (
        known_rule(tmp_path),
        first.checkout,
        ["vault_note"],
    )


# ---------------------------------------------------------------------------
# Host migrations (E24), history rewrites (E25)
# ---------------------------------------------------------------------------

_OLD_HOST = "https://github.com/acme/payments.git"
_NEW_HOST = "https://ghe.acme.example/platform/payments.git"


@pytest.mark.parametrize("order", ["clone_first", "repoint_first"])
def test_t11_host_migration_without_the_file_asks_once_per_store(
    tmp_path: Path, lab: Lab, order: str
) -> None:
    """T11 (E24 a, a', b, c): every URL replaced, history unchanged, no
    project file. A fresh clone from the new host seen first holds the
    history only: one question; answered, the next fresh clone and the
    re-pointed checkouts are silent. An existing checkout re-pointed
    first while another still names the old host: one question (L5);
    answered, the rest is silent. One question in the store either way."""
    a = make_repo(tmp_path / "a", commits=3, remote=_OLD_HOST)
    lab.write("m1", a)
    a2 = clone(a, tmp_path / "a2", url=_OLD_HOST)
    lab.view(a2)
    if order == "clone_first":
        n1 = clone(a, tmp_path / "n1", url=_NEW_HOST)
        res, brief = lab.view(n1)
        assert (res.rule, brief) == ("ask:one_witness", [])
        lab.answer(n1, "join")
        n2 = clone(a, tmp_path / "n2", url=_NEW_HOST)
        res, brief = lab.view(n2)
        assert (res.outcome, brief) == (SILENT, ["m1"])
        for k in (a, a2):
            git(k, "remote", "set-url", "origin", _NEW_HOST)
        assert lab.view(a)[0].outcome == lab.view(a2)[0].outcome == SILENT
    else:
        git(a, "remote", "set-url", "origin", _NEW_HOST)
        res, brief = lab.view(a)
        assert (res.outcome, brief) == (ASK, ["m1"])
        lab.answer(a, "join")
        git(a2, "remote", "set-url", "origin", _NEW_HOST)
        r2, _ = lab.view(a2)
        n1 = clone(a, tmp_path / "n1", url=_NEW_HOST)
        r3, b3 = lab.view(n1)
        assert (r2.outcome, r3.outcome, b3) == (SILENT, SILENT, ["m1"])
    assert lab.rows("proposal_opened").count("proposal_opened") == 1


def test_t12_host_migration_with_the_file_is_silent(tmp_path: Path, lab: Lab) -> None:
    """T12 (E24 d): with the project file committed, fresh clones from the
    new host and re-pointed checkouts are silent; no question."""
    a = make_repo(tmp_path / "a", commits=3, remote=_OLD_HOST)
    lab.write("m1", a)
    _committed_file(lab, a, "payments")
    a2 = clone(a, tmp_path / "a2", url=_OLD_HOST)
    lab.view(a2)
    n1 = clone(a, tmp_path / "n1", url=_NEW_HOST)
    res, brief = lab.view(n1)
    assert (res.outcome, brief) == (SILENT, ["m1"])
    n2 = clone(a, tmp_path / "n2", url=_NEW_HOST)
    assert lab.view(n2)[0].outcome == SILENT
    for k in (a, a2):
        git(k, "remote", "set-url", "origin", _NEW_HOST)
    assert lab.view(a)[0].outcome == lab.view(a2)[0].outcome == SILENT
    assert lab.rows("proposal_opened") == []


_LIB = "https://example.invalid/acme/lib.git"


def _repo_with_secret(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    _SEQ[0] += 1
    (path / "secret.txt").write_text(f"NOT-A-REAL-KEY {time.time_ns()} {_SEQ[0]}\n")
    (path / "a.py").write_text(
        "# the loader\n" + "".join(f"LINE_{j} = {j} * 3  # value\n" for j in range(6))
    )
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "start")
    for i in range(4):
        (path / f"m{i}.py").write_text(
            f"# module {i}\n" + "".join(f"V{j} = {i} + {j}  # term\n" for j in range(5))
        )
        git(path, "add", "-A")
        git(path, "commit", "-q", "-m", f"work {i}")
    git(path, "remote", "add", "origin", _LIB)
    return path


_FIXED_DATES = (
    "export GIT_AUTHOR_DATE='1700000000 +0000' GIT_COMMITTER_DATE='1700000000 +0000'"
)


def _rewrite(path: Path, *extra: str) -> None:
    """`git filter-branch` dropping secret.txt from every commit (every
    commit id changes; authors, dates and messages kept unless `extra`
    filters change them), its backup refs removed as filter-repo leaves
    none."""
    git(
        path,
        "filter-branch",
        "-f",
        "--index-filter",
        "git rm -q --cached --ignore-unmatch secret.txt",
        *extra,
        "--",
        "--all",
    )
    for ref in git(
        path, "for-each-ref", "--format=%(refname)", "refs/original"
    ).split():
        git(path, "update-ref", "-d", ref)
    git(path, "reflog", "expire", "--expire=now", "--all")
    git(path, "gc", "-q", "--prune=now")


def test_i26_history_rewritten_in_place_is_the_same_checkout(
    tmp_path: Path, lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I26 (E25 a): every commit id changed in place: the same git
    directory, silent; without birth time the token confirms it."""
    a = _repo_with_secret(tmp_path / "a" / "lib")
    lab.write("m1", a)
    before = git(a, "rev-parse", "HEAD")
    _rewrite(a)
    assert git(a, "rev-parse", "HEAD") != before
    res, brief = lab.view(a)
    assert (res.rule, brief) == (known_rule(tmp_path), ["m1"])
    monkeypatch.setattr(location, "REPORT_BIRTH", False)
    location.forget()
    res, brief = lab.view(a)
    assert (res.rule, brief) == ("object+token", ["m1"])


def test_i27_fresh_clone_of_a_rewritten_history_joins_without_the_file(
    tmp_path: Path, lab_of: Callable[..., Lab]
) -> None:
    """I27 (E25 b, b2, c): a fresh clone of the rewritten history in a store
    whose checkouts never saw it holds none of the recorded commits; its
    commits carry the recorded authors, dates and messages (Hf) and its
    remote: silent. With every date replaced too, the file contents of the
    recorded commits (Hb) and the remote: silent. Control: unrelated
    commits naming the remote are one witness, asked."""
    lab = lab_of("fp")
    src = _repo_with_secret(tmp_path / "b" / "upstream")
    mine = clone(src, tmp_path / "b" / "mine", url=_LIB)
    lab.write("m1", mine)
    _rewrite(src)
    fresh = clone(src, tmp_path / "b" / "fresh", url=_LIB)
    res, brief = lab.view(fresh)
    assert (res.outcome, res.rule, brief) == (SILENT, "join:Hf+R", ["m1"])
    spoof = make_repo(tmp_path / "c" / "spoof", commits=3, remote=_LIB)
    res, brief = lab.view(spoof)
    assert (res.rule, brief) == ("ask:one_witness", [])

    blobs = lab_of("blobs")
    src2 = _repo_with_secret(tmp_path / "b2" / "upstream")
    mine2 = clone(src2, tmp_path / "b2" / "mine", url=_LIB)
    blobs.write("m1", mine2)
    _rewrite(src2, "--env-filter", _FIXED_DATES)
    fresh2 = clone(src2, tmp_path / "b2" / "fresh", url=_LIB)
    res, brief = blobs.view(fresh2)
    assert (res.rule, brief) == ("join:Hb+R", ["m1"])


def test_i28_rewrite_of_dates_and_contents_asks_once_and_with_the_file_is_silent(
    tmp_path: Path, lab_of: Callable[..., Lab]
) -> None:
    """I28 (E25 d, e, c2): every date and every file's content replaced, no
    file: the remote only, one question; answered, the next fresh clone
    is silent by history and remote, one question in the store. With the
    project file committed before a rewrite of the dates: silent.
    Control: unrelated commits with the file copied in and the remote:
    history speaks against them, one question."""
    lab = lab_of("d")
    src = _repo_with_secret(tmp_path / "d" / "upstream")
    mine = clone(src, tmp_path / "d" / "mine", url=_LIB)
    lab.write("m1", mine)
    _rewrite(
        src,
        "--env-filter",
        _FIXED_DATES,
        "--tree-filter",
        "for f in *.py; do echo '# reformatted' >> \"$f\"; done",
    )
    d1 = clone(src, tmp_path / "d" / "fresh1", url=_LIB)
    res, brief = lab.view(d1)
    assert (res.rule, brief) == ("ask:one_witness", [])
    lab.answer(d1, "join")
    d2 = clone(src, tmp_path / "d" / "fresh2", url=_LIB)
    res, brief = lab.view(d2)
    assert (res.rule, brief) == ("join:H+R", ["m1"])
    assert lab.rows("proposal_opened").count("proposal_opened") == 1

    filed = lab_of("e")
    src_e = _repo_with_secret(tmp_path / "e" / "upstream")
    em = clone(src_e, tmp_path / "e" / "mine", url=_LIB)
    filed.write("m1", em)
    _committed_file(filed, em, "lib")
    git(em, "push", "-q", src_e, "main:refs/heads/incoming")
    git(src_e, "merge", "-q", "--ff-only", "incoming")
    _rewrite(src_e, "--env-filter", _FIXED_DATES)
    e1 = clone(src_e, tmp_path / "e" / "fresh", url=_LIB)
    res, brief = filed.view(e1)
    assert (res.outcome, brief) == (SILENT, ["m1"]) and "F" in _codes(res)
    x2 = make_repo(tmp_path / "c2" / "spoof", commits=3, remote=_LIB)
    shutil.copy(em / "bettermemory.toml", x2 / "bettermemory.toml")
    git(x2, "add", "-A")
    git(x2, "commit", "-q", "-m", "plant")
    res, brief = filed.view(x2)
    assert (res.outcome, brief) == (ASK, [])


# ---------------------------------------------------------------------------
# Machines (E26, E36), concurrency (E28), the store restored (E37)
# ---------------------------------------------------------------------------


def test_i36_a_migrated_laptop_rejoins_by_token_and_history(
    tmp_path: Path, lab: Lab
) -> None:
    """I36 (E26 b, d): the store and the state directory carried to
    another machine (other hardware): a new machine id; the repositories
    restored at the same paths as new objects re-resolve as new checkouts
    of their projects, by token and history (a commit-less one by its
    token), silent, and the old machine's last session is offered,
    labelled."""
    x = make_repo(tmp_path / "home" / "work" / "x", commits=2)
    u = unborn(tmp_path / "home" / "work" / "u", note="p\n")
    lab.write("x1", x)
    lab.write("xe", x, kind="episode", session="s-old-machine")
    lab.write("u1", u)
    carried = lab.root.parent / "carried-state"
    shutil.copytree(lab.root / lab.state, carried)
    other = open_machine(carried, hardware=lambda: "HW-NEW-LAPTOP")
    assert other.id != lab.identity.id and other.predecessor == lab.identity.id
    for src in (x, u):
        restored = copy(src, tmp_path / "restore" / src.name)
        remove(src)
        move(restored, src)
    location.forget()
    rx, bx = lab.view(x, identity=other)
    assert (rx.outcome, rx.rule, bx) == (SILENT, "join:H+T", ["x1"])
    hand = lab.handoff(rx, identity=other)
    assert (hand.session_id, hand.rule) == (
        "s-old-machine",
        "inherited_from_non_live_checkout",
    )
    ru, bu = lab.view(u, identity=other)
    assert (ru.outcome, bu) == (SILENT, ["u1"])
    assert "machine_cloned" in lab.rows("machine_cloned")


def test_i35_a_synced_folder_seen_from_two_machines(tmp_path: Path, lab: Lab) -> None:
    """I35 (E36 a, b): a `.git` in a synced folder, two machines sharing
    the store: born, the second machine's sighting is a second checkout
    of the project by token and history, silent, run-state per machine;
    commit-less, the token alone, silent (another machine's checkout is
    never live from here)."""
    second = open_machine(
        lab.root.parent / "second-state",
        hardware=lambda: "HW-MAC-2",
        hostname=lambda: "mac-2",
    )
    assert second.id != lab.identity.id
    s = make_repo(tmp_path / "Library" / "Mobile Documents" / "proj", commits=2)
    lab.write("m1", s)
    lab.write("e1", s, kind="episode", session="s-mac-1")
    res, brief = lab.view(s, identity=second)
    assert (res.rule, brief) == ("join:H+T", ["m1"])
    assert res.checkout != lab.checkout_of("m1")
    assert lab.handoff(res, identity=second).rule != "own_checkout"
    u = unborn(tmp_path / "Dropbox" / "draft", note="a\n")
    lab.write("u1", u)
    res, brief = lab.view(u, identity=second)
    assert (res.rule, brief) == ("join:T", ["u1"])


@contextlib.contextmanager
def evicted(path: Path, *, online: bool) -> Iterator[None]:
    """A file provider holds `path`'s data elsewhere (SF_DATALESS on
    macOS, the recall attributes on Windows); `online` says whether a
    fetch brings it within the budget (E36 c)."""
    st = os.stat(path)
    target = (st.st_dev, st.st_ino)
    real_evicted, real_fetch = location._evicted, location._fetch

    def flagged(s: os.stat_result) -> bool:
        return (s.st_dev, s.st_ino) == target or real_evicted(s)

    location._evicted = flagged  # type: ignore[assignment]
    location._fetch = lambda p, budget: online  # type: ignore[assignment]
    location.forget()
    try:
        yield
    finally:
        location._evicted = real_evicted
        location._fetch = real_fetch
        location.forget()


def test_i21_evicted_head_known_fetched_or_deferred(tmp_path: Path, lab: Lab) -> None:
    """I21 (E36 c1 to c3, D19): HEAD evicted by a file provider. A known
    checkout: its object decides, silent, delivered. First sight with the
    provider online: fetched within the budget, silent. First sight
    offline: that prompt shows nothing from any project and says why
    (L9); once the file arrives, the next prompt resolves it."""
    second = open_machine(
        lab.root.parent / "second-state",
        hardware=lambda: "HW-MAC-2",
        hostname=lambda: "mac-2",
    )
    s = make_repo(tmp_path / "iCloud" / "proj", commits=2)
    lab.write("m1", s)
    with evicted(s / ".git" / "HEAD", online=False):
        res, brief = lab.view(s)
    assert (res.outcome, brief) == (SILENT, ["m1"])
    assert res.rule.endswith("file_evicted")

    s2 = make_repo(tmp_path / "iCloud" / "proj2", commits=2)
    lab.write("m2", s2)
    with evicted(s2 / ".git" / "HEAD", online=True):
        res, brief = lab.view(s2, identity=second)
    assert (res.outcome, brief) == (SILENT, ["m2"])

    s3 = make_repo(tmp_path / "OneDrive" / "proj3", commits=2)
    lab.write("m3", s3)
    with evicted(s3 / ".git" / "HEAD", online=False):
        res, brief = lab.view(s3, identity=second)
    assert (res.outcome, brief) == (DEFERRED, [])
    assert any("evicted" in line and "HEAD" in line for line in res.lines)
    res, brief = lab.view(s3, identity=second)
    assert (res.outcome, brief) == (SILENT, ["m3"])
    assert lab.model(second).health().deferred >= 1


def test_i34_eight_concurrent_resolutions_of_one_new_checkout(
    tmp_path: Path, lab: Lab
) -> None:
    """I34 (E28 a, c): eight sessions resolving one checkout never seen, at
    once, each on its own store connection as the daemon's workers are:
    one checkout, one first-sight row, one answer; and eight token writes
    into one fresh git directory: one wins, every one reads its value."""
    for run in range(3):
        p = make_repo(tmp_path / f"s{run}" / "proj", commits=1)
        location.forget()
        start = threading.Barrier(8)
        out: list[Resolution | BaseException | None] = [None] * 8

        def resolve(i: int, p: Path = p, start: threading.Barrier = start) -> None:
            with Store(lab.root / "store") as mine:
                start.wait()
                try:
                    out[i] = resolve_caller(
                        mine, lab.config, str(p), identity=lab.identity
                    ).resolution
                except BaseException as exc:  # noqa: BLE001
                    out[i] = exc

        threads = [threading.Thread(target=resolve, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        answers = {r.checkout for r in out if isinstance(r, Resolution)}
        assert all(isinstance(r, Resolution) for r in out), out
        assert len(answers) == 1
        here = [
            k
            for k in lab.model().checkouts()
            if any(os.path.realpath(v.path) == os.path.realpath(p) for v in k.views)
        ]
        assert len(here) == 1
    assert lab.rows("resolution_new").count("resolution_new") == 3

    u = unborn(tmp_path / "tok" / "proj")
    gate = threading.Barrier(8)
    wrote: list[checkout_token.TokenWrite | None] = [None] * 8

    def write(i: int) -> None:
        gate.wait()
        wrote[i] = checkout_token.write(u / ".git", policy=True, bare=False)

    threads = [threading.Thread(target=write, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    statuses = [w.status for w in wrote if w is not None]
    assert statuses.count("written") == 1 and statuses.count("exists") == 7
    assert len({w.token for w in wrote if w is not None}) == 1


def test_i38_store_restored_from_an_older_backup(tmp_path: Path, lab: Lab) -> None:
    """I38 (E37 c): the store restored from a backup taken before a
    checkout was first seen: that checkout is seen again for the first
    time and resolves by its token and history, silent."""
    p = make_repo(tmp_path / "work" / "proj", commits=2)
    lab.write("m1", p)
    lab.close()
    backup = lab.root.parent / "store-backup"
    shutil.copytree(lab.root / "store", backup)
    lab.store = Store(lab.root / "store")
    c = copy(p, tmp_path / "later" / "proj")
    commit(c, "work after the backup")
    lab.write("m_after", c)
    lab.close()
    shutil.rmtree(lab.root / "store")
    shutil.copytree(backup, lab.root / "store")
    lab.store = Store(lab.root / "store")
    del lab.records["m_after"]
    location.forget()
    res, brief = lab.view(c)
    assert (res.outcome, brief) == (SILENT, ["m1"])
    assert _codes(res) >= {"H", "T"}


def test_x13_why_names_the_rows_that_decided_a_record(tmp_path: Path, lab: Lab) -> None:
    """X13 (E37 d, e): `bettermemory project why` for a record written in
    a checkout that was asked about and answered names the project, the
    checkout and the rule, and lists the rows, the answer among them; for
    a global record it says global."""
    url = "https://example.invalid/acme/proj.git"
    q = make_repo(tmp_path / "q" / "proj", commits=2, remote=url)
    lab.write("q1", q)
    commit(q, "an unrecorded tip")
    s = clone(q, tmp_path / "s" / "proj", "--depth", "1", url=url)
    assert lab.view(s)[0].outcome == ASK
    lab.answer(s, "join")
    lab.write("s1", s)
    why = lab.model().why(lab.records["s1"].id)
    kinds = [r.kind for r in why.rows]
    assert why.project == lab.project_of("q1") and why.checkout is not None
    assert {"proposal_opened", "proposal_answered", "checkout_registered"} <= set(kinds)
    outside = tmp_path / "outside"
    outside.mkdir()
    lab.write("g1", outside)
    assert lab.model().why(lab.records["g1"].id).project == GLOBAL


# ---------------------------------------------------------------------------
# GIT_DIR and separate git directories (E30), bare and mirror (E31)
# ---------------------------------------------------------------------------


def test_i22_git_dir_and_work_tree(
    tmp_path: Path, lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I22 (E30 a, b): a session under GIT_DIR and GIT_WORK_TREE: the token
    goes to $GIT_DIR/info and nothing into the tree, the next session is
    the same checkout by its object, and the capture keeps no cache (the
    signature declines). The same tree without the variables is outside
    any repository."""
    gd = tmp_path / "dot" / "repo.git"
    wt = tmp_path / "dot" / "home"
    wt.mkdir(parents=True)
    sh("git", "init", "-q", "-b", "main", "--bare", gd)
    monkeypatch.setenv("GIT_DIR", str(gd))
    monkeypatch.setenv("GIT_WORK_TREE", str(wt))
    git(wt, "config", "core.bare", "false")
    (wt / ".zshrc").write_text("export X=1\n")
    git(wt, "add", ".zshrc")
    git(wt, "commit", "-q", "-m", "dotfiles")
    lab.write("d1", wt)
    assert (gd / "info" / "bettermemory").is_file()
    assert sorted(p.name for p in wt.iterdir()) == [".zshrc"]
    res, brief = lab.view(wt)
    assert (res.rule, brief) == (known_rule(tmp_path), ["d1"])
    assert location.signature(str(wt)) is None
    monkeypatch.delenv("GIT_DIR")
    monkeypatch.delenv("GIT_WORK_TREE")
    location.forget()
    res, brief = lab.view(wt)
    assert (res.rule, brief) == ("outside_repository", [])


def test_i22_separate_git_dir_holds_the_token_and_names_one_checkout(
    tmp_path: Path, lab: Lab
) -> None:
    """I22 (E30 c, d, E19 f): `--separate-git-dir`: the token is written in
    the separate git directory and the working tree gets nothing; a second
    tree whose `.git` file names the same git directory is the same
    checkout at a second path; the first tree moved, the same checkout."""
    sep = tmp_path / "sep" / "git" / "app.git"
    sep.parent.mkdir(parents=True)
    w1 = tmp_path / "sep" / "tree1" / "app"
    w1.parent.mkdir(parents=True)
    sh("git", "init", "-q", "-b", "main", f"--separate-git-dir={sep}", w1)
    (w1 / "main.py").write_text("print(1)\n")
    git(w1, "add", "-A")
    git(w1, "commit", "-q", "-m", "one")
    lab.write("s1", w1)
    assert (sep / "info" / "bettermemory").is_file() and (w1 / ".git").is_file()
    assert sorted(p.name for p in w1.iterdir()) == [".git", "main.py"]
    w2 = copy(w1, tmp_path / "sep" / "tree2" / "app")
    res, _ = lab.view(w2)
    assert (res.rule, res.checkout) == (known_rule(tmp_path), lab.checkout_of("s1"))
    w3 = move(w1, tmp_path / "sep" / "moved" / "app")
    res, brief = lab.view(w3)
    assert (res.rule, brief) == (known_rule(tmp_path), ["s1"])


def test_i23_bare_mirror_and_worktrees_of_a_bare_repository(
    tmp_path: Path, lab: Lab
) -> None:
    """I23 (E31 a to c): a session inside a bare clone or a mirror: history
    and remote join it and no token is written (skipped:bare); worktrees
    added from a fresh bare clone: the first joins by history and remote
    with its token in the bare repository's worktrees folder, the second
    by the shared object store."""
    url = "https://example.invalid/acme/srv.git"
    p = make_repo(tmp_path / "work" / "srv", commits=3, remote=url)
    lab.write("m1", p)
    b = clone(p, tmp_path / "server" / "srv.git", "--bare", url=url)
    res, brief = lab.view(b)
    assert (res.rule, brief) == ("join:H+R", ["m1"])
    assert not (b / "info" / "bettermemory").exists()
    assert capture(str(b), write_token=True).token_status == "skipped:bare"
    m = clone(p, tmp_path / "mirror" / "srv.git", "--mirror", url=url)
    res, _ = lab.view(m)
    assert res.rule == "join:H+R" and not (m / "info" / "bettermemory").exists()
    b2 = clone(p, tmp_path / "wt" / "srv.git", "--bare", url=url)
    w1, w2 = tmp_path / "wt" / "main", tmp_path / "wt" / "feature"
    git(b2, "worktree", "add", "-q", w1, "main")
    git(b2, "worktree", "add", "-q", "-b", "feature", w2)
    res, _ = lab.view(w1)
    assert res.rule == "join:H+R"
    assert (b2 / "worktrees" / "main" / "info" / "bettermemory").is_file()
    assert not (b2 / "info" / "bettermemory").exists()
    res, brief = lab.view(w2)
    assert (res.rule, brief) == (
        known_rule(tmp_path, "common_object", "pointer"),
        ["m1"],
    )


@pytest.mark.usefixtures("no_birth")
def test_i23_without_birth_worktrees_of_a_bare_repository_join_by_their_pointers(
    tmp_path: Path, lab: Lab
) -> None:
    """I23 (E31 c, E15) without birth time: the first worktree of a fresh
    bare clone joins by history and remote; the second carries no token
    of the first's, and its git directory beside the first's under the
    bare repository's ``worktrees/`` proves the object store they share
    (``common_object+pointer``). The bare repository, seen after them, is
    proven the same way: its own git directory is the one their pointers
    name."""
    url = "https://example.invalid/acme/srv.git"
    p = make_repo(tmp_path / "work" / "srv", commits=3, remote=url)
    lab.write("m1", p)
    b2 = clone(p, tmp_path / "wt" / "srv.git", "--bare", url=url)
    w1, w2 = tmp_path / "wt" / "main", tmp_path / "wt" / "feature"
    git(b2, "worktree", "add", "-q", w1, "main")
    git(b2, "worktree", "add", "-q", "-b", "feature", w2)
    r1, _ = lab.view(w1)
    assert r1.rule == "join:H+R"
    r2, brief = lab.view(w2)
    assert (r2.rule, r2.project, brief) == ("common_object+pointer", r1.project, ["m1"])
    assert r2.checkout != r1.checkout
    rb, brief = lab.view(b2)
    assert (rb.rule, rb.project, brief) == ("common_object+pointer", r1.project, ["m1"])
    assert not (b2 / "info" / "bettermemory").exists()


def test_t17_clone_of_a_shared_bare_repository_by_path(
    tmp_path: Path, lab: Lab
) -> None:
    """T17 (E31 d): a clone from a shared bare repository's path: its
    origin names a path where a checkout of the project was seen (a local
    remote key) and it holds the history: silent."""
    url = "https://example.invalid/acme/srv.git"
    p = make_repo(tmp_path / "work" / "srv", commits=3, remote=url)
    lab.write("m1", p)
    b = clone(p, tmp_path / "server" / "srv.git", "--bare", url=url)
    lab.view(b)
    c = tmp_path / "dev" / "srv"
    c.parent.mkdir(parents=True)
    # By path, so origin is the path (the remote key under test), and
    # without hard links: a local clone links the packs into place and
    # reads them back at once, and on a cifs mount the read-back can
    # miss a link just made (the checkout token writer re-reads for the
    # same reason), which left the clone's refs pointing at objects it
    # could not find.
    sh("git", "clone", "-q", "--no-hardlinks", b, c)
    res, brief = lab.view(c)
    assert (res.rule, brief) == ("join:H+R", ["m1"])


# ---------------------------------------------------------------------------
# Shallow clones (E32)
# ---------------------------------------------------------------------------


def test_i24_shallow_then_unshallowed_is_answered_by_evidence(
    tmp_path: Path, lab: Lab
) -> None:
    """I24, I25 (E32 a to d): a shallow clone at an unrecorded tip without
    the file: the remote only, asked; unshallowed, history and remote
    answer the question by themselves (L4), what was written there joins
    the project. A shallow clone at a recorded tip: history and remote. A
    shallow clone with the project file committed: the file and the
    remote stand in for history: silent."""
    url = "https://example.invalid/acme/ci.git"
    p = make_repo(tmp_path / "work" / "ci", commits=3, remote=url)
    lab.write("m1", p)
    commit(p, "an unrecorded tip")
    s = clone(p, tmp_path / "runner" / "ci", "--depth", "1")
    src = git(s, "remote", "get-url", "origin")
    git(s, "remote", "set-url", "origin", url)
    res, brief = lab.view(s)
    assert (res.rule, brief) == ("ask:one_witness", [])
    lab.write("ci_note", s)
    git(s, "remote", "set-url", "origin", src)
    git(s, "fetch", "-q", "--unshallow")
    git(s, "remote", "set-url", "origin", url)
    res, brief = lab.view(s)
    assert (res.outcome, brief) == (SILENT, ["ci_note", "m1"])
    assert any("joined it" in line for line in res.lines)
    assert "proposal_answered" in lab.rows("proposal_answered")

    lab.view(p)
    t = clone(p, tmp_path / "runner2" / "ci", "--depth", "1", url=url)
    res, brief = lab.view(t)
    assert res.rule == "join:H+R" and "m1" in brief
    lab.init_file(p, "ci")
    git(p, "add", "-A")
    git(p, "commit", "-q", "-m", "project file")
    commit(p, "another unrecorded tip")
    f = clone(p, tmp_path / "runner3" / "ci", "--depth", "1", url=url)
    res, brief = lab.view(f)
    assert res.rule == "join:F+R" and "m1" in brief


def test_t13_ci_runners_ask_once_then_the_declared_remote_speaks(
    tmp_path: Path, lab: Lab
) -> None:
    """T13 (E32 e, f): CI runners, a shallow clone at a new tip each run,
    no file: the first asked; answered `join`, the remote is declared the
    project's and the next runners are silent. The declared remote does
    not speak where history does (unrelated commits) nor for a commit-less
    repository naming it."""
    url = "https://example.invalid/acme/runner-target.git"
    w = make_repo(tmp_path / "work" / "target", commits=3, remote=url)
    lab.write("t1", w)
    outcomes = []
    for run in range(3):
        commit(w, f"the commit CI run {run} tests")
        x = clone(w, tmp_path / f"ci-run-{run}" / "target", "--depth", "1", url=url)
        res, _ = lab.view(x)
        outcomes.append(res.outcome)
        if res.outcome == ASK:
            lab.answer(x, "join")
    assert outcomes == [ASK, SILENT, SILENT]
    assert "remote_declared" in lab.rows("remote_declared")
    z = make_repo(tmp_path / "other" / "target", commits=2, remote=url)
    y = unborn(tmp_path / "unborn" / "target", remote=url)
    rz, bz = lab.view(z)
    ry, by = lab.view(y)
    assert (rz.outcome, bz, ry.outcome, by) == (ASK, [], ASK, [])


# ---------------------------------------------------------------------------
# Monorepos, submodules, sparse checkouts (E18, E35)
# ---------------------------------------------------------------------------


def test_x05_monorepo_with_nested_files_delivers_up_the_chain(
    tmp_path: Path, lab: Lab
) -> None:
    """X5, T2 (E35 a to c): nested project files name sub-projects of the
    repository's project: a session in packages/api gets api's and the
    root's, never web's; at the root the brief has the root's and search
    reaches the sub-projects; a teammate's fresh clone finds the same
    sub-projects by their ids, silent."""
    url = "https://example.invalid/acme/mono.git"
    m = make_repo(tmp_path / "work" / "mono", commits=2, remote=url)
    for pkg in ("api", "web"):
        (m / "packages" / pkg).mkdir(parents=True)
        (m / "packages" / pkg / "main.py").write_text(f"# {pkg}\n")
    lab.init_file(m, "mono")
    lab.init_file(m / "packages" / "api", "api")
    lab.init_file(m / "packages" / "web", "web")
    git(m, "add", "-A")
    git(m, "commit", "-q", "-m", "project files")
    model = lab.model()
    mono = lab.caller(m).resolution.project
    for pkg in ("api", "web"):
        # `project init` in a package declared a part of the repository's
        # project (B17); the checkout keeps the repository's.
        found = project_file.parse(m / "packages" / pkg / "bettermemory.toml")
        assert isinstance(found, project_file.ProjectFile)
        part = model.file_binding(found.id)
        assert part not in (None, mono)
        assert model.project(str(part)).parent == mono
    lab.write("root_note", m)
    lab.write("api_note", m / "packages" / "api")
    lab.write("web_note", m / "packages" / "web")
    _, ba = lab.view(m / "packages" / "api")
    assert ba == ["api_note", "root_note"]
    rr, br = lab.view(m)
    assert br == ["root_note"]
    assert {"api_note", "web_note"} <= set(lab.admitted(rr, SEARCH_AUTO_SCOPE))
    c = clone(m, tmp_path / "teammate" / "mono", url=url)
    rc, bc = lab.view(c / "packages" / "web")
    assert (rc.outcome, bc) == (SILENT, ["root_note", "web_note"])


def test_x06_nested_file_of_another_repository_asks_until_answered(
    tmp_path: Path, lab: Lab
) -> None:
    """X6 (E35 e, f, g): a nested file whose id is bound to another
    repository's project: asked (L7), the session the enclosing project
    meanwhile, nothing of the other delivered; answered `project join`,
    the folder is the other project; answered `project init --new`, a
    new sub-project of the monorepo with an id of its own."""
    url = "https://example.invalid/acme/mono.git"
    m = make_repo(tmp_path / "work" / "mono", commits=2, remote=url)
    (m / "packages" / "api").mkdir(parents=True)
    lab.init_file(m, "mono")
    lab.init_file(m / "packages" / "api", "api")
    git(m, "add", "-A")
    git(m, "commit", "-q", "-m", "project files")
    lab.write("root_note", m)
    api, _ = lab.view(m / "packages" / "api")
    o = make_repo(
        tmp_path / "work" / "other",
        commits=2,
        remote="https://example.invalid/acme/other.git",
    )
    lab.init_file(o, "other")
    lab.write("other_note", o)
    tools = m / "packages" / "tools"
    tools.mkdir()
    shutil.copy(o / "bettermemory.toml", tools / "bettermemory.toml")
    rt, bt = lab.view(tools)
    assert rt.outcome == ASK and "other_note" not in bt and "root_note" in bt
    assert any(
        "which is not part of" in line
        and "bettermemory project init --new (in that folder)" in line
        for line in rt.lines
    )
    asked = [
        p
        for p in lab.model().pending()
        if p.kind == membership.SUBPROJECT_CONTRADICTION
    ]
    assert len(asked) == 1 and lab.view(tools)[0].proposal == asked[0].id
    lab.answer(tools, "join")
    rj, bj = lab.view(tools)
    assert (rj.outcome, bj) == (SILENT, ["other_note"])
    assert lab.model().pending() == []

    tools2 = m / "packages" / "tools2"
    tools2.mkdir()
    shutil.copy(o / "bettermemory.toml", tools2 / "bettermemory.toml")
    before = (tools2 / "bettermemory.toml").read_text()
    assert lab.view(tools2)[0].outcome == ASK
    lab.init_file(tools2, "tools2", new=True)
    lab.write("tools2_note", tools2)
    rn, bn = lab.view(tools2)
    assert (tools2 / "bettermemory.toml").read_text() != before
    assert (rn.outcome, bn) == (SILENT, ["root_note", "tools2_note"])
    model = lab.model()
    assert (
        model.project(str(rn.project)).parent == model.project(str(api.project)).parent
    )


def test_b17_init_new_below_the_root_makes_a_part_and_keeps_the_checkout(
    tmp_path: Path, lab: Lab
) -> None:
    """Decision B17: `project init --new` below a repository's root with no
    question open makes the folder a part with a fresh id, a sub-project
    of the enclosing project; the checkout is not split and keeps its
    project and token, and no separation is logged."""
    m = make_repo(tmp_path / "work" / "mono", commits=2)
    root = lab.write("root_note", m)
    token = (m / ".git" / "info" / "bettermemory").read_text()
    pkg = m / "packages" / "cli"
    pkg.mkdir(parents=True)
    fid = lab.init_file(pkg, "cli", new=True)
    model = lab.model()
    part = model.file_binding(fid)
    assert part not in (None, root.project)
    assert model.project(str(part)).parent == root.project
    assert model.checkout(str(root.checkout)).project == root.project
    assert (m / ".git" / "info" / "bettermemory").read_text() == token
    assert lab.rows("resolution_new") == ["resolution_new"]
    lab.write("cli_note", pkg)
    rp, bp = lab.view(pkg)
    assert (rp.outcome, rp.project, rp.checkout, bp) == (
        SILENT,
        part,
        root.checkout,
        ["cli_note", "root_note"],
    )
    rr, br = lab.view(m)
    assert (rr.project, br) == (root.project, ["root_note"])
    assert "cli_note" in lab.admitted(rr, SEARCH_AUTO_SCOPE)


def test_b17_init_rebind_below_the_root_keeps_the_part_or_makes_one(
    tmp_path: Path, lab: Lab
) -> None:
    """Decision B17: `project init --rebind` below a repository's root
    binds the fresh file to the part the replaced file named; over a file
    that names no project (it does not parse, B1) it makes a new part of
    the enclosing project. The checkout keeps its project either way."""
    m = make_repo(tmp_path / "work" / "mono", commits=2)
    root = lab.write("root_note", m)
    api = m / "packages" / "api"
    api.mkdir(parents=True)
    first = lab.init_file(api, "api")
    model = lab.model()
    part = model.file_binding(first)
    assert part is not None and model.project(part).parent == root.project
    lab.write("api_note", api)
    again = lab.init_file(api, "api")
    assert again != first and model.file_binding(again) == part
    ra, ba = lab.view(api)
    assert (ra.project, ba) == (part, ["api_note", "root_note"])
    broken = m / "packages" / "broken"
    broken.mkdir()
    (broken / "bettermemory.toml").write_text("[project\n")
    fresh = lab.init_file(broken, "broken")
    made = model.file_binding(fresh)
    assert made not in (None, part, root.project)
    assert model.project(str(made)).parent == root.project
    assert model.checkout(str(root.checkout)).project == root.project
    assert lab.view(broken)[0].project == made


def test_i32_submodule_and_sparse_checkout(tmp_path: Path, lab: Lab) -> None:
    """I32 (E18 a to d): a submodule is its own repository, the project it
    was cloned from by lineage; the superproject moved, both continue by
    their objects; a sparse clone is the monorepo's project; a monorepo is
    one project whatever package a session stands in."""
    engine = make_repo(tmp_path / "libs" / "engine", commits=2)
    lab.write("lib_note", engine)
    game = make_repo(tmp_path / "work" / "game", commits=1)
    git(
        game,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        engine.as_uri(),
        "engine",
    )
    git(game, "commit", "-q", "-m", "add engine")
    lab.write("super_note", game)
    lab.write("sub_note", game / "engine")
    assert lab.view(game)[1] == ["super_note"]
    assert lab.view(game / "engine")[1] == ["lib_note", "sub_note"]
    moved = move(game, tmp_path / "moved" / "game")
    rs, bs = lab.view(moved)
    rsub, bsub = lab.view(moved / "engine")
    assert (rs.rule, rsub.rule) == (known_rule(tmp_path), known_rule(tmp_path))
    assert bs == ["super_note"] and "sub_note" in bsub

    mono = make_repo(tmp_path / "mono" / "repo", commits=1)
    for pkg in ("api", "web"):
        (mono / pkg).mkdir()
        (mono / pkg / "main.py").write_text(f"# {pkg}\n")
    git(mono, "add", "-A")
    git(mono, "commit", "-q", "-m", "packages")
    lab.write("mono_note", mono / "api")
    sparse = clone(mono, tmp_path / "sparse" / "repo", "--no-checkout")
    git(sparse, "sparse-checkout", "set", "api")
    git(sparse, "checkout", "-q", "main")
    assert not (sparse / "web").exists()
    assert "mono_note" in lab.view(sparse / "api")[1]
    lab.write("web_note", mono / "web")
    _, ba = lab.view(mono / "api")
    _, bw = lab.view(mono / "web")
    assert {"mono_note", "web_note"} <= set(ba) and ba == bw


# ---------------------------------------------------------------------------
# Nothing readable (I20, L8, decision B6)
# ---------------------------------------------------------------------------


def test_i20_a_cwd_that_cannot_be_read_shows_nothing_and_keeps_its_writes(
    tmp_path: Path, lab: Lab
) -> None:
    """I20 (L8, decision B6): a cwd that does not exist (a dropped mount):
    INDETERMINATE, nothing from any project, the line says why; a write
    there lands in a provisional project of its own, never global, and no
    surface delivers it."""
    p = make_repo(tmp_path / "work" / "proj", commits=1)
    lab.write("m1", p)
    gone = tmp_path / "mnt" / "dropped" / "proj"
    res, brief = lab.view(gone)
    assert (res.outcome, res.project, brief) == (INDETERMINATE, None, [])
    assert any("cannot tell what" in line for line in res.lines)
    assert "resolution_indeterminate" in lab.rows("resolution_indeterminate")
    lab.write("lost", gone)
    home = lab.model().home(lab.records["lost"].id)
    assert home is not None and home.unresolved and home.project != GLOBAL
    rp, bp = lab.view(p)
    assert "lost" not in bp and "lost" not in lab.admitted(rp, SEARCH_AUTO_SCOPE)
    nowhere = lab.caller(None).resolution
    assert nowhere.outcome == INDETERMINATE


def test_i20_a_process_that_cannot_open_its_machine_keeps_its_writes(
    tmp_path: Path, local_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decision B6 where the machine is what cannot be read: a process
    whose state directory cannot be opened (a file stands where it should
    be, as when the shim serves in process because no daemon could start)
    resolves its caller INDETERMINATE under `NO_MACHINE`, with the line
    saying why, and a write there lands in the provisional project of that
    location, the same one at the next write, never global and never
    refused. A hook that could not read its own cwd is resolved the same
    way, under the same id."""
    p = make_repo(tmp_path / "work" / "proj", commits=1)
    blocker = local_tmp_path / "blocker"
    blocker.write_text("not a directory")
    monkeypatch.setenv("BETTERMEMORY_STATE_DIR", str(blocker / "state"))
    location.forget()
    store = Store(local_tmp_path / "store")
    cfg = Config()
    homes = []
    for label in ("first", "second"):
        who = resolve_caller(store, cfg, str(p), session="s")
        assert who.location.machine == NO_MACHINE
        assert (who.resolution.outcome, who.resolution.project) == (INDETERMINATE, None)
        assert any(
            "state directory could not be opened" in line
            for line in who.resolution.lines
        )
        with store.batch():
            m = store.write(
                content=f"memory {label}",
                scopes=["tools"],
                session="s",
                home=who.home(),
            )
            who.add_head_witness("write")
        homes.append(Membership(store, NO_MACHINE).home(m.id))
    first, second = homes
    assert first is not None and second is not None
    assert first.unresolved and first.project != GLOBAL
    assert (first.project, first.checkout) == (second.project, second.checkout)
    assert not (blocker / "state").exists()
    hooked = resolve_hook(
        store,
        cfg,
        str(p),
        {"location": {"state": INDETERMINATE, "probe": True, "cwd": str(p)}},
        session="s",
    )
    assert isinstance(hooked, Resolution) and hooked.outcome == INDETERMINATE


# ---------------------------------------------------------------------------
# Containers: a location the daemon cannot read (E10, design 8.7)
# ---------------------------------------------------------------------------

_DEVC = "https://example.invalid/acme/devc.git"


@dataclass
class Container:
    """A container's half of the hook client, played on this host: the
    capture runs at `real` (the container's files, which this test can
    read) and the location token names `mount`, a path the daemon cannot
    read, with the objects a virtiofs bind mount reports (another device
    and inode, no birth time) and the container's hostname. Its answers to
    the daemon's questions are read from its own repository; a view at
    its own mount is live there."""

    real: Path
    mount: str
    identity: MachineIdentity
    hostname: str = "container-a"
    device: str = "4242"

    def _inside(self, path: str) -> str:
        for base in {str(self.real), os.path.realpath(self.real)}:
            if path == base or path.startswith(base + os.sep):
                return self.mount + path[len(base) :]
        return path

    def token(self, cap: location.Capture) -> dict[str, Any]:
        raw = cap.to_token()
        for name in ("cwd", "root", "git_dir", "common_dir"):
            if isinstance(raw[name], str):
                raw[name] = self._inside(raw[name])
        for f in raw["files"]:
            f["dir"], f["path"] = self._inside(f["dir"]), self._inside(f["path"])
            f["object"] = None
        for i, name in enumerate(
            ("object", "common_object", "root_object", "cwd_object")
        ):
            if raw[name] is not None:
                raw[name] = [self.device, str(9_000_000 + i), None]
        raw["hostname"] = self.hostname
        return raw

    def probe(self) -> dict[str, Any]:
        return {
            "v": location.TOKEN_VERSION,
            "probe": True,
            "cwd": self.mount,
            "cwd_object": [self.device, "1", None],
            "hostname": self.hostname,
        }

    def answer(
        self,
        cap: location.Capture,
        question: Mapping[str, Any],
        answers: Mapping[str, Any],
    ) -> dict[str, Any]:
        views = [v for v in question.get("views") or () if isinstance(v, Mapping)]
        out = answer({k: v for k, v in question.items() if k != "views"}, answers, cap)
        states = dict(out.get("views") or {})
        for v in views:
            path = str(v["path"])
            mine = path == self.mount or path.startswith(self.mount + "/")
            states[path] = (
                ("live" if self.real.exists() else "gone") if mine else "gone"
            )
        out["views"] = states
        return out

    def post(self, lab: Lab, *, session: str = "s-container") -> Resolution:
        """The hook client's rounds (`_daemon_client._membership_rounds`)
        against `location.resolve_hook`."""
        cap = capture(
            str(self.real),
            write_token=lab.config.membership.write_token,
            use_cache=False,
        )
        posted: dict[str, Any] = {"location": self.probe()}
        answers: dict[str, Any] = {}
        for _ in range(location.EXCHANGE_ROUNDS + 1):
            out = resolve_hook(
                lab.store, lab.config, self.mount, posted, session=session
            )
            if isinstance(out, Resolution):
                return out
            assert isinstance(out, Exchange)
            q = out.question
            if q.get("location") or q.get("machine"):
                posted = {
                    "location": self.token(cap),
                    "machine": identity_to_json(self.identity),
                }
                continue
            answers = self.answer(cap, q, answers)
            posted = {
                "location": self.token(cap),
                "machine": identity_to_json(self.identity),
                "answers": answers,
            }
        raise AssertionError("the exchange did not settle")


def _container_machine(lab: Lab, name: str) -> MachineIdentity:
    """The machine of a container, its state directory beside the lab's
    on local disk."""
    return open_machine(
        lab.root.parent / f"{name}-state",
        hardware=lambda: f"HW-{name}",
        hostname=lambda: name,
    )


@pytest.mark.usefixtures("no_birth")
def test_b18_a_client_answers_a_view_question_by_the_same_rule(
    tmp_path: Path, lab_of: Callable[..., Lab]
) -> None:
    """Decision B18, design 8.7: the daemon's question about a view it
    cannot read names the checkout's last HEAD and init stamps beside its
    object and token, and the client answers it by the rule rung 2
    applies: a commit-less checkout by its stamps, a born one by its last
    HEAD held; a question without them finds nothing to confirm."""
    lab = lab_of(write_token=False)
    u = unborn(tmp_path / "c" / "unborn")
    lab.write("u1", u)
    b = make_repo(tmp_path / "c" / "born", commits=2)
    lab.write("b1", b)
    commit(b)
    model = lab.model()
    for label, where in (("u1", u), ("b1", b)):
        k = model.checkout(str(lab.checkout_of(label)))
        view = k.views[0]
        with pytest.raises(location.NeedWitnesses) as need:
            location.AnsweredLiveness({})(k, view)
        question = need.value.question
        [asked] = question["views"]
        assert asked["last_head"] == k.last_head
        assert asked["stamps"] == [list(s) for s in k.stamps]
        cap = capture(str(where), write_token=False)
        answers = answer(question, {}, cap)
        assert answers["views"] == {view.path: "live"}
        assert location.AnsweredLiveness(answers)(k, view) == membership.LIVE
        bare = {"views": [{**asked, "last_head": "HEAD", "stamps": [["x", "y"]]}]}
        assert answer(bare, {}, cap)["views"] == {view.path: "replaced"}


def test_i39_a_bind_mounted_repository_resolves_from_the_location_token(
    tmp_path: Path, lab: Lab
) -> None:
    """I39 (E10 a): the host repository bind-mounted at /workspaces/proj
    (virtiofs: another device and inode, no birth time). The daemon
    cannot read the path; from the location token and the commits the
    container checks, the host checkout's token and the history make it a
    checkout of the project for the container's machine, silent, the
    project's memory delivered."""
    h = make_repo(tmp_path / "host" / "proj", commits=2, remote=_DEVC)
    lab.write("h1", h)
    host_k = lab.checkout_of("h1")
    box = Container(h, "/workspaces/proj", _container_machine(lab, "container-a"))
    res = box.post(lab)
    assert res.outcome == SILENT and {"H", "T"} <= _codes(res)
    assert res.checkout != host_k and res.project == lab.project_of("h1")
    assert lab.admitted(res) == ["h1"]
    k = lab.model().checkout(str(res.checkout))
    assert k.machine == box.identity.id
    again = box.post(lab)
    assert (again.outcome, again.checkout) == (SILENT, res.checkout)


def test_i42_an_rc3_client_posting_a_container_path_reads_nothing(
    tmp_path: Path, lab: Lab
) -> None:
    """I42 (E10 a control, L8): an rc3 hook client posts the bare
    container path, no location: the daemon cannot read it, so nothing
    from any project for that prompt, a row, and the line says why; the
    rc4 client's probe that its own cwd is unreadable says the same."""
    h = make_repo(tmp_path / "host" / "proj", commits=2, remote=_DEVC)
    lab.write("h1", h)
    mount = str(tmp_path / "no-such-mount" / "workspaces" / "proj")
    res = resolve_hook(lab.store, lab.config, mount, None, session="s-rc3")
    assert isinstance(res, Resolution)
    assert (res.outcome, res.project) == (INDETERMINATE, None)
    assert lab.admitted(res) == []
    assert any("cannot tell what" in line for line in res.lines)
    assert "resolution_indeterminate" in lab.rows("resolution_indeterminate")
    probe = location.probe(mount)
    assert probe["state"] == location.INDETERMINATE
    res = resolve_hook(
        lab.store, lab.config, mount, {"location": probe}, session="s-rc4"
    )
    assert isinstance(res, Resolution) and res.outcome == INDETERMINATE


def test_i40_a_clone_or_a_copy_inside_a_container(tmp_path: Path, lab: Lab) -> None:
    """I40 (E10 b, d, e): a clone made inside the container joins by
    history and remote, its token its own; a write there, homed by that
    resolution, reaches the host's session. A copy inside the container
    at a path the host cannot see, its remote removed: the token
    captured inside and the history decide, silent."""
    h = make_repo(tmp_path / "host" / "proj", commits=2, remote=_DEVC)
    lab.write("h1", h)
    host_token = (h / ".git" / "info" / "bettermemory").read_text()
    inside = clone(h, tmp_path / "container-b" / "work" / "proj2", url=_DEVC)
    box = Container(inside, "/work/proj2", _container_machine(lab, "container-b"))
    res = box.post(lab)
    assert (res.outcome, res.rule) == (SILENT, "join:H+R")
    assert lab.admitted(res) == ["h1"]
    assert (inside / ".git" / "info" / "bettermemory").read_text() != host_token
    assert res.home is not None
    with lab.store.batch():
        m = lab.store.write(content="memory c1", scopes=["tools"], home=res.home)
    lab.records["c1"] = _Rec(m.id, "memory", "s-container")
    assert lab.view(h)[1] == ["c1", "h1"]

    copied = copy(h, tmp_path / "container-e" / "work" / "copy")
    git(copied, "remote", "remove", "origin")
    box_e = Container(copied, "/work/copy", _container_machine(lab, "container-e"))
    res = box_e.post(lab)
    assert (res.outcome, res.rule) == (SILENT, "join:H+T")
    assert "h1" in lab.admitted(res)


def test_i41_throwaway_containers_cloning_with_the_committed_file(
    tmp_path: Path, lab: Lab
) -> None:
    """I41 (E10 c): throwaway containers each make a shallow clone at a
    tip no witness names, the project file committed: the first by the
    file and the remote, the next (its tip a witness now) by the file,
    history and remote; silent, no question."""
    h = make_repo(tmp_path / "host" / "proj", commits=2, remote=_DEVC)
    lab.write("h1", h)
    _committed_file(lab, h, "devc")
    commit(h, "a tip no witness names")
    before = len(lab.rows("proposal_opened"))
    seen = []
    for i in (1, 2):
        c = clone(
            h, tmp_path / f"throwaway-{i}" / "work" / "p", "--depth", "1", url=_DEVC
        )
        box = Container(c, "/work/p", _container_machine(lab, f"throwaway-{i}"))
        res = box.post(lab)
        seen.append((res.rule, res.outcome, "h1" in lab.admitted(res)))
    assert seen == [("join:F+R", SILENT, True), ("join:F+H+R", SILENT, True)]
    assert len(lab.rows("proposal_opened")) == before


def _rows_since(lab: Lab, seq: int) -> list[str]:
    return [r.kind for r in lab.store.membership_log(since_seq=seq)]


def _last_seq(lab: Lab) -> int:
    return max((r.seq for r in lab.store.membership_log()), default=0)


def test_b24_a_write_the_daemon_cannot_read_takes_its_sessions_checkout(
    tmp_path: Path, lab: Lab
) -> None:
    """Decision B24 (E10 d): a container session resolved through its hook
    under the container's machine, not the daemon's. Its write at the
    mounted path, which the daemon cannot read, takes that checkout and
    project with rule ``session_resolved`` and one ``resolution_session``
    row; no provisional project is minted and no witness added. The same
    write by another session is provisional, as is one by the session at
    a path no view of its checkout holds."""
    h = make_repo(tmp_path / "host" / "proj", commits=2, remote=_DEVC)
    lab.write("h1", h)
    base = tmp_path / "no-such-mount"
    mount = str(base / "workspaces" / "proj")
    box = Container(h, mount, _container_machine(lab, "container-a"))
    stood = box.post(lab, session="s-box")
    assert stood.outcome == SILENT and stood.checkout is not None
    assert lab.model().checkout(stood.checkout).machine != lab.identity.id
    seq = _last_seq(lab)
    res = lab.write("c1", f"{mount}/src", session="s-box")
    assert (res.outcome, res.rule) == (SILENT, "session_resolved")
    assert (res.checkout, res.project, res.chain) == (
        stood.checkout,
        stood.project,
        stood.chain,
    )
    assert _rows_since(lab, seq) == ["resolution_session", "home_put"]
    [row] = list(lab.store.membership_log(["resolution_session"]))
    assert json.loads(row.payload) == {
        "checkout": stood.checkout,
        "project": stood.project,
        "session": "s-box",
        "path": f"{mount}/src",
    }
    home = lab.model().home(lab.records["c1"].id)
    assert home is not None and not home.unresolved
    assert (home.project, home.checkout) == (stood.project, stood.checkout)
    assert lab.admitted(stood) == ["c1", "h1"]

    other = lab.write("c2", f"{mount}/src", session="s-other")
    assert (other.outcome, other.project) == (INDETERMINATE, None)
    stray = lab.model().home(lab.records["c2"].id)
    assert stray is not None and stray.unresolved
    assert stray.project != stood.project
    away = lab.write("c3", str(base / "elsewhere"), session="s-box")
    assert (away.outcome, away.project) == (INDETERMINATE, None)
    outside = lab.model().home(lab.records["c3"].id)
    assert outside is not None and outside.unresolved
    assert lab.admitted(stood) == ["c1", "h1"]
    assert len(lab.rows("resolution_session")) == 1


def test_b24_a_provisional_write_merges_when_its_session_resolves_there(
    tmp_path: Path, lab: Lab
) -> None:
    """Decision B24: a session's write at the mounted path before its hook
    resolved there is provisional, its unresolved checkout keyed on the
    daemon's machine; when that session's hook resolves the path under
    the container's machine, the session proves the namespace and the
    provisional project merges into the checkout's, silently."""
    h = make_repo(tmp_path / "host" / "proj", commits=2, remote=_DEVC)
    lab.write("h1", h)
    mount = str(tmp_path / "no-such-mount" / "workspaces" / "proj")
    box = Container(h, mount, _container_machine(lab, "container-a"))
    assert lab.write("c1", mount, session="s-late").outcome == INDETERMINATE
    before = lab.model().home(lab.records["c1"].id)
    assert before is not None and before.unresolved
    provisional = lab.model().checkout(str(before.checkout))
    assert provisional.machine == lab.identity.id
    res = box.post(lab, session="s-late")
    assert res.outcome == SILENT and res.checkout is not None
    assert lab.model().checkout(res.checkout).machine == box.identity.id
    after = lab.model().home(lab.records["c1"].id)
    assert after is not None and not after.unresolved
    assert after.project == res.project
    assert lab.model().checkout(provisional.id).replaced_by == res.checkout
    assert lab.rows("project_merged") == ["project_merged"]
    assert lab.rows("proposal_opened") == []
    assert lab.admitted(res) == ["c1", "h1"]


def test_b24_a_session_forgotten_or_crowded_out_writes_as_b6(
    tmp_path: Path, lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decision B24: the process keeps a bounded number of session stands,
    the oldest crowded out first; a session crowded out or forgotten
    writes where nothing can be read as decision B6 homes it, until its
    next hook resolution."""
    monkeypatch.setattr(location, "STANDS_CAP", 1)
    h = make_repo(tmp_path / "host" / "proj", commits=2, remote=_DEVC)
    lab.write("h1", h)
    mount = str(tmp_path / "no-such-mount" / "workspaces" / "proj")
    box = Container(h, mount, _container_machine(lab, "container-a"))
    box.post(lab, session="s-a")
    box.post(lab, session="s-b")
    assert lab.write("a1", mount, session="s-a").outcome == INDETERMINATE
    assert lab.write("b1", mount, session="s-b").rule == "session_resolved"
    location.forget_session("s-b")
    assert lab.write("b2", mount, session="s-b").outcome == INDETERMINATE
    box.post(lab, session="s-b")
    assert lab.write("b3", mount, session="s-b").rule == "session_resolved"


# ---------------------------------------------------------------------------
# The session's first output (design 5.4, 8.4): the brief and prompt recall
# ---------------------------------------------------------------------------


def test_first_sight_line_leads_the_brief(tmp_path: Path, lab: Lab) -> None:
    """L2 (design 5.4): the first session in a checkout never seen leads
    its first output with the line naming the new project, before
    whatever the brief shows; the next session's output has no such line."""
    from bettermemory.recall import session_brief

    repo = make_repo(tmp_path / "work" / "fresh", commits=1)
    first = session_brief(
        session_id="s-first", cwd=repo, config=lab.config, store=lab.store
    )
    lead = first.stdout.splitlines()[0]
    assert lead.startswith(f"bettermemory: {os.path.realpath(repo)} is a new project ")
    again = session_brief(
        session_id="s-next", cwd=repo, config=lab.config, store=lab.store
    )
    assert "is a new project" not in again.stdout


def test_prompt_recall_shows_nothing_where_nothing_is_readable_or_deferred(
    tmp_path: Path, lab: Lab
) -> None:
    """Design 3.5, 8.4: a prompt whose cwd cannot be read (INDETERMINATE)
    or whose HEAD is still being fetched (DEFERRED) is shown nothing from
    any project, though the same prompt and origin show the pointer at a
    readable checkout; the resolution wrote its row and the output carries
    its line (L8, L9). The admission rule itself is unchanged."""
    from datetime import datetime, timedelta, timezone

    from bettermemory.origin import Origin
    from bettermemory.recall import prompt_recall

    repo = make_repo(tmp_path / "work" / "payments", commits=1)
    caller = Origin(
        cwd=str(repo), repo="git@github.com:owner/payments.git", worktree_root=str(repo)
    )
    written = lab.store.write(
        content="backup strategy uses triangular restic replication",
        scopes=["projects:payments"],
        origin=caller,
        home=lab.caller(repo).home(),
    )
    stamp = datetime.now(timezone.utc) - timedelta(hours=1)
    note = lab.store.put_memory(
        written.model_copy(update={"created": stamp, "updated": stamp})
    )

    def ask(session: str, cwd: Path) -> str:
        return prompt_recall(
            prompt="backup strategy",
            session_id=session,
            cwd=cwd,
            origin=caller,
            config=lab.config,
            store=lab.store,
        ).stdout

    assert note.id in ask("s-here", repo)
    out = ask("s-gone", tmp_path / "dropped" / "payments")
    assert "cannot tell what" in out and note.id not in out
    assert "resolution_indeterminate" in lab.rows("resolution_indeterminate")
    late = make_repo(tmp_path / "iCloud" / "payments", commits=1)
    with evicted(late / ".git" / "HEAD", online=False):
        out = ask("s-late", late)
    assert "evicted" in out and note.id not in out
    assert "resolution_deferred" in lab.rows("resolution_deferred")

"""The migration of design 8.6 (RS-F6, decisions D4 and D5; RS-F6-P2 and
the missed-ifs M1, M2, M10, M11, M12).

A synthetic store is built through the store's own API, its records
carrying origins as rc3 recorded them: at live roots (real repositories
made under the test's directory), at gone roots (paths that hold nothing
now), at a root the log shows re-homed to a live one, with no root and a
cwd inside a live root, with no root and a remote, outside any repository,
and with no origin at all; episodes and a removed memory among them. The
migration then homes every record with its rule, writes no memory,
tombstone or episode row (compared byte for byte), opens one legacy
question where a live checkout partly holds a gone root's recorded
commits, keeps `log verify` and the fold equal, writes nothing into any
repository, writes nothing at all on a dry run, and homes only what was
written since on a later run. Git runs with no user or system
configuration and a fixed author.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory import membership_migrate as mm
from bettermemory import project_file
from bettermemory.config import Config
from bettermemory.machine import MachineIdentity, open_machine
from bettermemory.membership import (
    GLOBAL,
    LEGACY,
    SUPERSEDED,
    Declaration,
    Membership,
    line_legacy,
)
from bettermemory.membership_migrate import (
    MigrationReport,
    ensure_migrated,
    migrate,
)
from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.origin import Origin
from bettermemory.store import Store

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

# A fixed instant from an epoch count: no rule reads it, and no test
# depends on the host's clock.
_NOW = datetime.fromtimestamp(1_800_000_000, tz=timezone.utc)
_ALPHA_URL = "https://example.invalid/acme/alpha.git"
_ALPHA_KEY = "example.invalid/acme/alpha"
_NOWHERE_URL = "https://example.invalid/acme/nowhere.git"
_LOST_URL = "https://example.invalid/acme/lost.git"
_UNKNOWN = "f" * 40


def _now() -> datetime:
    return _NOW


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
        "GIT_CONFIG",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_COUNT",
        "GIT_AUTHOR_DATE",
        "GIT_COMMITTER_DATE",
        "BETTERMEMORY_WORKSPACE",
    ):
        monkeypatch.delenv(var, raising=False)


def git(cwd: str | Path, *args: str, input_text: str | None = None) -> str:
    done = subprocess.run(
        ["git", *args],
        cwd=cwd,
        input=input_text,
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        raise AssertionError(f"git {args} failed: {done.stderr.strip()}")
    return done.stdout.strip()


_SEQ = [0]


def make_repo(path: Path, *, commits: int = 1, remote: str | None = None) -> list[str]:
    """A repository with a history of its own; returns its commits, oldest
    first."""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    (path / "src").mkdir(exist_ok=True)
    _SEQ[0] += 1
    marker = f"{os.getpid()}-{_SEQ[0]}-{time.time_ns()}"
    shas: list[str] = []
    for i in range(commits):
        (path / "src" / f"mod{i}.py").write_text(f"VALUE = {i}\n")
        (path / "README.md").write_text(f"fixture {path.name} {marker} rev {i}\n")
        git(path, "add", "-A")
        git(path, "commit", "-q", "-m", f"rev {i} {marker}")
        shas.append(git(path, "rev-parse", "HEAD"))
    if remote:
        git(path, "remote", "add", "origin", remote)
    return shas


def tree(paths: list[Path]) -> dict[str, tuple[int, int]]:
    """Every file and directory under each path, its git directory
    included, with size and modification time: a listing that changes
    when anything is created, removed or written there."""
    out: dict[str, tuple[int, int]] = {}
    for top in paths:
        for here, dirs, files in os.walk(top):
            for name in [*dirs, *files]:
                full = os.path.join(here, name)
                st = os.lstat(full)
                out[full] = (st.st_size if name in files else 0, st.st_mtime_ns)
        st = os.lstat(top)
        out[str(top)] = (0, st.st_mtime_ns)
    return out


def tables(store: Store) -> dict[str, list[str]]:
    """Every row of every table of the store, as text."""
    names = [
        str(r[0])
        for r in store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        )
    ]
    return {
        name: sorted(
            repr(tuple(r)) for r in store.conn.execute(f"SELECT * FROM {name}")
        )
        for name in names
    }


def record_rows(store: Store) -> dict[str, list[tuple[Any, ...]]]:
    """The memory, tombstone and episode rows, every column, by rowid."""
    return {
        table: [
            tuple(r)
            for r in store.conn.execute(f"SELECT * FROM {table} ORDER BY rowid")
        ]
        for table in ("memories", "tombstones", "episodes")
    }


def files_under(path: Path) -> dict[str, bytes]:
    return (
        {str(p): p.read_bytes() for p in sorted(path.rglob("*")) if p.is_file()}
        if path.exists()
        else {}
    )


# ---------------------------------------------------------------------------
# The synthetic store
# ---------------------------------------------------------------------------


@dataclass
class World:
    """The records rc3 would have written, the repositories they were
    written in, and the rule each record's home is expected to carry."""

    tmp: Path
    store: Store
    identity: MachineIdentity
    alpha: Path
    alpha2: Path
    beta: Path
    beta_copy: Path
    wsam6: Path
    delta: Path
    vault: Path
    outside: Path
    gone: dict[str, Path]
    alpha_commits: list[str]
    expected: dict[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)

    @property
    def repositories(self) -> list[Path]:
        return [
            self.alpha,
            self.alpha2,
            self.beta,
            self.beta_copy,
            self.wsam6,
            self.delta,
            self.vault,
        ]

    def memory(
        self,
        label: str,
        rule: str,
        *,
        root: Path | None = None,
        cwd: Path | None = None,
        repo: str | None = None,
        head: str | None = None,
        cited: tuple[str, ...] = (),
        no_origin: bool = False,
    ) -> Memory:
        origin = (
            None
            if no_origin
            else Origin(
                cwd=str(cwd or root) if (cwd or root) else None,
                repo=repo,
                worktree_root=str(root) if root else None,
            )
        )
        m = Memory(
            id=generate_ulid(),
            created=_NOW,
            updated=_NOW,
            scopes=["tools"],
            confidence=Confidence.MEDIUM,
            source=Source.EXPLICIT,
            body=f"memory {label}\n",
            origin=origin,
            verified_head=head,
            verified_commits=list(cited),
        )
        self.store.put_memory(m)
        self.expected[m.id] = rule
        self.labels[label] = m.id
        return m

    def episode(
        self,
        label: str,
        rule: str,
        *,
        root: Path | None = None,
        cwd: Path | None = None,
    ) -> str:
        e = self.store.write_episode(
            session_id="s1",
            body=f"episode {label}",
            origin=Origin(
                cwd=str(cwd or root) if (cwd or root) else None,
                worktree_root=str(root) if root else None,
            ),
            now=_NOW,
        )
        self.expected[e.id] = rule
        self.labels[label] = e.id
        return e.id

    def homes(self) -> dict[str, tuple[str, str | None, str]]:
        return {
            str(r["record_id"]): (str(r["project"]), r["checkout"], str(r["rule"]))
            for r in self.store.membership_rows("homes")
        }

    def model(self) -> Membership:
        return Membership(self.store, self.identity.id)


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    tmp = tmp_path.resolve()
    dev = tmp / "Developer"
    alpha, beta, wsam6, delta = (
        dev / "alpha",
        dev / "beta",
        dev / "wsam6",
        dev / "delta",
    )
    alpha_commits = make_repo(alpha, commits=3, remote=_ALPHA_URL)
    alpha2 = dev / "alpha-clone"
    git(tmp, "clone", "-q", alpha.as_uri(), str(alpha2))
    git(alpha2, "remote", "set-url", "origin", _ALPHA_URL)
    beta_head = make_repo(beta, commits=2)[-1]
    beta_copy = dev / "beta-copy"
    shutil.copytree(beta, beta_copy, symlinks=True)
    wsam6_commits = make_repo(wsam6, commits=3)
    make_repo(delta, commits=1)
    (delta / "sub").mkdir()
    vault = tmp / "vault"
    (vault / "notes").mkdir(parents=True)
    project_file.write(vault, name="vault")
    outside = tmp / "outside"
    outside.mkdir()
    gone = {
        name: tmp / "old" / name
        for name in ("alpha-copy", "dsh", "gamma", "moved", "lost", "beta-twin")
    }
    gone["desktop-wsam6"] = tmp / "Desktop" / "wsam6"
    store = Store(tmp / "store")
    w = World(
        tmp=tmp,
        store=store,
        identity=open_machine(tmp / "state"),
        alpha=alpha,
        alpha2=alpha2,
        beta=beta,
        beta_copy=beta_copy,
        wsam6=wsam6,
        delta=delta,
        vault=vault,
        outside=outside,
        gone=gone,
        alpha_commits=alpha_commits,
    )
    a1, a2, a3 = alpha_commits
    live = "migration:live:new"
    # Live roots: alpha (with a removed memory and an episode), a clone of
    # it (history and remote), beta, a remote-less copy of beta (history
    # alone: asked once), wsam6.
    w.memory("a1", live, root=alpha, repo=_ALPHA_URL, cited=(a1,))
    w.memory("a2", live, root=alpha, repo=_ALPHA_URL, head=a3)
    removed = w.memory("a-removed", live, root=alpha, repo=_ALPHA_URL)
    w.store.tombstone(removed.id, "test")
    w.episode("a-ep", live, root=alpha)
    w.memory("clone", "migration:live:join:H+R", root=alpha2, repo=_ALPHA_URL)
    w.memory("bc", "migration:live:ask:one_witness", root=beta_copy)
    w.memory("w1", live, root=wsam6)
    # A gone root whose verified head alpha holds: lineage.
    w.memory(
        "copy", "migration:lineage", root=gone["alpha-copy"], repo=_ALPHA_URL, head=a2
    )
    # A gone root under wsam6's folder name citing wsam6's commits.
    w.memory(
        "desk",
        "migration:lineage_cited+same_name",
        root=gone["desktop-wsam6"],
        cited=(wsam6_commits[0], wsam6_commits[1]),
    )
    w.episode(
        "desk-ep", "migration:lineage_cited+same_name", root=gone["desktop-wsam6"]
    )
    # A gone root citing two of alpha's commits and one nobody holds,
    # under another folder name: a project of its own and a question (D5).
    w.memory("dsh1", "migration:none", root=gone["dsh"], cited=(a1, _UNKNOWN))
    w.memory("dsh2", "migration:none", root=gone["dsh"], cited=(a3,))
    w.episode("dsh-ep", "migration:none", root=gone["dsh"])
    # A gone root with no recorded commit naming alpha's remote.
    w.memory("gamma", "migration:remote", root=gone["gamma"], repo=_ALPHA_URL)
    # A gone root whose memories the log shows re-homed to beta.
    for label in ("m1", "m2"):
        m = w.memory(label, "migration:live:new", root=gone["moved"])
        w.store.put_memory(
            m.model_copy(
                update={"origin": Origin(cwd=str(beta), worktree_root=str(beta))}
            )
        )
    w.episode("moved-ep", "migration:rehome", root=gone["moved"])
    # A gone root whose verified head two live projects hold (beta and
    # its copy, which is asked about): ambiguous, and a question.
    w.memory("twin", "migration:ambiguous", root=gone["beta-twin"], head=beta_head)
    # A gone root whose verified head and remote no live checkout has:
    # they become its project's evidence; no question.
    w.memory("lost", "migration:none", root=gone["lost"], repo=_LOST_URL, head=_UNKNOWN)
    # No root: the cwd inside a recorded root (D4), the remote, outside any
    # repository, no origin, a live repository and a folder project found
    # at the cwd now.
    w.memory("d4", "migration:cwd_in_root", cwd=alpha / "src")
    w.episode("d4-ep", "migration:cwd_in_root", cwd=alpha / "src")
    w.memory("rem", "migration:remote_no_root", cwd=tmp / "gone-cwd", repo=_ALPHA_URL)
    w.memory(
        "nowhere",
        "migration:remote_no_live_checkout",
        cwd=tmp / "gone-cwd",
        repo=_NOWHERE_URL,
    )
    w.memory("out", "migration:global:outside_repository", cwd=outside)
    w.memory("void", "migration:global:no_origin", no_origin=True)
    w.memory("delta", "migration:cwd:new", cwd=delta / "sub")
    w.memory("vault", "migration:cwd:", cwd=vault / "notes")
    yield w
    store.close()


def _homes_match(w: World) -> None:
    homes = w.homes()
    for rid, rule in w.expected.items():
        assert rid in homes, f"{rid} has no home"
        got = homes[rid][2]
        assert got == rule or (rule.endswith(":") and got.startswith(rule)), (
            rid,
            got,
            rule,
        )


def _project_of(w: World, label: str) -> str:
    home = w.model().home(w.labels[label])
    assert home is not None
    return home.project


def _checkout_of(w: World, label: str) -> str | None:
    home = w.model().home(w.labels[label])
    assert home is not None
    return home.checkout


def _question(report: MigrationReport, root: Path) -> mm.LegacyQuestion:
    (q,) = [q for q in report.proposals if q.root == str(root)]
    return q


def _location_of(identity: MachineIdentity, path: Path) -> Any:
    from bettermemory import location

    cap = location.capture(str(path), write_token=False, use_cache=False)
    return location.to_location(
        cap,
        machine=identity.id,
        history=location.RepositoryHistory(str(cap.common_dir)),
    )


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def test_every_record_is_homed_once_with_its_rule(world: World) -> None:
    """RS-F6-P2: every memory (active and removed) and every episode has
    exactly one home, with the rule design 8.6 gives its root."""
    report = migrate(world.store, world.identity, now=_now)
    _homes_match(world)
    homes = world.homes()
    assert len(homes) == len(world.expected)
    t = report.totals
    assert (t.memory_homes, t.tombstone_homes, t.episode_homes) == (21, 1, 5)
    assert report.homed == len(world.expected)
    assert sum(report.homes_by_rule.values()) == len(world.expected)


def test_m1_no_memory_tombstone_or_episode_row_is_rewritten(world: World) -> None:
    """M1: every memory, tombstone and episode row is byte for byte what
    it was, the origin columns included; the report counts 0 rewritten."""
    before = record_rows(world.store)
    report = migrate(world.store, world.identity, now=_now)
    assert record_rows(world.store) == before
    t = report.totals
    assert (t.memories_rewritten, t.tombstones_rewritten, t.episodes_rewritten) == (
        0,
        0,
        0,
    )


def test_per_root_counts_projects_and_checkouts(world: World) -> None:
    """The report's per-root lines: state, rule and records homed. A clone
    joins its original's project by history and remote, a remote-less copy
    is asked once; gone roots are tied to a live checkout's project, the
    re-homed root is a prior view of beta's checkout, the rest are
    projects of their own."""
    report = migrate(world.store, world.identity, now=_now)
    roots = {r.path: r for r in report.roots}
    g = world.gone
    got = {
        path: (r.state, r.rule, r.memories, r.episodes, r.tombstones)
        for path, r in roots.items()
    }
    assert got == {
        str(world.alpha): ("live", "migration:live:new", 2, 1, 1),
        str(world.alpha2): ("live", "migration:live:join:H+R", 1, 0, 0),
        str(world.beta): ("live", "migration:live:new", 2, 0, 0),
        str(world.beta_copy): ("live", "migration:live:ask:one_witness", 1, 0, 0),
        str(world.wsam6): ("live", "migration:live:new", 1, 0, 0),
        str(g["alpha-copy"]): ("gone", "migration:lineage", 1, 0, 0),
        str(g["desktop-wsam6"]): (
            "gone",
            "migration:lineage_cited+same_name",
            1,
            1,
            0,
        ),
        str(g["dsh"]): ("gone", "migration:none", 2, 1, 0),
        str(g["gamma"]): ("gone", "migration:remote", 1, 0, 0),
        str(g["moved"]): ("gone", "migration:rehome", 0, 1, 0),
        str(g["beta-twin"]): ("gone", "migration:ambiguous", 1, 0, 0),
        str(g["lost"]): ("gone", "migration:none", 1, 0, 0),
    }
    alpha = roots[str(world.alpha)]
    live_projects = {r.project for r in roots.values() if r.state == "live"}
    assert roots[str(world.alpha2)].project == alpha.project
    assert roots[str(world.beta_copy)].project != roots[str(world.beta)].project
    assert roots[str(g["alpha-copy"])].project == alpha.project
    assert roots[str(g["gamma"])].project == alpha.project
    assert roots[str(g["desktop-wsam6"])].project == roots[str(world.wsam6)].project
    assert roots[str(g["moved"])].checkout == roots[str(world.beta)].checkout
    for name in ("dsh", "beta-twin", "lost"):
        assert roots[str(g[name])].project not in live_projects
    assert _project_of(world, "d4") == alpha.project
    assert _checkout_of(world, "d4") == alpha.checkout
    assert _project_of(world, "rem") == alpha.project
    assert _project_of(world, "out") == GLOBAL
    assert _project_of(world, "void") == GLOBAL
    model = world.model()
    beta_k = model.checkout(str(roots[str(world.beta)].checkout))
    assert [v.path for v in beta_k.views_on(world.identity.id)] == [
        str(g["moved"]),
        str(world.beta),
    ]
    assert model.live_view(beta_k) == str(world.beta)
    for name in ("alpha-copy", "dsh", "lost"):
        legacy = model.checkout(str(roots[str(g[name])].checkout))
        assert legacy.legacy and legacy.object_key is None
        assert [v.path for v in legacy.views] == [str(g[name])]
    twin = model.project(roots[str(g["beta-twin"])].project)
    assert twin.provisional == "ambiguous_lineage"
    nowhere = model.project(_project_of(world, "nowhere"))
    assert nowhere.name == "nowhere"
    rootless = {(x.record, x.rule) for x in report.rootless}
    assert (world.labels["d4"], "migration:cwd_in_root") in rootless
    assert (world.labels["d4-ep"], "migration:cwd_in_root") in rootless
    assert (world.labels["delta"], "migration:cwd:new") in rootless
    t = report.totals
    print(
        f"projects={t.projects} checkouts={t.checkouts} views={t.views} "
        f"witnesses={t.witnesses} proposals={t.proposals} log_rows={t.log_rows}"
    )
    # alpha (with its clone), beta, beta's copy, wsam6, delta, the vault,
    # dsh, lost, the twin and the remote with no live checkout.
    assert t.projects == 10
    # Seven live checkouts and one legacy checkout per gone root but the
    # re-homed one, each with its view, and the re-homed root's view.
    assert (t.checkouts, t.views) == (13, 14)
    # The copy's first-sight question and the two legacy questions.
    assert t.proposals == 3 and len(report.proposals) == 2


def test_witnesses_and_the_evidence_of_a_project_with_no_live_checkout(
    world: World,
) -> None:
    """The commits a live root's records recorded that it holds become its
    project's witnesses; a gone root's verified head and remote that no
    live checkout has become its own project's evidence, so a folder of
    its that returns is seen by them."""
    report = migrate(world.store, world.identity, now=_now)
    roots = {r.path: r for r in report.roots}

    def witnesses(project: str) -> set[str]:
        return {
            str(r["commit_id"])
            for r in world.store.membership_rows(
                "project_witnesses", where={"project": project}
            )
        }

    a1, _a2, a3 = world.alpha_commits
    assert {a1, a3} <= witnesses(roots[str(world.alpha)].project)
    lost = roots[str(world.gone["lost"])].project
    assert witnesses(lost) == {_UNKNOWN}
    remotes = {
        str(r["remote_key"])
        for r in world.store.membership_rows("project_remotes", where={"project": lost})
    }
    assert remotes == {"example.invalid/acme/lost"}
    assert witnesses(roots[str(world.gone["dsh"])].project) == set()


def test_m2_no_live_root_loses_a_memory_rc3_delivered_there(world: World) -> None:
    """M2, RS-F6-P2: at every live root, the memories whose home is its
    project are at least those rc3's own rule (`recall.in_project`, read
    from the origin columns) gave a session there."""
    from bettermemory.recall import _rc3_in_project as in_project

    report = migrate(world.store, world.identity, now=_now)
    model = world.model()
    memories = list(world.store.iter_memories())
    homes = model.homes([m.id for m in memories])
    for r in report.roots:
        if r.state != "live":
            continue
        repo = _ALPHA_URL if r.path in (str(world.alpha), str(world.alpha2)) else None
        caller = Origin(cwd=r.path, repo=repo, worktree_root=r.path)
        rc3 = {m.id for m in memories if in_project(m.origin, caller)}
        mine = {mid for mid, h in homes.items() if h.project == r.project}
        print(f"{r.path}: rc3 {len(rc3)}, homed in its project {len(mine)}")
        assert rc3 <= mine, r.path


def test_d5_legacy_question_in_the_checkout_holding_the_commits(world: World) -> None:
    """D5, line L6: the gone root no live checkout's lineage claims, whose
    recorded commits alpha (and its clone) partly hold, two of three, is a
    project of its own with one durable legacy question, in the checkout
    holding the most (the first on a tie); the root nothing holds gets
    none."""
    report = migrate(world.store, world.identity, now=_now)
    roots = {r.path: r for r in report.roots}
    alpha, dsh = roots[str(world.alpha)], roots[str(world.gone["dsh"])]
    clone = roots[str(world.alpha2)]
    assert dsh.recorded == 3
    assert dsh.held == {alpha.checkout: 2, clone.checkout: 2}
    q = _question(report, world.gone["dsh"])
    line = line_legacy(
        "dsh",
        2,
        1,
        str(world.gone["dsh"]),
        2,
        "alpha",
        alpha.project,
        recorded=3,
    )
    assert (q.checkout, q.at, q.legacy_project, q.line) == (
        alpha.checkout,
        str(world.alpha),
        dsh.project,
        line,
    )
    assert line == (
        f"bettermemory: the records of dsh (2 memories, 1 episodes; its folder "
        f'{world.gone["dsh"]} is gone) may belong to project "alpha" here: this '
        f"repository holds 2 of their 3 recorded commits. Same project: "
        f"bettermemory project join {alpha.project}; separate: bettermemory "
        f"project new"
    )
    twin = _question(report, world.gone["beta-twin"])
    assert twin.checkout == roots[str(world.beta)].checkout
    assert "holds 1 of their 1 recorded commits" in twin.line
    assert not [q for q in report.proposals if q.root == str(world.gone["lost"])]
    model = world.model()
    (pending,) = [p for p in model.pending() if p.id == q.proposal]
    assert (pending.kind, pending.checkout, pending.line, pending.reason) == (
        LEGACY,
        alpha.checkout,
        line,
        mm.LEGACY_REASON,
    )
    # Every session at alpha names the question until it is answered.
    res = model.resolve(_location_of(world.identity, world.alpha))
    assert res.outcome == "ask" and line in model.first_output(res)


def test_d5_join_merges_the_legacy_records_into_the_checkouts_project(
    world: World,
) -> None:
    """`bettermemory project join <id>` in alpha answers the legacy
    question: dsh's records belong to alpha's project from then on, by one
    merge row, no home rewritten."""
    report = migrate(world.store, world.identity, now=_now)
    q = _question(report, world.gone["dsh"])
    alpha = {r.path: r for r in report.roots}[str(world.alpha)]
    homes_before = world.homes()
    model = Membership.open(world.store, world.identity)
    model.declare(
        Declaration(
            "join",
            "os:fixture",
            checkout=q.checkout,
            proposal=q.proposal,
            target=alpha.project,
        )
    )
    assert world.homes() == homes_before
    for label in ("dsh1", "dsh2", "dsh-ep"):
        assert _project_of(world, label) == alpha.project
    assert q.proposal not in {p.id for p in model.pending()}


def test_m10_log_verify_and_the_fold_after_the_migration(world: World) -> None:
    """M10: the chain verifies and a replay of the log reproduces every
    table, the membership tables included."""
    migrate(world.store, world.identity, now=_now)
    verified = world.store.log_verify()
    assert verified["status"] == "ok", verified["problems"]
    fold = world.store.refold()
    assert fold["status"] == "ok"
    assert all(
        not t["unaccounted"] and not t["missing"] for t in fold["membership"].values()
    )
    assert fold["membership"]["homes"]["rows"] == len(world.expected)


def test_m11_no_file_appears_in_any_repository(world: World) -> None:
    """M11, design 1.1: the probe reads; no token and no other file is
    written into any repository or folder project, and nothing there
    changes."""
    before = tree(world.repositories)
    migrate(world.store, world.identity, now=_now)
    assert tree(world.repositories) == before
    for repo in world.repositories:
        assert not (repo / ".git" / "info" / "bettermemory").exists()


def test_a_dry_run_writes_nothing_and_reports_what_a_run_writes(
    world: World, keys_dir: Path
) -> None:
    """A dry run is one transaction rolled back: every table of the store,
    the key directory and the repositories are as they were, and a real
    run afterwards writes what it reported."""
    store_before = tables(world.store)
    keys_before = files_under(keys_dir)
    repos_before = tree(world.repositories)
    dry = migrate(world.store, world.identity, dry_run=True, now=_now)
    assert dry.dry_run and "rolled back" in dry.kept
    assert tables(world.store) == store_before
    assert files_under(keys_dir) == keys_before
    assert tree(world.repositories) == repos_before
    assert dry.homed == len(world.expected)
    assert dry.lines()[0].startswith("bettermemory membership migrate (dry run:")
    real = migrate(world.store, world.identity, now=_now)
    assert real.totals == dry.totals
    assert real.homes_by_rule == dry.homes_by_rule
    assert [p.line for p in real.proposals] == [p.line for p in dry.proposals]
    _homes_match(world)


def test_idempotent_a_second_run_changes_nothing(world: World) -> None:
    """A second run on a migrated store writes no row and reports 0."""
    first = migrate(world.store, world.identity, now=_now)
    assert first.homed == len(world.expected)
    after_first = tables(world.store)
    again = migrate(world.store, world.identity, now=_now)
    assert again.homed == 0 and again.totals == mm.MigrationTotals()
    assert again.roots == () and again.proposals == ()
    assert tables(world.store) == after_first
    assert ensure_migrated(world.store, world.identity, config=Config()) is None
    assert tables(world.store) == after_first
    assert len(list(world.store.membership_log([mm.MIGRATED]))) == 1


def test_records_written_since_are_homed_by_the_same_rules(world: World) -> None:
    """Design 8.12: records an earlier build wrote after the migration (a
    rollback to rc3 and a re-upgrade) are homed at the next open by the
    same rules, at the checkouts and projects their roots were given; the
    rest is reported 0, and the legacy question is restated, not asked
    twice (M12)."""
    first = migrate(world.store, world.identity, now=_now)
    roots = {r.path: r for r in first.roots}
    g = world.gone
    world.memory("a-new", "migration:live:object", root=world.alpha, repo=_ALPHA_URL)
    world.memory("dsh-new", "migration:none", root=g["dsh"], cited=(_UNKNOWN,))
    world.episode("moved-new", "migration:rehome", root=g["moved"])
    world.memory("d4-new", "migration:cwd_in_root", cwd=world.alpha / "src")
    world.memory("out-new", "migration:global:outside_repository", cwd=world.outside)
    report = ensure_migrated(world.store, world.identity, config=Config())
    assert report is not None
    assert report.homed == 5
    _homes_match(world)
    got = {r.path: (r.rule, r.memories, r.episodes) for r in report.roots}
    assert got == {
        str(world.alpha): ("migration:live:object", 1, 0),
        str(g["dsh"]): ("migration:none", 1, 0),
        str(g["moved"]): ("migration:rehome", 0, 1),
    }
    for label, root in (("a-new", world.alpha), ("dsh-new", g["dsh"])):
        assert _checkout_of(world, label) == roots[str(root)].checkout
        assert _project_of(world, label) == roots[str(root)].project
    assert _checkout_of(world, "moved-new") == roots[str(world.beta)].checkout
    assert _checkout_of(world, "d4-new") == roots[str(world.alpha)].checkout
    t = report.totals
    assert (t.projects, t.checkouts, t.proposals) == (0, 0, 0)
    assert report.proposals == ()
    asked = [p for p in world.model().pending() if p.kind == LEGACY]
    assert len(asked) == len(first.proposals) == 2
    (dsh,) = [p for p in asked if p.id == _question(first, g["dsh"]).proposal]
    assert "(3 memories, 1 episodes;" in dsh.line
    assert ensure_migrated(world.store, world.identity, config=Config()) is None


def test_a_later_run_decides_new_roots_against_the_checkouts_there_are(
    world: World,
) -> None:
    """A later run meets roots no earlier run decided: a gone root whose
    verified heads only the verifications table and the log's verify rows
    recorded is tied by lineage to a live checkout registered earlier
    (probed again, read-only); the old path of a checkout a session
    registered and that moved since is that checkout's recorded view; a
    cwd under its new path, which the session saw, is inside a recorded
    root (D4)."""
    from bettermemory.location import resolve_here

    first = migrate(world.store, world.identity, now=_now)
    alpha = {r.path: r for r in first.roots}[str(world.alpha)]
    _a1, a2, a3 = world.alpha_commits
    late = world.tmp / "old" / "late"
    by_table = world.memory("late1", "migration:lineage", root=late)
    world.store.put_verification(by_table.id, verified_at=_NOW, verified_head=a2)
    by_log = world.memory("late2", "migration:lineage", root=late)
    world.store.record_event("verify", id=by_log.id, verified_head=a3)
    eps = world.tmp / "Developer" / "eps"
    make_repo(eps)
    seen = resolve_here(world.store, Config(), str(eps), identity=world.identity)
    moved = world.tmp / "Developer" / "eps-moved"
    os.rename(eps, moved)
    again = resolve_here(world.store, Config(), str(moved), identity=world.identity)
    assert again.checkout == seen.checkout
    world.memory("eps-old", "migration:view", root=eps)
    world.memory("eps-cwd", "migration:cwd_in_root", cwd=moved / "src")
    report = migrate(world.store, world.identity, now=_now)
    _homes_match(world)
    roots = {r.path: r for r in report.roots}
    assert (roots[str(late)].project, roots[str(late)].rule) == (
        alpha.project,
        "migration:lineage",
    )
    assert roots[str(eps)].checkout == seen.checkout
    assert _checkout_of(world, "eps-cwd") == seen.checkout
    assert report.totals.projects == 0


def test_the_report_names_its_roots_questions_and_cwd_records(world: World) -> None:
    """The printed report: per root, the questions with their lines, the
    records homed by their cwd (D4), and the totals."""
    report = migrate(world.store, world.identity, now=_now)
    text = "\n".join(report.lines())
    assert text.startswith(
        "bettermemory membership migrate: committed in one transaction"
    )
    assert f'  {world.alpha}: live, project "alpha"' in text
    for q in report.proposals:
        assert q.line in text
    assert f"  {world.labels['d4']} (memory) at {world.alpha / 'src'}: in " in text
    assert "rows rewritten: 0 memories, 0 removed memories, 0 episodes" in text
    as_dict = report.to_dict()
    assert as_dict["totals"]["memory_homes"] == 21
    assert len(as_dict["roots"]) == len(report.roots)


def _superseded(tmp: Path) -> tuple[Path, Path, str]:
    """Two repositories with no commit in common, the second carrying the
    first's checkout token (copied by hand), each written at; returns
    them and the token."""
    from bettermemory import checkout_token

    first, second = tmp / "first", tmp / "second"
    make_repo(first, commits=2)
    make_repo(second, commits=1)
    wrote = checkout_token.write(first / ".git", policy=True, bare=False)
    assert wrote.status == "written" and wrote.token is not None
    planted = checkout_token.token_path(second / ".git")
    planted.write_bytes(checkout_token.token_path(first / ".git").read_bytes())
    return first, second, wrote.token


def _write_episodes(store: Store, *roots: Path) -> None:
    for root in roots:
        store.write_episode(
            session_id="s1",
            body="episode",
            origin=Origin(cwd=str(root), worktree_root=str(root)),
            now=_NOW,
        )


def test_a_superseded_token_is_reported_and_not_written(tmp_path: Path) -> None:
    """A repository carrying another line's checkout token and none of its
    history (design 3.3, token_superseded): the ladder makes it a project
    of its own and wants it a token of its own; the migration writes none,
    names the checkout in its report and its row, records the checkout's
    token status as superseded (B15), and the token file is as it was."""
    tmp = tmp_path.resolve()
    first, second, _token = _superseded(tmp)
    store = Store(tmp / "store")
    try:
        identity = open_machine(tmp / "state")
        _write_episodes(store, first, first, second)
        before = tree([first, second])
        report = migrate(store, identity, now=_now)
        roots = {r.path: r for r in report.roots}
        assert roots[str(first)].project != roots[str(second)].project
        assert report.remint == (roots[str(second)].checkout,)
        assert tree([first, second]) == before
        (row,) = store.membership_log([mm.MIGRATED])
        assert f'"remint":["{roots[str(second)].checkout}"]' in row.payload
        assert "tokens due at the next sighting (1):" in report.lines()
        model = Membership(store, identity.id)
        mine, theirs = roots[str(second)].checkout, roots[str(first)].checkout
        assert mine is not None and theirs is not None
        assert model.checkout(mine).token_status == SUPERSEDED
        assert model.checkout(theirs).token_status != SUPERSEDED
    finally:
        store.close()


def test_a_superseded_token_is_reminted_at_the_next_sighting(tmp_path: Path) -> None:
    """B15: the migrated checkout whose token its history supersedes asks
    for a token of its own at its next sighting (`remint_token`, which
    `location.resolve_caller` applies in the caller's namespace) and at
    every sighting until one is written; `token_written` records the new
    token with status written, and the sighting after asks nothing."""
    from bettermemory import checkout_token

    tmp = tmp_path.resolve()
    first, second, old = _superseded(tmp)
    store = Store(tmp / "store")
    try:
        identity = open_machine(tmp / "state")
        _write_episodes(store, first, first, second)
        report = migrate(store, identity, now=_now)
        kid = {r.path: r for r in report.roots}[str(second)].checkout
        assert kid is not None
        model = Membership(store, identity.id)
        res = model.resolve(_location_of(identity, second))
        assert (res.checkout, res.remint_token) == (kid, True)
        k = model.checkout(kid)
        assert (k.token, k.token_status) == (old, SUPERSEDED)
        again = model.resolve(_location_of(identity, second))
        assert (again.checkout, again.remint_token) == (kid, True)
        wrote = checkout_token.remint(second / ".git", policy=True, bare=False)
        assert wrote.status == "written" and wrote.token not in (None, old)
        model.token_written(kid, wrote.token, wrote.status)
        k = model.checkout(kid)
        assert (k.token, k.token_status) == (wrote.token, "written")
        settled = model.resolve(_location_of(identity, second))
        assert (settled.checkout, settled.remint_token) == (kid, False)
        assert model.checkout(kid).token == wrote.token
        assert not model.resolve(_location_of(identity, first)).remint_token
    finally:
        store.close()


def test_a_superseded_token_settles_on_a_new_token_seen(tmp_path: Path) -> None:
    """B15, the hook's path: the client re-mints on the reply's request and
    calls no `token_written`; the next sighting reads the new token, takes
    it and its status, and asks nothing more."""
    from bettermemory import checkout_token

    tmp = tmp_path.resolve()
    first, second, old = _superseded(tmp)
    store = Store(tmp / "store")
    try:
        identity = open_machine(tmp / "state")
        _write_episodes(store, first, first, second)
        report = migrate(store, identity, now=_now)
        kid = {r.path: r for r in report.roots}[str(second)].checkout
        model = Membership(store, identity.id)
        assert model.resolve(_location_of(identity, second)).remint_token
        wrote = checkout_token.remint(second / ".git", policy=True, bare=False)
        assert wrote.token not in (None, old)
        res = model.resolve(_location_of(identity, second))
        assert (res.checkout, res.remint_token) == (kid, False)
        assert kid is not None
        k = model.checkout(kid)
        assert (k.token, k.token_status) == (wrote.token, "present")
    finally:
        store.close()


def _twin_contents() -> tuple[bytes, bytes]:
    """Two blob contents whose SHA-1 names share their first four hex
    characters, the first pair met in a fixed sequence."""
    seen: dict[str, bytes] = {}
    i = 0
    while True:
        content = f"twin {i}\n".encode()
        body = b"blob %d\0" % len(content) + content
        prefix = hashlib.sha1(body).hexdigest()[:4]
        if prefix in seen:
            return seen[prefix], content
        seen[prefix] = content
        i += 1


def _cite(store: Store, root: Path, cited: list[str], head: str | None) -> str:
    """A memory written at `root` citing `cited`, verified at `head`."""
    m = Memory(
        id=generate_ulid(),
        created=_NOW,
        updated=_NOW,
        scopes=["tools"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body=f"memory at {root.name}\n",
        origin=Origin(cwd=str(root), worktree_root=str(root)),
        verified_head=head,
        verified_commits=cited,
    )
    store.put_memory(m)
    return m.id


def test_an_abbreviated_cited_commit_is_held_as_the_object_it_names(
    tmp_path: Path,
) -> None:
    """B19: memories cite commits by 7-character ids, as models write them.
    The live repository holding two of a gone root's cited commits is
    asked the legacy question ("2 of their 5 recorded commits"); the live
    root's own abbreviated citation becomes a witness under its full id;
    an abbreviation two objects share and one no object starts with are
    held by no one, and no witness row carries an abbreviation."""
    from bettermemory import location

    tmp = tmp_path.resolve()
    app, gone = tmp / "app", tmp / "old" / "legacy-tool"
    shas = make_repo(app, commits=3)
    first, second = _twin_contents()
    twins = [
        git(app, "hash-object", "-w", "--stdin", input_text=c.decode())
        for c in (first, second)
    ]
    shared = os.path.commonprefix(twins)
    assert len(shared) >= 4
    names = git(app, "cat-file", "--batch-all-objects", "--batch-check").split("\n")
    held_names = [line.split()[0] for line in names if line]
    absent = next(
        p
        for p in (f"{i:07x}" for i in range(16**7))
        if not any(n.startswith(p) for n in held_names)
    )
    elsewhere = hashlib.sha1(b"a commit no repository here holds").hexdigest()
    store = Store(tmp / "store")
    try:
        identity = open_machine(tmp / "state")
        _cite(store, app, [shas[0][:7]], shas[2])
        _cite(store, gone, [shas[1][:7], shas[2][:7].upper(), shared], elsewhere)
        _cite(store, gone, [absent], None)
        history = location.RepositoryHistory(str(app / ".git"))
        assert history.expand([shas[0][:7], shared, absent, shas[1]]) == {
            shas[0][:7]: shas[0],
            shas[1]: shas[1],
        }
        assert history.holds([shas[0][:7].upper(), shared]) == {shas[0][:7]}
        assert history.holds_objects([twins[0][:12], shared]) == {twins[0][:12]}
        report = migrate(store, identity, now=_now)
        roots = {r.path: r for r in report.roots}
        live, legacy = roots[str(app)], roots[str(gone)]
        assert (legacy.rule, legacy.recorded) == ("migration:none", 5)
        assert legacy.held == {live.checkout: 2}
        q = _question(report, gone)
        assert (q.checkout, q.legacy_project) == (live.checkout, legacy.project)
        assert q.line == line_legacy(
            "legacy-tool",
            2,
            0,
            str(gone),
            2,
            "app",
            live.project,
            recorded=5,
        )
        assert "holds 2 of their 5 recorded commits" in q.line
        rows = store.membership_rows("project_witnesses")
        witnesses = {
            (str(r["project"]), str(r["commit_id"])): str(r["source"]) for r in rows
        }
        assert all(len(c) == 40 for _p, c in witnesses)
        assert witnesses[(live.project, shas[0])] == "migration:recorded_commit"
        assert witnesses[(legacy.project, elsewhere)] == "migration:verified_head"
        assert not {c for _p, c in witnesses} & set(twins)
        assert not [c for p, c in witnesses if p == legacy.project and c != elsewhere]
    finally:
        store.close()


def _at_remote(store: Store, url: str, *, root: Path | None, cwd: Path) -> str:
    """A memory written with remote `url`, at `root` (None: no root)."""
    m = Memory(
        id=generate_ulid(),
        created=_NOW,
        updated=_NOW,
        scopes=["tools"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body=f"memory of {url}\n",
        origin=Origin(
            cwd=str(cwd), repo=url, worktree_root=str(root) if root else None
        ),
    )
    store.put_memory(m)
    return m.id


def test_a_remote_key_is_carried_by_one_project(tmp_path: Path) -> None:
    """A gone root whose records name a remote, and a record with no root
    naming the same remote (spelled with other capitals): one project, the
    gone root's, homes both, and one row carries the key. A remote no
    project carries gets a project named after the repository, carrying
    the key, minted once for both its records."""
    tmp = tmp_path.resolve()
    gone, nowhere = tmp / "old" / "Verendo", tmp / "nowhere"
    verendo = "https://example.invalid/acme/verendo.git"
    noxious = "https://example.invalid/acme/noxious.git"
    store = Store(tmp / "store")
    try:
        identity = open_machine(tmp / "state")
        at_root = _at_remote(store, verendo, root=gone, cwd=gone)
        rootless = _at_remote(
            store, "https://example.invalid/Acme/Verendo.git", root=None, cwd=nowhere
        )
        loose = [_at_remote(store, noxious, root=None, cwd=nowhere) for _ in range(2)]
        report = migrate(store, identity, now=_now)
        homes = {
            str(r["record_id"]): (str(r["project"]), str(r["rule"]))
            for r in store.membership_rows("homes")
        }
        project = {r.path: r for r in report.roots}[str(gone)].project
        assert homes[at_root] == (project, "migration:none")
        assert homes[rootless] == (project, "migration:remote_no_live_checkout")
        remotes = store.membership_rows("project_remotes")
        assert [
            r["project"]
            for r in remotes
            if r["remote_key"] == "example.invalid/acme/verendo"
        ] == [project]
        model = Membership(store, identity.id)
        assert model.project(project).name == "Verendo"
        (minted,) = {homes[i][0] for i in loose}
        assert {homes[i][1] for i in loose} == {"migration:remote_no_live_checkout"}
        assert model.project(minted).name == "noxious"
        assert [
            r["project"]
            for r in remotes
            if r["remote_key"] == "example.invalid/acme/noxious"
        ] == [minted]
        assert not [p for p in model.projects() if p.name.startswith("remote:")]
        reasons = {
            json.loads(row.payload)["name"]: json.loads(row.payload)["reason"]
            for row in store.membership_log(["project_minted"])
        }
        assert reasons == {
            "Verendo": "migration_no_live_checkout",
            "noxious": "migration_remote_no_checkout",
        }
        assert report.totals.projects == 2
    finally:
        store.close()


def test_a_remote_two_projects_carry_homes_in_the_first(tmp_path: Path) -> None:
    """Two live repositories with no commit in common that name one remote
    are two projects carrying its key; a record with no root naming that
    remote mints nothing, goes to the one minted first
    (``remote_ambiguous``), and the report's line names both carriers."""
    tmp = tmp_path.resolve()
    shared = "https://example.invalid/acme/shared.git"
    one, two = tmp / "one", tmp / "two"
    make_repo(one, commits=2, remote=shared)
    make_repo(two, commits=1, remote=shared)
    store = Store(tmp / "store")
    try:
        identity = open_machine(tmp / "state")
        for root in (one, two):
            _at_remote(store, shared, root=root, cwd=root)
        rootless = _at_remote(store, shared, root=None, cwd=tmp / "nowhere")
        report = migrate(store, identity, now=_now)
        roots = {r.path: r for r in report.roots}
        carriers = sorted(
            {str(roots[str(one)].project), str(roots[str(two)].project)},
            key=lambda p: (len(p), p),
        )
        assert len(carriers) == 2
        remotes = store.membership_rows("project_remotes")
        assert (
            sorted(
                (
                    str(r["project"])
                    for r in remotes
                    if r["remote_key"].endswith("/shared")
                ),
                key=lambda p: (len(p), p),
            )
            == carriers
        )
        (home,) = store.membership_rows("homes", where={"record_id": rootless})
        assert (home["project"], home["rule"]) == (
            carriers[0],
            "migration:remote_ambiguous",
        )
        (listed,) = [x for x in report.rootless if x.record == rootless]
        assert listed.carriers == tuple(carriers)
        line = next(ln for ln in report.lines() if ln.startswith(f"  {rootless} "))
        assert line.endswith(f"; carried by {', '.join(carriers)}")
        assert report.totals.projects == 2
    finally:
        store.close()


def test_the_command_prints_a_dry_run_then_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`bettermemory membership migrate --dry-run` prints what a run would
    write and writes nothing; without the flag it runs; with no store it
    exits 2 naming the path."""
    from bettermemory.config import ENV_DIR_OVERRIDE, STORE_FILENAME

    directory = tmp_path.resolve() / "bm"
    monkeypatch.setenv(ENV_DIR_OVERRIDE, str(directory))
    with pytest.raises(SystemExit) as exc:
        mm.run_command(dry_run=True)
    assert exc.value.code == 2
    assert str(directory / STORE_FILENAME) in capsys.readouterr().err
    repo = tmp_path.resolve() / "repo"
    make_repo(repo)
    store = Store.open_or_create(directory / STORE_FILENAME)
    try:
        store.write_episode(
            session_id="s1",
            body="episode",
            origin=Origin(cwd=str(repo), worktree_root=str(repo)),
            now=_NOW,
        )
        before = tables(store)
    finally:
        store.close()
    mm.run_command(dry_run=True)
    out = capsys.readouterr().out
    assert out.startswith("bettermemory membership migrate (dry run: one transaction")
    assert f"  {repo}: live" in out
    store = Store.open(directory / STORE_FILENAME)
    try:
        assert tables(store) == before
    finally:
        store.close()
    mm.run_command(dry_run=False)
    out = capsys.readouterr().out
    assert out.startswith("bettermemory membership migrate: committed")
    store = Store.open(directory / STORE_FILENAME)
    try:
        assert not mm.unhomed(store)
    finally:
        store.close()


def test_ensure_migrated_on_a_store_with_nothing_to_home(tmp_path: Path) -> None:
    """A store with no record has nothing to migrate: no row is written."""
    store = Store(tmp_path / "store")
    try:
        before = tables(store)
        identity = open_machine(tmp_path / "state")
        assert ensure_migrated(store, identity, config=Config()) is None
        report = migrate(store, identity, dry_run=True)
        assert isinstance(report, MigrationReport) and report.homed == 0
        assert tables(store) == before
    finally:
        store.close()

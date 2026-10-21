"""The cost of membership at a known checkout and at scale (RS-F6, design
8.9, RS-F6-P8 and M6), at a reduced size: E16's counters and E29's batch
and witness bound.

E16: a checkout the migration registered is a known checkout at its next
sighting, resolved by its object; from then on a prompt there spawns no
git process and makes a bounded number of stat calls, with hundreds of
other checkouts registered beside it. E29: the first sight of a clone
asks its repository about every project's bounded witness set in one
batch, through the object reader and no git process; 301 sightings at
successive commits keep the witness bound. Git runs with no user or
system configuration and a fixed author; process spawns are counted by
patching `subprocess.Popen`, stat calls by patching `os.stat` and
`os.lstat`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory import location
from bettermemory.config import Config
from bettermemory.location import resolve_here
from bettermemory.machine import MachineIdentity, open_machine
from bettermemory.membership import KEEP_OLDEST, WITNESS_BOUND, Membership
from bettermemory.membership_migrate import migrate
from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.origin import Origin
from bettermemory.store import Store

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

_NOW = datetime.fromtimestamp(1_800_000_000, tz=timezone.utc)
_URL = "https://example.invalid/acme/game.git"


@pytest.fixture(autouse=True)
def neutral_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No user or system configuration reaches git, a fixed author and
    committer, and no variable that points git or bettermemory elsewhere."""
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


def git(cwd: str | Path, *args: str, stdin: str | None = None) -> str:
    """git's stdout, stripped. `stdin` goes in as UTF-8 bytes and the output
    is decoded here, with no text layer between: on Windows one turns each
    newline of a fast-import stream into CRLF, and git then reads the
    branch as ``refs/heads/main`` and a carriage return, which it refuses."""
    done = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        input=None if stdin is None else stdin.encode("utf-8"),
        check=False,
    )
    out = done.stdout.decode("utf-8", errors="replace").replace("\r\n", "\n")
    err = done.stderr.decode("utf-8", errors="replace").replace("\r\n", "\n")
    if done.returncode != 0:
        raise AssertionError(f"git {args} failed: {err.strip()}")
    return out.strip()


def make_repo(path: Path, *, commits: int = 2, remote: str | None = None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    (path / "src").mkdir(exist_ok=True)
    marker = f"{os.getpid()}-{time.time_ns()}"
    for i in range(commits):
        (path / "src" / f"mod{i}.py").write_text(f"VALUE = {i}\n")
        (path / "README.md").write_text(f"fixture {path.name} {marker} rev {i}\n")
        git(path, "add", "-A")
        git(path, "commit", "-q", "-m", f"rev {i} {marker}")
    if remote:
        git(path, "remote", "add", "origin", remote)
    return path


class _Counts:
    def __init__(self) -> None:
        self.spawns = 0
        self.stats = 0
        self.lstats = 0


@pytest.fixture
def counted(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[Callable[[], object]], _Counts]:
    """Run a call with every process spawn (`subprocess.Popen`, which
    `subprocess.run` uses) and every `os.stat` and `os.lstat` counted, the
    suite's own guard on captures (conftest) set aside so only the
    product's calls count."""
    real_stat, real_lstat, real_popen = os.stat, os.lstat, subprocess.Popen

    def measure(fn: Callable[[], object]) -> _Counts:
        counts = _Counts()

        def stat(*a: Any, **k: Any) -> os.stat_result:
            counts.stats += 1
            return real_stat(*a, **k)

        def lstat(*a: Any, **k: Any) -> os.stat_result:
            counts.lstats += 1
            return real_lstat(*a, **k)

        class Popen(real_popen):  # type: ignore[valid-type,misc]
            def __init__(self, *a: Any, **k: Any) -> None:
                counts.spawns += 1
                super().__init__(*a, **k)

        with monkeypatch.context() as m:
            for name in ("capture", "_capture_uncached"):
                fn_ = getattr(location, name)
                m.setattr(location, name, getattr(fn_, "__wrapped__", fn_))
            m.setattr(os, "stat", stat)
            m.setattr(os, "lstat", lstat)
            m.setattr(subprocess, "Popen", Popen)
            fn()
        return counts

    return measure


def _synthetic(
    store: Store, machine: str, *, projects: int, checkouts_each: int
) -> int:
    """`projects` projects at the witness bound, each with
    `checkouts_each` checkouts whose objects exist nowhere, written as
    membership rows in one batch; returns the witnesses written."""
    written = 0
    stamp = "1970-01-01T00:00:00.000Z"
    arrival = store.next_membership_number("project_witnesses", "arrival")
    order = store.next_membership_number("checkout_views", "seen_order")
    with store.batch():
        for i in range(projects):
            pid = f"S{i:05d}"
            store.put_membership(
                "project",
                {
                    "id": pid,
                    "name": f"proj{i}",
                    "merged_into": None,
                    "provisional": None,
                    "parent": None,
                    "tenant": None,
                    "folder": 0,
                    "boundary": 0,
                    "minted": stamp,
                },
            )
            store.put_membership(
                "project_remote",
                {"project": pid, "remote_key": f"example.invalid/org{i}/proj{i}"},
            )
            for _ in range(WITNESS_BOUND):
                store.put_membership(
                    "project_witness",
                    {
                        "project": pid,
                        "commit_id": os.urandom(20).hex(),
                        "arrival": arrival,
                        "source": "synthetic",
                        "fingerprint": None,
                    },
                )
                arrival += 1
                written += 1
            for k in range(checkouts_each):
                kid = f"Z{i:05d}{k:02d}"
                inode = str(10_000_000 + i * 100 + k)
                store.put_membership(
                    "checkout",
                    {
                        "id": kid,
                        "project": pid,
                        "machine": machine,
                        "kind": "primary",
                        "object_device": "7",
                        "object_inode": inode,
                        "object_birth": "1",
                        "common_device": "7",
                        "common_inode": inode,
                        "common_birth": "1",
                        "git_dir": f"/synthetic/{i}/{k}/.git",
                        "token": None,
                        "token_status": "n/a",
                        "stamps_json": [],
                        "last_head": None,
                        "branch": None,
                        "remotes_json": [],
                        "manifest_json": None,
                        "legacy": 0,
                        "retired": 0,
                        "registered": stamp,
                        "unresolved_json": None,
                    },
                )
                store.put_membership(
                    "checkout_view",
                    {
                        "checkout": kid,
                        "machine": machine,
                        "path": f"/synthetic/{i}/{k}",
                        "first_seen": stamp,
                        "last_seen": stamp,
                        "seen_order": order,
                    },
                )
                order += 1
    return written


@pytest.fixture
def lab(tmp_path: Path) -> Iterator[tuple[Store, MachineIdentity, Config]]:
    store = Store(tmp_path / "store")
    yield store, open_machine(tmp_path / "state"), Config()
    store.close()


# ---------------------------------------------------------------------------
# E16: a known checkout
# ---------------------------------------------------------------------------


def test_e16_a_migrated_checkout_is_known_and_costs_no_git(
    tmp_path: Path,
    lab: tuple[Store, MachineIdentity, Config],
    counted: Callable[[Callable[[], object]], _Counts],
) -> None:
    """E16, design 8.9, RS-F6-P8 and M6, among 200 other checkouts: the
    migration's probe spawns no git (the object reader reads the files);
    the checkout it registered is known at its next sighting, resolved by
    its object, which writes its token in the caller's namespace; from
    then on a prompt there spawns no git process and stays within a
    bounded number of stat calls. Measured on macOS (APFS, the test's
    temporary directory): the migration 0 spawns; the steady prompt 0
    spawns, 12 os.stat and 0 os.lstat calls (the same as at a checkout no
    migration registered, tests/test_membership_capture.py)."""
    store, identity, config = lab
    synthetic = _synthetic(store, identity.id, projects=20, checkouts_each=10)
    repo = make_repo(tmp_path.resolve() / "Developer" / "game", remote=_URL)
    store.put_memory(
        Memory(
            id=generate_ulid(),
            created=_NOW,
            updated=_NOW,
            scopes=["tools"],
            confidence=Confidence.MEDIUM,
            source=Source.EXPLICIT,
            body="memory at game\n",
            origin=Origin(cwd=str(repo), repo=_URL, worktree_root=str(repo)),
        )
    )
    reports = []
    migrating = counted(
        lambda: reports.append(migrate(store, identity, now=lambda: _NOW))
    )
    (report,) = reports
    (root,) = report.roots
    print(
        f"synthetic witnesses={synthetic}; migration: spawns={migrating.spawns} "
        f"stat={migrating.stats} lstat={migrating.lstats}"
    )
    assert migrating.spawns == 0
    assert not (repo / ".git" / "info" / "bettermemory").exists()

    first = resolve_here(store, config, str(repo), identity=identity)
    assert (first.checkout, first.rule) == (root.checkout, "object")
    assert (repo / ".git" / "info" / "bettermemory").is_file()
    checkout = Membership(store, identity.id).checkout(str(root.checkout))
    assert checkout.token is not None and checkout.token_status == "written"
    resolve_here(store, config, str(repo), identity=identity)

    results = []
    warm = counted(
        lambda: results.append(
            resolve_here(store, config, str(repo), identity=identity)
        )
    )
    print(f"steady: spawns={warm.spawns} stat={warm.stats} lstat={warm.lstats}")
    assert results[0].rule == "object" and results[0].checkout == root.checkout
    assert warm.spawns == 0
    assert warm.stats <= 12 and warm.lstats == 0


# ---------------------------------------------------------------------------
# E29: the first-sight batch and the witness bound
# ---------------------------------------------------------------------------


def test_e29_first_sight_over_thousands_of_witnesses_is_one_batch(
    tmp_path: Path,
    lab: tuple[Store, MachineIdentity, Config],
    counted: Callable[[Callable[[], object]], _Counts],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E29 (b), design 8.9, at 50 projects of 64 witnesses: the first
    sight of a clone asks its repository about every project's bounded
    set in one call of the history reader, answered by the object reader
    with no git process, and the clone joins by history and remote.
    Measured on macOS (APFS): one call of 3,201 commits, 0 spawns."""
    store, identity, config = lab
    synthetic = _synthetic(store, identity.id, projects=50, checkouts_each=1)
    origin = make_repo(tmp_path.resolve() / "real" / "game", remote=_URL)
    resolve_here(store, config, str(origin), identity=identity)
    clone = tmp_path.resolve() / "real" / "clone"
    git(tmp_path, "clone", "-q", origin.as_uri(), str(clone))
    git(clone, "remote", "set-url", "origin", _URL)
    witnesses = len(store.membership_rows("project_witnesses"))
    assert witnesses == synthetic + 1

    batches: list[int] = []
    real_holds = location.RepositoryHistory.holds

    def holds(self: location.RepositoryHistory, commits: Any) -> set[str]:
        batches.append(len(commits))
        return real_holds(self, commits)

    monkeypatch.setattr(location.RepositoryHistory, "holds", holds)
    results = []
    seen = counted(
        lambda: results.append(
            resolve_here(store, config, str(clone), identity=identity)
        )
    )
    print(
        f"witnesses={witnesses} batches={batches} spawns={seen.spawns} "
        f"stat={seen.stats} lstat={seen.lstats}"
    )
    assert batches == [witnesses]
    assert seen.spawns == 0
    assert results[0].rule == "join:H+R"


def test_e29_301_sightings_keep_the_witness_bound(
    tmp_path: Path, lab: tuple[Store, MachineIdentity, Config]
) -> None:
    """E29 (c), decision D15: a checkout seen at 301 successive commits
    adds each HEAD as a witness and keeps 64 (the 16 oldest always, the
    first sight's HEAD among them, 237 released); a fresh clone of it at
    its newest commit still joins by history and remote."""
    store, identity, config = lab
    repo = make_repo(tmp_path.resolve() / "bound" / "q", commits=1, remote=_URL)
    first_head = git(repo, "rev-parse", "HEAD")
    stream = []
    for i in range(300):
        stream.append(
            "commit refs/heads/main\n"
            f"committer Fixture <fixture@example.invalid> {1_700_000_000 + i} +0000\n"
            f"data {len(f'c{i}')}\nc{i}\n"
            + (f"from {first_head}\n" if i == 0 else "")
            + f"M 644 inline src/x{i}.py\ndata {len(f'X = {i}')}\nX = {i}\n\n"
        )
    git(repo, "fast-import", "--quiet", stdin="".join(stream))
    commits = git(repo, "rev-list", "--reverse", "main").split()
    assert commits[0] == first_head and len(commits) == 301
    ref = repo / ".git" / "refs" / "heads" / "main"
    resolved = []
    for sha in commits:
        # Bytes, so the ref file holds the newline git writes on every
        # platform, not a text layer's CRLF.
        ref.write_bytes(f"{sha}\n".encode("ascii"))
        location.forget()
        resolved.append(resolve_here(store, config, str(repo), identity=identity))
    model = Membership(store, identity.id)
    project = model.canonical(resolved[0].project)
    assert project is not None
    kept = [
        str(r["commit_id"])
        for r in store.membership_rows(
            "project_witnesses", where={"project": project}, order_by=("arrival",)
        )
    ]
    released = len(list(store.membership_log(["witness_released"])))
    print(f"sightings={len(commits)} kept={len(kept)} released={released}")
    assert len(kept) == WITNESS_BOUND
    assert kept[:KEEP_OLDEST] == commits[:KEEP_OLDEST]
    assert kept[KEEP_OLDEST:] == commits[-(WITNESS_BOUND - KEEP_OLDEST) :]
    assert released == len(commits) - WITNESS_BOUND
    clone = tmp_path.resolve() / "bound" / "q-clone"
    git(tmp_path, "clone", "-q", repo.as_uri(), str(clone))
    git(clone, "remote", "set-url", "origin", _URL)
    res = resolve_here(store, config, str(clone), identity=identity)
    assert res.rule == "join:H+R" and model.canonical(res.project) == project

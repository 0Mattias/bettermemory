"""Anchoring at the checkout's live view (design 3.6; design 4, failure 6;
design 8.3).

E14's eighteen checks, one test each, named by case: the rc3 column runs
`detect_path_drift` at the record's recorded worktree (the reading a record
with no home row still gets), the anchored column reads the same claims
through `verify.Anchoring` over the membership model. Then E18 (c), the
cited commit, a project with no live checkout, a folder checkout, and the
home and witness `memory_verify` records with its stamp.

Every repository is built with git under ``tmp_path`` with a neutral
configuration (the shape of experiments/lab.py), and every location is the
real capture of that directory (`location.capture_location`). The E18 (c)
and cited-commit tests run twice, the second time with githead's discovery
declining as it does on Windows (`discovery`), so the reads Windows takes
run on every platform.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory import githead, location
from bettermemory.config import Config, StorageConfig
from bettermemory.events import Recorder
from bettermemory.machine import MachineIdentity, open_machine
from bettermemory.membership import Membership, Resolution
from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.origin import Origin
from bettermemory.project_file import mint_id
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store
from bettermemory.verdict import memory_verdict
from bettermemory.verify import (
    SPARSE_CHECKOUT,
    UNANCHORED,
    UNCONFIRMED_COMMIT,
    Anchoring,
    ClaimAnchor,
    PathDriftReport,
    compute_commit_drift,
    detect_path_drift,
    unverifiable_attestations,
)

from ._mcp import call_tool, crash_text
from .conftest import set_git_discovery_ceiling

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

#: This machine's id in the model; `LAPTOP` is another machine's.
HERE = "machine-here"
LAPTOP = "machine-laptop"
_EPOCH = datetime.fromtimestamp(1_000_000_000, tz=timezone.utc)
_REMOTE = "git@github.com:example/anchoring.git"


@pytest.fixture(autouse=True)
def neutral_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No user or system git configuration, a fixed author, committer and
    date, no inherited repository variables, no workspace declaration."""
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


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def _remove(path: Path) -> None:
    """`shutil.rmtree` after making every entry writable: git writes its
    objects read-only, and Windows refuses to unlink a read-only file
    (WinError 5). A pass before the removal, not an error handler, whose
    keyword differs across the supported Pythons (``onexc`` from 3.12,
    ``onerror`` before)."""
    for p in [path, *path.rglob("*")]:
        if not p.is_symlink():
            try:
                p.chmod(0o755 if p.is_dir() else 0o644)
            except OSError:
                pass
    shutil.rmtree(path)


@pytest.fixture(params=["platform", "declined"])
def discovery(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """githead's discovery as this platform has it, and declining as it does
    off POSIX (`githead._POSIX`): `find_gitdir` and `gitdir_at` then answer
    None for every path, as on Windows, and verify's sparse-checkout and
    cited-commit reads go on through `githead.locate`."""
    if request.param == "declined":
        monkeypatch.setattr(githead, "_POSIX", False)


def _repo(path: Path, *files: str) -> Path:
    """A repository with one commit of its own (a random line in the
    README, so two repositories never share a commit) holding `files`
    under ``src/``."""
    (path / "src").mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    (path / "README.md").write_text(f"{path.name} {os.urandom(6).hex()}\n")
    for name in files:
        (path / "src" / name).write_text(f"# {name}\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "files")
    return path


def _commit_file(repo: Path, name: str) -> None:
    (repo / "src" / name).write_text(f"# {name}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", name)


class Bench:
    """A store, the membership model of each machine over it, and the
    sightings and records of one test (E14's lab, on the real capture)."""

    def __init__(self, tmp_path: Path) -> None:
        self.fx = tmp_path / "fx"
        self.fx.mkdir()
        self.store = Store(tmp_path / "store")
        self.config = Config(storage=StorageConfig(directory=str(tmp_path / "store")))

    def see(self, path: Path, machine: str = HERE) -> Resolution:
        """A session at `path` on `machine`: the capture and the ladder."""
        loc = location.capture_location(
            str(path), machine=machine, session=None, config=self.config
        )
        return Membership(self.store, machine).resolve(loc)

    def write(
        self,
        path: Path,
        *,
        machine: str = HERE,
        body: str = "a record",
        verified_paths: Sequence[str] = (),
        last_verified_at: datetime | None = None,
    ) -> str:
        """A record written at `path`, homed at its resolution."""
        res = self.see(path, machine)
        assert res.home is not None
        rid = generate_ulid()
        self.store.put_memory(
            Memory(
                id=rid,
                created=_EPOCH,
                updated=_EPOCH,
                scopes=["tools"],
                confidence=Confidence.MEDIUM,
                source=Source.EXPLICIT,
                body=body + "\n",
                origin=Origin(cwd=str(path), worktree_root=str(path)),
                verified_paths=list(verified_paths),
                last_verified_at=last_verified_at,
            ),
            home=res.home,
        )
        return rid

    def anchoring(self, caller: Path | None = None) -> Anchoring:
        """This machine's anchors, for a caller at `caller` (or none)."""
        res = self.see(caller) if caller is not None else None
        return Anchoring(Membership(self.store, HERE), res)

    def anchor(self, rid: str, caller: Path | None = None) -> ClaimAnchor:
        found = self.anchoring(caller).for_record(rid)
        assert found is not None
        return found

    def read(
        self,
        rid: str,
        body: str,
        verified: Sequence[str] = (),
        absent: Sequence[str] = (),
        caller: Path | None = None,
    ) -> PathDriftReport:
        """The anchored column: the claims read through the record's anchor."""
        return detect_path_drift(
            body,
            verified_paths=list(verified),
            absent_paths=list(absent),
            anchor=self.anchor(rid, caller),
        )


def _rc3(
    body: str, verified: Sequence[str], absent: Sequence[str], recorded: Path
) -> PathDriftReport:
    """The rc3 column: the claims read at the recorded worktree."""
    return detect_path_drift(
        body,
        verified_paths=list(verified),
        absent_paths=list(absent),
        worktree_root=str(recorded),
    )


def _ends(paths: Sequence[str], tail: str) -> bool:
    return any(p.replace(os.sep, "/").endswith(tail) for p in paths)


@pytest.fixture
def bench(tmp_path: Path) -> Iterator[Bench]:
    made = Bench(tmp_path)
    yield made
    made.store.close()


# ---------------------------------------------------------------------------
# E14 cases 1 to 4: a remote-less project, moved, and a shell at the old path
# ---------------------------------------------------------------------------


@dataclass
class Game:
    """E14's first record: a relative and an absolute attestation and a
    path attested absent, written at `old`."""

    old: Path
    rid: str
    body: str
    verified: list[str]
    absent: list[str]


def _game(bench: Bench) -> Game:
    old = _repo(bench.fx / "Desktop" / "game", "a.py", "b.py")
    rid = bench.write(old)
    return Game(
        old=old,
        rid=rid,
        body=f"The loader is in `src/a.py`; the parser is in `{old}/src/b.py`.",
        verified=["src/a.py", f"{old}/src/b.py"],
        absent=["src/gone.py"],
    )


def _moved(bench: Bench, game: Game, *, session: bool) -> Path:
    new = bench.fx / "Developer" / "game"
    new.parent.mkdir(parents=True)
    os.rename(game.old, new)
    if session:
        bench.see(new)
    return new


def test_e14_1_in_place_both_columns_verify_both_attestations(bench: Bench) -> None:
    game = _game(bench)
    rc3 = _rc3(game.body, game.verified, game.absent, game.old)
    new = bench.read(game.rid, game.body, game.verified, game.absent)
    assert len(rc3.verified) == 2 and not rc3.claim_anchored_missing
    assert len(new.verified) == 2 and not new.claim_anchored_missing
    assert new.anchor == "home_checkout"


def test_e14_2a_moved_rc3_reads_the_absolute_attestation_as_a_claim_anchored_miss(
    bench: Bench,
) -> None:
    game = _game(bench)
    _moved(bench, game, session=False)
    rc3 = _rc3(game.body, game.verified, game.absent, game.old)
    assert len(rc3.claim_anchored_missing) == 1


def test_e14_2a_moved_rc3_checks_no_relative_claim(bench: Bench) -> None:
    game = _game(bench)
    _moved(bench, game, session=False)
    rc3 = _rc3(game.body, game.verified, game.absent, game.old)
    assert not _ends(rc3.checked, "src/a.py")


def test_e14_2a_moved_without_a_session_there_reads_no_drift_three_unconfirmed(
    bench: Bench,
) -> None:
    game = _game(bench)
    _moved(bench, game, session=False)
    new = bench.read(game.rid, game.body, game.verified, game.absent)
    assert new.anchor == "no_live_checkout"
    assert not new.missing and not new.claim_anchored_missing
    assert len(new.unconfirmed) == 3
    assert set(new.unconfirmed_reasons) == {UNANCHORED, "no_live_checkout"}


def test_e14_2b_a_session_at_the_new_path_re_anchors_at_the_moved_checkout(
    bench: Bench,
) -> None:
    game = _game(bench)
    new_path = _moved(bench, game, session=True)
    new = bench.read(game.rid, game.body, game.verified, game.absent)
    assert new.anchor == "home_checkout"
    assert len(new.verified) == 2 and not new.claim_anchored_missing
    assert all(Path(p).is_relative_to(new_path) for p in new.verified)


def test_e14_2c_a_file_deleted_in_the_moved_checkout_is_drift(bench: Bench) -> None:
    game = _game(bench)
    new_path = _moved(bench, game, session=True)
    (new_path / "src" / "a.py").unlink()
    new = bench.read(game.rid, game.body, game.verified, game.absent)
    assert _ends(new.claim_anchored_missing, "src/a.py")
    assert not new.unconfirmed


def test_e14_2c_rc3_does_not_see_the_deletion(bench: Bench) -> None:
    game = _game(bench)
    new_path = _moved(bench, game, session=True)
    (new_path / "src" / "a.py").unlink()
    rc3 = _rc3(game.body, game.verified, game.absent, game.old)
    assert not _ends(rc3.claim_anchored_missing, "src/a.py")


def _shell(game: Game) -> None:
    """A plain folder recreated at the old path, as a tool does."""
    (game.old / "src").mkdir(parents=True)
    (game.old / "src" / "CharacterEditor 2.cs").write_text("// copy\n")


def test_e14_3_shell_at_the_old_path_rc3_reads_two_claim_anchored_misses(
    bench: Bench,
) -> None:
    game = _game(bench)
    _moved(bench, game, session=True)
    _shell(game)
    rc3 = _rc3(game.body, game.verified, game.absent, game.old)
    assert len(rc3.claim_anchored_missing) == 2


def test_e14_3_shell_at_the_old_path_leaves_the_anchored_read_unchanged(
    bench: Bench,
) -> None:
    game = _game(bench)
    _moved(bench, game, session=True)
    _shell(game)
    new = bench.read(game.rid, game.body, game.verified, game.absent)
    assert new.anchor == "home_checkout"
    assert len(new.verified) == 2 and not new.claim_anchored_missing


_SHIM = "The old shim `src/gone.py` was removed."


def test_e14_4_rc3_drops_a_path_attested_absent_that_is_present(
    bench: Bench,
) -> None:
    svc = _repo(bench.fx / "work" / "svc")
    bench.write(svc)
    (svc / "src" / "gone.py").write_text("# back\n")
    rc3 = _rc3(_SHIM, [], ["src/gone.py"], svc)
    buckets = [
        name
        for name, paths in rc3.to_dict().items()
        if name != "checked" and isinstance(paths, list) and _ends(paths, "gone.py")
    ]
    assert buckets == []


def test_e14_4_a_path_attested_absent_that_is_present_is_reported(
    bench: Bench,
) -> None:
    game = _game(bench)
    new_path = _moved(bench, game, session=True)
    (new_path / "src" / "gone.py").write_text("# back\n")
    moved = bench.read(game.rid, game.body, game.verified, game.absent)

    svc = _repo(bench.fx / "work" / "svc")
    rid = bench.write(svc)
    (svc / "src" / "gone.py").write_text("# back\n")
    in_place = bench.read(rid, _SHIM, [], ["src/gone.py"])

    assert len(moved.present_but_attested_absent) == 1
    assert len(in_place.present_but_attested_absent) == 1
    assert in_place.to_dict()["present_but_attested_absent"] == list(
        in_place.present_but_attested_absent
    )


# ---------------------------------------------------------------------------
# E14 case 5: attestations written in a linked worktree since removed
# ---------------------------------------------------------------------------


@dataclass
class Branch:
    primary: Path
    worktree: Path
    rid: str
    body: str
    verified: list[str]


def _dead_worktree(bench: Bench, *, primary_seen: bool) -> Branch:
    """A record written in a linked worktree, then the worktree removed;
    `primary_seen` says whether a session ran in the primary first."""
    primary = _repo(bench.fx / "Developer" / "app", "c.py")
    if primary_seen:
        bench.see(primary)
    worktree = bench.fx / "codex" / "worktrees" / "a1b2" / "app"
    worktree.parent.mkdir(parents=True)
    if primary_seen:
        _git(primary, "worktree", "add", "-q", "-b", "codex-task", str(worktree))
        _commit_file(worktree, "only_on_branch.py")
        body = (
            f"The client is `{worktree}/src/c.py`; the new hook is "
            f"`{worktree}/src/only_on_branch.py`."
        )
        verified = [f"{worktree}/src/c.py", f"{worktree}/src/only_on_branch.py"]
    else:
        _git(primary, "worktree", "add", "-q", "--detach", str(worktree))
        body = f"The helper is `{worktree}/src/c.py`."
        verified = [f"{worktree}/src/c.py"]
    rid = bench.write(worktree)
    _git(primary, "worktree", "remove", "--force", str(worktree))
    return Branch(primary, worktree, rid, body, verified)


def test_e14_5_rc3_reads_both_attestations_under_a_removed_worktree_as_misses(
    bench: Bench,
) -> None:
    branch = _dead_worktree(bench, primary_seen=True)
    rc3 = _rc3(branch.body, branch.verified, [], branch.worktree)
    assert len(rc3.claim_anchored_missing) == 2


def test_e14_5_a_stand_in_confirms_the_file_the_primary_has(bench: Bench) -> None:
    branch = _dead_worktree(bench, primary_seen=True)
    new = bench.read(branch.rid, branch.body, branch.verified)
    assert new.anchor == "project_checkout"
    assert _ends(new.verified, "src/c.py")
    assert all(Path(p).is_relative_to(branch.primary) for p in new.verified)


def test_e14_5_a_file_only_on_the_removed_branch_is_unconfirmed_not_drift(
    bench: Bench,
) -> None:
    branch = _dead_worktree(bench, primary_seen=True)
    new = bench.read(branch.rid, branch.body, branch.verified)
    assert _ends(new.unconfirmed, "only_on_branch.py")
    assert new.unconfirmed_reasons == ("project_checkout",)
    assert not new.missing and not new.claim_anchored_missing


def test_e14_5b_rc3_reads_a_claim_anchored_miss(bench: Bench) -> None:
    branch = _dead_worktree(bench, primary_seen=False)
    rc3 = _rc3(branch.body, branch.verified, [], branch.worktree)
    assert len(rc3.claim_anchored_missing) == 1


def test_e14_5b_no_checkout_of_the_project_here_reads_unconfirmed(
    bench: Bench,
) -> None:
    branch = _dead_worktree(bench, primary_seen=False)
    new = bench.read(branch.rid, branch.body, branch.verified)
    assert new.anchor == "no_live_checkout"
    assert len(new.unconfirmed) == 1 and new.unconfirmed_reasons == (UNANCHORED,)
    assert not new.missing and not new.claim_anchored_missing


# ---------------------------------------------------------------------------
# E14 case 6: a record written on another machine, read here
# ---------------------------------------------------------------------------


@dataclass
class Synced:
    host: Path
    laptop: Path
    rid: str
    body: str
    verified: list[str]


def _synced(bench: Bench) -> Synced:
    """The laptop's tree is a copy of a checkout this store has seen (so
    it carries its token); a record is written there on the laptop, and
    the path never exists on this machine."""
    host = _repo(bench.fx / "src-of-tool" / "tool", "t.py")
    bench.see(host)
    laptop = bench.fx / "laptop" / "Users" / "kim" / "tool"
    laptop.parent.mkdir(parents=True)
    shutil.copytree(host, laptop, symlinks=True)
    rid = bench.write(laptop, machine=LAPTOP)
    _remove(bench.fx / "laptop")
    bench.see(host)
    return Synced(
        host,
        laptop,
        rid,
        f"Entry point: `{laptop}/src/t.py`.",
        [f"{laptop}/src/t.py"],
    )


def test_e14_6_rc3_reads_another_machines_absolute_attestation_as_a_miss(
    bench: Bench,
) -> None:
    synced = _synced(bench)
    rc3 = _rc3(synced.body, synced.verified, [], synced.laptop)
    assert len(rc3.claim_anchored_missing) == 1


def test_e14_6_another_machines_record_is_confirmed_at_this_machines_checkout(
    bench: Bench,
) -> None:
    synced = _synced(bench)
    new = bench.read(synced.rid, synced.body, synced.verified)
    assert new.anchor == "project_checkout"
    assert len(new.verified) == 1 and not new.claim_anchored_missing
    assert Path(new.verified[0]).is_relative_to(synced.host)


# ---------------------------------------------------------------------------
# E18 (c): a sparse checkout
# ---------------------------------------------------------------------------


def _sparse(bench: Bench) -> tuple[Path, str]:
    """A monorepo's sparse clone holding only ``api``, and a record
    written there."""
    mono = _repo(bench.fx / "mono" / "repo")
    for package in ("api", "web"):
        (mono / package).mkdir()
        (mono / package / "main.py").write_text(f"# {package}\n")
    _git(mono, "add", "-A")
    _git(mono, "commit", "-q", "-m", "packages")
    sparse = bench.fx / "sparse" / "repo"
    sparse.parent.mkdir(parents=True)
    _git(sparse.parent, "clone", "-q", "--no-checkout", mono.as_uri(), str(sparse))
    _git(sparse, "sparse-checkout", "set", "api")
    _git(sparse, "checkout", "-q", "main")
    assert not (sparse / "web").exists()
    return sparse, bench.write(sparse / "api")


_WEB = "The web entry is `web/main.py`."


def test_e18c_rc3_reads_a_path_outside_the_sparse_cone_as_a_miss(
    bench: Bench,
) -> None:
    sparse, _rid = _sparse(bench)
    rc3 = _rc3(_WEB, ["web/main.py"], [], sparse)
    assert len(rc3.claim_anchored_missing) == 1


@pytest.mark.usefixtures("discovery")
def test_e18c_a_path_outside_the_sparse_cone_reads_unconfirmed_not_missing(
    bench: Bench,
) -> None:
    sparse, rid = _sparse(bench)
    new = bench.read(rid, _WEB, ["web/main.py"])
    assert new.anchor == "home_checkout"
    assert not new.missing and not new.claim_anchored_missing
    assert new.unconfirmed_reasons == (SPARSE_CHECKOUT,)
    anchor = bench.anchor(rid)
    assert unverifiable_attestations(["web/main.py"], anchor=anchor) == []
    assert unverifiable_attestations(["web/main.py"], anchor=anchor, stored=True) == []


@pytest.mark.usefixtures("discovery")
def test_e18c_a_path_the_sparse_head_does_not_track_is_still_missing(
    bench: Bench,
) -> None:
    sparse, rid = _sparse(bench)
    new = bench.read(rid, "The docs entry is `docs/main.py`.", ["docs/main.py"])
    assert _ends(new.claim_anchored_missing, "docs/main.py")
    assert not new.unconfirmed


# ---------------------------------------------------------------------------
# The cited commit: the home's store, then the caller's, else unconfirmed
# ---------------------------------------------------------------------------


def _cited(bench: Bench) -> tuple[Path, str, str]:
    home = _repo(bench.fx / "Desktop" / "game", "a.py")
    head = _git(home, "rev-parse", "HEAD")
    return home, head, bench.write(home)


def _clone_then_move(bench: Bench, home: Path) -> Path:
    """A clone of the home checkout, seen (a checkout of its project that
    holds the cited commit); then the home moves with no session at its
    new path, so it has no live view here."""
    clone = bench.fx / "clone" / "game"
    clone.parent.mkdir(parents=True)
    _git(clone.parent, "clone", "-q", str(home), str(clone))
    bench.see(clone)
    moved = bench.fx / "Developer" / "game"
    moved.parent.mkdir(parents=True)
    os.rename(home, moved)
    return clone


def _commit_leg(anchor: ClaimAnchor, head: str) -> Any:
    return compute_commit_drift(
        _EPOCH,
        None,
        caller_origin=None,
        verified_paths=["src/a.py"],
        verified_head=head,
        anchor=anchor,
    )


@pytest.mark.usefixtures("discovery")
def test_cited_commit_resolves_in_the_home_checkouts_store(bench: Bench) -> None:
    home, head, rid = _cited(bench)
    anchor = bench.anchor(rid)
    root, rule = anchor.commit_root(head)
    assert rule == "home_checkout" and root is not None
    assert Path(root).resolve() == home.resolve()
    leg = _commit_leg(anchor, head)
    assert leg is not None
    assert (leg.status, leg.commits_since_verify, leg.basis) == (
        "clean",
        0,
        "reachability",
    )


@pytest.mark.usefixtures("discovery")
def test_cited_commit_resolves_in_the_callers_store_when_the_home_is_not_live(
    bench: Bench,
) -> None:
    home, head, rid = _cited(bench)
    clone = _clone_then_move(bench, home)
    anchor = bench.anchor(rid, caller=clone)
    assert anchor.rule == "caller_checkout_same_project"
    root, rule = anchor.commit_root(head)
    assert rule == "caller_checkout" and root is not None
    assert Path(root).resolve() == clone.resolve()
    assert _commit_leg(anchor, head) is not None


@pytest.mark.usefixtures("discovery")
def test_cited_commit_no_live_store_holding_it_reads_unconfirmed(
    bench: Bench,
) -> None:
    home, head, rid = _cited(bench)
    _clone_then_move(bench, home)
    other = _repo(bench.fx / "other" / "tool", "u.py")
    anchor = bench.anchor(rid, caller=other)
    # A stand-in of the project is live and holds the commit; it is not
    # where a cited commit resolves.
    assert anchor.rule == "project_checkout"
    assert anchor.commit_root(head) == (None, UNCONFIRMED_COMMIT)
    assert _commit_leg(anchor, head) is None


# ---------------------------------------------------------------------------
# A project with no live checkout; a folder checkout
# ---------------------------------------------------------------------------


def test_a_project_with_no_live_checkout_reads_every_path_claim_unconfirmed(
    bench: Bench,
) -> None:
    old = _repo(bench.fx / "Desktop" / "game", "a.py", "b.py")
    now = _EPOCH + timedelta(days=1)
    body = f"The loader is in `src/a.py`; the parser is in `{old}/src/b.py`."
    verified = ["src/a.py", f"{old}/src/b.py"]
    rid = bench.write(old, body=body, verified_paths=verified, last_verified_at=_EPOCH)
    os.rename(old, bench.fx / "elsewhere")
    memory = bench.store.load_one(rid)

    anchored = memory_verdict(
        memory,
        caller_origin=None,
        now=now,
        stale_after_days=30,
        anchoring=bench.anchoring(),
    )
    rc3 = memory_verdict(memory, caller_origin=None, now=now, stale_after_days=30)

    drift = anchored.path_drift
    assert drift.anchor == "no_live_checkout"
    assert not drift.missing and not drift.claim_anchored_missing
    assert sorted(drift.to_dict()["unconfirmed"], key=lambda u: u["path"]) == [
        {"path": f"{old}/src/b.py", "reason": UNANCHORED},
        {"path": "src/a.py", "reason": "no_live_checkout"},
    ]
    assert anchored.staleness_verdict == "fresh"
    assert rc3.path_drift.claim_anchored_missing


def _folder(bench: Bench) -> Path:
    folder = bench.fx / "notes"
    (folder / "sub").mkdir(parents=True)
    (folder / "docs").mkdir()
    (folder / "bettermemory.toml").write_text(
        f'[project]\nid = "{mint_id()}"\nname = "notes"\n'
    )
    (folder / "docs" / "plan.md").write_text("plan\n")
    return folder


def test_a_folder_checkout_anchors_relative_claims_at_the_folder_root(
    bench: Bench,
) -> None:
    folder = _folder(bench)
    rid = bench.write(folder / "sub")
    held = bench.read(rid, "See `docs/plan.md`.", ["docs/plan.md"])
    assert held.anchor == "home_checkout"
    assert len(held.verified) == 1
    assert Path(held.verified[0]).resolve() == (folder / "docs" / "plan.md").resolve()

    (folder / "docs" / "plan.md").unlink()
    gone = bench.read(rid, "See `docs/plan.md`.", ["docs/plan.md"])
    assert _ends(gone.claim_anchored_missing, "docs/plan.md")


def test_a_folder_checkout_translates_absolute_claims_across_its_views(
    bench: Bench,
) -> None:
    folder = _folder(bench)
    rid = bench.write(folder / "sub")
    moved = bench.fx / "archive" / "notes"
    moved.parent.mkdir(parents=True)
    os.rename(folder, moved)
    bench.see(moved / "sub")
    claim = f"{folder}/docs/plan.md"
    new = bench.read(rid, f"See `{claim}`.", [claim])
    assert new.anchor == "home_checkout"
    assert len(new.verified) == 1
    assert Path(new.verified[0]).resolve() == (moved / "docs" / "plan.md").resolve()


# ---------------------------------------------------------------------------
# memory_verify: the home and the witness, with the stamp
# ---------------------------------------------------------------------------


@pytest.fixture
def this_machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MachineIdentity:
    """The process's machine, read from fixed facts under the test's own
    directory, not from the host."""
    identity = open_machine(
        tmp_path / "machine", hardware=lambda: None, hostname=lambda: "fixture-host"
    )
    monkeypatch.setattr(location, "process_machine", lambda: identity)
    return identity


def _server_at(
    store: Store, memory_dir: Path, cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> Any:
    """A server whose caller stands in `cwd`."""
    import bettermemory._handlers as handlers_module

    state = SessionState()
    server = build_server(
        config=Config(storage=StorageConfig(directory=str(memory_dir))),
        store=store,
        state=state,
        recorder=Recorder(store=store, session_id=state.session_id),
    )
    origin = Origin(cwd=str(cwd), repo=_REMOTE, branch="main", worktree_root=str(cwd))

    def fake_capture(cwd: Path | None = None) -> Origin:
        return origin

    monkeypatch.setattr(handlers_module, "capture_origin", fake_capture)
    return server


def _unhomed(store: Store, repo: Path) -> str:
    """A record with no home row, written at `repo` (as before the
    migration homes it)."""
    written = store.write(
        content="The build runs from the repository root.",
        scopes=["tools"],
        origin=Origin(
            cwd=str(repo), repo=_REMOTE, branch="main", worktree_root=str(repo)
        ),
    )
    return written.id


def _model(store: Store, identity: MachineIdentity) -> Membership:
    return Membership(store, identity.id)


async def test_memory_verify_records_the_home_and_the_witness_with_the_stamp(
    memory_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    this_machine: MachineIdentity,
) -> None:
    repo = _repo(tmp_path / "work" / "svc", "a.py")
    head = _git(repo, "rev-parse", "HEAD")
    store = Store(memory_dir)
    rid = _unhomed(store, repo)
    assert _model(store, this_machine).home(rid) is None

    server = _server_at(store, memory_dir, repo, monkeypatch)
    await call_tool(server, "memory_verify", {"id": rid})

    model = _model(store, this_machine)
    home = model.home(rid)
    assert home is not None and home.checkout is not None
    view = model.live_view(model.checkout(home.checkout))
    assert view is not None and Path(view).resolve() == repo.resolve()
    witnesses = store.membership_rows(
        "project_witnesses", where={"project": home.project}
    )
    assert head in {row["commit_id"] for row in witnesses}
    stamped = store.load_one(rid)
    assert stamped.last_verified_at is not None
    assert stamped.verified_head == head
    store.close()


async def test_memory_verify_writes_no_home_and_no_stamp_when_the_witness_fails(
    memory_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    this_machine: MachineIdentity,
) -> None:
    repo = _repo(tmp_path / "work" / "svc", "a.py")
    store = Store(memory_dir)
    rid = _unhomed(store, repo)
    server = _server_at(store, memory_dir, repo, monkeypatch)

    def refuse(self: location.Caller, source: str) -> None:
        raise RuntimeError("witness write refused")

    monkeypatch.setattr(location.Caller, "add_head_witness", refuse)
    with pytest.raises(Exception) as excinfo:
        await call_tool(server, "memory_verify", {"id": rid})
    assert "witness write refused" in crash_text(excinfo.value)

    assert _model(store, this_machine).home(rid) is None
    assert store.load_one(rid).last_verified_at is None
    store.close()


async def test_memory_verify_from_another_tree_leaves_a_record_with_no_home(
    memory_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    this_machine: MachineIdentity,
) -> None:
    written_at = _repo(tmp_path / "work" / "svc", "a.py")
    caller = _repo(tmp_path / "work" / "other", "b.py")
    store = Store(memory_dir)
    rid = _unhomed(store, written_at)
    server = _server_at(store, memory_dir, caller, monkeypatch)

    await call_tool(server, "memory_verify", {"id": rid})

    assert _model(store, this_machine).home(rid) is None
    assert store.load_one(rid).last_verified_at is not None
    store.close()


# ---------------------------------------------------------------------------
# One verdict on every surface: memory_show and each memory_search hit
# ---------------------------------------------------------------------------

_DECOY = (
    "The loader feeds the parser; loader before parser, the parser after "
    "the loader, loader and parser, loader parser loader parser."
)


async def test_e14_2b_memory_show_and_a_non_top_search_hit_read_one_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    this_machine: MachineIdentity,
) -> None:
    """A homed record read from its checkout after the checkout moved: the
    anchored read (`memory_show`, the expanded top hit) verifies both
    attestations at the moved checkout, while the hit builder read the
    claims at the recorded worktree (the rc3 reading E14 2a pins as wrong
    for a homed record), stat'd the absolute attestation at the old path
    and escalated the hit on a claim-anchored miss (ENG-165). Every hit now
    reads its claims through the request's anchoring, so the record reads
    one verdict as a non-top `memory_search` hit and on `memory_show`."""
    bench = Bench(tmp_path)
    here = this_machine.id
    old = _repo(bench.fx / "Desktop" / "game", "a.py", "b.py")
    body = f"The loader is in `src/a.py`; the parser is in `{old}/src/b.py`."
    rid = bench.write(
        old, machine=here, body=body, verified_paths=["src/a.py", f"{old}/src/b.py"]
    )
    decoy = bench.write(old, machine=here, body=_DECOY)
    new = bench.fx / "Developer" / "game"
    new.parent.mkdir(parents=True)
    os.rename(old, new)
    bench.see(new, machine=here)

    server = _server_at(bench.store, tmp_path / "store", new, monkeypatch)
    await call_tool(server, "memory_verify", {"id": rid})
    await call_tool(server, "memory_verify", {"id": decoy})

    shown = await call_tool(server, "memory_show", {"id": rid})
    raw = await call_tool(
        server,
        "memory_search",
        {"query": "loader parser", "expand_top": True, "max_results": 5},
    )
    hits = raw.get("result", raw) if isinstance(raw, dict) else raw
    ids = [hit["id"] for hit in hits]
    assert ids[0] == decoy and rid in ids[1:], "premise: the record is not the top hit"
    hit = hits[ids.index(rid)]

    assert shown["staleness_verdict"] == "fresh"
    assert hit["staleness_verdict"] == shown["staleness_verdict"]
    assert not hit.get("path_drift", {}).get("claim_anchored_missing")
    bench.store.close()

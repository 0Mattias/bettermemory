"""`memory_admin(health)` reads commit drift as `memory_show` does (ENG-170, G3).

The tool resolves the request's caller (`handlers._shared.caller_anchoring`)
and passes its LINKS admission and anchoring, so `commit_drift_debt` lists a
record as drifted exactly where `memory_show` shows a commit drift for it: a
record with no home row has no commit leg on `memory_show` and is not listed
either, and from a linked worktree both count in the caller's checkout.
`bettermemory health` resolves no caller, because resolving one registers
the checkout, and it writes nothing.

The repository is built with git under ``tmp_path`` with a neutral
configuration, as tests/test_membership_anchoring.py builds its own.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from bettermemory import location, verify_anchoring
from bettermemory.config import Config, StorageConfig
from bettermemory.events import Recorder
from bettermemory.health import compute_health
from bettermemory.machine import MachineIdentity, open_machine
from bettermemory.membership import Home, Membership
from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.origin import Origin
from bettermemory.server import build_server
from bettermemory.server import main as cli_main
from bettermemory.session import SessionState
from bettermemory.store import Store
from bettermemory.verify_anchoring import Anchoring

from ._mcp import call_tool
from .conftest import set_git_discovery_ceiling

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

_REMOTE = "git@github.com:example/health-anchoring.git"


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


def _repo(path: Path) -> Path:
    """A repository with one commit holding ``src/a.py`` and ``src/b.py``."""
    (path / "src").mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    (path / "README.md").write_text(f"{path.name} {os.urandom(6).hex()}\n")
    for name in ("a.py", "b.py"):
        (path / "src" / name).write_text(f"# {name}\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "files")
    return path


def _change(repo: Path, name: str) -> None:
    with (repo / "src" / name).open("a") as handle:
        handle.write("changed = True\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"change {name}")


def _stand_at(cwd: Path, monkeypatch: pytest.MonkeyPatch, branch: str = "main") -> None:
    """Every handler's origin capture names `cwd` as the caller's."""
    import bettermemory._handlers as handlers_module

    origin = Origin(cwd=str(cwd), repo=_REMOTE, branch=branch, worktree_root=str(cwd))

    def fake_capture(cwd: Path | None = None) -> Origin:
        return origin

    monkeypatch.setattr(handlers_module, "capture_origin", fake_capture)


def _server_at(
    store: Store, memory_dir: Path, cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> Any:
    """A server whose caller stands in `cwd`."""
    state = SessionState()
    server = build_server(
        config=Config(storage=StorageConfig(directory=str(memory_dir))),
        store=store,
        state=state,
        recorder=Recorder(store=store, session_id=state.session_id),
    )
    _stand_at(cwd, monkeypatch)
    return server


async def _shown_and_listed(server: Any, ids: list[str]) -> tuple[set[str], set[str]]:
    """The records `memory_show` shows a commit drift on, and the ones
    health's `commit_drift_debt` lists, both read by the same caller."""
    shown: set[str] = set()
    for rid in ids:
        drift = (await call_tool(server, "memory_show", {"id": rid}))["commit_drift"]
        if drift is not None and drift["commits_since_verify"]:
            shown.add(rid)
    health = await call_tool(server, "memory_admin", {"action": "health"})
    debt = health["commit_drift_debt"]
    listed = {row["id"] for row in debt["rows"]} if debt is not None else set()
    return shown, listed


def _write(store: Store, repo: Path, cited: str) -> str:
    """A record citing `cited`, written at `repo` with no home row."""
    written = store.write(
        content=f"The service's entry point lives in `{cited}`.",
        scopes=["tools"],
        origin=Origin(
            cwd=str(repo), repo=_REMOTE, branch="main", worktree_root=str(repo)
        ),
    )
    return written.id


async def test_commit_drift_debt_lists_what_memory_show_shows_drifted(
    memory_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    this_machine: MachineIdentity,
) -> None:
    """One homed and one unhomed record in the caller's repository, both
    verified at HEAD and both with a later commit to the file they cite:
    `memory_show` shows a commit drift on the homed one only, and health's
    `commit_drift_debt` lists the same set."""
    repo = _repo(tmp_path / "work" / "svc")
    store = Store(memory_dir)
    homed = _write(store, repo, "src/a.py")
    unhomed = _write(store, repo, "src/b.py")
    server = _server_at(store, memory_dir, repo, monkeypatch)
    # memory_verify stamps the record and homes it at the caller's checkout;
    # the store's own stamp leaves the other with no home row.
    await call_tool(
        server, "memory_verify", {"id": homed, "verified_paths": ["src/a.py"]}
    )
    store.mark_verified(
        unhomed,
        verified_paths=["src/b.py"],
        verified_head=_git(repo, "rev-parse", "HEAD"),
    )
    model = Membership(store, this_machine.id)
    assert model.home(homed) is not None
    assert model.home(unhomed) is None
    _change(repo, "a.py")
    _change(repo, "b.py")

    shown: set[str] = set()
    for rid in (homed, unhomed):
        drift = (await call_tool(server, "memory_show", {"id": rid}))["commit_drift"]
        if drift is not None and drift["commits_since_verify"]:
            shown.add(rid)
    assert shown == {homed}

    health = await call_tool(server, "memory_admin", {"action": "health"})
    debt = health["commit_drift_debt"]
    assert debt is not None
    assert {row["id"] for row in debt["rows"]} == shown
    assert debt["total_drifted"] == len(shown)
    store.close()


@pytest.mark.parametrize("changed_in", ["home", "caller"])
async def test_from_a_linked_worktree_health_lists_what_memory_show_shows(
    changed_in: str,
    memory_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    this_machine: MachineIdentity,
) -> None:
    """A record homed at checkout A, read from B, a linked worktree of A
    made before any change (review D2). A commit to the cited file in A
    only is not in B's history: `memory_show` from B shows no drift, and
    health from B lists nothing. The same commit in B only: both count
    it. Health counts where `memory_show` counts, in the caller's
    checkout, not at the home's `commit_root`."""
    repo = _repo(tmp_path / "work" / "svc")
    linked = tmp_path / "work" / "svc-side"
    _git(repo, "worktree", "add", "-q", "-b", "side", str(linked))
    store = Store(memory_dir)
    rid = _write(store, repo, "src/a.py")
    server = _server_at(store, memory_dir, repo, monkeypatch)
    await call_tool(
        server, "memory_verify", {"id": rid, "verified_paths": ["src/a.py"]}
    )
    assert Membership(store, this_machine.id).home(rid) is not None
    _change(repo if changed_in == "home" else linked, "a.py")

    _stand_at(linked, monkeypatch, branch="side")
    shown, listed = await _shown_and_listed(server, [rid])
    assert shown == (set() if changed_in == "home" else {rid})
    assert listed == shown
    store.close()


def _row_counts(memory_dir: Path) -> dict[str, int]:
    """Every table of the store and its row count, the log included."""
    store = Store(memory_dir)
    try:
        names = [
            str(row[0])
            for row in store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        ]
        return {
            name: int(
                store.conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            )
            for name in names
        }
    finally:
        store.close()


def _files(root: Path) -> set[str]:
    return {str(path.relative_to(root)) for path in root.rglob("*")}


def test_the_cli_report_writes_nothing(
    memory_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    this_machine: MachineIdentity,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`bettermemory health` in a fresh repository against an empty store
    creates nothing under `.git` (no checkout token in `.git/info`) and
    adds no row to any table, the log included: no project minted, no
    checkout registered (review D3). Only the tool, which runs inside a
    session, resolves the caller."""
    repo = _repo(tmp_path / "work" / "fresh")
    before_rows = _row_counts(memory_dir)
    before_files = _files(repo / ".git")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("BETTERMEMORY_DIR", str(memory_dir))
    monkeypatch.setattr(sys, "argv", ["bettermemory", "health", "--json"])
    cli_main()
    report = json.loads(capsys.readouterr().out)
    assert "commit_drift_debt" in report
    assert not (repo / ".git" / "info" / "bettermemory").exists()
    assert _files(repo / ".git") == before_files
    assert _row_counts(memory_dir) == before_rows


# ---------------------------------------------------------------------------
# One read of the caller's object store per cited commit (ENG-192, G3)
# ---------------------------------------------------------------------------

_VERIFIED = datetime(2001, 1, 1, tzinfo=timezone.utc)


class _StandInModel:
    """A membership model whose every record is homed in one project whose
    home checkout has no live view here, read by a caller standing in the
    checkout at `root`: each cited commit is resolved in the caller's
    store (`ClaimAnchor.commit_root`), the read the memo keeps."""

    def __init__(self, root: Path) -> None:
        self.root = str(root)

    def home(self, record_id: str) -> Home:
        return Home(
            record_id=record_id,
            kind="record",
            project="p-elsewhere",
            checkout="k-home",
            rule="test",
            decided="2026-01-01T00:00:00Z",
            recorded_project="p-elsewhere",
        )

    def homes(self, record_ids: Sequence[str]) -> dict[str, Home]:
        return {rid: self.home(rid) for rid in record_ids}

    def anchor(self, record_id: str, caller: Any) -> tuple[str | None, str]:
        return None, "no_live_checkout"

    def checkout(self, checkout_id: str) -> SimpleNamespace:
        return SimpleNamespace(id=checkout_id, views=(), remotes=())

    def live_view(self, checkout: Any) -> str:
        return self.root


def _stand_in(root: Path) -> Anchoring:
    caller = SimpleNamespace(checkout="k-caller")
    return Anchoring(cast(Any, _StandInModel(root)), cast(Any, caller))


def _citing(commit: str) -> Memory:
    """A verified record citing `commit`, with a claim anchor and an origin
    in another repository than the caller's."""
    return Memory(
        id=generate_ulid(),
        created=_VERIFIED,
        updated=_VERIFIED,
        scopes=["tools"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body="The entry point lives in src/a.py.\n",
        origin=Origin(repo="git@github.com:example/elsewhere.git"),
        verified_paths=["src/a.py"],
        last_verified_at=_VERIFIED,
        verified_head=commit,
    )


def test_a_report_reads_the_callers_store_once_per_cited_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Twelve homed records whose home has no live view here, six citing
    one commit and six another, both held by the caller's checkout: the
    report asks whether the caller's store holds each commit once, not
    once a record, and still groups all twelve at the caller's checkout."""
    repo = _repo(tmp_path / "caller")
    first = _git(repo, "rev-parse", "HEAD")
    _change(repo, "a.py")
    second = _git(repo, "rev-parse", "HEAD")
    asked: list[tuple[str, str]] = []
    real = verify_anchoring.holds_commit

    def counted(root: str | Path, sha: str) -> bool:
        asked.append((str(root), sha))
        return real(root, sha)

    monkeypatch.setattr(verify_anchoring, "holds_commit", counted)
    memories = [_citing(first) for _ in range(6)] + [_citing(second) for _ in range(6)]
    report = compute_health(memories, [], anchoring=_stand_in(repo))
    assert sorted(asked) == sorted([(str(repo), first), (str(repo), second)])
    drift = report.cross_repo_drift
    assert drift is not None
    assert [(g.worktree_root, g.candidates) for g in drift.groups] == [(str(repo), 12)]


def test_the_memo_keeps_at_most_its_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past `_HELD_COMMIT_MEMO_CAP` the least recently asked answer is
    dropped and read again when asked again; the memo never holds more."""
    asked: list[str] = []

    def counted(root: str | Path, sha: str) -> bool:
        asked.append(sha)
        return sha == "a" * 40

    monkeypatch.setattr(verify_anchoring, "holds_commit", counted)
    monkeypatch.setattr(verify_anchoring, "_HELD_COMMIT_MEMO_CAP", 2)
    anchoring = _stand_in(tmp_path)
    shas = ["a" * 40, "b" * 40, "a" * 40, "c" * 40, "b" * 40, "a" * 40]
    answers = [anchoring.holds(str(tmp_path), sha) for sha in shas]
    assert answers == [True, False, True, False, False, True]
    # a and b read; a answered from the memo; c read, b dropped; b read
    # again, a dropped; a read again, c dropped.
    assert asked == ["a" * 40, "b" * 40, "c" * 40, "b" * 40, "a" * 40]
    assert len(anchoring._held) == 2
